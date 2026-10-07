import json
from datetime import datetime

from fastapi.testclient import TestClient

from toshi import analytics
from toshi.broker import PaperBroker, Position
from toshi.config import Config
from toshi.data import SyntheticProvider
from toshi.db import DB, JST
from toshi.engine import Engine
from toshi.indicators import intraday_features
from toshi.risk import RiskManager, exit_signals
from toshi.strategy import RuleStrategy, Strategy
from toshi.web.app import create_app

DAY = SyntheticProvider().intraday("7203").index[-1].date()  # 擬似データの最終営業日


def at(hm: str):
    h, m = map(int, hm.split(":"))
    return lambda: datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=JST)


def cfg(tmp_path=None, **kw):
    return Config(universe=["7203", "6758", "9984", "8306"], data_source="synthetic", db_path=":memory:",
                  report_dir=str(tmp_path) if tmp_path else "", **kw)


def d(sym, action, lots=1, conf=0.9):
    return dict(symbol=sym, action=action, lots=lots, confidence=conf, reason="t")


class Greedy(Strategy):
    name = "greedy"

    def decide(self, ctx):
        acts = [d(s, "sell", 0) for s in ctx["positions"]] or [d(s, "buy", 1) for s in ctx["features"]]
        return "test", acts

    def review(self, payload):
        return {"summary": "ok", "worked": [], "failed": [], "lessons": ["L1"]}


def make_engine(strategy=None, clock="10:00", tmp_path=None, **kw):
    c = cfg(tmp_path, **kw)
    db = DB(":memory:")
    return Engine(c, db, PaperBroker(db, c.initial_cash), SyntheticProvider(), strategy or RuleStrategy(), clock=at(clock))


def test_paper_broker_buy_sell():
    db = DB(":memory:")
    b = PaperBroker(db, 1_000_000)
    assert b.order("7203", "buy", 100, 3000).ok
    assert not b.order("7203", "sell", 200, 3000).ok
    assert b.order("7203", "sell", 100, 3100).ok
    assert not b.order("9984", "buy", 1000, 9000).ok  # 資金不足


def test_intraday_features():
    p = SyntheticProvider()
    f = intraday_features(p.intraday("7203"), p.history("7203"))
    for k in ("vwap", "opening_range_high", "rsi14_5m", "gap_pct", "volume_ratio", "daily_trend"):
        assert k in f


def test_risk_rules():
    rm = RiskManager(cfg())
    args = dict(cash=1_000_000, equity=1_000_000, orders_today=0, day_start_equity=1_000_000, halted=False)
    o, n = rm.review([d("A", "sell"), d("B", "buy", lots=10)], {}, {"A": 1000, "B": 1000}, **args)
    assert n["A"].startswith("未保有") and [(x.symbol, x.qty) for x in o] == [("B", 500)]
    o, n = rm.review([d("B", "buy")], {}, {"B": 1000}, **args, can_enter=False)
    assert not o and "時間外" in n["B"]
    o, n = rm.review([d("B", "buy")], {}, {"B": 1000}, **args, blocked={"B": "クールダウン"})
    assert not o and n["B"] == "クールダウン"
    o, n = rm.review([d("B", "buy")], {}, {"B": 1000}, **(args | {"halted": True}))
    assert not o and "キルスイッチ" in n["B"]
    o, n = rm.review([d("B", "buy")], {}, {"B": 1000}, **(args | {"equity": 970_000}))
    assert not o and "日次損失" in n["B"]


def test_exit_signals():
    pos = {s: Position(s, 100, 1000) for s in "ABC"}
    out = exit_signals(cfg(), pos, {"A": 980, "B": 1031, "C": 1014}, {"C": 1030})
    assert {o.symbol: o.source for o in out} == {"A": "risk-stop", "B": "risk-takeprofit", "C": "risk-trailing"}


def test_entry_then_flatten_and_cooldown():
    e = make_engine(Greedy(), "10:00")
    r = e.run_cycle()
    assert any("買" in x for x in r["executed"]), r
    assert e.managed_positions()
    # 大引け前は Claude に聞かず全決済
    e.clock = at("15:16")
    r = e.run_cycle()
    assert r["strategy"] == "system" and not e.managed_positions()
    # 決済直後はクールダウンで再エントリー不可
    e.clock = at("15:17")
    blocked = e._blocked(e.db.query("SELECT * FROM orders WHERE status='filled'"), e.clock())
    assert blocked


def test_no_entry_outside_window_and_lunch():
    for hm in ("09:01", "11:28", "14:50"):
        e = make_engine(Greedy(), hm)
        r = e.run_cycle()
        assert not r["executed"], (hm, r)
    assert make_engine(Greedy(), "12:00").run_cycle() == {"skipped": "市場時間外"}


def test_manual_holdings_untouched():
    """口座に手動で持っている株はシステム管理外(強制決済しない)。"""
    e = make_engine(Greedy(), "15:20")
    e.broker.order("7203", "buy", 100, 1000)  # システム外の保有
    e.run_cycle()
    assert e.broker.positions()["7203"].qty == 100


def test_strategy_failure_falls_back():
    class Boom(Strategy):
        name = "boom"

        def decide(self, ctx):
            raise RuntimeError("api down")

    r = make_engine(Boom()).run_cycle()
    assert r["strategy"] == "rule-fallback" and "api down" in r["market_view"]


def test_daily_stats_accumulate(tmp_path):
    e = make_engine(Greedy(), "10:00", tmp_path)
    e.run_cycle()
    e.clock = at("10:30")
    e.run_cycle()  # 全て売り
    e.clock = at("15:45")
    e.maybe_daily()
    date = str(DAY)
    row = e.db.query("SELECT * FROM daily_stats WHERE date=?", (date,))[0]
    assert row["trades"] >= 1 and row["wins"] + row["losses"] == row["trades"]
    assert json.loads(row["review"])["lessons"] == ["L1"]
    assert (tmp_path / f"{date}.json").exists() and (tmp_path / "daily_stats.csv").exists()
    # 教訓は翌日の判断プロンプトへ
    assert analytics.recent_for_prompt(e.db)[0]["lessons"] == ["L1"]
    c = analytics.cumulative(e.db)
    assert c["days"] == 1 and c["trades"] == row["trades"]
    # 二重実行しない
    e.maybe_daily()
    assert len(e.db.query("SELECT * FROM daily_stats")) == 1


def test_backfill_missed_days():
    e = make_engine(Greedy(), "10:00")
    e.db.execute("INSERT INTO equity(ts,equity,cash) VALUES(?,?,?)", ("2026-01-05 10:00:00", 3_000_000, 3_000_000))
    e.db.execute("INSERT INTO equity(ts,equity,cash) VALUES(?,?,?)", ("2026-01-05 15:20:00", 3_010_000, 3_010_000))
    assert analytics.backfill(e, "2026-01-06", include_today=False) == ["2026-01-05"]
    assert e.db.query("SELECT pnl FROM daily_stats")[0]["pnl"] == 10_000


def test_stale_data_blocks_entry():
    """無料データが遅れすぎている銘柄には新規エントリーしない。"""

    class Stale(SyntheticProvider):
        def intraday(self, symbol):
            df = super().intraday(symbol)
            return df[df.index <= df.index[-1].replace(hour=9, minute=35)]

    c = cfg()
    db = DB(":memory:")
    e = Engine(c, db, PaperBroker(db, c.initial_cash), Stale(), Greedy(), clock=at("10:30"))
    r = e.run_cycle()
    assert not r["executed"] and e.data_delay_min == 50
    e.clock = at("10:15")  # 遅れ35分なら許容
    assert e.run_cycle()["executed"]


def test_claude_strategy_uses_structured_output():
    """Sonnet/Opus 5.5 は forced tool_choice が 400 になるため、構造化出力で呼ぶこと。"""
    from types import SimpleNamespace

    from toshi.strategy import ClaudeStrategy

    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            body = json.dumps({"market_view": "強い", "decisions": [
                {"symbol": "7203", "action": "buy", "lots": 1, "confidence": 1.7, "reason": "r"},
                {"symbol": "XXXX", "action": "buy", "lots": 1, "confidence": 0.9, "reason": "unknown"}]})
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=body)])

    st = ClaudeStrategy("k", "claude-sonnet-5-5")
    st.client = SimpleNamespace(messages=FakeMessages())
    view, ds = st.decide({"features": {"7203": {}}, "positions": {}})
    assert view == "強い" and [d["symbol"] for d in ds] == ["7203"] and ds[0]["confidence"] == 1.0
    kw = calls[0]
    assert "tool_choice" not in kw and "tools" not in kw
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["output_config"]["effort"] == "medium"


def test_api(tmp_path):
    e = make_engine(Greedy(), "10:00", tmp_path)
    e.run_cycle()
    cl = TestClient(create_app(e))
    assert cl.get("/").status_code == 200
    s = cl.get("/api/summary").json()
    assert s["mode"] == "paper" and s["equity"] > 0 and s["schedule"]["flatten"] == "15:15"
    assert cl.post("/api/daily/run").json()["ok"]
    assert cl.get("/api/daily").json()[0]["review"]["summary"] == "ok"
    assert cl.get("/api/cumulative").json()["days"] == 1
    assert cl.post("/api/halt?on=true").json()["halted"] is True


def test_token_auth():
    e = make_engine()
    e.cfg.dash_token = "secret"
    cl = TestClient(create_app(e))
    assert cl.get("/api/summary").status_code == 401
    assert cl.get("/api/summary", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_evaluation_after_eval_days(tmp_path):
    e = make_engine(Greedy(), "10:00", tmp_path)
    e.cfg.eval_days = 3
    for i, pnl in enumerate([5000, -2000, 8000]):
        d = f"2026-01-0{i + 5}"
        e.db.execute("INSERT INTO equity(ts,equity,cash) VALUES(?,?,?)", (d + " 10:00:00", 500_000, 500_000))
        e.db.execute("INSERT INTO equity(ts,equity,cash) VALUES(?,?,?)", (d + " 15:20:00", 500_000 + pnl, 500_000))
    analytics.backfill(e, "2026-01-08", include_today=False)
    assert analytics.maybe_evaluate(e) is not None  # Greedy.evaluate 未実装 → ルールベース判定
    ev = json.loads(e.db.get("evaluation"))
    assert ev["days"] == 3 and ev["verdict"] and ev["by"] == "rule-fallback"
    assert analytics.maybe_evaluate(e) is None  # 1回だけ
    assert (tmp_path / "evaluation_3days.json").exists()
    names = [c["name"] for c in analytics.checks(e.db, 500_000)]
    assert "累計損益がプラス" in names


def test_unaffordable_symbols_excluded():
    """1単元が1銘柄の上限額を超える銘柄は判断対象にしない(資金50万円想定)。"""
    seen = {}

    class Spy(Greedy):
        def decide(self, ctx):
            seen.update(ctx["features"])
            return "x", []

    e = make_engine(Spy(), "10:00")
    e.run_cycle()
    lim = e.cfg.max_position_pct * e.cfg.initial_cash
    assert all(f["one_lot_cost"] <= lim for f in seen.values())
