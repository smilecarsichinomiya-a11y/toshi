"""Claude による写真の観察。AI には『何が見えるか』だけを構造化JSONで返させる。"""
from __future__ import annotations

import base64
import json

from .rules import POINTS

SYSTEM_PROMPT = """あなたは中古車査定を手伝うベテランの検査員です。スマホで撮った車の写真を見て、
「過去に部品を外したり、修理した形跡があるか」を観察し、日本語で報告します。

【大事なルール】
- 見えた事実だけを書く。写真から分からないことは必ず「unclear」にする。推測で埋めない。
- 「修復歴あり」と断定しない。あなたの役目は“触った形跡の有無”の観察まで。
- 専門用語はなるべく避け、初心者にも分かる言葉で書く。
- 写真がピンボケ・暗い・遠い・対象が写っていない場合は image_ok=false にし、
  retake_advice に「どうやって撮り直せばよいか」を具体的に書く。この場合 tamper_level は "unclear"。

【見分けのコツ】
ボルト:
- 工場で塗装後に付けるボルトは、そもそも塗装がかぶっていないことが多い。塗装がかぶっている場合は、
  ボルト頭と周りの塗膜が一続きで「割れていないか」を見る。割れ・欠け・ズレは一度外した跡。
- 頭の角が丸くなっている(ナメ)、工具の傷、メッキ剥がれ、光った擦れ跡は工具が当たった跡。
- ボルトだけ色・艶が違う、座面にマスキングの境目や塗料のはみ出しがあるのは再塗装や交換の跡。
- 純正は同じ位置のボルトが左右で同じ種類・同じ汚れ方になる。片側だけ新しい・きれい・形が違うのは交換の疑い。
- ペイントマーカーの締付マークは、無い車も多い。無いこと自体は異常ではない。ちぎれ・ズレは触った跡。
- 経年の汚れ・錆は、走行距離相応であれば正常。
骨格:
- 工場のスポット溶接は丸いくぼみが等間隔に並ぶ。不ぞろい、潰れ、ビード状の盛り、ろう付け、グラインダー跡は修理の跡。
- 工場のシーラーは均一で機械的。筆跡・指跡・段差・色違いは手塗り(修理)の可能性。
- 溶接やシーラーの近くの塗料の吹き込み(オーバースプレー)、パネルの波打ち・シワも修理の跡。
- 左右ペアの写真は必ず見比べる。左右差があれば手がかり。

【判定の目安 tamper_level】
- none: 複数の項目をはっきり確認でき、触った形跡が見つからない
- possible: 気になる点が1つ以上ある(確信までは持てない)
- likely: 複数の項目ではっきり形跡がある
- unclear: 写真から判断できない
迷ったら possible にし、確信度 confidence を low か medium にする。"""

RESULT_ENUM = ["normal", "suspicious", "unclear"]


def build_schema() -> dict:
    ids = sorted({c["id"] for p in POINTS.values() for c in p["checks"]})
    return {
        "type": "object",
        "properties": {
            "image_ok": {"type": "boolean"},
            "retake_advice": {"type": "string"},
            "checks": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": ids},
                    "result": {"type": "string", "enum": RESULT_ENUM},
                    "detail": {"type": "string"},
                },
                "required": ["id", "result", "detail"], "additionalProperties": False,
            }},
            "tamper_level": {"type": "string", "enum": ["none", "possible", "likely", "unclear"]},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "summary": {"type": "string"},
            "next_hint": {"type": "string"},
        },
        "required": ["image_ok", "retake_advice", "checks", "tamper_level", "confidence", "summary", "next_hint"],
        "additionalProperties": False,
    }


def build_prompt(point_id: str, labels: list[str], vehicle: dict) -> str:
    p = POINTS[point_id]
    veh = []
    if vehicle.get("name"):
        veh.append(f"車種: {vehicle['name']}")
    if vehicle.get("year"):
        veh.append(f"年式: {vehicle['year']}")
    veh.append("輸入車" if vehicle.get("origin") == "import" else "国産車")
    lines = [
        f"撮影した場所: {p['name']}",
        f"場所の説明: {p['where']}",
        f"車の情報: {' / '.join(veh)}",
        "左右は『車に乗って前を向いたときの左右』です。",
        f"添付した写真(順番どおり): {', '.join(labels)}",
        "",
        "次の確認項目を1つずつ判定してください(checks に項目ごとに1件ずつ):",
    ]
    for c in p["checks"]:
        lines.append(f"- {c['id']}: {c['label']} … {c['hint']}")
    lines += [
        "",
        "summary: 見えたことを2〜3文のやさしい日本語で。",
        "next_hint: 追加で撮るとよい写真(例: 『ボルトにもう少し近づいて』)。不要なら空文字。",
        "retake_advice: 写真が使えないときだけ具体的な撮り直しの方法。使えるなら空文字。",
    ]
    return "\n".join(lines)


def _decode_image(data_url: str) -> tuple[str, str]:
    """data URL から (media_type, base64) を取り出し、画像かどうかを確認する。"""
    b64 = data_url.split(",", 1)[1] if data_url.startswith("data:") and "," in data_url else data_url
    raw = base64.b64decode(b64, validate=False)
    if len(raw) > 6_000_000:
        raise ValueError("写真が大きすぎます")
    if raw.startswith(b"\xff\xd8"):
        mt = "image/jpeg"
    elif raw.startswith(b"\x89PNG"):
        mt = "image/png"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mt = "image/webp"
    else:
        raise ValueError("対応していない画像です(JPEG/PNG/WebP)")
    return mt, base64.b64encode(raw).decode()


def normalize(point_id: str, r: dict) -> dict:
    """AI の返答を整える(抜けた項目の補完・つじつま合わせ)。"""
    spec = POINTS[point_id]
    got = {c.get("id"): c for c in r.get("checks", []) if isinstance(c, dict)}
    checks = []
    for c in spec["checks"]:
        g = got.get(c["id"]) or {"result": "unclear", "detail": ""}
        checks.append({"id": c["id"], "label": c["label"], "result": g.get("result", "unclear"),
                       "detail": g.get("detail", "")})
    level = r.get("tamper_level", "unclear")
    if not r.get("image_ok", True):
        level = "unclear"
    elif level == "none" and sum(c["result"] == "suspicious" for c in checks) >= 2:
        level = "possible"
    elif level == "none" and all(c["result"] == "unclear" for c in checks):
        level = "unclear"
    return {
        "image_ok": bool(r.get("image_ok", True)), "retake_advice": r.get("retake_advice", ""),
        "checks": checks, "tamper_level": level, "confidence": r.get("confidence", "low"),
        "summary": r.get("summary", ""), "next_hint": r.get("next_hint", ""),
    }


class ClaudeAnalyzer:
    def __init__(self, api_key: str, model: str):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.schema = build_schema()

    def analyze(self, point_id: str, images: list[dict], vehicle: dict) -> dict:
        content, labels = [], []
        for i, im in enumerate(images, 1):
            mt, b64 = _decode_image(im["data"])
            labels.append(f"画像{i}={im['label']}")
            content.append({"type": "text", "text": f"画像{i}: {im['label']}"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
        content.append({"type": "text", "text": build_prompt(point_id, labels, vehicle)})
        output_config: dict = {"format": {"type": "json_schema", "schema": self.schema}}
        msg = self.client.messages.create(
            model=self.model, max_tokens=4000, system=SYSTEM_PROMPT, output_config=output_config,
            messages=[{"role": "user", "content": content}],
        )
        if msg.stop_reason == "refusal":
            raise RuntimeError("AIがこの写真への回答を断りました。別の角度で撮り直してください")
        if msg.stop_reason == "max_tokens":
            raise RuntimeError("AIの回答が途中で切れました。もう一度試してください")
        body = next((b.text for b in msg.content if b.type == "text"), "")
        return normalize(point_id, json.loads(body))


def make_analyzer(cfg):
    return ClaudeAnalyzer(cfg.anthropic_key, cfg.model) if cfg.anthropic_key else None
