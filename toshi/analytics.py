"""日次成績の集計・蓄積と、Claude による振り返り。"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
from collections import defaultdict

from .db import DB, ts

log = logging.getLogger("toshi.analytics")


def round_trips(db: DB, date: str) -> list[dict]:
    """当日の約定を 買い→売り の往復取引に組み立てる。"""
    rows = db.query("SELECT * FROM orders WHERE status='filled' AND ts LIKE ? ORDER BY id", (date + "%",))
    open_buys: dict[str, list[dict]] = defaultdict(list)
    trips = []
    for o in rows:
        if o["side"] == "buy":
            open_buys[o["symbol"]].append(o)
            continue
        entry = open_buys[o["symbol"]][-1] if open_buys[o["symbol"]] else None
        trips.append({
            "symbol": o["symbol"], "qty": o["qty"], "exit_ts": o["ts"], "exit_price": o["price"],
            "exit_source": o["source"], "pnl": o["pnl"] or 0.0,
            "entry_ts": entry["ts"] if entry else None, "entry_price": entry["price"] if entry else None,
            "entry_reason": entry["reason"] if entry else "",
            "exit_reason": o["reason"],
        })
    return trips


def _group(trips: list[dict], key) -> dict:
    g: dict[str, dict] = {}
    for t in trips:
        k = key(t)
        e = g.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0})
        e["n"] += 1
        e["wins"] += t["pnl"] > 0
        e["pnl"] = round(e["pnl"] + t["pnl"], 1)
    return g


def compute_day(engine, date: str) -> dict | None:
    db = engine.db
    eq = db.query("SELECT ts,equity FROM equity WHERE ts LIKE ? ORDER BY ts", (date + "%",))
    trips = round_trips(db, date)
    n_orders = db.query("SELECT COUNT(*) n FROM orders WHERE ts LIKE ?", (date + "%",))[0]["n"]
    if not eq and not n_orders:
        return None  # 休場日・未稼働日
    realized = sum(t["pnl"] for t in trips)
    start = float(db.get(f"day_start_{date}") or (eq[0]["equity"] if eq else 0))
    end = eq[-1]["equity"] if eq else start + realized
    peak, max_dd = start, 0.0
    for r in eq:
        peak = max(peak, r["equity"])
        if peak > 0:
            max_dd = max(max_dd, (peak - r["equity"]) / peak * 100)
    wins = [t["pnl"] for t in trips if t["pnl"] > 0]
    losses = [t["pnl"] for t in trips if t["pnl"] <= 0]
    gw, gl = sum(wins), -sum(losses)
    dec = db.query("SELECT action, COUNT(*) n FROM decisions WHERE ts LIKE ? GROUP BY action", (date + "%",))
    bench = None
    try:
        bench = engine.data.day_return(engine.cfg.benchmark, date)
    except Exception as e:  # noqa: BLE001
        log.warning("benchmark failed: %s", e)
    detail = {
        "trades": trips,
        "by_exit": _group(trips, lambda t: t["exit_source"]),
        "by_symbol": _group(trips, lambda t: t["symbol"]),
        "by_entry_hour": _group(trips, lambda t: (t["entry_ts"] or t["exit_ts"])[11:13] + "時台"),
        "decisions": {r["action"]: r["n"] for r in dec},
        "orders": n_orders,
        "avg_holding_min": _avg_hold(trips),
    }
    return {
        "date": date, "start_equity": start, "end_equity": end, "pnl": end - start,
        "pnl_pct": round((end / start - 1) * 100, 3) if start else 0.0, "realized": realized,
        "trades": len(trips), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(trips), 3) if trips else None,
        "profit_factor": round(gw / gl, 2) if gl > 0 else (None if not wins else 99.0),
        "gross_win": gw, "gross_loss": gl,
        "avg_win": round(gw / len(wins), 1) if wins else None,
        "avg_loss": round(-gl / len(losses), 1) if losses else None,
        "max_dd_pct": round(max_dd, 3), "bench_pct": bench, "detail": detail,
    }


def _avg_hold(trips: list[dict]) -> float | None:
    from datetime import datetime

    mins = [(datetime.fromisoformat(t["exit_ts"]) - datetime.fromisoformat(t["entry_ts"])).total_seconds() / 60
            for t in trips if t["entry_ts"]]
    return round(sum(mins) / len(mins), 1) if mins else None


COLS = ["date", "start_equity", "end_equity", "pnl", "pnl_pct", "realized", "trades", "wins", "losses",
        "win_rate", "profit_factor", "gross_win", "gross_loss", "avg_win", "avg_loss", "max_dd_pct", "bench_pct"]


def run_daily(engine, date: str, with_review: bool = True) -> dict | None:
    """日次成績を集計して DB・レポートファイルに保存し、振り返りを生成する。何度実行しても上書きで安全。"""
    stats = compute_day(engine, date)
    engine.db.set(f"daily_done_{date}", "1")
    if stats is None:
        return None
    review = None
    if with_review:
        review = make_review(engine, stats)
    db = engine.db
    vals = [stats[c] for c in COLS] + [json.dumps(stats["detail"], ensure_ascii=False),
                                       json.dumps(review, ensure_ascii=False) if review else None, ts()]
    db.execute(f"INSERT OR REPLACE INTO daily_stats({','.join(COLS)},detail,review,created_at) "
               f"VALUES({','.join('?' * (len(COLS) + 3))})", tuple(vals))
    stats["review"] = review
    _write_reports(engine, stats)
    log.info("daily stats %s: pnl=%.0f trades=%d", date, stats["pnl"], stats["trades"])
    return stats


def make_review(engine, stats: dict) -> dict:
    db = engine.db
    payload = {
        "today": {k: stats[k] for k in COLS} | {"detail": stats["detail"]},
        "decisions": db.query("SELECT ts,symbol,action,lots,confidence,reason,outcome FROM decisions "
                              "WHERE ts LIKE ? AND action!='hold' ORDER BY id LIMIT 200", (stats["date"] + "%",)),
        "recent_days": [{k: r[k] for k in COLS} for r in
                        db.query("SELECT * FROM daily_stats WHERE date<? ORDER BY date DESC LIMIT 10", (stats["date"],))],
    }
    try:
        return engine.strategy.review(payload) | {"by": engine.strategy.name}
    except Exception as e:  # noqa: BLE001
        log.warning("review failed: %s", e)
        from .strategy import RuleStrategy

        return RuleStrategy().review(payload) | {"by": "rule-fallback", "error": str(e)}


def _write_reports(engine, stats: dict) -> None:
    d = engine.cfg.report_dir
    if not d:
        return
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{stats['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1, default=str)
    rows = engine.db.query(f"SELECT {','.join(COLS)} FROM daily_stats ORDER BY date")
    with open(os.path.join(d, "daily_stats.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)


def backfill(engine, today: str, include_today: bool) -> list[str]:
    """未集計の稼働日をすべて集計する(停止していた日の取りこぼし防止)。"""
    db = engine.db
    dates = {r["d"] for r in db.query("SELECT DISTINCT substr(ts,1,10) d FROM equity "
                                      "UNION SELECT DISTINCT substr(ts,1,10) d FROM orders")}
    done = {r["date"] for r in db.query("SELECT date FROM daily_stats")}
    todo = sorted(d for d in dates - done if d < today or (include_today and d == today))
    for d in todo:
        run_daily(engine, d)
    return todo


def cumulative(db: DB) -> dict:
    rows = db.query("SELECT * FROM daily_stats ORDER BY date")
    if not rows:
        return {"days": 0}
    pcts = [r["pnl_pct"] or 0 for r in rows]
    mean = sum(pcts) / len(pcts)
    sd = math.sqrt(sum((p - mean) ** 2 for p in pcts) / (len(pcts) - 1)) if len(pcts) > 1 else 0
    peak, mdd, eq = 0.0, 0.0, 0.0
    curve = []
    for r in rows:
        eq += r["pnl"] or 0
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        curve.append({"date": r["date"], "cum_pnl": eq})
    trades = sum(r["trades"] or 0 for r in rows)
    wins = sum(r["wins"] or 0 for r in rows)
    gw, gl = sum(r["gross_win"] or 0 for r in rows), sum(r["gross_loss"] or 0 for r in rows)
    bench = [r["bench_pct"] for r in rows if r["bench_pct"] is not None]
    return {
        "days": len(rows), "win_days": sum(1 for r in rows if (r["pnl"] or 0) > 0),
        "total_pnl": eq, "avg_daily_pnl": eq / len(rows), "avg_daily_pct": round(mean, 3),
        "trades": trades, "win_rate": round(wins / trades, 3) if trades else None,
        "profit_factor": round(gw / gl, 2) if gl else None,
        "max_drawdown": mdd, "sharpe": round(mean / sd * math.sqrt(245), 2) if sd else None,
        "bench_avg_pct": round(sum(bench) / len(bench), 3) if bench else None,
        "curve": curve,
    }


def recent_for_prompt(db: DB, n: int = 5) -> list[dict]:
    """売買判断プロンプトに渡す直近の成績と教訓。"""
    out = []
    for r in db.query("SELECT * FROM daily_stats ORDER BY date DESC LIMIT ?", (n,)):
        rv = json.loads(r["review"]) if r["review"] else {}
        out.append({"date": r["date"], "pnl_pct": r["pnl_pct"], "trades": r["trades"], "win_rate": r["win_rate"],
                    "profit_factor": r["profit_factor"], "bench_pct": r["bench_pct"],
                    "lessons": rv.get("lessons", [])})
    return out
