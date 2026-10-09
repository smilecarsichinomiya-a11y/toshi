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


def test_improvement_loop(tmp_path):
    """振り返りの提案は範囲に丸められて保存され、承認で初めて設定に反映、前後比較が付く。"""
    class Prop(Greedy):
        def review(self, payload):
            assert "tunable" in payload and payload["applied_changes"] == []
            return {"summary": "s", "worked": [], "failed": [], "lessons": [],
                    "proposals": [{"param": "stop_loss_pct", "value": 0.0001, "rationale": "浅すぎる損切りで往復"},
                                  {"param": "initial_cash", "value": 1e9, "rationale": "x"}]}

    e = make_engine(Prop(), "10:00", tmp_path)
    d0 = "2026-01-05"
    e.db.execute("INSERT INTO equity(ts,equity,cash) VALUES(?,?,?)", (d0 + " 10:00:00", 500_000, 500_000))
    e.db.execute("INSERT INTO equity(ts,equity,cash) VALUES(?,?,?)", (d0 + " 15:20:00", 505_000, 505_000))
    analytics.run_daily(e, d0)
    imps = analytics.improvements(e.db)
    assert len(imps) == 1 and imps[0]["param"] == "stop_loss_pct"  # 未知の項目は捨てる
    assert imps[0]["new_value"] == 0.008 and e.cfg.stop_loss_pct == 0.015  # 範囲に丸め・未承認では不変
    analytics.decide_improvement(e, imps[0]["id"], True)
    assert e.cfg.stop_loss_pct == 0.008
    other = cfg()
    analytics.load_overrides(other, e.db)  # 再起動後も維持
    assert other.stop_loss_pct == 0.008
    assert analytics.improvements(e.db)[0]["before"]["days"] == 1
    assert analytics.decide_improvement(e, imps[0]["id"], False) is None  # 二重決定は不可


def test_premarket_picks_drive_watchlist(tmp_path):
    """朝の選定銘柄だけが売買対象になる。不正・買えない銘柄は除外、失敗した日は標準銘柄に戻る。"""
    from toshi import premarket

    class Pick(Greedy):
        def premarket(self, ctx):
            assert ctx["max_price_per_share"] > 0
            return {"outlook": "地合い良好", "sources": ["x"], "picks": [
                {"symbol": "6758", "name": "A", "news": "n", "reason": "r"},
                {"symbol": "BAD!", "name": "B", "news": "n", "reason": "r"},
                {"symbol": "6758", "name": "dup", "news": "n", "reason": "r"}]}

    seen = {}

    class Spy(Pick):
        def decide(self, ctx):
            seen.update(ctx)
            return "t", []

    e = make_engine(Spy(), "10:00", tmp_path, min_avg_volume=0, max_position_pct=1.0)
    out = premarket.run(e, "2026-01-05")
    assert [p["symbol"] for p in out["picks"]] == ["6758"] and len(out["dropped"]) == 2
    assert premarket.watchlist(e, "2026-01-05") == ["6758"]
    assert premarket.watchlist(e, "2026-01-06") == e.cfg.universe  # 選定が無い日は標準銘柄
    e.run_cycle(force=True)
    today = e.clock().strftime("%Y-%m-%d")
    e.db.set(f"premarket_{today}", json.dumps(out))
    e.run_cycle(force=True)
    assert set(seen["features"]) <= {"6758"} and seen["today_focus"]["outlook"] == "地合い良好"
    assert premarket.latest(e.db)["picks"][0]["symbol"] == "6758"
    # 未対応の戦略(NotImplementedError)は何もしない
    assert premarket.run(make_engine(Greedy(), "10:00"), "2026-01-05") is None


# ---------------- 日足スイング(毎晩のシグナル通知) ----------------
import numpy as np
import pandas as pd

from toshi import swing
from toshi.signals import SignalService, format_notice


def bars(n=140, breakout=True, drop_after=None, base=1000.0):
    """上昇トレンドの日足。最終日に高値更新+出来高急増を作る(breakout=True)。"""
    idx = pd.bdate_range(end="2026-01-09", periods=n)
    close = base * (1 + 0.003) ** np.arange(n)  # 緩やかな上昇
    close = close + np.sin(np.arange(n) / 3) * 3  # 小さな揺れ(高値を毎日は更新させない)
    open_ = np.r_[close[0], close[:-1]]
    high, low = close * 1.004, close * 0.996
    vol = np.full(n, 1_000_000.0)
    if breakout:
        close[-1] = close[:-1].max() * 1.03
        high[-1] = close[-1] * 1.003
        vol[-1] = 2_500_000
    df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx)
    if drop_after is not None:
        df.iloc[drop_after:, df.columns.get_loc("Close")] *= 0.85
    return df


P = swing.Params(capital=500_000)


def test_buy_signal_sizing_and_stop():
    d = swing.prep(bars(), P)
    rows = {"7203": swing.rowmap(d)[d.index[-1].strftime("%Y-%m-%d")]}
    assert swing.buy_ok(rows["7203"], P)
    sim = swing.Sim(P)
    fills, sigs = sim.process_day(d.index[-1].strftime("%Y-%m-%d"), rows)
    assert len(sigs) == 1 and sigs[0]["side"] == "buy"
    s = sigs[0]
    assert s["amount"] <= 0.25 * 500_000 and s["shares"] == int(125_000 // s["price"])  # 25%上限・株数は逆算
    assert 0.03 <= s["stop_pct"] <= 0.08 and s["stop_price"] < s["price"]


def test_no_signal_without_breakout():
    d = swing.prep(bars(breakout=False), P)
    assert not swing.buy_ok(swing.rowmap(d)[d.index[-1].strftime("%Y-%m-%d")], P)


def test_max_positions_and_cash_cap():
    """候補が6銘柄あっても、同時保有は4銘柄まで。1銘柄は資金の25%以内。"""
    maps = {f"{1000 + i}": swing.rowmap(swing.prep(bars(base=1000 + 50 * i), P)) for i in range(6)}
    last = sorted(next(iter(maps.values())))[-1]
    sim = swing.Sim(P)
    _, sigs = sim.process_day(last, {s: m[last] for s, m in maps.items()})
    assert len([x for x in sigs if x["side"] == "buy"]) == 4
    assert all(x["amount"] <= 125_000 for x in sigs)
    # 翌日の始値で約定 → 保有4銘柄、さらに買いシグナルは出ない
    nxt = {s: {**m[last], "Open": m[last]["Close"]} for s, m in maps.items()}
    fills, sigs2 = sim.process_day("2026-01-12", nxt)
    assert sum(f["filled"] for f in fills) == 4 and len(sim.positions) == 4
    assert all(x["side"] != "buy" for x in sigs2)


def test_stop_loss_and_exit_signal_and_pnl():
    d = swing.prep(bars(), P)
    m = swing.rowmap(d)
    last = d.index[-1].strftime("%Y-%m-%d")
    sim = swing.Sim(P)
    sim.process_day(last, {"A": m[last]})
    row = {**m[last], "Open": 1000.0, "ll": 500.0}
    fills, sigs = sim.process_day("2026-01-12", {"A": {**row, "Close": 1000.0}})  # 約定(+コスト)
    pos = sim.positions["A"]
    assert abs(pos["avg"] - 1000 * 1.002) < 1e-6
    crash = {**row, "Close": pos["stop"] * 0.99, "Open": pos["stop"] * 0.99}
    _, sigs = sim.process_day("2026-01-13", {"A": crash})
    assert [x["side"] for x in sigs] == ["sell"] and "損切り" in sigs[0]["reason"]
    fills, _ = sim.process_day("2026-01-14", {"A": {**crash, "Open": crash["Close"]}})
    assert not sim.positions and sim.trades[0]["pnl"] < 0


def test_backtest_runs_and_reports():
    res = swing.backtest({"7203": bars(n=600, breakout=False), "6758": bars(n=600, breakout=False, base=2000)}, P)
    for w in ("1年", "2年"):
        m = res["windows"][w]
        assert {"trades", "win_rate", "max_dd_pct", "total_return_pct"} <= set(m)
    rules = swing.describe_rules(P)
    assert "25%" in " ".join(rules["size"]) and "4銘柄" in " ".join(rules["size"])


class FakeNotifier:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def channels(self):
        return ["メール"]

    def send(self, subject, text):
        self.sent.append((subject, text))
        return [{"channel": "メール", "ok": self.ok, "error": "" if self.ok else "boom"}]


class FixedData:
    def __init__(self, frames):
        self.frames = frames

    def daily(self, symbol, years=3):
        return self.frames.get(symbol)


def svc_for(frames, clock="18:00", notifier=None, **kw):
    c = cfg(None, **kw)
    c.signal_universe = list(frames)
    db = DB(":memory:")
    return SignalService(c, db, FixedData(frames), notifier or FakeNotifier(), clock=at(clock)), db


def test_service_nightly_run_notifies_and_is_idempotent():
    n = FakeNotifier()
    svc, db = svc_for({"7203": bars()}, notifier=n)
    r = svc.run()
    assert r["signals"] == 1 and len(n.sent) == 1
    head, body = n.sent[0]
    assert "トヨタ自動車(7203)" in body and "買い" in body and "損切り価格" in body and "概算" in body
    row = db.query("SELECT * FROM swing_signals")[0]
    assert row["status"] == "pending" and row["notified"] == 1 and row["user_action"] is None
    assert svc.run()["skipped"] == "判定済み" and len(n.sent) == 1  # 二重通知しない


def test_service_failed_notification_is_recorded_not_fatal():
    svc, db = svc_for({"7203": bars()}, notifier=FakeNotifier(ok=False))
    r = svc.run()
    assert "NG" in r["notified"]
    assert db.query("SELECT notified FROM swing_signals")[0]["notified"] == 0


def test_service_catches_up_missed_days_and_fills_paper():
    frames = {"7203": bars()}
    svc, db = svc_for(frames)
    svc.run()  # 1/9 の買いシグナル
    nxt = frames["7203"].copy()
    nxt.loc[pd.Timestamp("2026-01-12")] = [1500.0, 1520.0, 1490.0, 1510.0, 1_000_000.0]
    nxt.loc[pd.Timestamp("2026-01-13")] = [1510.0, 1530.0, 1500.0, 1520.0, 1_000_000.0]
    svc.data = FixedData({"7203": nxt})
    svc.clock = at("18:00")
    svc.run(notify=False)
    sig = db.query("SELECT * FROM swing_signals ORDER BY id")[0]
    assert sig["status"] == "filled" and sig["fill_date"] == "2026-01-12" and abs(sig["fill_price"] - 1503.0) < 1e-6
    assert db.query("SELECT COUNT(*) n FROM swing_positions")[0]["n"] == 1
    assert db.query("SELECT COUNT(*) n FROM swing_equity")[0]["n"] >= 2


def test_service_ignores_unfinished_today_bar():
    frames = {"7203": bars()}
    svc, db = svc_for(frames, clock="10:00")  # 場中: 1/9 の足が当日分なら使わない
    svc.clock = at("10:00")
    assert svc.run()["as_of"] <= "2026-01-09"


def test_signal_api_and_user_action(tmp_path):
    e = make_engine(Greedy(), "18:00", tmp_path)
    svc, _ = svc_for({"7203": bars()})
    svc.db = e.db
    svc.run()
    cl = TestClient(create_app(e, svc))
    sigs = cl.get("/api/swing/signals").json()
    assert sigs[0]["symbol"] == "7203" and sigs[0]["user_action"] is None
    r = cl.post(f"/api/swing/signals/{sigs[0]['id']}/action", json={"action": "ordered", "price": 1234.5, "shares": 5})
    assert r.status_code == 200
    s2 = cl.get("/api/swing/signals").json()[0]
    assert s2["user_action"] == "ordered" and s2["user_price"] == 1234.5 and s2["user_shares"] == 5
    assert cl.post(f"/api/swing/signals/{s2['id']}/action", json={"action": "bogus"}).status_code == 422
    summ = cl.get("/api/swing/summary").json()
    assert summ["mode"] == "paper" and summ["adherence"]["ordered"] == 1
    assert "buy" in cl.get("/api/swing/rules").json()["rules"]


def test_format_notice_contents():
    head, body = format_notice("2026-01-09", [
        dict(symbol="7203", side="buy", shares=7, amount=125_000.0, stop_price=1700, stop_pct=0.05, reason="r1"),
        dict(symbol="6758", side="sell", shares=3, amount=30_000.0, stop_price=None, stop_pct=None, reason="r2")], 500_000)
    assert "売り1件・買い1件" in head and "ソニーグループ(6758)" in body and "損切り価格 1,700円" in body


def test_kabumini_universe_filter(tmp_path, monkeypatch):
    """かぶミニ対象外の銘柄(楽天G・野村HD)は自動で除外。対象銘柄はリアルタイム取引も可。"""
    from toshi import universe
    from toshi.config import load_config

    assert universe.mini_info("7203") == {"open": True, "realtime": True}
    assert universe.mini_info("4755") is None and universe.mini_info("8604") is None
    assert universe.filter_mini(["7203", "4755"]) == (["7203"], ["4755"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOSHI_SIGNAL_UNIVERSE", raising=False)
    monkeypatch.delenv("TOSHI_MINI_ONLY", raising=False)
    c = load_config()
    assert "7203" in c.signal_universe and "4755" not in c.signal_universe and "8604" not in c.signal_universe
    assert set(c.signal_excluded) == {"4755", "8604"} and len(c.signal_universe) == 56
    monkeypatch.setenv("TOSHI_MINI_ONLY", "no")
    assert len(load_config().signal_universe) == 58
