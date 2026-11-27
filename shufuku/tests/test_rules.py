import base64

from fastapi.testclient import TestClient

from shufuku import rules
from shufuku.ai import build_prompt, build_schema, normalize
from shufuku.app import create_app
from shufuku.config import Config

CFG = Config()


def full_film(v=110, **over):
    d = {p: v for p in rules.PANEL_ORDER}
    d.update(over)
    return d


def done_points(level="none"):
    return {p: {"user": {"none": "ok", "possible": "sus", "likely": "bad"}[level]} for p in rules.BASIC_POINTS}


def test_film_levels_and_limits():
    case = {"vehicle": {"origin": "domestic", "color": "solid"}, "film": {"bonnet": 120, "fender_r": 200, "fender_l": 300, "roof_center": 500, "door_fr": 10}}
    r = rules.evaluate_film(case, CFG)
    p = r["panels"]
    assert p["bonnet"]["level"] == 0
    assert p["fender_r"]["level"] == 1
    assert p["fender_l"]["level"] == 2
    assert p["roof_center"]["level"] == 3
    assert p["door_fr"]["level"] == -1  # 計測不良
    assert rules.film_limits({"origin": "import", "color": "pearl"}, CFG)["normal"] == 220
    assert rules.film_limits({"normal_max": 180}, CFG)["normal"] == 180


def test_pair_and_relative_diff():
    film = full_film(110, door_fr=100, door_fl=170)  # 左右差70, どちらも新車の範囲
    r = rules.evaluate_film({"vehicle": {}, "film": film}, CFG)["panels"]
    assert r["door_fl"]["level"] == 1 and r["door_fl"]["pair_diff"] == 70
    assert r["door_fr"]["level"] == 0


def test_two_door_excludes_rear_doors():
    r = rules.evaluate_film({"vehicle": {"doors": 2}, "film": {}}, CFG)
    assert "door_rr" not in r["expected_ids"] and r["expected"] == len(rules.PANEL_ORDER) - 2


def test_verdict_hold_low_mid_high():
    assert rules.evaluate({}, CFG)["verdict"]["key"] == "hold"
    clean = {"vehicle": {}, "film": full_film(), "points": done_points("none")}
    assert rules.evaluate(clean, CFG)["verdict"]["key"] == "low"
    # 骨格に近いルーフが厚い → 骨格の疑い
    roof = {"vehicle": {}, "film": full_film(roof_center=300), "points": done_points("none")}
    r = rules.evaluate(roof, CFG)
    assert r["skeleton_suspect"] and r["verdict"]["key"] in ("mid", "high")
    # 骨格ポイントが濃厚
    pts = done_points("none")
    pts["side_member_front"] = {"user": "bad"}
    r = rules.evaluate({"vehicle": {}, "film": full_film(), "points": pts}, CFG)
    assert r["verdict"]["key"] == "high" and r["skeleton_suspect"]


def test_suggestions_chain_from_findings():
    pts = {"core_support": {"user": "bad"}}
    case = {"vehicle": {}, "film": full_film(fender_r=260), "points": pts}
    r = rules.evaluate(case, CFG)
    s = {x["id"]: x for x in r["suggestions"]}
    assert s["side_member_front"]["tier"] == "must"
    assert any("ラジエーター" in x for x in s["side_member_front"]["reasons"])
    assert s["fender_top"]["priority"] >= 3 and any("膜厚" in x for x in s["fender_top"]["reasons"])
    assert s["core_support"]["done"] and r["suggestions"][-1]["done"]


def test_user_override_beats_ai():
    st = {"ai": {"tamper_level": "likely"}, "user": "ok"}
    assert rules.effective_level(st) == ("none", "user")
    assert rules.effective_level({"ai": {"tamper_level": "possible"}}) == ("possible", "ai")
    assert rules.effective_level({}) == (None, "")


def test_memo_mentions_findings():
    case = {"vehicle": {"name": "テスト車", "year": 2018}, "film": full_film(fender_r=300), "points": done_points("possible"),
            "visual": {"gap_hood": "ng"}}
    m = rules.evaluate(case, CFG)["memo"]
    assert "テスト車" in m and "右フェンダー" in m and "そろっていない" in m


def test_schema_and_prompt():
    sch = build_schema()
    ids = sch["properties"]["checks"]["items"]["properties"]["id"]["enum"]
    assert "paint_seal" in ids and "weld_spots" in ids
    pr = build_prompt("hood_hinge", ["画像1=左側"], {"name": "x", "origin": "import"})
    assert "paint_seal" in pr and "輸入車" in pr and "lr_diff" in pr
    assert "lr_diff" not in build_prompt("trunk_floor", ["画像1=全体"], {})


def test_normalize_fills_and_guards():
    r = normalize("hood_hinge", {"image_ok": False, "tamper_level": "likely", "checks": []})
    assert r["tamper_level"] == "unclear" and len(r["checks"]) == len(rules.POINTS["hood_hinge"]["checks"])
    r = normalize("trunk_floor", {"image_ok": True, "tamper_level": "none", "checks": [
        {"id": "sealer", "result": "suspicious", "detail": ""}, {"id": "waves", "result": "suspicious", "detail": ""}]})
    assert r["tamper_level"] == "possible"


class Fake:
    def analyze(self, pid, images, vehicle):
        return {"image_ok": True, "retake_advice": "", "checks": [], "tamper_level": "possible",
                "confidence": "low", "summary": "テスト", "next_hint": ""}


JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 50).decode()


def test_api():
    c = TestClient(create_app(CFG, Fake()))
    assert c.get("/").status_code == 200
    m = c.get("/api/meta").json()
    assert m["ai"] and "hood_hinge" in m["points"]
    r = c.post("/api/evaluate", json={"vehicle": {}, "film": {"bonnet": 300}}).json()
    assert r["film"]["panels"]["bonnet"]["level"] == 2
    ok = c.post("/api/analyze", json={"point_id": "hood_hinge", "images": [{"label": "左側", "data": JPEG}]})
    assert ok.status_code == 200 and ok.json()["tamper_level"] == "possible"
    assert c.post("/api/analyze", json={"point_id": "nope", "images": [{"label": "a", "data": JPEG}]}).status_code == 400
    assert TestClient(create_app(CFG, None)).post("/api/analyze", json={"point_id": "hood_hinge", "images": [{"label": "a", "data": JPEG}]}).status_code == 503


def test_access_code():
    c = TestClient(create_app(Config(access_code="1234"), Fake()))
    assert c.get("/api/meta").status_code == 401
    assert c.get("/api/meta", headers={"x-access-code": "1234"}).status_code == 200
