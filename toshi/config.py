from __future__ import annotations

import os
from dataclasses import dataclass, field

from .universe import SIGNAL_UNIVERSE


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


# 売買が活発な大型株。資金で1単元(100株)が買えない銘柄は自動で対象外になる
DEFAULT_UNIVERSE = ("8306,8411,8316,8604,8601,9432,9434,4755,4689,7201,7267,6752,5020,9501,6178,"
                    "7203,8058,5401,7011,6501,6758")


@dataclass
class Config:
    # --- 動作モード: signals(毎晩の日足シグナルを通知。既定) / daytrade(旧: 日中の仮想デイトレ) ---
    mode: str = "signals"
    signal_at: str = "18:00"  # 毎営業日、この時刻以降にシグナルを判定する
    signal_universe: list[str] = field(default_factory=list)
    signal_pos_pct: float = 0.25  # 1銘柄の上限(総資産に対する割合)
    signal_max_positions: int = 4
    signal_cost_pct: float = 0.002  # 片道のコスト(スプレッド等)の仮定
    notify_empty: bool = False  # シグナルが無い日にも通知する
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_to: str = ""
    line_token: str = ""  # LINE Messaging API のチャネルアクセストークン
    line_user_id: str = ""
    data_source: str = "yfinance"
    universe: list[str] = field(default_factory=list)
    benchmark: str = "1306"  # TOPIX連動ETF。日次成績の比較対象
    model: str = "claude-sonnet-5-5"
    anthropic_key: str = ""
    initial_cash: float = 500_000
    lot_size: int = 100
    # --- リスク (デイトレ向け既定値) ---
    max_positions: int = 2
    max_position_pct: float = 0.50
    cash_reserve_pct: float = 0.05
    stop_loss_pct: float = 0.015
    trailing_stop_pct: float = 0.015
    take_profit_pct: float = 0.03
    daily_loss_limit_pct: float = 0.02
    max_orders_per_day: int = 20
    cooldown_min: int = 30
    max_roundtrips_per_symbol: int = 2
    max_data_delay_min: int = 45  # 最新の足がこれより古い銘柄には新規エントリーしない
    # --- 時間割 (JST, HH:MM) ---
    entry_start: str = "09:30"
    entry_end: str = "14:30"
    flatten_at: str = "15:15"  # これ以降は全ポジションを強制決済 (持ち越さない)
    premarket_at: str = "08:30"  # 寄り付き前のニュース確認・注目銘柄の選定
    watch_max: int = 8  # 1日に注目する銘柄数の上限
    min_avg_volume: int = 200_000  # 直近20日平均の出来高(株)がこれ未満の銘柄は選ばない
    review_at: str = "15:40"  # 日次成績の集計・振り返り
    eval_days: int = 20  # この営業日数たまったら総合評価を出す
    interval_min: int = 15
    auto_start: bool = True
    # --- ダッシュボード ---
    host: str = "127.0.0.1"
    port: int = 8000
    dash_token: str = ""
    db_path: str = "data/toshi.db"
    report_dir: str = "data/reports"


def load_config() -> Config:
    _load_dotenv()
    uni = _s("TOSHI_UNIVERSE", DEFAULT_UNIVERSE)
    sig = _s("TOSHI_SIGNAL_UNIVERSE", "")
    cfg = Config(
        mode=_s("TOSHI_MODE", "signals"),
        signal_at=_s("TOSHI_SIGNAL_AT", "18:00"),
        signal_universe=[u.strip() for u in sig.split(",") if u.strip()] or list(SIGNAL_UNIVERSE),
        signal_pos_pct=_f("TOSHI_SIGNAL_POS_PCT", 0.25),
        signal_max_positions=_i("TOSHI_SIGNAL_MAX_POSITIONS", 4),
        signal_cost_pct=_f("TOSHI_SIGNAL_COST_PCT", 0.002),
        notify_empty=_s("TOSHI_NOTIFY_EMPTY", "no").lower() == "yes",
        smtp_host=_s("TOSHI_SMTP_HOST", ""),
        smtp_port=_i("TOSHI_SMTP_PORT", 587),
        smtp_user=_s("TOSHI_SMTP_USER", ""),
        smtp_password=_s("TOSHI_SMTP_PASSWORD", ""),
        mail_to=_s("TOSHI_MAIL_TO", ""),
        line_token=_s("TOSHI_LINE_TOKEN", ""),
        line_user_id=_s("TOSHI_LINE_USER_ID", ""),
        data_source=_s("TOSHI_DATA", "yfinance"),
        universe=[u.strip() for u in uni.split(",") if u.strip()],
        benchmark=_s("TOSHI_BENCHMARK", "1306"),
        model=_s("TOSHI_MODEL", "claude-sonnet-5-5"),
        anthropic_key=_s("ANTHROPIC_API_KEY", ""),
        initial_cash=_f("TOSHI_INITIAL_CASH", 500_000),
        lot_size=_i("TOSHI_LOT_SIZE", 100),
        max_positions=_i("TOSHI_MAX_POSITIONS", 2),
        max_position_pct=_f("TOSHI_MAX_POSITION_PCT", 0.50),
        cash_reserve_pct=_f("TOSHI_CASH_RESERVE_PCT", 0.05),
        stop_loss_pct=_f("TOSHI_STOP_LOSS_PCT", 0.015),
        trailing_stop_pct=_f("TOSHI_TRAILING_STOP_PCT", 0.015),
        take_profit_pct=_f("TOSHI_TAKE_PROFIT_PCT", 0.03),
        daily_loss_limit_pct=_f("TOSHI_DAILY_LOSS_LIMIT_PCT", 0.02),
        max_orders_per_day=_i("TOSHI_MAX_ORDERS_PER_DAY", 20),
        max_data_delay_min=_i("TOSHI_MAX_DATA_DELAY_MIN", 45),
        cooldown_min=_i("TOSHI_COOLDOWN_MIN", 30),
        max_roundtrips_per_symbol=_i("TOSHI_MAX_ROUNDTRIPS", 2),
        entry_start=_s("TOSHI_ENTRY_START", "09:30"),
        entry_end=_s("TOSHI_ENTRY_END", "14:30"),
        flatten_at=_s("TOSHI_FLATTEN_AT", "15:15"),
        premarket_at=_s("TOSHI_PREMARKET_AT", "08:30"),
        watch_max=_i("TOSHI_WATCH_MAX", 8),
        min_avg_volume=_i("TOSHI_MIN_AVG_VOLUME", 200_000),
        review_at=_s("TOSHI_REVIEW_AT", "15:40"),
        eval_days=_i("TOSHI_EVAL_DAYS", 20),
        interval_min=_i("TOSHI_INTERVAL_MIN", 15),
        auto_start=_s("TOSHI_AUTO_START", "yes").lower() == "yes",
        host=_s("TOSHI_HOST", "127.0.0.1"),
        port=_i("TOSHI_PORT", 8000),
        dash_token=_s("TOSHI_DASH_TOKEN", ""),
        db_path=_s("TOSHI_DB", "data/toshi.db"),
        report_dir=_s("TOSHI_REPORT_DIR", "data/reports"),
    )
    return cfg
