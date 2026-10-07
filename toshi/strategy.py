from __future__ import annotations

import json
import logging

log = logging.getLogger("toshi.strategy")

SYSTEM_PROMPT = """あなたは日本株(東証・現物のみ・ロングオンリー)のスイングトレードを担当する運用責任者です。
日足ベースの指標と現在のポートフォリオを見て、各銘柄を buy / sell / hold のいずれかで判断し、
submit_decisions ツールで必ず回答してください。

方針:
- 保有期間は数日〜数週間。トレンドに沿った押し目買い・ブレイクアウトを優先し、逆張りのナンピンはしない。
- 買いは「SMA25>SMA75(または上向き)」「過熱していない(RSI14が目安として75未満)」「リスクリワードが取れる」など複数根拠が揃うときのみ。
- 売りは「トレンド崩れ(SMA25割れ等)」「RSI過熱後の失速」「利益確定」など根拠があるときのみ。保有していない銘柄は売れない(空売り禁止)。
- 迷うときは hold。無理に毎回売買する必要はない。確度が低い提案は confidence を低くする(0〜1)。
- lots は売買単位(1単元=lot_size株)の数。1単元の金額が資金余力に収まらない銘柄は買わない。
- 損切り・利確・トレーリングは別途システムが機械的に強制執行する。それを前提にしつつ、含み損の放置はしない。
- reason は日本語で簡潔に(根拠となった指標の数値を含める)。
注意: これは参考情報に基づく自動判断であり、利益を保証するものではありません。"""

TOOL = {
    "name": "submit_decisions",
    "description": "各銘柄の売買判断を提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "market_view": {"type": "string", "description": "全体の相場観(1〜2文)"},
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
                        "lots": {"type": "integer", "minimum": 0},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                    "required": ["symbol", "action", "lots", "confidence", "reason"],
                },
            },
        },
        "required": ["market_view", "decisions"],
    },
}


class Strategy:
    name = "base"

    def decide(self, ctx: dict) -> tuple[str, list[dict]]:
        """ctx: {features:{sym:{...}}, positions:{sym:{qty,avg_price,pnl_pct}}, cash, equity, cfg:{...}}
        -> (market_view, decisions)"""
        raise NotImplementedError


class ClaudeStrategy(Strategy):
    name = "claude"

    def __init__(self, api_key: str, model: str):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def decide(self, ctx: dict) -> tuple[str, list[dict]]:
        user = ("以下が現在の状況です。全銘柄について判断してください。\n```json\n"
                + json.dumps(ctx, ensure_ascii=False, indent=1) + "\n```")
        msg = self.client.messages.create(
            model=self.model, max_tokens=4000, system=SYSTEM_PROMPT,
            tools=[TOOL], tool_choice={"type": "tool", "name": "submit_decisions"},
            messages=[{"role": "user", "content": user}],
        )
        for block in msg.content:
            if block.type == "tool_use":
                return block.input.get("market_view", ""), _clean(block.input.get("decisions", []), ctx)
        raise RuntimeError("Claude がツール呼び出しを返しませんでした")


class RuleStrategy(Strategy):
    """API キー未設定・API 障害時のフォールバック (単純なトレンドフォロー)。"""

    name = "rule-fallback"

    def decide(self, ctx: dict) -> tuple[str, list[dict]]:
        out = []
        for sym, f in ctx["features"].items():
            held = sym in ctx["positions"]
            up = f["sma75"] is not None and f["sma25"] > f["sma75"]
            if not held and up and f["price"] > f["sma25"] and f["sma5"] > f["sma25"] and f["rsi14"] < 70:
                out.append(dict(symbol=sym, action="buy", lots=1, confidence=0.6,
                                reason=f"上昇トレンド(SMA25>75, 価格>SMA25) RSI={f['rsi14']}"))
            elif held and (f["price"] < f["sma25"] or f["rsi14"] > 80):
                out.append(dict(symbol=sym, action="sell", lots=0, confidence=0.5,
                                reason=f"SMA25割れまたは過熱 RSI={f['rsi14']}"))
            else:
                out.append(dict(symbol=sym, action="hold", lots=0, confidence=0.5, reason="シグナルなし"))
        return "ルールベース(フォールバック)で判断", out


def _clean(decisions: list[dict], ctx: dict) -> list[dict]:
    valid = set(ctx["features"])
    out = []
    for d in decisions:
        if d.get("symbol") in valid and d.get("action") in ("buy", "sell", "hold"):
            d["lots"] = max(0, int(d.get("lots") or 0))
            d["confidence"] = float(d.get("confidence") or 0)
            d["reason"] = str(d.get("reason") or "")
            out.append(d)
    return out


def make_strategy(cfg) -> Strategy:
    if cfg.anthropic_key:
        return ClaudeStrategy(cfg.anthropic_key, cfg.model)
    log.warning("ANTHROPIC_API_KEY 未設定: ルールベースのフォールバックを使用します")
    return RuleStrategy()
