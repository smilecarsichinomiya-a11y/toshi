from __future__ import annotations

import logging
import threading

from .broker import Broker
from .data import DataProvider
from .db import DB, now, ts
from .indicators import features
from .risk import Order, RiskManager, exit_signals
from .strategy import RuleStrategy, Strategy

log = logging.getLogger("toshi.engine")
MIN_BUY_CONFIDENCE = 0.55


def market_open() -> bool:
    """東証の立会時間 (平日 9:00-11:30 / 12:30-15:30)。祝日・年末年始は未考慮。"""
    n = now()
    if n.weekday() >= 5:
        return False
    m = n.hour * 60 + n.minute
    return 9 * 60 <= m <= 11 * 60 + 30 or 12 * 60 + 30 <= m <= 15 * 60 + 30


class Engine:
    def __init__(self, cfg, db: DB, broker: Broker, data: DataProvider, strategy: Strategy):
        self.cfg, self.db, self.broker, self.data, self.strategy = cfg, db, broker, data, strategy
        self.risk = RiskManager(cfg)
        self._run_lock = threading.Lock()
        self._stop = threading.Event()
        self.last_run: str = ""
        self.last_error: str = ""

    # --- 状態 ---
    @property
    def halted(self) -> bool:
        return self.db.get("halted", "0") == "1"

    def set_halted(self, v: bool) -> None:
        self.db.set("halted", "1" if v else "0")

    def equity(self, prices: dict[str, float]) -> tuple[float, float]:
        cash = self.broker.cash()
        eq = cash + sum(p.qty * prices.get(s, p.avg_price) for s, p in self.broker.positions().items())
        return eq, cash

    # --- 1サイクル ---
    def run_cycle(self, force: bool = False) -> dict:
        if not self._run_lock.acquire(blocking=False):
            return {"skipped": "実行中"}
        try:
            if not force and not market_open():
                return {"skipped": "市場時間外"}
            return self._cycle()
        except Exception as e:  # noqa: BLE001
            log.exception("cycle failed")
            self.last_error = str(e)
            self.db.execute("INSERT INTO runs(ts,summary,strategy,error) VALUES(?,?,?,?)",
                            (ts(), "", self.strategy.name, str(e)))
            return {"error": str(e)}
        finally:
            self._run_lock.release()

    def _cycle(self) -> dict:
        cfg, db = self.cfg, self.db
        positions = self.broker.positions()
        symbols = sorted(set(cfg.universe) | set(positions))
        feats, prices = {}, {}
        for s in symbols:
            df = self.data.history(s)
            f = features(df)
            if f:
                feats[s], prices[s] = f, f["price"]
        if not prices:
            raise RuntimeError("株価データを取得できませんでした")

        equity, cash = self.equity(prices)
        today = now().strftime("%Y-%m-%d")
        if db.get(f"day_start_{today}") is None:
            db.set(f"day_start_{today}", str(equity))
        day_start = float(db.get(f"day_start_{today}"))
        db.execute("INSERT OR REPLACE INTO equity(ts,equity,cash) VALUES(?,?,?)", (ts(), equity, cash))

        # 高値更新 (トレーリング用)
        hw = {r["symbol"]: r["high_water"] for r in db.query("SELECT * FROM position_meta")}
        for s, p in positions.items():
            px = prices.get(s, p.avg_price)
            hw[s] = max(hw.get(s, px), px)
            db.execute("INSERT INTO position_meta(symbol,high_water,opened_at) VALUES(?,?,?) "
                       "ON CONFLICT(symbol) DO UPDATE SET high_water=excluded.high_water", (s, hw[s], ts()))

        executed: list[str] = []
        # 1) 強制エグジット
        for o in exit_signals(cfg, positions, prices, hw):
            executed.append(self._execute(o))

        # 2) Claude の判断 → リスク審査
        positions = self.broker.positions()
        equity, cash = self.equity(prices)
        ctx = {
            "date": today,
            "cfg": {"lot_size": cfg.lot_size, "max_positions": cfg.max_positions,
                    "max_position_pct": cfg.max_position_pct, "cash_reserve_pct": cfg.cash_reserve_pct},
            "cash": round(cash), "equity": round(equity),
            "positions": {s: {"qty": p.qty, "avg_price": round(p.avg_price, 1),
                              "pnl_pct": round((prices.get(s, p.avg_price) / p.avg_price - 1) * 100, 2)}
                          for s, p in positions.items()},
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
        n_today = db.query("SELECT COUNT(*) n FROM orders WHERE ts LIKE ? AND status='filled'", (today + "%",))[0]["n"]
        orders, notes = self.risk.review(actionable, positions, prices, cash, equity, n_today, day_start, self.halted)
        for d in decisions:
            if "_skip" in d:
                notes[d["symbol"]] = d["_skip"]
        for o in orders:
            executed.append(self._execute(o))

        for d in decisions:
            outcome = notes.get(d["symbol"]) or ("実行" if d["action"] != "hold" else "")
            db.execute("INSERT INTO decisions(ts,symbol,action,lots,confidence,reason,outcome) VALUES(?,?,?,?,?,?,?)",
                       (ts(), d["symbol"], d["action"], d["lots"], d["confidence"], d["reason"], outcome))
        db.execute("INSERT INTO runs(ts,summary,strategy,error) VALUES(?,?,?,?)", (ts(), view, strat.name, ""))
        self.last_run, self.last_error = ts(), ""
        return {"market_view": view, "strategy": strat.name, "executed": executed, "notes": notes}

    def _execute(self, o: Order) -> str:
        before = self.broker.positions().get(o.symbol)
        fill = self.broker.order(o.symbol, o.side, o.qty, o.price)
        pnl = None
        if fill.ok and o.side == "sell":
            if before:
                pnl = (fill.price - before.avg_price) * o.qty
            if o.symbol not in self.broker.positions():
                self.db.execute("DELETE FROM position_meta WHERE symbol=?", (o.symbol,))
        self.db.execute(
            "INSERT INTO orders(ts,symbol,side,qty,price,status,source,reason,broker_ref,pnl) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts(), o.symbol, o.side, o.qty, fill.price, "filled" if fill.ok else "rejected",
             o.source, o.reason + ("" if fill.ok else f" [失敗: {fill.message}]"), fill.ref, pnl))
        msg = f"{'買' if o.side == 'buy' else '売'} {o.symbol} x{o.qty} @{fill.price:.0f} ({o.source}) {'OK' if fill.ok else 'NG ' + fill.message}"
        log.info(msg)
        return msg

    # --- スケジューラ ---
    def loop(self) -> None:
        while not self._stop.is_set():
            self.run_cycle()  # キルスイッチ中も損切り監視と記録は継続(新規買いはRiskManagerが拒否)
            self._stop.wait(self.cfg.interval_min * 60)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.loop, daemon=True, name="toshi-loop")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
