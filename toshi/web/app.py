from __future__ import annotations

import os
import threading

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

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
        pos = broker.positions()
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
            "mode": "live" if cfg.live else "paper", "broker": broker.name, "strategy": engine.strategy.name,
            "model": cfg.model, "halted": engine.halted, "market_open": market_open(),
            "equity": equity, "cash": cash, "day_pnl": equity - start, "total_pnl": equity - base,
            "realized_pnl": realized, "last_run": engine.last_run, "last_error": engine.last_error,
            "market_view": last[0]["summary"] if last else "", "universe": cfg.universe,
            "limits": {"stop_loss": cfg.stop_loss_pct, "trailing": cfg.trailing_stop_pct,
                       "take_profit": cfg.take_profit_pct, "daily_loss": cfg.daily_loss_limit_pct,
                       "max_positions": cfg.max_positions, "max_position_pct": cfg.max_position_pct},
        }

    @app.get("/api/positions", dependencies=[Depends(auth)])
    def positions():
        out = []
        for s, p in engine.broker.positions().items():
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

    @app.post("/api/run", dependencies=[Depends(auth)])
    def run_now():
        # 手動実行は市場時間外でも判断・記録まで行う(paper向け)。live では市場時間外は実行しない。
        threading.Thread(target=engine.run_cycle, kwargs={"force": not cfg.live}, daemon=True).start()
        return {"started": True}

    @app.post("/api/halt", dependencies=[Depends(auth)])
    def halt(on: bool = True):
        engine.set_halted(on)
        return {"halted": engine.halted}

    return app
