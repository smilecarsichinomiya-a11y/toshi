from __future__ import annotations

import json
import logging

log = logging.getLogger("toshi.strategy")

SYSTEM_PROMPT = """あなたは日本株(東証・現物・ロングオンリー)の仮想売買で、デイトレードを担当する運用責任者です。
5分足ベースの指標、現在のポートフォリオ、本日の取引、直近の成績を見て、各銘柄を buy / sell / hold で判断してください。
この判断は約15分ごとに呼び出されます。

前提:
- 株価は無料データのため、約20分程度遅れています(data_delay_min が実際の遅れ)。数分単位の値動きを狙うスキャルピングは不可能です。
  数十分〜数時間かけて伸びる「その日のトレンド」に乗ることを狙ってください。
- ポジションは当日中に必ず手仕舞います(システムが flatten 時刻に全て強制決済)。

売買スタイル (スキャルピングではなくデイトレード):
- 目標は1回の取引で +2〜3%。保有時間は30分〜数時間を想定。1日の取引は0〜3回程度で十分で、無理に回数を増やさない。
- 優先する型: ①寄り付き後のレンジ(opening_range)を出来高を伴って上抜け、VWAP の上で推移する銘柄
  ②日足トレンド(daily_trend)が上向きで、当日も VWAP の上を維持して押し目をつけた銘柄。
- 避ける: VWAP を下回って戻れない銘柄、出来高の伴わない上昇、RSI(5分)が極端に高い後の追いかけ買い、
  値幅(atr_5m_pct)が小さすぎて +2% が見込めない銘柄、ギャップが大きく荒れている銘柄。
- 損切り(約1.5%)・利確(約3%)・トレーリングはシステムが機械的に強制執行する。
- 保有銘柄は、トレンド継続中なら hold で利を伸ばす。VWAP割れや SMA9<SMA21 などトレンド崩れが明確なら sell。
  小さな上下で売買を繰り返さない。保有していない銘柄は売れない(空売り禁止)。
- 迷うときは hold。確度(confidence, 0〜1)は正直に。0.55未満の買いは実行されない。
- lots は売買単位(1単元=lot_size株)の数。one_lot_cost が資金余力に収まらない銘柄は買わない。
- recent_performance の教訓(lessons)を踏まえ、同じ失敗を繰り返さない。
- reason は日本語で簡潔に(根拠の数値を含める)。
- decisions には features にある全銘柄を1件ずつ含める。

出力は指定の JSON スキーマに従うこと。"""

_DECISION = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
        "lots": {"type": "integer", "description": "売買単位数(0以上)"},
        "confidence": {"type": "number", "description": "0〜1"},
        "reason": {"type": "string"},
    },
    "required": ["symbol", "action", "lots", "confidence", "reason"],
    "additionalProperties": False,
}
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "market_view": {"type": "string", "description": "現在の相場観(1〜2文)"},
        "decisions": {"type": "array", "items": _DECISION},
    },
    "required": ["market_view", "decisions"],
    "additionalProperties": False,
}

REVIEW_PROMPT = """あなたはデイトレード運用(仮想売買)の振り返り担当です。渡された「本日の成績」「全取引」「判断ログ」
「直近の日次成績」を分析してください。
- 事実(数値)に基づき、勝因・敗因を具体的に書く。運と実力を混同しない(取引数が少ない場合は断定しない)。
- lessons は翌日以降の売買判断にそのまま渡される。短く具体的で、実行できる行動指針にする(最大5個)。
  例:「出来高比1.5未満のレンジ上抜けは本日2戦0勝。出来高が伴うまで待つ」
- ベンチマーク(TOPIX連動ETF)との比較も踏まえる。取引しなかった日は、見送りが妥当だったかを評価する。
出力は指定の JSON スキーマに従うこと。"""

_STRS = {"type": "array", "items": {"type": "string"}}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "本日の総括(3〜5文)"},
        "worked": _STRS,
        "failed": _STRS,
        "lessons": _STRS,
    },
    "required": ["summary", "worked", "failed", "lessons"],
    "additionalProperties": False,
}


class Strategy:
    name = "base"

    def decide(self, ctx: dict) -> tuple[str, list[dict]]:
        raise NotImplementedError

    def review(self, payload: dict) -> dict:
        raise NotImplementedError


class ClaudeStrategy(Strategy):
    name = "claude"

    def __init__(self, api_key: str, model: str):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def _call(self, system: str, schema: dict, payload: dict, text: str, effort: str) -> dict:
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if not self.model.startswith("claude-haiku"):  # Haiku 4.5 は effort 非対応
            output_config["effort"] = effort
        msg = self.client.messages.create(
            model=self.model, max_tokens=16000, system=system, output_config=output_config,
            messages=[{"role": "user", "content": text + "\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"}],
        )
        if msg.stop_reason == "refusal":
            raise RuntimeError("Claude が回答を拒否しました")
        if msg.stop_reason == "max_tokens":
            raise RuntimeError("Claude の出力が上限で途切れました")
        body = next((b.text for b in msg.content if b.type == "text"), "")
        return json.loads(body)

    def decide(self, ctx: dict) -> tuple[str, list[dict]]:
        r = self._call(SYSTEM_PROMPT, DECISION_SCHEMA, ctx, "現在の状況です。全銘柄について判断してください。", "medium")
        return r.get("market_view", ""), _clean(r.get("decisions", []), ctx)

    def review(self, payload: dict) -> dict:
        r = self._call(REVIEW_PROMPT, REVIEW_SCHEMA, payload, "本日のデータです。振り返ってください。", "high")
        r["lessons"] = r.get("lessons", [])[:5]
        return r


class RuleStrategy(Strategy):
    """API キー未設定・API 障害時のフォールバック (VWAP・オープニングレンジのトレンドフォロー)。"""

    name = "rule-fallback"

    def decide(self, ctx: dict) -> tuple[str, list[dict]]:
        out = []
        for sym, f in ctx["features"].items():
            held = sym in ctx["positions"]
            vr = f.get("volume_ratio") or 0
            if (not held and f["price"] > f["vwap"] and f["price"] > f["opening_range_high"]
                    and f["sma9_5m"] > f["sma21_5m"] and f["rsi14_5m"] < 75 and vr >= 1.2
                    and f.get("daily_trend") == "up"):
                out.append(dict(symbol=sym, action="buy", lots=1, confidence=0.6,
                                reason=f"OR上抜け+VWAP上 RSI={f['rsi14_5m']} 出来高比{vr}"))
            elif held and (f["price"] < f["vwap"] or f["sma9_5m"] < f["sma21_5m"] or f["rsi14_5m"] > 85):
                out.append(dict(symbol=sym, action="sell", lots=0, confidence=0.6,
                                reason=f"VWAP割れ/短期MAデッドクロス/過熱 RSI={f['rsi14_5m']}"))
            else:
                out.append(dict(symbol=sym, action="hold", lots=0, confidence=0.5, reason="シグナルなし"))
        return "ルールベース(フォールバック)で判断", out

    def review(self, payload: dict) -> dict:
        t = payload["today"]
        return {"summary": f"(ルールベース集計) 損益{t['pnl']:,.0f}円 取引{t['trades']}回 勝率{(t['win_rate'] or 0) * 100:.0f}%",
                "worked": [], "failed": [], "lessons": []}


def _clean(decisions: list[dict], ctx: dict) -> list[dict]:
    valid = set(ctx["features"])
    out = []
    for d in decisions:
        if d.get("symbol") in valid and d.get("action") in ("buy", "sell", "hold"):
            d["lots"] = max(0, int(d.get("lots") or 0))
            d["confidence"] = min(1.0, max(0.0, float(d.get("confidence") or 0)))
            d["reason"] = str(d.get("reason") or "")
            out.append(d)
    return out


def make_strategy(cfg) -> Strategy:
    if cfg.anthropic_key:
        return ClaudeStrategy(cfg.anthropic_key, cfg.model)
    log.warning("ANTHROPIC_API_KEY 未設定: ルールベースのフォールバックを使用します")
    return RuleStrategy()
