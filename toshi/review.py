"""日々の振り返り(毎晩・API不要)と、週次の Claude の振り返り・改善提案(バックテストで自動検証)。"""
from __future__ import annotations

import json
import logging

from . import swing
from .db import DB
from .universe import name_of

log = logging.getLogger("toshi.review")


def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    aw = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    al = -sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    return {"trades": n, "win_rate": round(len(wins) / n, 3) if n else None,
            "profit_factor": round(gw / gl, 2) if gl else None, "pnl": round(sum(t["pnl"] for t in trades)),
            "avg_win": round(aw), "avg_loss": round(al),
            "payoff": round(aw / al, 2) if al else None}  # 平均の勝ち額 ÷ 平均の負け額


def daily_review(db: DB, as_of: str, capital: float) -> dict:
    """その日の振り返り。数字と、数字から機械的に言える観察だけを出す(Claude は使わない)。"""
    eqs = db.query("SELECT * FROM swing_equity WHERE date<=? ORDER BY date DESC LIMIT 2", (as_of,))
    today = eqs[0] if eqs else None
    prev = eqs[1] if len(eqs) > 1 else None
    peak = db.query("SELECT MAX(equity) m FROM swing_equity")[0]["m"] or capital
    trades = db.query("SELECT * FROM swing_trades ORDER BY id")
    by = {k: _stats([t for t in trades if (t["strategy"] or "breakout") == k]) for k in swing.STRATEGY_LABEL}
    positions = db.query("SELECT * FROM swing_positions")
    out = {
        "date": as_of,
        "equity": round(today["equity"]) if today else None,
        "day_pnl": round(today["equity"] - prev["equity"]) if today and prev else None,
        "total_pnl": round(today["equity"] - capital) if today else None,
        "drawdown_pct": round((1 - today["equity"] / peak) * 100, 1) if today and peak else 0.0,
        "filled_today": [{"name": name_of(r["symbol"]), "symbol": r["symbol"], "side": r["side"],
                          "price": round(r["fill_price"]), "shares": r["fill_shares"], "strategy": r["strategy"]}
                         for r in db.query("SELECT * FROM swing_signals WHERE fill_date=? AND status='filled'", (as_of,))],
        "closed_today": [{"name": t["name"], "symbol": t["symbol"], "pnl": round(t["pnl"]), "pnl_pct": round(t["pnl_pct"], 1),
                          "reason": t["reason"]} for t in trades if t["exit_date"] == as_of],
        "positions": [{"name": name_of(r["symbol"]), "symbol": r["symbol"], "strategy": r["strategy"],
                       "pnl_pct": round(((r["last_price"] or r["avg_price"]) / r["avg_price"] - 1) * 100, 1)}
                      for r in positions],
        "all": _stats(trades), "by_strategy": by,
    }
    out["notes"] = observations(out, trades)
    return out


def observations(rv: dict, trades: list[dict]) -> list[str]:
    """数字から言える注意点(売買が少ないうちは、断定しない)。"""
    notes, a = [], rv["all"]
    if a["trades"] < 10:
        notes.append(f"決済した取引が{a['trades']}回です。10回に満たないうちは、勝率や損益の傾向を判断できません。")
    else:
        if a["win_rate"] is not None and a["win_rate"] < 0.35 and (a["payoff"] or 0) < 2:
            notes.append(f"勝率{a['win_rate'] * 100:.0f}%で、勝ちの平均が負けの平均の{a['payoff']}倍にとどまっています。"
                         "勝率が低くても、勝ちが負けの2倍以上あれば利益は出ます。今は届いていません。")
        if a["profit_factor"] is not None and a["profit_factor"] < 1:
            notes.append(f"PF(総利益÷総損失)が{a['profit_factor']}で、1を下回っています。今のところ損失超過です。")
    for k, label in swing.STRATEGY_LABEL.items():
        st = rv["by_strategy"][k]
        if st["trades"] >= 8 and st["profit_factor"] is not None and st["profit_factor"] < 1:
            notes.append(f"「{label}」は{st['trades']}回でPF {st['profit_factor']}と、利益が出ていません。条件の見直しを検討します。")
    streak = 0
    for t in reversed(trades):
        if t["pnl"] <= 0:
            streak += 1
        else:
            break
    if streak >= 4:
        notes.append(f"{streak}連敗中です。ルール通りの損切りなら問題ありませんが、相場の地合いが変わっていないか確認してください。")
    if rv["drawdown_pct"] >= 10:
        notes.append(f"資産が最高値から-{rv['drawdown_pct']}%下がっています。")
    return notes


def format_review(rv: dict) -> list[str]:
    """通知メール用の文章。"""
    yen = lambda x: "-" if x is None else f"{x:+,}円"  # noqa: E731
    lines = ["■ 今日の振り返り",
             f"ペーパー口座: 総資産 {rv['equity']:,}円(今日 {yen(rv['day_pnl'])} / 累計 {yen(rv['total_pnl'])})"
             if rv["equity"] is not None else "ペーパー口座: まだ記録がありません"]
    for t in rv["closed_today"]:
        lines.append(f"・決済: {t['name']}({t['symbol']}) {yen(t['pnl'])}({t['pnl_pct']:+.1f}%) {t['reason']}")
    for f in rv["filled_today"]:
        lines.append(f"・約定: {f['name']}({f['symbol']}) {'買い' if f['side'] == 'buy' else '売り'} {f['shares']:,}株 @{f['price']:,}円")
    if rv["positions"]:
        lines.append("保有中: " + "、".join(f"{p['name']} {p['pnl_pct']:+.1f}%" for p in rv["positions"]))
    a = rv["all"]
    if a["trades"]:
        lines.append(f"通算: {a['trades']}回・勝率{a['win_rate'] * 100:.0f}%・PF {a['profit_factor'] if a['profit_factor'] is not None else '-'}")
    lines += [f"※{n}" for n in rv["notes"]]
    return lines


def save(db: DB, date: str, kind: str, body: dict, now: str) -> None:
    db.execute("INSERT OR REPLACE INTO swing_reviews(date,kind,body,created_at) VALUES(?,?,?,?)",
               (date, kind, json.dumps(body, ensure_ascii=False), now))


def latest(db: DB, kind: str) -> dict | None:
    r = db.query("SELECT * FROM swing_reviews WHERE kind=? ORDER BY date DESC LIMIT 1", (kind,))
    return {"date": r[0]["date"], **json.loads(r[0]["body"])} if r else None


# ---------------------------------------------------------------- 週次の振り返りと改善提案
def applied_changes(db: DB) -> list[dict]:
    """適用済みの変更。適用の前後で、ペーパー口座の決済済み取引の成績を比べる。"""
    out = []
    for r in db.query("SELECT * FROM swing_proposals WHERE status='applied' ORDER BY id DESC LIMIT 10"):
        d = r["applied_from"]
        before = _stats(db.query("SELECT * FROM swing_trades WHERE exit_date<=? ORDER BY id DESC LIMIT 30", (d,)))
        after = _stats(db.query("SELECT * FROM swing_trades WHERE exit_date>? ORDER BY id", (d,)))
        out.append({"id": r["id"], "applied_from": d, "param": r["param"], "label": swing.PARAM_LABEL.get(r["param"], r["param"]),
                    "old": r["old_value"], "new": r["new_value"], "rationale": r["rationale"], "before": before, "after": after})
    return out


def weekly_payload(db: DB, p: swing.Params, backtest_result: dict | None) -> dict:
    trades = db.query("SELECT * FROM swing_trades ORDER BY id DESC LIMIT 40")
    ua = {r["user_action"] or "none": r["n"] for r in db.query("SELECT user_action, COUNT(*) n FROM swing_signals GROUP BY user_action")}
    return {
        "settings": {k: getattr(p, k) for k in swing.TUNABLE},
        "tunable": {k: {"min": lo, "max": hi, "label": swing.PARAM_LABEL[k]} for k, (lo, hi) in swing.TUNABLE.items()},
        "paper_trades_latest_first": [
            {"name": t["name"], "strategy": t["strategy"] or "breakout", "entry": t["entry_date"], "exit": t["exit_date"],
             "pnl_pct": round(t["pnl_pct"], 1), "reason": t["reason"]} for t in trades],
        "paper_stats_all": _stats(db.query("SELECT * FROM swing_trades")),
        "paper_stats_by_strategy": {k: _stats(db.query("SELECT * FROM swing_trades WHERE COALESCE(strategy,'breakout')=?", (k,)))
                                    for k in swing.STRATEGY_LABEL},
        "equity_curve": [{"date": r["date"], "equity": round(r["equity"])} for r in db.query("SELECT * FROM swing_equity ORDER BY date DESC LIMIT 30")][::-1],
        "user_orders": ua, "backtest": backtest_result, "applied_changes": applied_changes(db),
        "pending_proposals": [{"param": r["param"], "value": r["new_value"]} for r in db.query("SELECT * FROM swing_proposals WHERE status='pending'")],
    }


def rule_weekly(db: DB, rv: dict) -> dict:
    """API キーが無いときの週次の振り返り(数字のまとめだけ。改善提案は出さない)。"""
    a = rv["all"]
    return {"summary": (f"(数字のまとめ) 決済した取引{a['trades']}回、通算損益{a['pnl']:+,}円。"
                        "Claude による分析と改善提案には、ANTHROPIC_API_KEY の設定が必要です。"),
            "worked": [], "failed": [], "lessons": rv["notes"], "proposals": [], "by": "rule"}


def make_proposals(svc, week: str, review: dict, bars: dict, bench=None) -> list[dict]:
    """Claude の提案を、範囲チェック → 2期間のバックテストで検証 → 保存する。"""
    db, base = svc.db, svc.p
    busy = {r["param"] for r in db.query("SELECT param FROM swing_proposals WHERE status='pending'")}
    out = []
    for pr in review.get("proposals") or []:
        if len(out) >= 2:  # 検証して保存するのは、有効な提案のうち2件まで
            break
        name = pr.get("param")
        new = swing.clamp_param(name, pr.get("value"), base)
        if new is None or name in busy or new == getattr(base, name):
            continue
        busy.add(name)
        cand = swing.with_change(base, name, new)
        try:
            ev = swing.evaluate_change(bars, base, cand, bench)
        except Exception as e:  # noqa: BLE001
            log.warning("evaluate failed: %s", e)
            ev = {"passed": False, "reasons": [f"検証に失敗: {e}"], "base": {}, "cand": {}}
        pid = db.execute(
            "INSERT INTO swing_proposals(created_at,week,param,old_value,new_value,rationale,evaluation,passed) VALUES(?,?,?,?,?,?,?,?)",
            (svc.clock().strftime("%Y-%m-%d %H:%M:%S"), week, name, float(getattr(base, name)), float(new),
             str(pr.get("rationale") or ""), json.dumps(ev, ensure_ascii=False, default=float), int(ev["passed"])))
        out.append({"id": pid, "param": name, "new": new, "passed": ev["passed"]})
    return out
