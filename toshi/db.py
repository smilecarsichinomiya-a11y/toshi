from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, qty INTEGER, avg_price REAL);
-- システム自身が建てたポジション(口座内の手動保有株には触れない)
CREATE TABLE IF NOT EXISTS managed (symbol TEXT PRIMARY KEY, qty INTEGER, avg_price REAL, high_water REAL, opened_at TEXT);
-- 日次成績 (毎日自動集計・蓄積)
CREATE TABLE IF NOT EXISTS daily_stats (
  date TEXT PRIMARY KEY, start_equity REAL, end_equity REAL, pnl REAL, pnl_pct REAL, realized REAL,
  trades INTEGER, wins INTEGER, losses INTEGER, win_rate REAL, profit_factor REAL, gross_win REAL, gross_loss REAL,
  avg_win REAL, avg_loss REAL, max_dd_pct REAL, bench_pct REAL, detail TEXT, review TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, qty INTEGER,
  price REAL, status TEXT, source TEXT, reason TEXT, broker_ref TEXT, pnl REAL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, action TEXT, lots INTEGER,
  confidence REAL, reason TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, equity REAL, cash REAL);
-- 改善提案(Claudeの振り返りが出し、人が承認したものだけ適用。適用前後の成績を比較する)
CREATE TABLE IF NOT EXISTS improvements (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, date TEXT, param TEXT, old_value REAL, new_value REAL,
  rationale TEXT, status TEXT DEFAULT 'pending', decided_at TEXT, applied_from TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, summary TEXT, strategy TEXT, error TEXT);
"""


def now() -> datetime:
    return datetime.now(JST)


def ts() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def execute(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def get(self, key: str, default: str | None = None) -> str | None:
        r = self.query("SELECT v FROM kv WHERE k=?", (key,))
        return r[0]["v"] if r else default

    def set(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
