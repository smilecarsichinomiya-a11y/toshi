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
-- 日足スイングのシグナル(毎晩)・ペーパー口座・ユーザーが実際に注文したかの記録
CREATE TABLE IF NOT EXISTS swing_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, as_of TEXT, created_at TEXT, symbol TEXT, name TEXT, side TEXT,
  shares INTEGER, est_price REAL, est_amount REAL, stop_price REAL, stop_pct REAL, reason TEXT,
  status TEXT DEFAULT 'pending', fill_date TEXT, fill_price REAL, fill_shares INTEGER, note TEXT,
  notified INTEGER DEFAULT 0,
  user_action TEXT, user_price REAL, user_shares INTEGER, user_note TEXT, user_at TEXT);
CREATE TABLE IF NOT EXISTS swing_positions (
  symbol TEXT PRIMARY KEY, shares INTEGER, avg_price REAL, stop_pct REAL, stop_price REAL, entry_date TEXT,
  last_price REAL);
CREATE TABLE IF NOT EXISTS swing_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, name TEXT, entry_date TEXT, exit_date TEXT, shares INTEGER,
  entry_price REAL, exit_price REAL, pnl REAL, pnl_pct REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS swing_equity (date TEXT PRIMARY KEY, equity REAL, cash REAL);
CREATE TABLE IF NOT EXISTS swing_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, as_of TEXT, n_signals INTEGER, notified TEXT, note TEXT);
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
