from fastapi.testclient import TestClient

from toshi.broker import PaperBroker, Position
from toshi.config import Config
from toshi.data import SyntheticProvider
from toshi.db import DB
from toshi.engine import Engine
from toshi.risk import RiskManager, exit_signals
from toshi.strategy import RuleStrategy, Strategy
from toshi.web.app import create_app


def cfg(**kw):
    return Config(universe=["7203", "6758", "9984"], data_source="synthetic", db_path=":memory:", **kw)


def d(sym, action, lots=1, conf=0.9):
    return dict(symbol=sym, action=action, lots=lots, confidence=conf, reason="t")


def test_paper_broker_buy_sell():
    db = DB(":memory:")
    b = PaperBroker(db, 1_000_000)
    assert b.order("7203", "buy", 100, 3000).ok
    assert b.positions()["7203"].qty == 100
    assert not b.order("7203", "sell", 200, 3000).ok
    assert b.order("7203", "sell", 100, 3100).ok
    assert "7203" not in b.positions()
    assert not b.order("9984", "buy", 1000, 9000).ok  # 資金不足


def test_risk_limits_and_no_short():
    c = cfg()
    rm = RiskManager(c)
    pos = {}
    orders, notes = rm.review([d("A", "sell"), d("B", "buy", lots=10)], pos, {"A": 1000, "B": 1000},
                              cash=1_000_000, equity=1_000_000, orders_today=0, day_start_equity=1_000_000, halted=False)
    assert notes["A"].startswith("未保有")
    # 1銘柄上限30% → 300株
    assert [(o.symbol, o.qty) for o in orders] == [("B", 300)]
    # キルスイッチ / 日次損失で買い停止
    o2, n2 = rm.review([d("B", "buy")], pos, {"B": 1000}, 1_000_000, 1_000_000, 0, 1_000_000, halted=True)
    assert not o2 and "キルスイッチ" in n2["B"]
    o3, n3 = rm.review([d("B", "buy")], pos, {"B": 1000}, 1_000_000, 950_000, 0, 1_000_000, halted=False)
    assert not o3 and "日次損失" in n3["B"]
    # 高すぎて1単元買えない
    o4, n4 = rm.review([d("B", "buy")], pos, {"B": 20000}, 1_000_000, 1_000_000, 0, 1_000_000, halted=False)
    assert not o4 and "1単元" in n4["B"]


def test_exit_signals():
    c = cfg()
    pos = {"A": Position("A", 100, 1000), "B": Position("B", 100, 1000), "C": Position("C", 100, 1000)}
    out = exit_signals(c, pos, {"A": 920, "B": 1250, "C": 1100}, {"C": 1300})
    assert {o.symbol: o.source for o in out} == {"A": "risk-stop", "B": "risk-takeprofit", "C": "risk-trailing"}


def make_engine(strategy=None):
    c = cfg()
    db = DB(":memory:")
    return Engine(c, db, PaperBroker(db, c.initial_cash), SyntheticProvider(), strategy or RuleStrategy())


class Greedy(Strategy):
    name = "greedy"

    def decide(self, ctx):
        return "test", [d(s, "buy", lots=5) for s in ctx["features"]]


def test_cycle_respects_risk():
    e = make_engine(Greedy())
    r = e.run_cycle(force=True)
    assert "error" not in r, r
    eq, cash = e.equity({s: p.avg_price for s, p in e.broker.positions().items()})
    assert cash >= 0.1 * eq * 0.99  # 現金リザーブ維持
    for p in e.broker.positions().values():
        assert p.qty * p.avg_price <= 0.31 * eq


def test_strategy_failure_falls_back():
    class Boom(Strategy):
        name = "boom"

        def decide(self, ctx):
            raise RuntimeError("api down")

    r = make_engine(Boom()).run_cycle(force=True)
    assert r["strategy"] == "rule-fallback" and "api down" in r["market_view"]


def test_api_and_halt():
    e = make_engine(Greedy())
    e.run_cycle(force=True)
    cl = TestClient(create_app(e))
    assert cl.get("/").status_code == 200
    s = cl.get("/api/summary").json()
    assert s["mode"] == "paper" and s["equity"] > 0
    assert cl.get("/api/orders").json() and cl.get("/api/decisions").json()
    assert cl.post("/api/halt?on=true").json()["halted"] is True
    n = len(cl.get("/api/orders").json())
    e.run_cycle(force=True)
    buys = [o for o in cl.get("/api/orders").json()[: len(cl.get("/api/orders").json()) - n] if o["side"] == "buy"]
    assert not buys


def test_token_auth():
    e = make_engine()
    e.cfg.dash_token = "secret"
    cl = TestClient(create_app(e))
    assert cl.get("/api/summary").status_code == 401
    assert cl.get("/api/summary", headers={"Authorization": "Bearer secret"}).status_code == 200
