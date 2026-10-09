"""日足スイング(数日〜数週間)のシグナル判定・ペーパー口座・バックテスト。

ライブ(毎晩の判定)とバックテストで同じ Sim を使うので、「バックテストの条件」と「通知される条件」は必ず一致する。
約定は「シグナルの翌営業日の始値 + コスト」と仮定する(かぶミニの寄り付き約定を想定)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

import pandas as pd

from .indicators import atr


@dataclass
class Params:
    capital: float = 500_000
    pos_pct: float = 0.25  # 1銘柄の上限(総資産に対する割合)
    max_positions: int = 4  # 同時保有の上限
    breakout_days: int = 20  # 何日の高値を超えたら買いか
    exit_days: int = 10  # 何日の安値を割ったら売りか
    vol_ratio: float = 1.2  # 出来高が平均の何倍以上か
    max_overheat: float = 0.15  # 25日線からの乖離の上限
    atr_mult: float = 2.0  # 損切り幅 = ATR × 倍率
    stop_min: float = 0.03
    stop_max: float = 0.08
    cost_pct: float = 0.002  # 片道のコスト(スプレッド等)の仮定

    @classmethod
    def from_cfg(cls, cfg) -> "Params":
        return cls(capital=cfg.initial_cash, pos_pct=cfg.signal_pos_pct, max_positions=cfg.signal_max_positions,
                   cost_pct=cfg.signal_cost_pct)

    def key(self) -> str:
        return "|".join(f"{f.name}={getattr(self, f.name)}" for f in fields(self))


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def yen(x: float) -> float:
    return round(x) if x >= 1000 else round(x, 1)


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
    return d


def rowmap(d: pd.DataFrame) -> dict[str, dict]:
    return {ts.strftime("%Y-%m-%d"): row for ts, row in d.to_dict("index").items()}


def buy_ok(r: dict, p: Params) -> bool:
    """買い条件(すべて満たす)。NaN との比較は False になるので、指標が足りない序盤は自動で見送り。"""
    c = r["Close"]
    return bool(r["sma25"] > r["sma75"] and c > r["sma75"]
                and c > r["hh"]
                and r["Volume"] >= p.vol_ratio * r["vavg"]
                and c <= r["sma25"] * (1 + p.max_overheat))


def stop_pct(r: dict, p: Params) -> float:
    a = _num(r["atr"])
    raw = p.atr_mult * a / r["Close"] if a == a else p.stop_max
    return min(p.stop_max, max(p.stop_min, raw))


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

    def process_day(self, date: str, rows: dict[str, dict]) -> tuple[list[dict], list[dict]]:
        """date の全銘柄の行(rows)を渡す。(約定の一覧, 今夜の新しいシグナル) を返す。"""
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
                self.positions[o["symbol"]] = dict(shares=n, avg=px, stop_pct=o["stop_pct"],
                                                   stop=px * (1 - o["stop_pct"]), entry_date=date)
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
                         pnl_pct=(px / pos["avg"] - 1) * 100, reason=o["reason"])
                self.trades.append(t)
                fills.append({**o, "filled": True, "fill_date": date, "fill_price": px,
                              "fill_shares": pos["shares"], "pnl": t["pnl"]})
        self.pending = keep

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
            if r["Close"] <= pos["stop"]:
                reason = f"損切り: 終値{r['Close']:,.0f}円が損切り価格{yen(pos['stop']):,.0f}円以下"
            elif r["Close"] < r["ll"]:
                reason = f"上昇の終わり: 終値{r['Close']:,.0f}円が直近{p.exit_days}日の安値{r['ll']:,.0f}円を割った"
            else:
                continue
            sigs.append(dict(symbol=s, side="sell", shares=pos["shares"], price=r["Close"],
                             amount=pos["shares"] * r["Close"], stop_pct=None, stop_price=None, reason=reason,
                             signal_date=date))
        held = set(self.positions) | pending_syms
        slots = p.max_positions - len(self.positions) - sum(1 for o in self.pending if o["side"] == "buy")
        reserved = sum(o["shares"] * self.last_close.get(o["symbol"], 0) for o in self.pending if o["side"] == "buy")
        cash_avail = self.cash - reserved
        if slots > 0:
            cands = []
            for s, r in rows.items():
                if s not in held and buy_ok(r, p):
                    roc = _num(r["roc60"])
                    cands.append((roc if roc == roc else -9.0, s, r))
            cands.sort(key=lambda x: (-x[0], x[1]))  # 直近3か月の上昇率が大きい順
            for _, s, r in cands:
                if slots <= 0:
                    break
                c = r["Close"]
                n = int(min(p.pos_pct * eq, cash_avail / (1 + p.cost_pct)) // c)
                if n < 1:
                    continue
                sp = stop_pct(r, p)
                vr = r["Volume"] / r["vavg"]
                sigs.append(dict(
                    symbol=s, side="buy", shares=n, price=c, amount=n * c, stop_pct=sp, stop_price=yen(c * (1 - sp)),
                    reason=(f"{p.breakout_days}日高値{r['hh']:,.0f}円を終値{c:,.0f}円で更新・出来高は平均の{vr:.1f}倍・"
                            f"25日線>75日線の上昇トレンド"),
                    signal_date=date))
                cash_avail -= n * c * (1 + p.cost_pct)
                slots -= 1
        self.pending.extend(sigs)
        return fills, sigs


def metrics(sim: Sim, bench_ret: float | None = None) -> dict:
    t = sim.trades
    wins = [x for x in t if x["pnl"] > 0]
    gw = sum(x["pnl"] for x in wins)
    gl = -sum(x["pnl"] for x in t if x["pnl"] <= 0)
    peak, mdd = 0.0, 0.0
    for _, e, _ in sim.curve:
        peak = max(peak, e)
        if peak:
            mdd = max(mdd, (peak - e) / peak)
    final = sim.curve[-1][1] if sim.curve else sim.p.capital
    holds = [(pd.Timestamp(x["exit_date"]) - pd.Timestamp(x["entry_date"])).days for x in t]
    return {
        "trades": len(t), "wins": len(wins), "win_rate": round(len(wins) / len(t), 3) if t else None,
        "max_dd_pct": round(mdd * 100, 1), "total_return_pct": round((final / sim.p.capital - 1) * 100, 1),
        "profit_factor": round(gw / gl, 2) if gl else None,
        "avg_pnl_pct": round(sum(x["pnl_pct"] for x in t) / len(t), 2) if t else None,
        "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else None,
        "open_positions": len(sim.positions), "final_equity": round(final),
        "bench_return_pct": bench_ret,
    }


def backtest(bars: dict[str, pd.DataFrame], p: Params, windows: dict[str, int] | None = None,
             bench: pd.DataFrame | None = None) -> dict:
    """過去の日足に同じルールを当てはめる。windows = {"1年": 365, "2年": 730} (日数)。"""
    windows = windows or {"1年": 365, "2年": 730}
    maps = {s: rowmap(prep(df, p)) for s, df in bars.items()}
    dates = sorted({d for m in maps.values() for d in m})
    if not dates:
        return {"error": "株価データがありません"}
    end = pd.Timestamp(dates[-1])
    out = {"end": dates[-1], "symbols": len(maps), "windows": {}}
    for label, days in windows.items():
        start = (end - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        sim = Sim(p)
        for d in (x for x in dates if x >= start):
            sim.process_day(d, {s: m[d] for s, m in maps.items() if d in m})
        b = None
        if bench is not None and len(bench):
            bb = bench[bench.index >= pd.Timestamp(start)]["Close"]
            if len(bb) > 1:
                b = round((float(bb.iloc[-1]) / float(bb.iloc[0]) - 1) * 100, 1)
        out["windows"][label] = {"start": next((x for x in dates if x >= start), start), **metrics(sim, b)}
    return out


def describe_rules(p: Params) -> dict:
    """現在のルールを、初心者向けの文章にする(数値は設定と必ず一致する)。"""
    pct = lambda x: f"{x * 100:g}%"  # noqa: E731
    return {
        "outline": ("日足(1日1本のローソク足)を、毎営業日の夜に判定します。上昇の勢いがついた大型株を買い、"
                    "勢いが終わったら売る「順張り」のスイングトレード(数日〜数週間の保有)です。"
                    "売買は翌営業日の寄り付きで行う想定です。"),
        "buy_title": "買い条件(次の4つをすべて満たしたとき)",
        "buy": [
            "上昇トレンドである: 株価が75日移動平均線より上にあり、25日線が75日線より上にある。"
            "(移動平均線=過去の終値の平均。長く上がり続けている銘柄だけを選びます)",
            f"高値を更新した: 終値が、直近{p.breakout_days}営業日の最高値を上回った。(上昇に勢いがついた合図です)",
            f"出来高が増えている: 出来高が直近20日の平均の{p.vol_ratio:g}倍以上。(多くの人が買っている裏づけです)",
            f"買われすぎでない: 終値が25日線から{pct(p.max_overheat)}以上離れていない。(急騰の天井での高値づかみを避けます)",
            "条件を満たす銘柄が多いときは、直近3か月の上昇率が大きい順に選びます。",
        ],
        "sell_title": "売り条件(保有中に、どちらかになったとき)",
        "sell": [
            "損切り: 終値が損切り価格以下になった。(下の「損切り価格」を参照)",
            f"上昇の終わり: 終値が、直近{p.exit_days}営業日の最安値を下回った。(伸びた利益を守るための売りです)",
        ],
        "stop_title": "損切り価格の決め方",
        "stop": [
            f"買値 −(値動きの大きさ×{p.atr_mult:g})。値動きの大きさにはATR(1日の平均的な値幅)を使うので、"
            f"値動きが激しい銘柄ほど広めになります。ただし買値の{pct(p.stop_min)}〜{pct(p.stop_max)}の範囲に収めます。",
            "損切りは夜の終値で判定します。日中に価格を割っても、その場では通知されません。"
            "急落やギャップで、損切り価格より大きく下がった価格で売ることになる場合があります。",
        ],
        "size_title": "金額と株数の決め方",
        "size": [
            f"1銘柄の上限は資金(現金+保有株の時価)の{pct(p.pos_pct)}、同時に持つのは最大{p.max_positions}銘柄まで。",
            "株数は「上限金額 ÷ 株価」を切り捨てて決めます(1株単位で買える、かぶミニを前提にしています)。",
            "現金が足りないとき、枠が埋まっているときは、買いシグナルを出しません。",
        ],
        "notes": [
            "約定は翌営業日の始値で、コスト(スプレッド等)として片道"
            f"{p.cost_pct * 100:g}%を見込んだ仮定です。実際の約定価格とは異なります。",
            "ペーパートレードです。シグナルはすべて実行されたものとして仮想の口座で成績を計算します。"
            "実際に注文したかどうかは、ダッシュボードで別に記録します。",
        ],
    }
