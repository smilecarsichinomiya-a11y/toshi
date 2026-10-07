from __future__ import annotations

import json
import logging

log = logging.getLogger("toshi.strategy")

SYSTEM_PROMPT = """あなたは日本株(東証・現物・ロングオンリー)のデイトレードを担当する運用責任者です。
5分足ベースの指標と現在のポートフォリオ・本日の取引・直近の成績を見て、各銘柄を buy / sell / hold で判断し、
submit_decisions ツールで必ず回答してください。この判断は約5分ごとに呼び出されます。

デイトレの鉄則:
- ポジションは当日中に必ず手仕舞う(システムが flatten 時刻に全て強制決済する)。持ち越し前提のエントリーはしない。
- 優先する型: ①寄り付き後レンジ(opening_range)の上抜け+出来高増 ②VWAP上でのトレンド継続・押し目 ③強い銘柄のVWAP付近までの押し目。
  VWAPを下回って戻れない銘柄、出来高の伴わない上昇、RSI(5分)が極端に過熱した直後の追いかけ買いは避ける。
- 日足トレンド(daily_trend)が上向きの銘柄を優先。ギャップ(gap_pct)が大きい日は値動きが荒いので慎重に。
- 損切り(約1%)・利確(約2%)・トレーリングは別途システムが機械的に強制執行する。ATR(atr_5m_pct)が小さすぎて
  利幅が取れない銘柄、手数料以外のコスト(スリッページ約0.05%/片道)に負ける値幅しか期待できない場面は見送る。
- 同じ銘柄の売買を短時間に繰り返さない(システムがクールダウン・回数制限をかける)。迷うときは hold。
  トレードしない日・時間帯があってよい。確度が低いなら confidence を低く(0〜1)。0.55未満の買いは実行されない。
- 保有銘柄は、含み益の確保・トレンド崩れ(VWAP割れ/SMA9<SMA21)・大引けまでの残り時間を踏まえ売却を判断する。
  保有していない銘柄は売れない(空売り禁止)。
- lots は売買単位(1単元=lot_size株)の数。1単元の金額(one_lot_cost)が資金余力に収まらない銘柄は買わない。
- recent_performance の反省(lessons)を踏まえて判断し、同じ失敗を繰り返さない。
- reason は日本語で簡潔に(根拠の数値を含める)。
注意: 参考情報に基づく自動判断であり、利益を保証するものではありません。"""

DECISION_TOOL = {
    "name": "submit_decisions",
    "description": "各銘柄の売買判断を提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "market_view": {"type": "string", "description": "現在の相場観(1〜2文)"},
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

REVIEW_PROMPT = """あなたはデイトレード運用の振り返り担当です。渡された「本日の成績」「全取引」「Claudeの判断ログ」
「直近の日次成績」を分析し、submit_review で回答してください。
- 事実(数値)に基づき、勝因・敗因を具体的に。運と実力を混同しない(サンプルが少ない場合は断定しない)。
- lessons は翌日以降の売買判断プロンプトにそのまま入る。短く具体的で、実行可能な行動指針にする(最大5個)。
  例:「寄り直後15分のブレイク買いは本日3戦0勝。出来高比1.5未満では見送る」
- ベンチマーク(TOPIX連動ETF)との比較も踏まえる。"""

REVIEW_TOOL = {
    "name": "submit_review",
    "description": "日次の振り返りを提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "本日の総括(3〜5文)"},
            "worked": {"type": "array", "items": {"type": "string"}},
            "failed": {"type": "array", "items": {"type": "string"}},
            "lessons": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        },
        "required": ["summary", "worked", "failed", "lessons"],
    },
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

    def _call(self, system: str, tool: dict, payload: dict, text: str) -> dict:
        msg = self.client.messages.create(
            model=self.model, max_tokens=4000, system=system,
            tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": text + "\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"}],
        )
        for block in msg.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError("Claude がツール呼び出しを返しませんでした")

    def decide(self, ctx: dict) -> tuple[str, list[dict]]:
        r = self._call(SYSTEM_PROMPT, DECISION_TOOL, ctx, "現在の状況です。全銘柄について判断してください。")
        return r.get("market_view", ""), _clean(r.get("decisions", []), ctx)

    def review(self, payload: dict) -> dict:
        return self._call(REVIEW_PROMPT, REVIEW_TOOL, payload, "本日のデータです。振り返ってください。")


class RuleStrategy(Strategy):
    """API キー未設定・API 障害時のフォールバック (VWAP・オープニングレンジのモメンタム)。"""

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
            d["confidence"] = float(d.get("confidence") or 0)
            d["reason"] = str(d.get("reason") or "")
            out.append(d)
    return out


def make_strategy(cfg) -> Strategy:
    if cfg.anthropic_key:
        return ClaudeStrategy(cfg.anthropic_key, cfg.model)
    log.warning("ANTHROPIC_API_KEY 未設定: ルールベースのフォールバックを使用します")
    return RuleStrategy()
