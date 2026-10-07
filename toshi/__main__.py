from __future__ import annotations

import logging

import uvicorn

from .broker import make_broker
from .config import load_config
from .data import make_provider
from .db import DB
from .engine import Engine
from .strategy import make_strategy
from .web.app import create_app


def build_engine(cfg=None) -> Engine:
    cfg = cfg or load_config()
    db = DB(cfg.db_path)
    return Engine(cfg, db, make_broker(cfg, db), make_provider(cfg.data_source), make_strategy(cfg))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    engine = build_engine()
    cfg = engine.cfg
    if cfg.host not in ("127.0.0.1", "localhost") and not cfg.dash_token:
        raise SystemExit("外部公開(TOSHI_HOST)する場合は TOSHI_DASH_TOKEN を必ず設定してください")
    print(f"[toshi] mode={'LIVE' if cfg.live else 'paper'} strategy={engine.strategy.name} universe={len(cfg.universe)}銘柄")
    if cfg.auto_start:
        engine.start()
    uvicorn.run(create_app(engine), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
