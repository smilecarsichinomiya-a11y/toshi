from __future__ import annotations

import hmac
import logging
import os
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import rules
from .ai import normalize
from .config import Config

log = logging.getLogger("shufuku")
HERE = os.path.dirname(__file__)


class Image(BaseModel):
    label: str = Field(max_length=40)
    data: str


class AnalyzeReq(BaseModel):
    point_id: str
    images: list[Image] = Field(min_length=1, max_length=4)
    vehicle: dict = {}


def create_app(cfg: Config, analyzer) -> FastAPI:
    """analyzer: analyze(point_id, images, vehicle) -> dict を持つもの(None なら AI なし)。"""
    app = FastAPI(title="修復歴チェッカー")

    fails: dict[str, list[float]] = {}  # 合言葉の総当たり対策(IPごとの失敗時刻)

    def client_id(request: Request) -> str:
        xff = request.headers.get("x-forwarded-for", "")
        return xff.split(",")[-1].strip() if xff else (request.client.host if request.client else "?")

    def auth(request: Request):
        if not cfg.access_code:
            return
        who, now = client_id(request), time.time()
        recent = [t for t in fails.get(who, []) if now - t < 600]
        if len(recent) >= 10:
            raise HTTPException(429, "間違いが続いたため、10分ほど待ってからやり直してください")
        if not hmac.compare_digest(request.headers.get("x-access-code", ""), cfg.access_code):
            recent.append(now)
            fails[who] = recent
            raise HTTPException(401, "アクセスコードが違います")

    @app.get("/")
    def index():
        return FileResponse(os.path.join(HERE, "web", "index.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/api/meta", dependencies=[Depends(auth)])
    def meta():
        return {
            "ai": analyzer is not None, "model": cfg.model,
            "panels": rules.PANELS, "panel_order": rules.PANEL_ORDER, "panel_groups": rules.PANEL_GROUPS,
            "points": rules.POINTS, "point_order": rules.POINT_ORDER, "basic_points": rules.BASIC_POINTS,
            "visual_checks": [{"id": c, "q": q, "points": p} for c, q, p in rules.VISUAL_CHECKS],
            "level_label": rules.LEVEL_LABEL, "point_level_label": rules.LEVEL_LABEL_POINT,
            "defaults": {"domestic": cfg.film_normal_domestic, "import": cfg.film_normal_import},
        }

    @app.post("/api/evaluate", dependencies=[Depends(auth)])
    def evaluate(case: dict):
        return rules.evaluate(case, cfg)

    @app.post("/api/analyze", dependencies=[Depends(auth)])
    def analyze(req: AnalyzeReq):
        if analyzer is None:
            raise HTTPException(503, "AIが設定されていません。自分の目で判定してください(.env に APIキーを設定するとAIが使えます)")
        if req.point_id not in rules.POINTS:
            raise HTTPException(400, "不明な撮影ポイントです")
        try:
            result = analyzer.analyze(req.point_id, [i.model_dump() for i in req.images], req.vehicle)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # APIキー誤り・通信エラー・上限など
            log.exception("AI呼び出しに失敗")
            raise HTTPException(502, f"AIの判定に失敗しました: {_friendly(e)}")
        return normalize(req.point_id, result)

    return app


def _friendly(e: Exception) -> str:
    name = type(e).__name__
    text = str(e)
    if "authentication" in name.lower() or "401" in text:
        return "APIキーが正しくありません(.env を確認してください)"
    if "RateLimit" in name or "429" in text:
        return "混み合っています。少し待ってからやり直してください"
    if "Connection" in name or "Timeout" in name:
        return "通信できませんでした。電波を確認してください"
    return text[:120]
