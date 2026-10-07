from __future__ import annotations

import os
import threading

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

import json

from .. import analytics, premarket
from ..engine import Engine, market_open

HERE = os.path.dirname(__file__)


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="toshi dashboard")
    cfg, db = engine.cfg, engine.db

    def auth(request: Request):
        if cfg.dash_token and request.headers.get("authorization") != f"Bearer {cfg.dash_token}":
            raise HTTPException(401, "unauthorized")

    @app.get("/")
    def index():
        return FileResponse(os.path.join(HERE, "index.html"))

    @app.get("/api/summary", dependencies=[Depends(auth)])
    def summary():
        broker = engine.broker
        pos = engine.managed_positions()
        prices = {}
        for s in pos:
            px = engine.data.last_price(s)
            prices[s] = px or pos[s].avg_price
        equity, cash = engine.equity(prices)
        today = db.query("SELECT strftime('%Y-%m-%d','now','+9 hours') d")[0]["d"]
        start = float(db.get(f"day_start_{today}") or equity)
        first = db.query("SELECT equity FROM equity ORDER BY ts LIMIT 1")
        base = first[0]["equity"] if first else equity
        realized = db.query("SELECT COALESCE(SUM(pnl),0) p FROM orders WHERE status='filled'")[0]["p"]
        last = db.query("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        return {
            "mode": "paper", "broker": broker.name, "strategy": engine.strategy.name,
            "model": cfg.model, "halted": engine.halted, "market_open": market_open(),
            "equity": equity, "cash": cash, "day_pnl": equity - start, "total_pnl": equity - base,
            "realized_pnl": realized, "last_run": engine.last_run, "last_error": engine.last_error,
            "market_view": last[0]["summary"] if last else "", "universe": cfg.universe,
            "data_delay_min": engine.data_delay_min,
            "schedule": {"entry": f"{cfg.entry_start}-{cfg.entry_end}", "flatten": cfg.flatten_at,
                         "review": cfg.review_at, "interval_min": cfg.interval_min},
            "limits": {"stop_loss": cfg.stop_loss_pct, "trailing": cfg.trailing_stop_pct,
                       "take_profit": cfg.take_profit_pct, "daily_loss": cfg.daily_loss_limit_pct,
                       "max_positions": cfg.max_positions, "max_position_pct": cfg.max_position_pct},
        }

    @app.get("/api/positions", dependencies=[Depends(auth)])
    def positions():
        out = []
        for s, p in engine.managed_positions().items():
            px = engine.data.last_price(s) or p.avg_price
            out.append({"symbol": s, "qty": p.qty, "avg_price": p.avg_price, "price": px,
                        "value": px * p.qty, "pnl": (px - p.avg_price) * p.qty,
                        "pnl_pct": (px / p.avg_price - 1) * 100})
        return out

    @app.get("/api/orders", dependencies=[Depends(auth)])
    def orders(limit: int = 100):
        return db.query("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (min(limit, 500),))

    @app.get("/api/decisions", dependencies=[Depends(auth)])
    def decisions(limit: int = 100):
        return db.query("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (min(limit, 500),))

    @app.get("/api/equity", dependencies=[Depends(auth)])
    def equity():
        return db.query("SELECT ts,equity,cash FROM equity ORDER BY ts")

    @app.get("/api/runs", dependencies=[Depends(auth)])
    def runs():
        return db.query("SELECT * FROM runs ORDER BY id DESC LIMIT 30")

    @app.get("/api/daily", dependencies=[Depends(auth)])
    def daily(limit: int = 60):
        rows = db.query("SELECT * FROM daily_stats ORDER BY date DESC LIMIT ?", (min(limit, 1000),))
        for r in rows:
            r["detail"] = json.loads(r["detail"]) if r["detail"] else None
            r["review"] = json.loads(r["review"]) if r["review"] else None
        return rows

    @app.get("/api/cumulative", dependencies=[Depends(auth)])
    def cumulative():
        return analytics.cumulative(db)

    @app.get("/api/evaluation", dependencies=[Depends(auth)])
    def evaluation():
        ev = db.get("evaluation")
        return {"eval_days": cfg.eval_days, "days": analytics.cumulative(db)["days"],
                "checks": analytics.checks(db, cfg.initial_cash), "result": json.loads(ev) if ev else None}

    @app.get("/api/premarket", dependencies=[Depends(auth)])
    def premarket_latest():
        return premarket.latest(db)

    @app.post("/api/premarket/run", dependencies=[Depends(auth)])
    def premarket_run():
        d = engine.clock().strftime("%Y-%m-%d")
        threading.Thread(target=lambda: premarket.run(engine, d), daemon=True).start()
        return {"started": True}

    @app.get("/api/improvements", dependencies=[Depends(auth)])
    def improvements():
        return analytics.improvements(db)

    @app.post("/api/improvements/{imp_id}", dependencies=[Depends(auth)])
    def decide(imp_id: int, approve: bool):
        r = analytics.decide_improvement(engine, imp_id, approve)
        if r is None:
            raise HTTPException(404, "承認待ちの提案が見つかりません")
        return r

    @app.post("/api/daily/run", dependencies=[Depends(auth)])
    def daily_run(date: str | None = None):
        d = date or engine.clock().strftime("%Y-%m-%d")
        st = analytics.run_daily(engine, d)
        return {"date": d, "ok": st is not None}

    @app.post("/api/run", dependencies=[Depends(auth)])
    def run_now():
        # 手動実行は市場時間外でも判断・記録まで行う(仮想売買なので実害はない)
        threading.Thread(target=engine.run_cycle, kwargs={"force": True}, daemon=True).start()
        return {"started": True}

    @app.post("/api/halt", dependencies=[Depends(auth)])
    def halt(on: bool = True):
        engine.set_halted(on)
        return {"halted": engine.halted}

    return app
