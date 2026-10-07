"""寄り付き前の準備: Claude がニュースを調べ、その日に注目する銘柄(ウォッチリスト)を選ぶ。"""
from __future__ import annotations

import json
import logging
import re

from .db import DB, ts

log = logging.getLogger("toshi.premarket")
CODE = re.compile(r"^\d{3}[0-9A-Z]$")


def get(db: DB, date: str) -> dict | None:
    v = db.get(f"premarket_{date}")
    return json.loads(v) if v else None


def latest(db: DB) -> dict | None:
    r = db.query("SELECT v FROM kv WHERE k LIKE 'premarket_2%' ORDER BY k DESC LIMIT 1")
    return json.loads(r[0]["v"]) if r else None


def watchlist(engine, date: str) -> list[str]:
    """当日の売買対象。朝の選定が無い・失敗した日は、設定の標準銘柄(universe)を使う。"""
    pm = get(engine.db, date)
    return [p["symbol"] for p in pm["picks"]] if pm and pm.get("picks") else list(engine.cfg.universe)


def validate(engine, picks: list[dict]) -> tuple[list[dict], list[str]]:
    """実在・株価・出来高を確認する。1単元が買えない銘柄や出来高の少ない銘柄は外す。"""
    cfg, data = engine.cfg, engine.data
    budget = cfg.max_position_pct * engine.broker.cash()
    ok, dropped, seen = [], [], set()
    for p in picks:
        s = str(p.get("symbol", "")).strip().upper().removesuffix(".T")
        if not CODE.match(s) or s in seen:
            dropped.append(f"{s or '?'}: 銘柄コードが不正または重複")
            continue
        seen.add(s)
        h = data.history(s, 30)
        if h is None or h.empty:
            dropped.append(f"{s}: 株価データを取得できない")
        elif float(h["Close"].iloc[-1]) * cfg.lot_size > budget:
            dropped.append(f"{s}: 1単元が資金の上限を超える")
        elif float(h["Volume"].tail(20).mean()) < cfg.min_avg_volume:
            dropped.append(f"{s}: 出来高が少なく売買しにくい")
        else:
            ok.append({**p, "symbol": s})
        if len(ok) >= cfg.watch_max:
            break
    return ok, dropped


def run(engine, date: str) -> dict | None:
    """朝の銘柄選定を1回実行して保存する。失敗時は None(標準銘柄にフォールバック)。"""
    from . import analytics

    cfg, db = engine.cfg, engine.db
    ctx = {"date": date, "max_picks": cfg.watch_max, "lot_size": cfg.lot_size,
           "max_price_per_share": round(cfg.max_position_pct * engine.broker.cash() / cfg.lot_size),
           "standard_universe": cfg.universe, "recent_performance": analytics.recent_for_prompt(db)}
    try:
        r = engine.strategy.premarket(ctx)
    except NotImplementedError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("premarket failed: %s", e)
        engine.last_error = f"朝の銘柄選定に失敗(標準銘柄で売買します): {e}"
        return None
    picks, dropped = validate(engine, r.get("picks", []))
    if not picks:
        log.warning("premarket: 有効な銘柄がありません %s", dropped)
        return None
    out = {"date": date, "created_at": ts(), "by": engine.strategy.name, "outlook": r.get("outlook", ""),
           "sources": r.get("sources", []), "picks": picks, "dropped": dropped}
    db.set(f"premarket_{date}", json.dumps(out, ensure_ascii=False))
    return out
