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
    benchmark: str = "1306"  # TOPIX連動ETF。日次成績の比較対象
    model: str = "claude-sonnet-5-5"
    anthropic_key: str = ""
    initial_cash: float = 3_000_000
    lot_size: int = 100
    # --- リスク (デイトレ向け既定値) ---
    max_positions: int = 3
    max_position_pct: float = 0.33
    cash_reserve_pct: float = 0.05
    stop_loss_pct: float = 0.01
    trailing_stop_pct: float = 0.01
    take_profit_pct: float = 0.02
    daily_loss_limit_pct: float = 0.02
    max_orders_per_day: int = 40
    cooldown_min: int = 15
    max_roundtrips_per_symbol: int = 3
    # --- 時間割 (JST, HH:MM) ---
    entry_start: str = "09:05"
    entry_end: str = "14:45"
    flatten_at: str = "15:15"  # これ以降は全ポジションを強制決済 (持ち越さない)
    review_at: str = "15:40"  # 日次成績の集計・振り返り
    interval_min: int = 5
    auto_start: bool = True
    # --- ダッシュボード ---
    host: str = "127.0.0.1"
    port: int = 8000
    dash_token: str = ""
    # --- 楽天証券 (マーケットスピードII RSS ブリッジ) ---
    bridge_dir: str = "data/bridge"
    db_path: str = "data/toshi.db"
    report_dir: str = "data/reports"

    @property
    def live(self) -> bool:
        return self.mode == "live" and self.live_confirm


def load_config() -> Config:
    _load_dotenv()
    uni = _s("TOSHI_UNIVERSE", "7203,6758,9984,8306,8316,8411,9432,7011,7012,5401,6501,8035")
    cfg = Config(
        mode=_s("TOSHI_MODE", "paper"),
        live_confirm=_s("TOSHI_LIVE_CONFIRM", "no").lower() == "yes",
        data_source=_s("TOSHI_DATA", "yfinance"),
        universe=[u.strip() for u in uni.split(",") if u.strip()],
        benchmark=_s("TOSHI_BENCHMARK", "1306"),
        model=_s("TOSHI_MODEL", "claude-sonnet-5-5"),
        anthropic_key=_s("ANTHROPIC_API_KEY", ""),
        initial_cash=_f("TOSHI_INITIAL_CASH", 3_000_000),
        lot_size=_i("TOSHI_LOT_SIZE", 100),
        max_positions=_i("TOSHI_MAX_POSITIONS", 3),
        max_position_pct=_f("TOSHI_MAX_POSITION_PCT", 0.33),
        cash_reserve_pct=_f("TOSHI_CASH_RESERVE_PCT", 0.05),
        stop_loss_pct=_f("TOSHI_STOP_LOSS_PCT", 0.01),
        trailing_stop_pct=_f("TOSHI_TRAILING_STOP_PCT", 0.01),
        take_profit_pct=_f("TOSHI_TAKE_PROFIT_PCT", 0.02),
        daily_loss_limit_pct=_f("TOSHI_DAILY_LOSS_LIMIT_PCT", 0.02),
        max_orders_per_day=_i("TOSHI_MAX_ORDERS_PER_DAY", 40),
        cooldown_min=_i("TOSHI_COOLDOWN_MIN", 15),
        max_roundtrips_per_symbol=_i("TOSHI_MAX_ROUNDTRIPS", 3),
        entry_start=_s("TOSHI_ENTRY_START", "09:05"),
        entry_end=_s("TOSHI_ENTRY_END", "14:45"),
        flatten_at=_s("TOSHI_FLATTEN_AT", "15:15"),
        review_at=_s("TOSHI_REVIEW_AT", "15:40"),
        interval_min=_i("TOSHI_INTERVAL_MIN", 5),
        auto_start=_s("TOSHI_AUTO_START", "yes").lower() == "yes",
        host=_s("TOSHI_HOST", "127.0.0.1"),
        port=_i("TOSHI_PORT", 8000),
        dash_token=_s("TOSHI_DASH_TOKEN", ""),
        bridge_dir=_s("TOSHI_BRIDGE_DIR", "data/bridge"),
        db_path=_s("TOSHI_DB", "data/toshi.db"),
        report_dir=_s("TOSHI_REPORT_DIR", "data/reports"),
    )
    if cfg.mode == "live" and not cfg.live_confirm:
        print("[toshi] TOSHI_MODE=live ですが TOSHI_LIVE_CONFIRM=yes が無いため paper で動作します。")
    return cfg
