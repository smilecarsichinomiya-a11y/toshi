from __future__ import annotations

import os
from dataclasses import dataclass, field


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def _s(name: str, default: str) -> str:
    return os.environ.get(name) or default


@dataclass
class Config:
    mode: str = "paper"
    live_confirm: bool = False
    data_source: str = "yfinance"
    universe: list[str] = field(default_factory=list)
    model: str = "claude-sonnet-5-5"
    anthropic_key: str = ""
    initial_cash: float = 3_000_000
    max_positions: int = 5
    max_position_pct: float = 0.30
    cash_reserve_pct: float = 0.10
    stop_loss_pct: float = 0.07
    trailing_stop_pct: float = 0.10
    take_profit_pct: float = 0.20
    daily_loss_limit_pct: float = 0.03
    max_orders_per_day: int = 10
    interval_min: int = 60
    auto_start: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    dash_token: str = ""
    kabu_url: str = "http://localhost:18080/kabusapi"
    kabu_password: str = ""
    db_path: str = "data/toshi.db"
    lot_size: int = 100

    @property
    def live(self) -> bool:
        return self.mode == "live" and self.live_confirm


def load_config() -> Config:
    _load_dotenv()
    uni = _s("TOSHI_UNIVERSE", "7203,6758,9984,8306,9432,6861,8035,4063,6098,7974")
    cfg = Config(
        mode=_s("TOSHI_MODE", "paper"),
        live_confirm=_s("TOSHI_LIVE_CONFIRM", "no").lower() == "yes",
        data_source=_s("TOSHI_DATA", "yfinance"),
        universe=[u.strip() for u in uni.split(",") if u.strip()],
        model=_s("TOSHI_MODEL", "claude-sonnet-5-5"),
        anthropic_key=_s("ANTHROPIC_API_KEY", ""),
        initial_cash=_f("TOSHI_INITIAL_CASH", 3_000_000),
        max_positions=_i("TOSHI_MAX_POSITIONS", 5),
        max_position_pct=_f("TOSHI_MAX_POSITION_PCT", 0.30),
        cash_reserve_pct=_f("TOSHI_CASH_RESERVE_PCT", 0.10),
        stop_loss_pct=_f("TOSHI_STOP_LOSS_PCT", 0.07),
        trailing_stop_pct=_f("TOSHI_TRAILING_STOP_PCT", 0.10),
        take_profit_pct=_f("TOSHI_TAKE_PROFIT_PCT", 0.20),
        daily_loss_limit_pct=_f("TOSHI_DAILY_LOSS_LIMIT_PCT", 0.03),
        max_orders_per_day=_i("TOSHI_MAX_ORDERS_PER_DAY", 10),
        interval_min=_i("TOSHI_INTERVAL_MIN", 60),
        auto_start=_s("TOSHI_AUTO_START", "yes").lower() == "yes",
        host=_s("TOSHI_HOST", "127.0.0.1"),
        port=_i("TOSHI_PORT", 8000),
        dash_token=_s("TOSHI_DASH_TOKEN", ""),
        kabu_url=_s("KABU_API_URL", "http://localhost:18080/kabusapi"),
        kabu_password=_s("KABU_API_PASSWORD", ""),
        db_path=_s("TOSHI_DB", "data/toshi.db"),
        lot_size=_i("TOSHI_LOT_SIZE", 100),
    )
    if cfg.mode == "live" and not cfg.live_confirm:
        print("[toshi] TOSHI_MODE=live ですが TOSHI_LIVE_CONFIRM=yes が無いため paper で動作します。")
    return cfg
