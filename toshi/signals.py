"""毎営業日の夜に日足でシグナルを判定し、保存・通知する(ペーパートレード)。"""
from __future__ import annotations

import json
import logging
import threading

import pandas as pd

from . import review, swing
from .db import DB, now
from .notify import Notifier
from .strategy import make_strategy
from .universe import MINI_AS_OF, name_of, pool_realtime

log = logging.getLogger("toshi.signals")
BACKTEST_MAX_AGE_DAYS = 7


def format_notice(as_of: str, sigs: list[dict], capital: float, review_lines: list[str] | None = None) -> tuple[str, str]:
    """通知の本文。銘柄名・コード、買い/売り、推奨株数、概算金額、損切り価格。"""
    sells = [s for s in sigs if s["side"] == "sell"]
    buys = [s for s in sigs if s["side"] == "buy"]
    lines = [f"{as_of} の引け後に判定したシグナルです(ペーパートレード)。",
             "翌営業日の寄り付きで注文する想定です。株数・金額は終値で計算した目安です。", ""]
    tag = lambda s: f" [{swing.STRATEGY_LABEL.get(s.get('strategy') or 'breakout')}]"  # noqa: E731
    if sells:
        lines.append("■ 売り")
        for s in sells:
            lines += [f"・{name_of(s['symbol'])}({s['symbol']}) 売り {s['shares']:,}株 概算 {s['amount']:,.0f}円{tag(s)}",
                      f"  理由: {s['reason']}"]
        lines.append("")
    if buys:
        lines.append("■ 買い")
        for s in buys:
            lines += [f"・{name_of(s['symbol'])}({s['symbol']}) 買い {s['shares']:,}株 概算 {s['amount']:,.0f}円{tag(s)}",
                      f"  損切り価格 {s['stop_price']:,.0f}円(買値の約-{s['stop_pct'] * 100:.1f}%)"]
            if s.get("target"):
                lines.append(f"  利益確定の目安 {s['target']:,.0f}円(押し目前の高値)。戻らなければ約10営業日で売り")
            lines.append(f"  理由: {s['reason']}")
        lines.append("")
    if not sigs:
        lines.append("本日の売買シグナルはありません。")
        lines.append("")
    if review_lines:
        lines += review_lines + [""]
    lines.append("※投資判断と注文は自己責任です。注文したか見送ったかは、ダッシュボードに記録してください。")
    if MINI_AS_OF:
        lines.append(f"※かぶミニ対象銘柄は{MINI_AS_OF}時点の一覧で絞っています。注文前に、対象かどうかをご確認ください。")
    head = f"【toshi】{as_of} 売り{len(sells)}件・買い{len(buys)}件" if sigs else f"【toshi】{as_of} シグナルなし"
    return head, "\n".join(lines)


class SignalService:
    def __init__(self, cfg, db: DB, data, notifier: Notifier | None = None, clock=now, strategy=None):
        self.cfg, self.db, self.data, self.clock = cfg, db, data, clock
        self._strategy = strategy
        self.p = swing.Params.from_cfg(cfg)
        self.load_overrides()
        self.weekly_running = False
        self.notifier = notifier or Notifier(cfg)
        self.last_error = ""
        self.running = False
        self.bt_running = False
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def reviewer(self):
        if self._strategy is None:
            self._strategy = make_strategy(self.cfg)  # API キーがあれば Claude、無ければ(振り返りは数字のまとめだけ)
        return self._strategy

    def load_overrides(self) -> None:
        """承認済みの設定変更を、起動時に反映する。"""
        for name in swing.TUNABLE:
            v = self.db.get(f"swing_override_{name}")
            if v is not None:
                new = swing.clamp_param(name, v, self.p)
                if new is not None:
                    setattr(self.p, name, new)

    # --- データ ---
    def _completed(self, df: pd.DataFrame) -> pd.DataFrame:
        """場中・引け直後は、当日の途中の足を使わない(確定した日足だけで判定する)。"""
        t = self.clock()
        if t.weekday() < 5 and t.strftime("%H:%M") < "15:45":
            df = df[df.index < pd.Timestamp(t.strftime("%Y-%m-%d"))]
        return df

    def pool(self) -> list[str]:
        """データを取得する銘柄。liquid: かぶミニのリアルタイム対象(売買代金の上位は Market が毎日選ぶ) / fixed: 固定リスト。
        保有中・注文中の銘柄は、対象から外れても必ず含める(売り判定のため)。"""
        base = pool_realtime() if self.cfg.universe_mode == "liquid" else []
        base = base or list(self.cfg.signal_universe)
        held = {r["symbol"] for r in self.db.query("SELECT symbol FROM swing_positions")}
        held |= {r["symbol"] for r in self.db.query("SELECT symbol FROM swing_signals WHERE status='pending'")}
        return sorted(set(base) | held)

    def _fetch(self, symbols: list[str]) -> tuple[dict[str, pd.DataFrame], list[str]]:
        got = self.data.daily_many(symbols, 3)
        ok, failed = {}, []
        for s in symbols:
            df = self._completed(got[s]) if s in got and got[s] is not None else None
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
                                 stop=r["stop_price"], entry_date=r["entry_date"],
                                 strategy=r["strategy"] or "breakout", target=r["target"], days=r["days"] or 0)
               for r in db.query("SELECT * FROM swing_positions")}
        last = {r["symbol"]: r["last_price"] for r in db.query("SELECT * FROM swing_positions") if r["last_price"]}
        pend = [dict(symbol=r["symbol"], side=r["side"], shares=r["shares"], stop_pct=r["stop_pct"],
                     reason=r["reason"], sid=r["id"], signal_date=r["as_of"], price=r["est_price"],
                     strategy=r["strategy"] or "breakout", target=r["target"])
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
                       "pnl_pct,reason,strategy) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (t["symbol"], name_of(t["symbol"]), t["entry_date"], t["exit_date"], t["shares"],
                        t["entry_price"], t["exit_price"], t["pnl"], t["pnl_pct"], t["reason"], t["strategy"]))
        for s in sigs:
            s["sid"] = db.execute(
                "INSERT INTO swing_signals(as_of,created_at,symbol,name,side,shares,est_price,est_amount,stop_price,"
                "stop_pct,reason,strategy,target) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (date, self.clock().strftime("%Y-%m-%d %H:%M:%S"), s["symbol"], name_of(s["symbol"]), s["side"],
                 s["shares"], s["price"], s["amount"], s["stop_price"], s["stop_pct"], s["reason"],
                 s.get("strategy"), s.get("target")))
        db.execute("DELETE FROM swing_positions")
        for sym, x in sim.positions.items():
            db.execute("INSERT INTO swing_positions(symbol,shares,avg_price,stop_pct,stop_price,entry_date,last_price,"
                       "strategy,target,days) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (sym, x["shares"], x["avg"], x["stop_pct"], x["stop"], x["entry_date"],
                        sim.last_close.get(sym), x["strategy"], x.get("target"), x.get("days") or 0))
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
            raw, failed = self._fetch(self.pool())
            if not raw:
                raise RuntimeError("株価データを取得できませんでした(ネット接続を確認してください)")
            mk = swing.Market(raw, self.p)
            dates = mk.dates
            last, done = dates[-1], self.db.get("swing_last_date")
            if done == last:
                return {"as_of": last, "skipped": "判定済み", "failed": len(failed)}
            sim = self._load_sim()
            todo = [d for d in dates if done is None or d > done] if done else [last]
            final: list[dict] = []
            for d in todo:  # 停止していた日があれば、1日ずつ順番に追いつく
                n_before = len(sim.trades)
                fills, sigs = sim.process_day(d, mk.rows_for(d, sim.need()))
                self._save_day(sim, d, fills, sigs, n_before)
                final = sigs
            rv = review.daily_review(self.db, last, self.p.capital)
            review.save(self.db, last, "daily", rv, self.clock().strftime("%Y-%m-%d %H:%M:%S"))
            sent = ""
            if notify and (final or self.cfg.notify_empty):
                head, body = format_notice(last, final, self.p.capital, review.format_review(rv))
                res = self.notifier.send(head, body)
                sent = ", ".join(f"{r['channel']}:{'OK' if r['ok'] else 'NG ' + r['error']}" for r in res) \
                    or "通知先が未設定"
                if res and all(r["ok"] for r in res):
                    for s in final:
                        self.db.execute("UPDATE swing_signals SET notified=1 WHERE id=?", (s["sid"],))
            note = f"{len(todo)}日分を判定 / {len(raw)}銘柄のデータを使用" + (f"(取得失敗・履歴不足 {len(failed)}銘柄)" if failed else "")
            self.db.set("swing_universe_info", json.dumps({"loaded": len(raw), "failed": len(failed), "as_of": last}))
            self.db.execute("INSERT INTO swing_runs(ts,as_of,n_signals,notified,note) VALUES(?,?,?,?,?)",
                            (self.clock().strftime("%Y-%m-%d %H:%M:%S"), last, len(final), sent, note))
            self.last_error = ""
            self._maybe_weekly(last, raw, notify)
            return {"as_of": last, "signals": len(final), "notified": sent, "failed": len(failed)}
        except Exception as e:  # noqa: BLE001
            log.exception("signal run failed")
            self.last_error = str(e)
            return {"error": str(e)}
        finally:
            self.running = False
            self._lock.release()

    # --- 週次の振り返りと改善提案 ---
    def _maybe_weekly(self, as_of: str, raw: dict, notify: bool) -> None:
        """金曜の夜(または前回から8日以上たったとき)に、Claude の週次の振り返りを、別スレッドで1回行う。"""
        last = self.db.get("swing_weekly_last")
        due = pd.Timestamp(as_of).weekday() == 4 or last is None or (pd.Timestamp(as_of) - pd.Timestamp(last)).days >= 8
        if self.cfg.weekly_review and due and last != as_of and not self.weekly_running:
            self.db.set("swing_weekly_last", as_of)  # 失敗しても同じ日に何度も呼ばない(API費用の暴走防止)
            threading.Thread(target=self.weekly, args=(as_of, raw, notify), daemon=True, name="toshi-weekly").start()

    def weekly(self, as_of: str | None = None, raw: dict | None = None, notify: bool = True) -> dict:
        if self.weekly_running:
            return {"skipped": "実行中"}
        self.weekly_running = True
        try:
            if raw is None:
                raw, _ = self._fetch(self.pool())
            as_of = as_of or self.db.get("swing_last_date") or self.clock().strftime("%Y-%m-%d")
            rv = review.daily_review(self.db, as_of, self.p.capital)
            payload = review.weekly_payload(self.db, self.p, self.backtest_result())
            try:
                r = self.reviewer().swing_review(payload) | {"by": getattr(self.reviewer(), "name", "claude")}
            except NotImplementedError:
                r = review.rule_weekly(self.db, rv)
            except Exception as e:  # noqa: BLE001
                log.warning("weekly review failed: %s", e)
                r = review.rule_weekly(self.db, rv) | {"error": str(e)}
                self.last_error = f"週次の振り返り: {e}"
            bench = None
            try:
                bench = self.data.daily(self.cfg.benchmark, 3)
            except Exception:  # noqa: BLE001
                pass
            props = review.make_proposals(self, as_of, r, raw, bench) if raw else []
            review.save(self.db, as_of, "weekly", r | {"made": props}, self.clock().strftime("%Y-%m-%d %H:%M:%S"))
            if notify:
                lines = [f"{as_of} 時点の、1週間の振り返りです。", "", r["summary"], ""]
                lines += [f"・{x}" for x in r.get("lessons", [])]
                if props:
                    lines += ["", "■ 改善案(ダッシュボードで、承認か却下を選んでください)"]
                    for pr in props:
                        lines.append(f"・{swing.PARAM_LABEL.get(pr['param'], pr['param'])} → {pr['new']}"
                                     f"({'バックテストの検証をクリア' if pr['passed'] else '検証は未クリア。見送り推奨'})")
                self.notifier.send(f"【toshi】週次の振り返り({as_of})", "\n".join(lines))
            return {"as_of": as_of, "proposals": props}
        except Exception as e:  # noqa: BLE001
            log.exception("weekly failed")
            self.last_error = f"週次の振り返り: {e}"
            return {"error": str(e)}
        finally:
            self.weekly_running = False

    def decide_proposal(self, pid: int, approve: bool) -> dict | None:
        r = self.db.query("SELECT * FROM swing_proposals WHERE id=? AND status='pending'", (pid,))
        if not r:
            return None
        r = r[0]
        now_s = self.clock().strftime("%Y-%m-%d %H:%M:%S")
        if approve:
            new = swing.clamp_param(r["param"], r["new_value"], self.p)
            setattr(self.p, r["param"], new)
            self.db.set(f"swing_override_{r['param']}", str(r["new_value"]))
            self.db.execute("UPDATE swing_proposals SET status='applied',decided_at=?,applied_from=? WHERE id=?",
                            (now_s, self.db.get("swing_last_date") or now_s[:10], pid))
        else:
            self.db.execute("UPDATE swing_proposals SET status='rejected',decided_at=? WHERE id=?", (now_s, pid))
        return self.db.query("SELECT * FROM swing_proposals WHERE id=?", (pid,))[0]

    def notify_test(self) -> list[dict]:
        if not self.notifier.channels():
            return []
        return self.notifier.send("【toshi】通知テスト", "これは toshi からの通知テストです。届いていれば設定は正常です。")

    # --- バックテスト ---
    def backtest_key(self) -> str:
        pool = f"{self.cfg.universe_mode}:{len(pool_realtime())}" if self.cfg.universe_mode == "liquid" \
            else ",".join(self.cfg.signal_universe)
        return self.p.key() + "|" + pool

    def backtest(self) -> dict | None:
        if self.bt_running:
            return None
        self.bt_running = True
        try:
            raw, failed = self._fetch(self.pool())
            if not raw:
                raise RuntimeError("株価データを取得できませんでした")
            bench = None
            try:
                bench = self.data.daily(self.cfg.benchmark, 3)
            except Exception:  # noqa: BLE001
                pass
            res = swing.backtest(raw, self.p, bench=bench)
            res |= {"key": self.backtest_key(), "ts": self.clock().strftime("%Y-%m-%d %H:%M"), "failed": len(failed)}
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
