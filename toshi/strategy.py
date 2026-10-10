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
- today_focus は今朝のニュース調査で選んだ注目銘柄(材料と狙い方つき)。features はこの銘柄が中心。
  材料の方向と、5分足の動き(VWAP・出来高)が一致したときだけ買う。材料があっても動きが伴わなければ見送る。
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
- proposals は設定値の変更案。毎日出す必要はなく、直近の複数日・複数取引に共通する根拠があるときだけ(0〜2個)。
  1日の結果に合わせた変更は過剰適合になる。tunable に挙げた項目と範囲内でのみ提案する。
  applied_changes に過去に適用した変更と、その前後の成績がある。効果が出ていない変更は元に戻す案も検討する。
  同時に複数の項目を変えると効果が分からなくなるので、未判定(pending)の案がある項目は再提案しない。
出力は指定の JSON スキーマに従うこと。"""

_STRS = {"type": "array", "items": {"type": "string"}}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "本日の総括(3〜5文)"},
        "worked": _STRS,
        "failed": _STRS,
        "lessons": _STRS,
        "proposals": {"type": "array", "items": {
            "type": "object",
            "properties": {"param": {"type": "string"}, "value": {"type": "number"}, "rationale": {"type": "string"}},
            "required": ["param", "value", "rationale"], "additionalProperties": False}},
    },
    "required": ["summary", "worked", "failed", "lessons", "proposals"],
    "additionalProperties": False,
}


EVAL_PROMPT = """あなたはデイトレード運用(仮想売買)の検証責任者です。所定の検証期間が終わりました。
渡された「累計成績」「合否チェック」「日次成績の一覧」「各日の教訓」から、この戦略を評価してください。
- verdict は次のどれか: "実運用を検討してよい" / "改善して検証を続ける" / "見直しが必要"。
- 取引数が少ない場合や、相場環境(TOPIXの動き)に助けられただけの場合は、慎重に判断する。
- 投資初心者にも分かる言葉で書く。専門用語には短い説明を添える。
- improvements は、設定値や売買ルールの具体的な変更案にする(最大5個)。
出力は指定の JSON スキーマに従うこと。"""

EVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["実運用を検討してよい", "改善して検証を続ける", "見直しが必要"]},
        "summary": {"type": "string"},
        "strengths": _STRS,
        "weaknesses": _STRS,
        "improvements": _STRS,
    },
    "required": ["verdict", "summary", "strengths", "weaknesses", "improvements"],
    "additionalProperties": False,
}


class Strategy:
    name = "base"

    def decide(self, ctx: dict) -> tuple[str, list[dict]]:
        raise NotImplementedError

    def review(self, payload: dict) -> dict:
        raise NotImplementedError

    def evaluate(self, payload: dict) -> dict:
        raise NotImplementedError

    def premarket(self, ctx: dict) -> dict:
        raise NotImplementedError  # 未対応の戦略は標準銘柄(universe)で売買する

    def swing_review(self, payload: dict) -> dict:
        raise NotImplementedError  # API キーが無いときは、Claude の振り返りは行わない


PREMARKET_RESEARCH = """あなたは日本株デイトレードの朝の準備担当です。本日(date)の東証の寄り付き前です。
Web検索で次を調べ、調査メモ(日本語、箇条書き)にまとめてください。
- 前日の米国市場(ダウ・ナスダック・半導体)、為替(ドル円)、日経先物、原油・金利など、今日の地合いを左右する材料
- 今日の日本株で注目される材料: 決算発表、業績修正、上方修正、大型提携、政策・規制、格上げ、急騰急落の背景など
- 材料が出ていて出来高が増えそうな銘柄を、銘柄名と4桁の証券コードつきで。1株が max_price_per_share 円以下の銘柄を優先する
事実と推測を区別し、出典(媒体名・URL)を残すこと。確認できなかったことは「不明」と書く。"""

PREMARKET_PICK = """調査メモをもとに、本日デイトレードで注目する銘柄を最大 max_picks 個選んでください。
- 選ぶ基準: 材料があり出来高が伸びそうで、日中に+2〜3%の値幅が見込める流動性の高い銘柄。ロング(買い)で狙う前提。
- 材料のない大型株でも、standard_universe の中に地合い的に狙える銘柄があれば含めてよい。
- 1株が max_price_per_share 円を超える銘柄、流動性の低い銘柄、急騰後で過熱した銘柄は避ける。
- 地合いが悪く見送りが妥当なら、少数(0〜3銘柄)に絞ってよい。無理に数を揃えない。
- 銘柄コードは調査メモにあるものだけ。思い出しや推測で書かない。
- reason には根拠となるニュースと、どう狙うか(例: 寄り後の押し目買い)を書く。
出力は指定の JSON スキーマに従うこと。"""

PREMARKET_SCHEMA = {
    "type": "object",
    "properties": {
        "outlook": {"type": "string", "description": "今日の地合いの見立て(2〜4文)"},
        "picks": {"type": "array", "items": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "4桁の証券コード"}, "name": {"type": "string"},
                           "news": {"type": "string", "description": "材料(1文)"},
                           "reason": {"type": "string", "description": "狙い方"}},
            "required": ["symbol", "name", "news", "reason"], "additionalProperties": False}},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outlook", "picks", "sources"],
    "additionalProperties": False,
}


SWING_REVIEW_PROMPT = """あなたは日本株の日足スイングトレード(ペーパートレード・かぶミニで1株単位)の運用責任者です。
目的は、**長期的な利益を最大化する**ことです。渡されたデータ(ペーパー口座の取引・成績、売買の種類別の成績、バックテスト、
現在の設定、過去に適用した変更とその前後の成績)を分析し、1週間の振り返りと、設定の改善案を出してください。

守ること:
- 事実(数値)に基づく。運と実力を混同しない。**取引が10件未満の種類や期間は、断定しない**(「サンプル不足」と書く)。
- 負けた取引は、損切り幅・保有期間・地合い(ベンチマークの動き)・売買の種類の観点で、共通点を探す。
- 勝ちを伸ばす・負けを小さくする・無駄な売買を減らす、のうち、利益への寄与が大きいものから提案する。
- proposals は 0〜2個。**根拠が弱いとき・サンプルが少ないときは、0個(変更なし)でよい。** 無理に出さない。
  提案は tunable に挙げた設定と範囲内のみ。pending_proposals にある設定は再提案しない。
  提案はシステムが2つの期間のバックテストで自動検証し、合格したものだけがユーザーに推奨される。過去の1期間にだけ合う変更や、
  売買回数を減らして見かけの成績を良くするだけの変更は不合格になる。
- applied_changes に、適用済みの変更とその前後の成績がある。効果が出ていない変更は、元に戻す案も検討する。
- 初心者にも分かる言葉で書く。専門用語には短い説明を添える。
出力は指定の JSON スキーマに従うこと。"""

SWING_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "今週の総括(3〜6文)"},
        "worked": {"type": "array", "items": {"type": "string"}},
        "failed": {"type": "array", "items": {"type": "string"}},
        "lessons": {"type": "array", "items": {"type": "string"}},
        "proposals": {"type": "array", "items": {
            "type": "object",
            "properties": {"param": {"type": "string"}, "value": {"type": "number"}, "rationale": {"type": "string"}},
            "required": ["param", "value", "rationale"], "additionalProperties": False}},
    },
    "required": ["summary", "worked", "failed", "lessons", "proposals"],
    "additionalProperties": False,
}


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

    def swing_review(self, payload: dict) -> dict:
        r = self._call(SWING_REVIEW_PROMPT, SWING_REVIEW_SCHEMA, payload, "今週のデータです。振り返って、改善案を出してください。", "high")
        r["lessons"] = r.get("lessons", [])[:5]
        r["proposals"] = r.get("proposals", [])[:2]
        return r

    def premarket(self, ctx: dict) -> dict:
        """1) Web検索で調査 2) 調査メモから銘柄を構造化出力で選ぶ。"""
        first = {"role": "user", "content": PREMARKET_RESEARCH + "\n```json\n"
                 + json.dumps({k: ctx[k] for k in ("date", "max_price_per_share")}, ensure_ascii=False) + "\n```"}
        msgs = [first]
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10,
                  "user_location": {"type": "approximate", "country": "JP", "timezone": "Asia/Tokyo"}}]
        notes = ""
        for _ in range(4):  # 長い検索は pause_turn で中断されるので続きを依頼する
            msg = self.client.messages.create(model=self.model, max_tokens=16000, tools=tools, messages=msgs)
            notes += "".join(b.text for b in msg.content if b.type == "text")
            if msg.stop_reason != "pause_turn":
                break
            msgs = [first, {"role": "assistant", "content": msg.content}]
        if not notes.strip():
            raise RuntimeError("ニュースの調査結果が空でした")
        return self._call(PREMARKET_PICK, PREMARKET_SCHEMA, ctx | {"research_notes": notes},
                          "調査メモと条件です。本日の注目銘柄を選んでください。", "high")

    def evaluate(self, payload: dict) -> dict:
        return self._call(EVAL_PROMPT, EVAL_SCHEMA, payload, "検証期間の全データです。評価してください。", "high")


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
                "worked": [], "failed": [], "lessons": [], "proposals": []}

    def evaluate(self, payload: dict) -> dict:
        checks = payload["checks"]
        n = sum(c["ok"] for c in checks)
        verdict = ("実運用を検討してよい" if n == len(checks) else
                   "改善して検証を続ける" if n >= len(checks) - 2 else "見直しが必要")
        return {"verdict": verdict, "summary": f"(ルールベース判定) 合格 {n}/{len(checks)} 項目",
                "strengths": [c["name"] for c in checks if c["ok"]],
                "weaknesses": [c["name"] for c in checks if not c["ok"]], "improvements": []}


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
