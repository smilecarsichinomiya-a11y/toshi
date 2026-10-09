"""毎営業日の夜に日足でシグナルを判定し、保存・通知する(ペーパートレード)。"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from . import swing
from .db import DB, now
from .notify import Notifier
from .universe import name_of

log = logging.getLogger("toshi.signals")
BACKTEST_MAX_AGE_DAYS = 7


def format_notice(as_of: str, sigs: list[dict], capital: float) -> tuple[str, str]:
    """通知の本文。銘柄名・コード、買い/売り、推奨株数、概算金額、損切り価格。"""
    sells = [s for s in sigs if s["side"] == "sell"]
    buys = [s for s in sigs if s["side"] == "buy"]
    lines = [f"{as_of} の引け後に判定したシグナルです(ペーパートレード)。",
             "翌営業日の寄り付きで注文する想定です。株数・金額は終値で計算した目安です。", ""]
    if sells:
        lines.append("■ 売り")
        for s in sells:
            lines += [f"・{name_of(s['symbol'])}({s['symbol']}) 売り {s['shares']:,}株 概算 {s['amount']:,.0f}円",
                      f"  理由: {s['reason']}"]
        lines.append("")
    if buys:
        lines.append("■ 買い")
        for s in buys:
            lines += [f"・{name_of(s['symbol'])}({s['symbol']}) 買い {s['shares']:,}株 概算 {s['amount']:,.0f}円",
                      f"  損切り価格 {s['stop_price']:,.0f}円(買値の約-{s['stop_pct'] * 100:.1f}%)",
                      f"  理由: {s['reason']}"]
        lines.append("")
    if not sigs:
        lines.append("本日の売買シグナルはありません。")
        lines.append("")
    lines.append("※投資判断と注文は自己責任です。注文したか見送ったかは、ダッシュボードに記録してください。")
    head = f"【toshi】{as_of} 売り{len(sells)}件・買い{len(buys)}件" if sigs else f"【toshi】{as_of} シグナルなし"
    return head, "\n".join(lines)


class SignalService:
    def __init__(self, cfg, db: DB, data, notifier: Notifier | None = None, clock=now):
        self.cfg, self.db, self.data, self.clock = cfg, db, data, clock
        self.p = swing.Params.from_cfg(cfg)
        self.notifier = notifier or Notifier(cfg)
        self.last_error = ""
        self.running = False
        self.bt_running = False
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # --- データ ---
    def _completed(self, df: pd.DataFrame) -> pd.DataFrame:
        """場中・引け直後は、当日の途中の足を使わない(確定した日足だけで判定する)。"""
        t = self.clock()
        if t.weekday() < 5 and t.strftime("%H:%M") < "15:45":
            df = df[df.index < pd.Timestamp(t.strftime("%Y-%m-%d"))]
        return df

    def _fetch(self, symbols: list[str]) -> tuple[dict[str, pd.DataFrame], list[str]]:
        def one(s):
            try:
                return s, self.data.daily(s, 3)
            except Exception as e:  # noqa: BLE001
                log.warning("daily %s failed: %s", s, e)
                return s, None

        with ThreadPoolExecutor(max_workers=6) as ex:
            res = list(ex.map(one, symbols))
        ok, failed = {}, []
        for s, df in res:
            df = self._completed(df) if df is not None else None
            if df is None or len(df) < 80:
                failed.append(s)
            else:
                ok[s] = df
        return ok, failed

    # --- 状態の読み書き ---
    def _load_sim(self) -> swing.Sim:
        db = self.db
        cash = db.get("swing_cash")
        pos = {r["symbol"]: dict(shares=r["shares"], avg=r["avg_price"], stop_pct=r["stop_pct"],
                                 stop=r["stop_price"], entry_date=r["entry_date"])
               for r in db.query("SELECT * FROM swing_positions")}
        last = {r["symbol"]: r["last_price"] for r in db.query("SELECT * FROM swing_positions") if r["last_price"]}
        pend = [dict(symbol=r["symbol"], side=r["side"], shares=r["shares"], stop_pct=r["stop_pct"],
                     reason=r["reason"], sid=r["id"], signal_date=r["as_of"], price=r["est_price"])
                for r in db.query("SELECT * FROM swing_signals WHERE status='pending' ORDER BY id")]
        return swing.Sim(self.p, cash=float(cash) if cash is not None else None, positions=pos, pending=pend,
                         last_close=last)

    def _save_day(self, sim: swing.Sim, date: str, fills: list[dict], sigs: list[dict], n_trades_before: int) -> None:
        db = self.db
        for f in fills:
            if f["filled"]:
                db.execute("UPDATE swing_signals SET status='filled',fill_date=?,fill_price=?,fill_shares=? WHERE id=?",
                           (f["fill_date"], f["fill_price"], f["fill_shares"], f["sid"]))
            else:
                db.execute("UPDATE swing_signals SET status='cancelled',fill_date=?,note=? WHERE id=?",
                           (f["fill_date"], f.get("note", ""), f["sid"]))
        for t in sim.trades[n_trades_before:]:
            db.execute("INSERT INTO swing_trades(symbol,name,entry_date,exit_date,shares,entry_price,exit_price,pnl,"
                       "pnl_pct,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (t["symbol"], name_of(t["symbol"]), t["entry_date"], t["exit_date"], t["shares"],
                        t["entry_price"], t["exit_price"], t["pnl"], t["pnl_pct"], t["reason"]))
        for s in sigs:
            s["sid"] = db.execute(
                "INSERT INTO swing_signals(as_of,created_at,symbol,name,side,shares,est_price,est_amount,stop_price,"
                "stop_pct,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (date, self.clock().strftime("%Y-%m-%d %H:%M:%S"), s["symbol"], name_of(s["symbol"]), s["side"],
                 s["shares"], s["price"], s["amount"], s["stop_price"], s["stop_pct"], s["reason"]))
        db.execute("DELETE FROM swing_positions")
        for sym, x in sim.positions.items():
            db.execute("INSERT INTO swing_positions VALUES(?,?,?,?,?,?,?)",
                       (sym, x["shares"], x["avg"], x["stop_pct"], x["stop"], x["entry_date"],
                        sim.last_close.get(sym)))
        _, eq, cash = sim.curve[-1]
        db.execute("INSERT OR REPLACE INTO swing_equity VALUES(?,?,?)", (date, eq, cash))
        db.set("swing_cash", str(sim.cash))
        db.set("swing_last_date", date)

    # --- 判定 ---
    def run(self, notify: bool = True) -> dict:
        """最新の確定日足まで判定する。判定済みなら何もしない。何度呼んでも安全。"""
        if not self._lock.acquire(blocking=False):
            return {"skipped": "実行中"}
        self.running = True
        try:
            raw, failed = self._fetch(self.cfg.signal_universe)
            if not raw:
                raise RuntimeError("株価データを取得できませんでした(ネット接続を確認してください)")
            maps = {s: swing.rowmap(swing.prep(df, self.p)) for s, df in raw.items()}
            dates = sorted({d for m in maps.values() for d in m})
            last, done = dates[-1], self.db.get("swing_last_date")
            if done == last:
                return {"as_of": last, "skipped": "判定済み", "failed": failed}
            sim = self._load_sim()
            todo = [d for d in dates if done is None or d > done] if done else [last]
            final: list[dict] = []
            for d in todo:  # 停止していた日があれば、1日ずつ順番に追いつく
                n_before = len(sim.trades)
                fills, sigs = sim.process_day(d, {s: m[d] for s, m in maps.items() if d in m})
                self._save_day(sim, d, fills, sigs, n_before)
                final = sigs
            sent = ""
            if notify and (final or self.cfg.notify_empty):
                head, body = format_notice(last, final, self.p.capital)
                res = self.notifier.send(head, body)
                sent = ", ".join(f"{r['channel']}:{'OK' if r['ok'] else 'NG ' + r['error']}" for r in res) \
                    or "通知先が未設定"
                if res and all(r["ok"] for r in res):
                    for s in final:
                        self.db.execute("UPDATE swing_signals SET notified=1 WHERE id=?", (s["sid"],))
            note = f"{len(todo)}日分を判定" + (f" / 取得失敗: {','.join(failed)}" if failed else "")
            self.db.execute("INSERT INTO swing_runs(ts,as_of,n_signals,notified,note) VALUES(?,?,?,?,?)",
                            (self.clock().strftime("%Y-%m-%d %H:%M:%S"), last, len(final), sent, note))
            self.last_error = ""
            return {"as_of": last, "signals": len(final), "notified": sent, "failed": failed}
        except Exception as e:  # noqa: BLE001
            log.exception("signal run failed")
            self.last_error = str(e)
            return {"error": str(e)}
        finally:
            self.running = False
            self._lock.release()

    def notify_test(self) -> list[dict]:
        if not self.notifier.channels():
            return []
        return self.notifier.send("【toshi】通知テスト", "これは toshi からの通知テストです。届いていれば設定は正常です。")

    # --- バックテスト ---
    def backtest_key(self) -> str:
        return self.p.key() + "|" + ",".join(self.cfg.signal_universe)

    def backtest(self) -> dict | None:
        if self.bt_running:
            return None
        self.bt_running = True
        try:
            raw, failed = self._fetch(self.cfg.signal_universe)
            if not raw:
                raise RuntimeError("株価データを取得できませんでした")
            bench = None
            try:
                bench = self.data.daily(self.cfg.benchmark, 3)
            except Exception:  # noqa: BLE001
                pass
            res = swing.backtest(raw, self.p, bench=bench)
            res |= {"key": self.backtest_key(), "ts": self.clock().strftime("%Y-%m-%d %H:%M"), "failed": failed}
            self.db.set("swing_backtest", json.dumps(res, ensure_ascii=False))
            return res
        except Exception as e:  # noqa: BLE001
            log.exception("backtest failed")
            self.last_error = f"バックテスト: {e}"
            return None
        finally:
            self.bt_running = False

    def backtest_result(self) -> dict | None:
        v = self.db.get("swing_backtest")
        return json.loads(v) if v else None

    def backtest_stale(self) -> bool:
        r = self.backtest_result()
        if not r or r.get("key") != self.backtest_key():
            return True
        age = pd.Timestamp(self.clock().strftime("%Y-%m-%d")) - pd.Timestamp(r["ts"][:10])
        return age.days >= BACKTEST_MAX_AGE_DAYS

    # --- スケジューラ ---
    def tick(self) -> None:
        """5分ごとに呼ばれる。毎営業日 signal_at 以降に1回判定する(データ未更新・休場は 23:30 まで再試行)。"""
        t = self.clock()
        today, hm = t.strftime("%Y-%m-%d"), t.strftime("%H:%M")
        if t.weekday() < 5 and hm >= self.cfg.signal_at and self.db.get(f"swing_sched_{today}") is None:
            tries = int(self.db.get(f"swing_tries_{today}") or 0)
            self.db.set(f"swing_tries_{today}", str(tries + 1))
            res = self.run()
            if res.get("as_of") == today or hm >= "23:30" or tries >= 8:
                self.db.set(f"swing_sched_{today}", "1")
        if self.backtest_stale() and not self.bt_running:
            threading.Thread(target=self.backtest, daemon=True, name="toshi-backtest").start()

    def loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
            self._stop.wait(300)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.loop, daemon=True, name="toshi-signals")
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
