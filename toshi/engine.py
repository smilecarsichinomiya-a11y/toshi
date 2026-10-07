from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from . import analytics, premarket
from .broker import Broker, Position
from .data import DataProvider
from .db import DB, now
from .indicators import intraday_features
from .risk import Order, RiskManager, exit_signals
from .strategy import RuleStrategy, Strategy

log = logging.getLogger("toshi.engine")
MIN_BUY_CONFIDENCE = 0.55


def _hm(t: datetime) -> str:
    return t.strftime("%H:%M")


def in_session(t: datetime) -> bool:
    """東証の立会時間 (平日 9:00-11:30 / 12:30-15:30)。祝日・年末年始は未考慮(データ無しで何もしない)。"""
    if t.weekday() >= 5:
        return False
    hm = _hm(t)
    return "09:00" <= hm <= "11:30" or "12:30" <= hm <= "15:30"


def market_open() -> bool:
    return in_session(now())


class Engine:
    def __init__(self, cfg, db: DB, broker: Broker, data: DataProvider, strategy: Strategy, clock=now):
        self.cfg, self.db, self.broker, self.data, self.strategy = cfg, db, broker, data, strategy
        self.clock = clock
        analytics.load_overrides(cfg, db)
        self.risk = RiskManager(cfg)
        self._run_lock = threading.Lock()
        self._stop = threading.Event()
        self.last_run: str = ""
        self.last_error: str = ""
        self.data_delay_min: float | None = None

    def _ts(self) -> str:
        return self.clock().strftime("%Y-%m-%d %H:%M:%S")

    # --- 状態 ---
    @property
    def halted(self) -> bool:
        return self.db.get("halted", "0") == "1"

    def set_halted(self, v: bool) -> None:
        self.db.set("halted", "1" if v else "0")

    def managed_positions(self) -> dict[str, Position]:
        """システムが建てたポジションのみ。証券口座の実保有数を上限とする(手動保有株は対象外)。"""
        held = self.broker.positions()
        out = {}
        for r in self.db.query("SELECT * FROM managed WHERE qty>0"):
            q = min(r["qty"], held[r["symbol"]].qty if r["symbol"] in held else 0)
            if q > 0:
                out[r["symbol"]] = Position(r["symbol"], q, r["avg_price"])
        return out

    def equity(self, prices: dict[str, float]) -> tuple[float, float]:
        cash = self.broker.cash()
        eq = cash + sum(p.qty * prices.get(s, p.avg_price) for s, p in self.managed_positions().items())
        return eq, cash

    def entry_allowed(self, t: datetime) -> bool:
        hm = _hm(t)
        return self.cfg.entry_start <= hm <= self.cfg.entry_end and not ("11:25" <= hm < "12:35")

    # --- 1サイクル ---
    def run_cycle(self, force: bool = False) -> dict:
        """force=True: 時間制約を無視して判断まで行う(paper での手動実行・テスト用)。"""
        if not self._run_lock.acquire(blocking=False):
            return {"skipped": "実行中"}
        try:
            if not force and not in_session(self.clock()):
                return {"skipped": "市場時間外"}
            return self._cycle(force)
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed")
            self.last_error = str(e)
            self.db.execute("INSERT INTO runs(ts,summary,strategy,error) VALUES(?,?,?,?)",
                            (self._ts(), "", self.strategy.name, str(e)))
            return {"error": str(e)}
        finally:
            self._run_lock.release()

    def _cycle(self, force: bool) -> dict:
        cfg, db = self.cfg, self.db
        t = self.clock()
        today = t.strftime("%Y-%m-%d")
        flatten = not force and _hm(t) >= cfg.flatten_at
        can_enter = force or self.entry_allowed(t)

        positions = self.managed_positions()
        pm = premarket.get(db, today)
        symbols = sorted(set(premarket.watchlist(self, today)) | set(positions))
        feats, prices, delays = {}, {}, []
        # 1単元が1銘柄の上限額を超える銘柄は判断対象から外す(Claude に無駄な判断をさせない)
        self._equity_hint = self.broker.cash() + sum(p.qty * p.avg_price for p in positions.values())
        for s in symbols:
            bars = self.data.intraday(s)
            f = intraday_features(bars, self.data.history(s))
            if f:
                # 5分足の確定時刻(=足の開始+5分)から現在までの遅れ
                delay = max(0.0, (t - bars.index[-1].to_pydatetime()).total_seconds() / 60 - 5)
                f["data_delay_min"] = round(delay)
                delays.append(delay)
                prices[s] = f["price"]
                affordable = f["price"] * cfg.lot_size <= cfg.max_position_pct * self._equity_hint
                if s in positions or (affordable and (force or delay <= cfg.max_data_delay_min)):
                    feats[s] = f
            elif s in positions:
                px = self.data.last_price(s)
                if px:
                    prices[s] = px
        if not prices:
            raise RuntimeError("株価データを取得できませんでした")
        self.data_delay_min = round(sorted(delays)[len(delays) // 2]) if delays else None

        equity, cash = self.equity(prices)
        if db.get(f"day_start_{today}") is None:
            db.set(f"day_start_{today}", str(equity))
        day_start = float(db.get(f"day_start_{today}"))
        db.execute("INSERT OR REPLACE INTO equity(ts,equity,cash) VALUES(?,?,?)", (self._ts(), equity, cash))

        hw = {}
        for s, p in positions.items():
            row = db.query("SELECT high_water FROM managed WHERE symbol=?", (s,))
            hw[s] = max(row[0]["high_water"] or 0, prices.get(s, 0)) if row else prices.get(s, 0)
            db.execute("UPDATE managed SET high_water=? WHERE symbol=?", (hw[s], s))

        executed: list[str] = []
        # 1) 強制エグジット: 大引け前の全決済 / 前日からの持ち越し / 損切り・利確・トレーリング
        opened = {r["symbol"]: r["opened_at"] for r in db.query("SELECT symbol,opened_at FROM managed")}
        forced: list[Order] = []
        for s, p in positions.items():
            px = prices.get(s)
            if not px:
                continue
            if flatten:
                forced.append(Order(s, "sell", p.qty, px, "risk-flatten", f"大引け前の強制決済({cfg.flatten_at}以降)"))
            elif (opened.get(s) or today) < today:
                forced.append(Order(s, "sell", p.qty, px, "risk-overnight", "持ち越しポジションの解消"))
        done = {o.symbol for o in forced}
        forced += [o for o in exit_signals(cfg, {s: p for s, p in positions.items() if s not in done}, prices, hw)]
        for o in forced:
            executed.append(self._execute(o))

        if flatten:
            view = f"{cfg.flatten_at}以降: 新規売買なし・全決済"
            db.execute("INSERT INTO runs(ts,summary,strategy,error) VALUES(?,?,?,?)", (self._ts(), view, "system", ""))
            self.last_run, self.last_error = self._ts(), ""
            return {"market_view": view, "strategy": "system", "executed": executed, "notes": {}}

        # 2) Claude の判断 → リスク審査
        positions = self.managed_positions()
        equity, cash = self.equity(prices)
        todays = db.query("SELECT ts,symbol,side,qty,price,source,pnl FROM orders "
                          "WHERE ts LIKE ? AND status='filled' ORDER BY id", (today + "%",))
        fh, fm = map(int, cfg.flatten_at.split(":"))
        ctx = {
            "now": t.strftime("%Y-%m-%d %H:%M"), "can_enter_new": can_enter,
            "minutes_to_flatten": max(0, fh * 60 + fm - (t.hour * 60 + t.minute)),
            "cfg": {"lot_size": cfg.lot_size, "max_positions": cfg.max_positions,
                    "max_position_pct": cfg.max_position_pct, "stop_loss_pct": cfg.stop_loss_pct,
                    "take_profit_pct": cfg.take_profit_pct, "trailing_stop_pct": cfg.trailing_stop_pct},
            "cash": round(cash), "equity": round(equity),
            "today": {"pnl": round(equity - day_start), "trades": todays[-30:]},
            "positions": {s: {"qty": p.qty, "avg_price": round(p.avg_price, 1),
                              "pnl_pct": round((prices.get(s, p.avg_price) / p.avg_price - 1) * 100, 2),
                              "opened_at": opened.get(s)}
                          for s, p in positions.items()},
            "recent_performance": analytics.recent_for_prompt(db),
            "today_focus": {"outlook": pm["outlook"], "picks": pm["picks"]} if pm else None,
            "features": {s: {**f, "one_lot_cost": round(f["price"] * cfg.lot_size)} for s, f in feats.items()},
        }
        strat = self.strategy
        try:
            view, decisions = strat.decide(ctx)
        except Exception as e:  # noqa: BLE001
            log.warning("strategy %s failed (%s) -> fallback", strat.name, e)
            self.last_error = f"{strat.name}: {e}"
            strat = RuleStrategy()
            view, decisions = strat.decide(ctx)
            view = f"[{self.strategy.name}失敗: {e}] " + view

        for d in decisions:
            if d["action"] == "buy" and d["confidence"] < MIN_BUY_CONFIDENCE:
                d["_skip"] = f"確信度{d['confidence']:.2f}が閾値{MIN_BUY_CONFIDENCE}未満"
        actionable = [d for d in decisions if "_skip" not in d]
        n_today = sum(1 for _ in todays)
        orders, notes = self.risk.review(actionable, positions, prices, cash, equity, n_today, day_start,
                                         self.halted, can_enter, self._blocked(todays, t))
        for d in decisions:
            if "_skip" in d:
                notes[d["symbol"]] = d["_skip"]
        for o in orders:
            executed.append(self._execute(o))

        for d in decisions:
            outcome = notes.get(d["symbol"]) or ("実行" if d["action"] != "hold" else "")
            db.execute("INSERT INTO decisions(ts,symbol,action,lots,confidence,reason,outcome) VALUES(?,?,?,?,?,?,?)",
                       (self._ts(), d["symbol"], d["action"], d["lots"], d["confidence"], d["reason"], outcome))
        db.execute("INSERT INTO runs(ts,summary,strategy,error) VALUES(?,?,?,?)", (self._ts(), view, strat.name, ""))
        self.last_run = self._ts()
        if strat is self.strategy:
            self.last_error = ""
        return {"market_view": view, "strategy": strat.name, "executed": executed, "notes": notes}

    def _blocked(self, todays: list[dict], t: datetime) -> dict[str, str]:
        """クールダウン中・往復回数上限の銘柄は新規買いしない(往復ビンタ防止)。"""
        out: dict[str, str] = {}
        sells: dict[str, list[str]] = {}
        for o in todays:
            if o["side"] == "sell":
                sells.setdefault(o["symbol"], []).append(o["ts"])
        for s, tss in sells.items():
            if len(tss) >= self.cfg.max_roundtrips_per_symbol:
                out[s] = f"本日の往復回数上限({self.cfg.max_roundtrips_per_symbol})"
                continue
            last = datetime.fromisoformat(tss[-1]).replace(tzinfo=t.tzinfo)
            if (t - last).total_seconds() < self.cfg.cooldown_min * 60:
                out[s] = f"決済後{self.cfg.cooldown_min}分のクールダウン中"
        return out

    def _execute(self, o: Order) -> str:
        before = self.managed_positions().get(o.symbol)
        fill = self.broker.order(o.symbol, o.side, o.qty, o.price)
        pnl = None
        if fill.ok:
            if o.side == "buy":
                q = o.qty + (before.qty if before else 0)
                avg = (fill.price * o.qty + (before.avg_price * before.qty if before else 0)) / q
                self.db.execute("INSERT INTO managed(symbol,qty,avg_price,high_water,opened_at) VALUES(?,?,?,?,?) "
                                "ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price",
                                (o.symbol, q, avg, fill.price, self._ts()))
            else:
                if before:
                    pnl = (fill.price - before.avg_price) * o.qty
                left = (before.qty if before else 0) - o.qty
                if left > 0:
                    self.db.execute("UPDATE managed SET qty=? WHERE symbol=?", (left, o.symbol))
                else:
                    self.db.execute("DELETE FROM managed WHERE symbol=?", (o.symbol,))
        self.db.execute(
            "INSERT INTO orders(ts,symbol,side,qty,price,status,source,reason,broker_ref,pnl) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self._ts(), o.symbol, o.side, o.qty, fill.price, "filled" if fill.ok else "rejected",
             o.source, o.reason + ("" if fill.ok else f" [失敗: {fill.message}]"), fill.ref, pnl))
        msg = (f"{'買' if o.side == 'buy' else '売'} {o.symbol} x{o.qty} @{fill.price:.0f} ({o.source}) "
               f"{'OK' if fill.ok else 'NG ' + fill.message}")
        log.info(msg)
        return msg

    # --- 日次成績 (毎日必ず) ---
    def maybe_daily(self) -> None:
        t = self.clock()
        today = t.strftime("%Y-%m-%d")
        if t.weekday() < 5 and _hm(t) >= self.cfg.review_at and self.db.get(f"daily_done_{today}") is None:
            with self._run_lock:
                self._snapshot()
                analytics.run_daily(self, today)
                analytics.maybe_evaluate(self)

    def maybe_premarket(self) -> None:
        """寄り付き前(既定8:30)に、その日の注目銘柄を1回選ぶ。起動が遅れた日も、前場の間は追いつく。"""
        t = self.clock()
        today = t.strftime("%Y-%m-%d")
        if (t.weekday() >= 5 or not (self.cfg.premarket_at <= _hm(t) < "11:30")
                or self.db.get(f"premarket_done_{today}") is not None):
            return
        self.db.set(f"premarket_done_{today}", "1")  # 失敗しても再試行しない(API費用と時間の暴走防止)
        with self._run_lock:
            premarket.run(self, today)

    def _snapshot(self) -> None:
        pos = self.managed_positions()
        prices = {s: self.data.last_price(s) or p.avg_price for s, p in pos.items()}
        eq, cash = self.equity(prices)
        self.db.execute("INSERT OR REPLACE INTO equity(ts,equity,cash) VALUES(?,?,?)", (self._ts(), eq, cash))

    # --- スケジューラ ---
    def loop(self) -> None:
        t = self.clock()
        try:
            analytics.backfill(self, t.strftime("%Y-%m-%d"), include_today=_hm(t) >= self.cfg.review_at)
            analytics.maybe_evaluate(self)
        except Exception:  # noqa: BLE001
            log.exception("backfill failed")
        step = self.cfg.interval_min * 60
        while not self._stop.is_set():
            try:
                self.maybe_premarket()
            except Exception:  # noqa: BLE001
                log.exception("premarket job failed")
            self.run_cycle()  # キルスイッチ中も損切り・強制決済は継続(新規買いのみ RiskManager が拒否)
            try:
                self.maybe_daily()
            except Exception:  # noqa: BLE001
                log.exception("daily job failed")
            self._stop.wait(step - time.time() % step + 3)  # 5分足の確定直後に合わせる

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.loop, daemon=True, name="toshi-loop")
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
