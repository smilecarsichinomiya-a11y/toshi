"""日足スイング(数日〜数週間)のシグナル判定・ペーパー口座・バックテスト。

ライブ(毎晩の判定)とバックテストで同じ Market / Sim を使うので、「バックテストの条件」と「通知される条件」は必ず一致する。
約定は「シグナルの翌営業日の始値 + コスト」と仮定する(かぶミニの寄り付き約定を想定)。

売買は2種類:
  breakout … 上昇トレンドの高値更新を買う(数週間保有)
  pullback … 上昇トレンドの一時的な下げ(押し目)を買う(最長10営業日の短期)
"""
from __future__ import annotations

from dataclasses import dataclass, fields

import numpy as np
import pandas as pd

from .indicators import atr

STRATEGY_LABEL = {"breakout": "高値更新(順張り)", "pullback": "押し目買い(短期)"}


@dataclass
class Params:
    capital: float = 500_000
    pos_pct: float = 0.20  # 1銘柄の上限(総資産に対する割合)
    max_positions: int = 5  # 同時保有の上限
    cost_pct: float = 0.002  # 片道のコスト(スプレッド等)の仮定
    topn: int = 0  # 売買代金の上位N銘柄だけを対象にする(0=絞らない)
    # --- 高値更新(breakout) ---
    breakout_days: int = 20
    exit_days: int = 10
    vol_ratio: float = 1.2
    max_overheat: float = 0.15
    atr_mult: float = 2.0
    stop_min: float = 0.03
    stop_max: float = 0.08
    # --- 押し目買い(pullback) ---
    pullback: bool = True
    pb_lookback: int = 5  # 何日の高値からの下げを見るか
    pb_drop: float = 0.04  # 直近高値から何%下げたら押し目か
    pb_atr_mult: float = 1.5
    pb_stop_min: float = 0.02
    pb_stop_max: float = 0.05
    pb_max_hold: int = 10  # 最長の保有営業日数
    pb_max_positions: int = 2  # 押し目買いで同時に持てる銘柄数(枠を使い切らないため)

    @classmethod
    def from_cfg(cls, cfg) -> "Params":
        return cls(capital=cfg.initial_cash, pos_pct=cfg.signal_pos_pct, max_positions=cfg.signal_max_positions,
                   cost_pct=cfg.signal_cost_pct, pullback=cfg.signal_pullback,
                   topn=cfg.signal_topn if cfg.universe_mode == "liquid" else 0)

    def key(self) -> str:
        return "|".join(f"{f.name}={getattr(self, f.name)}" for f in fields(self))


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def yen(x: float) -> float:
    return round(x) if x >= 1000 else round(x, 1)


# ---------------------------------------------------------------- 指標と条件
def prep(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    """日足(Open/High/Low/Close/Volume)に判定用の指標を足す。当日を含まない指標は shift(1) で作る。"""
    d = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    d = d[~d.index.duplicated(keep="last")].sort_index().copy()
    c = d["Close"]
    d["sma25"] = c.rolling(25).mean()
    d["sma75"] = c.rolling(75).mean()
    d["hh"] = d["High"].shift(1).rolling(p.breakout_days).max()
    d["ll"] = d["Low"].shift(1).rolling(p.exit_days).min()
    d["vavg"] = d["Volume"].shift(1).rolling(20).mean()
    d["atr"] = atr(d, 14)
    d["roc60"] = c / c.shift(60) - 1
    d["high5"] = d["High"].rolling(p.pb_lookback).max()
    d["prev_close"] = c.shift(1)
    d["tov20"] = (c * d["Volume"]).rolling(20).mean()  # 売買代金の20日平均(銘柄選びに使う)
    return d


def breakout_ok(r: dict, p: Params) -> bool:
    """高値更新の買い条件(すべて満たす)。NaN との比較は False になるので、指標が足りない序盤は自動で見送り。"""
    c = r["Close"]
    return bool(r["sma25"] > r["sma75"] and c > r["sma75"] and c > r["hh"]
                and r["Volume"] >= p.vol_ratio * r["vavg"] and c <= r["sma25"] * (1 + p.max_overheat))


def pullback_ok(r: dict, p: Params) -> bool:
    """押し目買いの条件(すべて満たす): 上昇トレンドのまま、直近高値から下げて、当日は反発。"""
    c = r["Close"]
    return bool(r["sma25"] > r["sma75"] and c > r["sma75"] and c > r["sma25"]
                and c <= r["high5"] * (1 - p.pb_drop) and c > r["prev_close"])


buy_ok = breakout_ok  # 互換用の別名


def breakout_vec(d: pd.DataFrame, p: Params) -> pd.Series:
    c = d["Close"]
    return ((d["sma25"] > d["sma75"]) & (c > d["sma75"]) & (c > d["hh"]) & (d["Volume"] >= p.vol_ratio * d["vavg"])
            & (c <= d["sma25"] * (1 + p.max_overheat)))


def pullback_vec(d: pd.DataFrame, p: Params) -> pd.Series:
    c = d["Close"]
    return ((d["sma25"] > d["sma75"]) & (c > d["sma75"]) & (c > d["sma25"]) & (c <= d["high5"] * (1 - p.pb_drop))
            & (c > d["prev_close"]))


def _clip_stop(r: dict, mult: float, lo: float, hi: float) -> float:
    a = _num(r["atr"])
    raw = mult * a / r["Close"] if a == a else hi
    return min(hi, max(lo, raw))


def stop_pct(r: dict, p: Params, strategy: str = "breakout") -> float:
    if strategy == "pullback":
        return _clip_stop(r, p.pb_atr_mult, p.pb_stop_min, p.pb_stop_max)
    return _clip_stop(r, p.atr_mult, p.stop_min, p.stop_max)


def rowmap(d: pd.DataFrame) -> dict[str, dict]:
    return {ts.strftime("%Y-%m-%d"): row for ts, row in d.to_dict("index").items()}


# ---------------------------------------------------------------- 市場データ(全銘柄の指標・候補の一覧)
class Market:
    """全銘柄の日足から、日ごとの売買候補を作る。メモリを抑えるため、必要な行だけを辞書にして返す。

    topn > 0 のとき、その日の売買代金(20日平均)が上位 topn の銘柄だけを新規の買い候補にする。
    保有中の銘柄は、順位に関係なく毎日の行を返す(売り判定のため)。
    """

    COLS = ["Open", "High", "Low", "Close", "Volume", "sma25", "sma75", "hh", "ll", "vavg", "atr", "roc60",
            "high5", "prev_close", "tov20"]

    def __init__(self, bars: dict[str, pd.DataFrame], p: Params):
        self.p = p
        prepped = {s: prep(df, p) for s, df in bars.items()}
        top = None
        if p.topn > 0 and prepped:
            tov = pd.DataFrame({s: d["tov20"] for s, d in prepped.items()})
            top = tov.rank(axis=1, ascending=False, method="first") <= p.topn
        self.arr: dict[str, np.ndarray] = {}
        self.pos: dict[str, dict[str, int]] = {}
        self.cand: dict[str, list[str]] = {}
        dates: set[str] = set()
        for s, d in prepped.items():
            ds = d.index.strftime("%Y-%m-%d")
            self.arr[s] = d[self.COLS].to_numpy(dtype=float)
            self.pos[s] = {x: i for i, x in enumerate(ds)}
            dates.update(ds)
            flag = breakout_vec(d, p)
            if p.pullback:
                flag = flag | pullback_vec(d, p)
            if top is not None:
                flag = flag & top[s].reindex(d.index).fillna(False)
            for x in np.asarray(ds)[flag.to_numpy()]:
                self.cand.setdefault(x, []).append(s)
        self.dates = sorted(dates)
        self.symbols = list(prepped)

    def row(self, s: str, date: str) -> dict | None:
        i = self.pos.get(s, {}).get(date)
        return None if i is None else dict(zip(self.COLS, self.arr[s][i]))

    def rows_for(self, date: str, need: set[str]) -> dict[str, dict]:
        """date の行を、保有・注文中の銘柄(need)と、新規の買い候補について返す。"""
        out = {}
        for s in set(need) | set(self.cand.get(date, ())):
            r = self.row(s, date)
            if r is not None:
                out[s] = r
        return out


# ---------------------------------------------------------------- ペーパー口座
class Sim:
    """ペーパー口座。process_day を日付順に呼ぶ。シグナルは自動で pending に積まれ、次の日の始値で約定する。"""

    def __init__(self, p: Params, cash: float | None = None, positions: dict | None = None,
                 pending: list | None = None, last_close: dict | None = None):
        self.p = p
        self.cash = p.capital if cash is None else cash
        self.positions: dict[str, dict] = positions or {}
        self.pending: list[dict] = pending or []
        self.last_close: dict[str, float] = last_close or {}
        self.trades: list[dict] = []
        self.curve: list[tuple[str, float, float]] = []

    def equity(self) -> float:
        return self.cash + sum(x["shares"] * self.last_close.get(s, x["avg"]) for s, x in self.positions.items())

    def need(self) -> set[str]:
        """今日の行が必要な銘柄(保有中・注文中)。"""
        return set(self.positions) | {o["symbol"] for o in self.pending}

    def process_day(self, date: str, rows: dict[str, dict]) -> tuple[list[dict], list[dict]]:
        """date の行(保有・注文中・買い候補の銘柄)を渡す。(約定の一覧, 今夜の新しいシグナル) を返す。"""
        p = self.p
        fills: list[dict] = []
        # 1) 前の営業日までのシグナルを、今日の始値で約定
        keep = []
        for o in self.pending:
            r = rows.get(o["symbol"])
            if r is None:  # この日は取引なし(売買停止など): 持ち越し
                keep.append(o)
                continue
            if o["side"] == "buy":
                px = r["Open"] * (1 + p.cost_pct)
                n = min(o["shares"], int(self.cash // px))
                if n <= 0:
                    fills.append({**o, "filled": False, "fill_date": date, "note": "資金不足で見送り"})
                    continue
                self.cash -= n * px
                self.positions[o["symbol"]] = dict(
                    shares=n, avg=px, stop_pct=o["stop_pct"], stop=px * (1 - o["stop_pct"]), entry_date=date,
                    strategy=o.get("strategy", "breakout"), target=o.get("target"), days=0)
                fills.append({**o, "filled": True, "fill_date": date, "fill_price": px, "fill_shares": n})
            else:
                pos = self.positions.pop(o["symbol"], None)
                if pos is None:
                    fills.append({**o, "filled": False, "fill_date": date, "note": "保有がないため見送り"})
                    continue
                px = r["Open"] * (1 - p.cost_pct)
                self.cash += px * pos["shares"]
                t = dict(symbol=o["symbol"], entry_date=pos["entry_date"], exit_date=date, shares=pos["shares"],
                         entry_price=pos["avg"], exit_price=px, pnl=(px - pos["avg"]) * pos["shares"],
                         pnl_pct=(px / pos["avg"] - 1) * 100, reason=o["reason"], strategy=pos["strategy"])
                self.trades.append(t)
                fills.append({**o, "filled": True, "fill_date": date, "fill_price": px,
                              "fill_shares": pos["shares"], "pnl": t["pnl"]})
        self.pending = keep
        for pos in self.positions.values():  # 保有日数(営業日)を進める。約定した当日は0
            if pos["entry_date"] != date:
                pos["days"] = (pos.get("days") or 0) + 1

        # 2) 終値で時価評価
        for s, r in rows.items():
            self.last_close[s] = r["Close"]
        eq = self.equity()
        self.curve.append((date, eq, self.cash))

        # 3) 今夜のシグナル: 売り(保有銘柄) → 買い(空き枠と現金の範囲)
        sigs: list[dict] = []
        pending_syms = {o["symbol"] for o in self.pending}
        for s, pos in self.positions.items():
            r = rows.get(s)
            if r is None or s in pending_syms:
                continue
            reason = self._exit_reason(pos, r)
            if reason:
                sigs.append(dict(symbol=s, side="sell", shares=pos["shares"], price=r["Close"],
                                 amount=pos["shares"] * r["Close"], stop_pct=None, stop_price=None, target=None,
                                 reason=reason, signal_date=date, strategy=pos["strategy"]))
        held = set(self.positions) | pending_syms
        slots = p.max_positions - len(self.positions) - sum(1 for o in self.pending if o["side"] == "buy")
        pb_open = (sum(1 for x in self.positions.values() if x["strategy"] == "pullback")
                   + sum(1 for o in self.pending if o["side"] == "buy" and o.get("strategy") == "pullback"))
        reserved = sum(o["shares"] * self.last_close.get(o["symbol"], 0) for o in self.pending if o["side"] == "buy")
        cash_avail = self.cash - reserved
        if slots > 0:
            order = self._candidates(rows, held)
            for strat, s, r in order:
                if slots <= 0:
                    break
                if strat == "pullback" and pb_open >= p.pb_max_positions:
                    continue
                c = r["Close"]
                n = int(min(p.pos_pct * eq, cash_avail / (1 + p.cost_pct)) // c)
                if n < 1:
                    continue
                sp = stop_pct(r, p, strat)
                sigs.append(dict(
                    symbol=s, side="buy", shares=n, price=c, amount=n * c, stop_pct=sp, stop_price=yen(c * (1 - sp)),
                    target=yen(r["high5"]) if strat == "pullback" else None, strategy=strat,
                    reason=self._buy_reason(strat, r), signal_date=date))
                cash_avail -= n * c * (1 + p.cost_pct)
                slots -= 1
                pb_open += strat == "pullback"
        self.pending.extend(sigs)
        return fills, sigs

    def _candidates(self, rows: dict[str, dict], held: set[str]) -> list[tuple[str, str, dict]]:
        """買い候補を、高値更新 → 押し目の順に、直近3か月の上昇率が大きい順で並べる。"""
        p = self.p
        b, q = [], []
        for s, r in rows.items():
            if s in held:
                continue
            roc = _num(r["roc60"])
            key = (-(roc if roc == roc else -9.0), s)
            if breakout_ok(r, p):
                b.append((key, "breakout", s, r))
            elif p.pullback and pullback_ok(r, p):
                q.append((key, "pullback", s, r))
        return [(st, s, r) for _, st, s, r in sorted(b, key=lambda x: x[0]) + sorted(q, key=lambda x: x[0])]

    def _exit_reason(self, pos: dict, r: dict) -> str | None:
        p, c = self.p, r["Close"]
        if c <= pos["stop"]:
            return f"損切り: 終値{c:,.0f}円が損切り価格{yen(pos['stop']):,.0f}円以下"
        if pos["strategy"] == "pullback":
            if pos.get("target") and c >= pos["target"]:
                return f"利益確定: 終値{c:,.0f}円が押し目前の高値{pos['target']:,.0f}円まで戻った"
            if (pos.get("days") or 0) >= p.pb_max_hold:
                return f"時間切れ: 買ってから{p.pb_max_hold}営業日たっても戻らなかった"
            return None
        if c < r["ll"]:
            return f"上昇の終わり: 終値{c:,.0f}円が直近{p.exit_days}日の安値{r['ll']:,.0f}円を割った"
        return None

    def _buy_reason(self, strat: str, r: dict) -> str:
        p, c = self.p, r["Close"]
        if strat == "pullback":
            drop = (1 - c / r["high5"]) * 100
            return (f"上昇トレンド中の押し目: 直近{p.pb_lookback}日の高値{r['high5']:,.0f}円から{drop:.1f}%下げ、"
                    f"終値{c:,.0f}円で前日より反発(25日線の上)")
        vr = r["Volume"] / r["vavg"]
        return (f"{p.breakout_days}日高値{r['hh']:,.0f}円を終値{c:,.0f}円で更新・出来高は平均の{vr:.1f}倍・"
                f"25日線>75日線の上昇トレンド")


# ---------------------------------------------------------------- 成績とバックテスト
def _stats(t: list[dict]) -> dict:
    wins = [x for x in t if x["pnl"] > 0]
    gw = sum(x["pnl"] for x in wins)
    gl = -sum(x["pnl"] for x in t if x["pnl"] <= 0)
    return {"trades": len(t), "wins": len(wins), "win_rate": round(len(wins) / len(t), 3) if t else None,
            "profit_factor": round(gw / gl, 2) if gl else None,
            "avg_pnl_pct": round(sum(x["pnl_pct"] for x in t) / len(t), 2) if t else None,
            "pnl": round(sum(x["pnl"] for x in t))}


def metrics(sim: Sim, bench_ret: float | None = None) -> dict:
    t = sim.trades
    peak, mdd = 0.0, 0.0
    for _, e, _ in sim.curve:
        peak = max(peak, e)
        if peak:
            mdd = max(mdd, (peak - e) / peak)
    final = sim.curve[-1][1] if sim.curve else sim.p.capital
    holds = [(pd.Timestamp(x["exit_date"]) - pd.Timestamp(x["entry_date"])).days for x in t]
    return {
        **_stats(t),
        "max_dd_pct": round(mdd * 100, 1), "total_return_pct": round((final / sim.p.capital - 1) * 100, 1),
        "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else None,
        "open_positions": len(sim.positions), "final_equity": round(final), "bench_return_pct": bench_ret,
        "by_strategy": {k: _stats([x for x in t if x["strategy"] == k]) for k in STRATEGY_LABEL},
    }


def backtest(bars: dict[str, pd.DataFrame], p: Params, windows: dict[str, int] | None = None,
             bench: pd.DataFrame | None = None) -> dict:
    """過去の日足に同じルールを当てはめる。windows = {"1年": 365, "2年": 730} (日数)。"""
    windows = windows or {"1年": 365, "2年": 730}
    mk = Market(bars, p)
    if not mk.dates:
        return {"error": "株価データがありません"}
    end = pd.Timestamp(mk.dates[-1])
    out = {"end": mk.dates[-1], "symbols": len(mk.symbols), "topn": p.topn, "windows": {}}
    for label, days in windows.items():
        start = (end - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        sim = Sim(p)
        for d in (x for x in mk.dates if x >= start):
            sim.process_day(d, mk.rows_for(d, sim.need()))
        b = None
        if bench is not None and len(bench):
            bb = bench[bench.index >= pd.Timestamp(start)]["Close"]
            if len(bb) > 1:
                b = round((float(bb.iloc[-1]) / float(bb.iloc[0]) - 1) * 100, 1)
        out["windows"][label] = {"start": next((x for x in mk.dates if x >= start), start), **metrics(sim, b)}
    return out


# ---------------------------------------------------------------- 初心者向けの説明
def describe_rules(p: Params) -> dict:
    """現在のルールを、初心者向けの文章にする(数値は設定と必ず一致する)。"""
    pct = lambda x: f"{x * 100:g}%"  # noqa: E731
    universe = (f"売買代金(1日に売買された金額)が大きい上位{p.topn}銘柄を、毎晩選び直します(楽天証券のかぶミニ対象から)。"
                if p.topn else "あらかじめ決めた銘柄の中から選びます。")
    return {
        "outline": ("日足(1日1本のローソク足)を、毎営業日の夜に判定します。売買は2種類です。"
                    "①上昇の勢いがついた株を買って数週間持つ「高値更新の順張り」、"
                    "②上昇中の株が一時的に下げたところを買って数日で売る「押し目買い(短期)」。"
                    "売買は翌営業日の寄り付きで行う想定です。"),
        "universe": universe,
        "buy_title": "①高値更新の順張り — 買い条件(次の4つをすべて満たしたとき)",
        "buy": [
            "上昇トレンドである: 株価が75日移動平均線より上にあり、25日線が75日線より上にある。"
            "(移動平均線=過去の終値の平均。長く上がり続けている銘柄だけを選びます)",
            f"高値を更新した: 終値が、直近{p.breakout_days}営業日の最高値を上回った。(上昇に勢いがついた合図です)",
            f"出来高が増えている: 出来高が直近20日の平均の{p.vol_ratio:g}倍以上。(多くの人が買っている裏づけです)",
            f"買われすぎでない: 終値が25日線から{pct(p.max_overheat)}以上離れていない。(急騰の天井での高値づかみを避けます)",
        ],
        "sell_title": "①高値更新の順張り — 売り条件(どちらかになったとき)",
        "sell": [
            "損切り: 終値が損切り価格以下になった。(下の「損切り価格」を参照)",
            f"上昇の終わり: 終値が、直近{p.exit_days}営業日の最安値を下回った。(伸びた利益を守るための売りです)",
        ],
        "pullback_title": "②押し目買い(短期) — 買い条件(次の4つをすべて満たしたとき)" if p.pullback else "",
        "pullback": [
            "上昇トレンドである(①と同じ。75日線の上で、25日線が75日線より上)。",
            f"一時的に下げた: 終値が、直近{p.pb_lookback}営業日の高値より{pct(p.pb_drop)}以上低い。",
            "トレンドは崩れていない: 終値が25日線より上にある。",
            "反発の兆しがある: 終値が前の日の終値より高い。",
            "売り条件は、次のどれかです。損切り / 押し目前の高値まで戻った(利益確定) / "
            f"買ってから{p.pb_max_hold}営業日たっても戻らない(時間切れ)。",
            f"枠を使い切らないよう、押し目買いで同時に持てるのは最大{p.pb_max_positions}銘柄までです。",
        ] if p.pullback else [],
        "stop_title": "損切り価格の決め方",
        "stop": [
            f"①は、買値 −(値動きの大きさ×{p.atr_mult:g})。ただし買値の{pct(p.stop_min)}〜{pct(p.stop_max)}の範囲に収めます。"
            + (f"②は、買値 −(値動きの大きさ×{p.pb_atr_mult:g})で、買値の{pct(p.pb_stop_min)}〜{pct(p.pb_stop_max)}の範囲です。"
               if p.pullback else ""),
            "値動きの大きさにはATR(1日の平均的な値幅)を使うので、値動きが激しい銘柄ほど広めになります。",
            "損切りは夜の終値で判定します。日中に価格を割っても、その場では通知されません。"
            "急落やギャップで、損切り価格より大きく下がった価格で売ることになる場合があります。",
        ],
        "size_title": "金額と株数の決め方",
        "size": [
            f"1銘柄の上限は資金(現金+保有株の時価)の{pct(p.pos_pct)}、同時に持つのは最大{p.max_positions}銘柄まで。",
            "株数は「上限金額 ÷ 株価」を切り捨てて決めます(1株単位で買える、かぶミニを前提にしています)。",
            "現金が足りないとき、枠が埋まっているときは、買いシグナルを出しません。",
            "条件を満たす銘柄が多いときは、①を先に、直近3か月の上昇率が大きい順に選びます。",
        ],
        "notes": [
            "約定は翌営業日の始値で、コスト(スプレッド等)として片道"
            f"{p.cost_pct * 100:g}%を見込んだ仮定です。実際の約定価格とは異なります。",
            "ペーパートレードです。シグナルはすべて実行されたものとして仮想の口座で成績を計算します。"
            "実際に注文したかどうかは、ダッシュボードで別に記録します。",
        ],
    }
