"""診断ルール: 膜厚の判定・部位の定義・疑い度の集計・次に撮る場所の提案。

AI(画像認識)は「何が見えるか」を返すだけで、最終的な疑い度はここのルールで計算します。
"""
from __future__ import annotations

import statistics

# ---------------------------------------------------------------- 膜厚
LEVEL_LABEL = {
    -1: "計測不良の可能性",
    0: "正常範囲",
    1: "再塗装の疑い",
    2: "厚い再塗装・パテの疑い",
    3: "板金・パテの疑いが強い",
}
PAIR_DIFF_UM = 60  # 左右の差がこれ以上なら要注意
RELATIVE_DIFF_UM = 60  # 他パネルの中央値よりこれ以上厚ければ要注意
MIN_VALID_UM = 40  # これ未満は計測不良(アルミ・樹脂・当て方ミス)の可能性

# 部位: (id, 名前, グループ, 左右の相方, 重み, 後席ドア用か)
_PANEL_ROWS = [
    ("bonnet", "ボンネット", "body", None, 1, False),
    ("fender_r", "右フェンダー", "body", "fender_l", 1, False),
    ("door_fr", "右 前ドア", "body", "door_fl", 1, False),
    ("door_rr", "右 後ろドア", "body", "door_rl", 1, True),
    ("quarter_r", "右クォーター(後ろの側面)", "body", "quarter_l", 2, False),
    ("trunk", "トランク/リアゲート", "body", None, 1, False),
    ("quarter_l", "左クォーター(後ろの側面)", "body", "quarter_r", 2, False),
    ("door_rl", "左 後ろドア", "body", "door_rr", 1, True),
    ("door_fl", "左 前ドア", "body", "door_fr", 1, False),
    ("fender_l", "左フェンダー", "body", "fender_r", 1, False),
    ("roof_front", "ルーフ 前", "roof", None, 3, False),
    ("roof_center", "ルーフ 中", "roof", None, 3, False),
    ("roof_rear", "ルーフ 後ろ", "roof", None, 3, False),
    ("pillar_a_r", "右 Aピラー(フロントガラス脇)", "pillar", "pillar_a_l", 3, False),
    ("pillar_b_r", "右 Bピラー(ドアの間)", "pillar", "pillar_b_l", 3, False),
    ("pillar_c_r", "右 Cピラー(後ろの柱)", "pillar", "pillar_c_l", 3, False),
    ("pillar_a_l", "左 Aピラー(フロントガラス脇)", "pillar", "pillar_a_r", 3, False),
    ("pillar_b_l", "左 Bピラー(ドアの間)", "pillar", "pillar_b_r", 3, False),
    ("pillar_c_l", "左 Cピラー(後ろの柱)", "pillar", "pillar_c_r", 3, False),
    ("rocker_r", "右 ロッカー(ドア下の敷居)", "rocker", "rocker_l", 2, False),
    ("rocker_l", "左 ロッカー(ドア下の敷居)", "rocker", "rocker_r", 2, False),
]
PANEL_GROUPS = [
    ("body", "ボディ(車を一周)"),
    ("roof", "ルーフ(屋根)"),
    ("pillar", "ピラー(柱)"),
    ("rocker", "ロッカー(ドア下)"),
]
PANELS = {
    r[0]: {"id": r[0], "name": r[1], "group": r[2], "pair": r[3], "weight": r[4], "rear_door": r[5]}
    for r in _PANEL_ROWS
}
PANEL_ORDER = [r[0] for r in _PANEL_ROWS]

# 膜厚の異常があるとき、近くのどの撮影ポイントを優先するか
PANEL_TO_POINTS = {
    "bonnet": ["hood_hinge", "core_support"],
    "fender": ["fender_top", "fender_front", "hood_hinge"],
    "door_f": ["door_hinge", "pillar_a_base"],
    "door_r": ["door_hinge", "pillar_b"],
    "quarter": ["trunk_hinge", "rear_panel", "trunk_floor"],
    "trunk": ["trunk_hinge", "trunk_floor", "rear_panel"],
    "roof": ["roof_edge", "pillar_a_base", "pillar_b"],
    "pillar_a": ["pillar_a_base", "roof_edge"],
    "pillar_b": ["pillar_b", "roof_edge"],
    "pillar_c": ["rear_panel", "trunk_floor"],
    "rocker": ["pillar_b", "pillar_a_base"],
}


def _panel_family(pid: str) -> str:
    for key in ("pillar_a", "pillar_b", "pillar_c", "door_f", "door_r", "fender", "quarter", "roof", "rocker"):
        if pid.startswith(key):
            return key
    return pid  # bonnet / trunk


def film_limits(vehicle: dict, cfg=None) -> dict:
    """この車の膜厚しきい値(μm)。車の情報から自動で決め、手入力で上書きもできる。"""
    dom = getattr(cfg, "film_normal_domestic", 150)
    imp = getattr(cfg, "film_normal_import", 200)
    nm = imp if vehicle.get("origin") == "import" else dom
    nm += {"metallic": 10, "pearl": 20}.get(vehicle.get("color"), 0)
    override = vehicle.get("normal_max")
    if isinstance(override, (int, float)) and 60 <= override <= 600:
        nm = int(override)
    return {"normal": nm, "mild": nm + 100, "strong": nm + 250, "min_valid": MIN_VALID_UM}


def _num(v):
    if isinstance(v, bool) or v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def expected_panels(vehicle: dict) -> list[str]:
    two_door = vehicle.get("doors") == 2
    return [p for p in PANEL_ORDER if not (two_door and PANELS[p]["rear_door"])]


def evaluate_film(case: dict, cfg=None) -> dict:
    vehicle = case.get("vehicle") or {}
    lim = film_limits(vehicle, cfg)
    film = case.get("film") or {}
    expected = expected_panels(vehicle)
    values = {p: _num(film.get(p)) for p in expected}
    measured = {p: v for p, v in values.items() if v is not None}
    valid = [v for v in measured.values() if v >= lim["min_valid"]]
    median = statistics.median(valid) if len(valid) >= 6 else None

    out: dict[str, dict] = {}
    for pid, v in measured.items():
        if v < lim["min_valid"]:
            out[pid] = {"value": v, "level": -1, "label": LEVEL_LABEL[-1], "notes": [
                "値が小さすぎます。アルミ・樹脂パネルか、当て方のミスかもしれません(再測定を)"], "points": 0}
            continue
        level = 0 if v <= lim["normal"] else 1 if v <= lim["mild"] else 2 if v <= lim["strong"] else 3
        notes = []
        if level:
            notes.append(f"{v:g}μm(新車の目安 〜{lim['normal']}μm)")
        if median is not None and v - median >= RELATIVE_DIFF_UM:
            notes.append(f"この車の他の部位(中央値{median:g}μm)より明らかに厚い")
            level = max(level, 1)
        out[pid] = {"value": v, "level": level, "label": LEVEL_LABEL[level], "notes": notes, "points": 0}

    # 左右差
    for pid, info in out.items():
        pair = PANELS[pid]["pair"]
        if info["level"] < 0 or not pair or pair not in out or out[pair]["level"] < 0:
            continue
        diff = info["value"] - out[pair]["value"]
        if diff >= PAIR_DIFF_UM:
            info["pair_diff"] = diff
            info["notes"].append(f"反対側({PANELS[pair]['name']})より{diff:g}μm厚い")
            if info["level"] == 0:
                info["level"] = 1
                info["label"] = LEVEL_LABEL[1]
            info["points"] += PANELS[pid]["weight"]  # 左右差ぶんの加点

    for pid, info in out.items():
        if info["level"] > 0:
            info["points"] += info["level"] * PANELS[pid]["weight"]

    return {
        "limits": lim, "median": median, "expected": len(expected),
        "measured": len(measured), "panels": out, "expected_ids": expected,
    }


# ---------------------------------------------------------------- 撮影ポイント
BOLT_CHECKS = [
    ("paint_seal", "ボルト頭まわりの塗膜", "ボルト頭と周りの塗装が一続きで、ヒビ割れていないか(割れ・欠けは一度外した跡)"),
    ("tool_marks", "工具の跡", "頭の角のナメ・傷・メッキ剥がれ・光った擦れ跡がないか"),
    ("repaint", "上塗り・マスキング跡", "ボルトや座面に後から塗った塗料、境目、はみ出しがないか"),
    ("bolt_type", "ボルトの種類", "純正らしいか。色・形・メッキが他のボルトや反対側と違わないか"),
    ("marks", "締付マーク", "白・黄色などのマーキング線がちぎれていないか(無い車も多い)"),
    ("lr_diff", "左右の違い", "反対側と比べて汚れ・錆・塗膜の状態が違わないか"),
]
STRUCT_CHECKS = [
    ("weld_spots", "スポット溶接の跡", "工場のスポット跡は丸く等間隔でそろう。不ぞろい・潰れ・数の違いは疑い"),
    ("weld_repair", "後付けの溶接・ろう付け", "ビード状の盛り・ろう付け・グラインダーで削った跡がないか"),
    ("sealer", "シーラー(防水の充填材)", "工場出荷は均一で機械的。筆跡・段差・色違いの手塗りは疑い"),
    ("waves", "波打ち・シワ・曲がり", "鈑金跡、パネルの波打ち、折れ・シワがないか"),
    ("overspray", "塗料の吹き込み", "本来塗らない場所に、色や艶の違う塗料がついていないか"),
    ("lr_diff", "左右の違い", "反対側と比べて、溶接・シーラー・塗装の状態が違わないか"),
]

# (id, 名前, 種類, 左右ペアか, 重み, 基本セットか, 場所, 撮り方, 追加で確認する先)
_POINT_ROWS = [
    ("hood_hinge", "ボンネットのヒンジボルト", "bolt", True, 1, True,
     "ボンネットを開け、根元(フロントガラス側)の蝶番(ちょうつがい)を探します。ヒンジを車体にとめているボルトです。",
     ["fender_top", "core_support"]),
    ("fender_top", "フェンダー上端のボルト", "bolt", True, 1, True,
     "ボンネットを開けた状態で、タイヤの上のフェンダー(外側パネル)の上端に並ぶボルトです。",
     ["fender_front", "core_support", "apron"]),
    ("door_hinge", "ドアのヒンジボルト", "bolt", True, 1, True,
     "ドアを全開にして、前側の根元にある蝶番のボルト(車体側)です。",
     ["pillar_a_base", "pillar_b"]),
    ("core_support", "ラジエーターサポートの取付ボルト", "bolt", True, 2, True,
     "ボンネットを開け、エンジンルームの一番前にある横向きの骨組み(ラジエーターの前〜上)の左右端のボルトです。",
     ["side_member_front", "strut_tower", "apron"]),
    ("trunk_hinge", "トランク/リアゲートのヒンジボルト", "bolt", True, 1, True,
     "トランクかリアゲートを開け、根元の蝶番を車体にとめているボルトです。",
     ["rear_panel", "trunk_floor"]),
    ("fender_front", "フェンダー前下のボルト", "bolt", True, 1, False,
     "ヘッドライト横〜バンパーとフェンダーの境目あたり。フェンダー前端をとめているボルトです。",
     ["core_support", "side_member_front"]),
    ("headlight_stay", "ヘッドライトの取付ボルト", "bolt", True, 1, False,
     "ボンネットを開けて、ヘッドライトの上側・後ろ側にあるとめ具のボルトです。",
     ["core_support", "side_member_front"]),
    ("side_member_front", "フロントサイドメンバー先端(骨格)", "struct", True, 3, False,
     "車の前から覗き込むか、バンパー下から。エンジンルーム左右の太い骨組み(フレーム)の先端、バンパーのすぐ奥です。",
     ["strut_tower", "apron"]),
    ("strut_tower", "ストラットタワー周辺(骨格)", "struct", True, 3, False,
     "ボンネットを開けて、左右のタイヤの上にある丸く盛り上がった部分と、その周りです。",
     ["apron", "side_member_front"]),
    ("apron", "エプロン・インナーパネル(骨格)", "struct", True, 2, False,
     "エンジンルームの左右の側面の壁。ストラットタワーから前へ続くパネルの継ぎ目と溶接点です。",
     ["strut_tower", "side_member_front"]),
    ("pillar_a_base", "Aピラー下部・ドア開口部(骨格)", "struct", True, 3, False,
     "前ドアを開け、フロントガラス脇の柱(Aピラー)の下端と、ドア枠の継ぎ目・ゴムの内側です。",
     ["pillar_b", "roof_edge"]),
    ("pillar_b", "Bピラー下部・ドア開口部(骨格)", "struct", True, 3, False,
     "前後ドアの間の柱(Bピラー)の下端。ドア枠のゴムを少しめくった内側です。",
     ["roof_edge", "pillar_a_base"]),
    ("trunk_floor", "トランクの床(骨格)", "struct", False, 3, False,
     "トランクの床マットをめくり、スペアタイヤ(収納)の周りの床の波打ちと、左右の継ぎ目を見ます。",
     ["rear_panel"]),
    ("rear_panel", "リアパネル・テール周辺(骨格)", "struct", False, 3, False,
     "トランクの奥(後ろの壁)と、テールランプ裏の周りです。",
     ["trunk_floor"]),
    ("roof_edge", "ルーフの縁(骨格)", "struct", False, 3, False,
     "ルーフと窓枠・ドア枠のあいだの継ぎ目。モールやルーフレールの下も見ます。",
     ["pillar_a_base", "pillar_b"]),
]
POINT_ORDER = [r[0] for r in _POINT_ROWS]


def _make_point(r) -> dict:
    pid, name, kind, pair, weight, basic, where, follow = r
    slots = (
        [{"id": "l", "label": "左側", "required": True}, {"id": "r", "label": "右側", "required": True},
         {"id": "close", "label": "気になる所のアップ(任意)", "required": False}]
        if pair else
        [{"id": "main", "label": "全体", "required": True}, {"id": "close", "label": "気になる所のアップ(任意)", "required": False}]
    )
    checks = [c for c in (BOLT_CHECKS if kind == "bolt" else STRUCT_CHECKS) if pair or c[0] != "lr_diff"]
    tips = (
        "ボルト頭が画面の真ん中に大きく写るよう、10〜15cmまで近づき、斜め45°から撮ります。暗い所はスマホのライトを使い、左右は同じ条件で撮ってください。"
        if kind == "bolt" else
        "溶接やシーラーの線が見えるよう、20〜30cmの距離で撮ります。ライトで斜めから照らすと凹凸がはっきり写ります。全体が見える引きの写真→気になる所のアップの順で。"
    )
    return {
        "id": pid, "name": name, "kind": kind, "pair": pair, "weight": weight, "basic": basic,
        "where": where, "tips": tips, "slots": slots,
        "checks": [{"id": c[0], "label": c[1], "hint": c[2]} for c in checks], "follow_ups": follow,
    }


POINTS = {r[0]: _make_point(r) for r in _POINT_ROWS}
BASIC_POINTS = [p for p in POINT_ORDER if POINTS[p]["basic"]]

# 'possible'/'likely' のとき1点あたり(重みを掛ける)
POINT_SCORE = {
    "bolt": {"none": 0, "unclear": 0, "possible": 1, "likely": 3},
    "struct": {"none": 0, "unclear": 0, "possible": 3, "likely": 6},
}
USER_TO_LEVEL = {"ok": "none", "sus": "possible", "bad": "likely", "unk": "unclear"}
LEVEL_LABEL_POINT = {
    "none": "触った形跡は見つからない", "possible": "触った形跡の疑いあり",
    "likely": "触った形跡がかなり濃厚", "unclear": "判断できない",
}

# 目視チェック: (id, 質問, 疑いのときの点)
VISUAL_CHECKS = [
    ("gap_hood", "ボンネットのすき間(チリ)が左右でそろっている", 1),
    ("gap_doors", "ドアのすき間(チリ)が前後左右でそろっている", 1),
    ("gap_trunk", "トランク/リアゲートのすき間がそろっている", 1),
    ("glass_year", "ガラスの刻印の年が、どれも車の年式と近い(1枚だけ新しい、は×)", 1),
    ("lamp_date", "ライト・バンパー裏の製造年が左右で同じ(片方だけ新しい、は×)", 1),
    ("bumper_back", "バンパー裏のシーラー・塗装に不自然な所がない", 1),
    ("rust_bias", "錆や汚れが、片側の部品だけ極端に違うことがない", 1),
    ("warning", "エアバッグ・シートベルト関連の警告灯やベルトの異常がない", 2),
    ("interior", "室内に水跡・焦げ・土の臭いや、不自然なはがれがない", 1),
]

VISUAL_NG = {
    "gap_hood": "ボンネットのすき間(チリ)が左右でそろっていない",
    "gap_doors": "ドアのすき間(チリ)が前後左右でそろっていない",
    "gap_trunk": "トランク/リアゲートのすき間がそろっていない",
    "glass_year": "ガラスの刻印の年が、車の年式と合わないものがある",
    "lamp_date": "ライト・バンパー裏の製造年が左右で違う",
    "bumper_back": "バンパー裏のシーラー・塗装に不自然な所がある",
    "rust_bias": "錆や汚れが、片側の部品だけ極端に違う",
    "warning": "エアバッグ・シートベルト関連の警告灯やベルトに異常がある",
    "interior": "室内に水跡・焦げ・土の臭いや、不自然なはがれがある",
}


# ---------------------------------------------------------------- 集計
def effective_level(state: dict | None) -> tuple[str | None, str]:
    """(疑いレベル, 出どころ)。自分の見立て > AI の順に採用。未判定は (None, '')。"""
    if not state:
        return None, ""
    user = state.get("user")
    if user in USER_TO_LEVEL:
        return USER_TO_LEVEL[user], "user"
    ai = state.get("ai")
    if ai and ai.get("tamper_level") in POINT_SCORE["bolt"]:
        return ai["tamper_level"], "ai"
    return None, ""


def evaluate_points(case: dict) -> dict:
    out = {}
    for pid, spec in POINTS.items():
        level, src = effective_level((case.get("points") or {}).get(pid))
        pts = POINT_SCORE[spec["kind"]].get(level, 0) * spec["weight"] if level else 0
        out[pid] = {"level": level, "source": src, "points": pts,
                    "done": level in ("none", "possible", "likely")}
    return out


def evaluate_visual(case: dict) -> dict:
    answers = case.get("visual") or {}
    out = {}
    for cid, q, pts in VISUAL_CHECKS:
        a = answers.get(cid)
        out[cid] = {"answer": a, "points": pts if a == "ng" else 0}
    return out


def build_suggestions(case: dict, film: dict, points: dict) -> list[dict]:
    """次に撮る場所を、膜厚の異常・触った形跡のある場所からの連鎖で並べる。"""
    prio: dict[str, int] = {p: 0 for p in POINT_ORDER}
    reasons: dict[str, list[str]] = {p: [] for p in POINT_ORDER}
    for pid in BASIC_POINTS:
        prio[pid] += 1
    for panel, info in film["panels"].items():
        if info["level"] >= 1:
            for target in PANEL_TO_POINTS.get(_panel_family(panel), []):
                prio[target] += 3 if info["level"] >= 2 else 2
                reasons[target].append(f"膜厚: {PANELS[panel]['name']}が{info['value']:g}μm({info['label']})")
    for pid, st in points.items():
        if st["level"] in ("possible", "likely"):
            for target in POINTS[pid]["follow_ups"]:
                prio[target] += 5 if st["level"] == "likely" else 3
                reasons[target].append(f"{POINTS[pid]['name']}に{LEVEL_LABEL_POINT[st['level']]}")

    sugg = []
    for pid in POINT_ORDER:
        st = points[pid]
        tier = "must" if prio[pid] >= 5 else "high" if prio[pid] >= 3 else "basic" if prio[pid] >= 1 else "optional"
        sugg.append({"id": pid, "priority": prio[pid], "tier": tier, "reasons": reasons[pid], "done": st["done"]})
    order = {p: i for i, p in enumerate(POINT_ORDER)}
    sugg.sort(key=lambda s: (s["done"], -s["priority"], order[s["id"]]))
    return sugg


def evaluate(case: dict, cfg=None) -> dict:
    film = evaluate_film(case, cfg)
    points = evaluate_points(case)
    visual = evaluate_visual(case)
    suggestions = build_suggestions(case, film, points)

    reasons: list[dict] = []
    for pid, info in film["panels"].items():
        if info["points"]:
            reasons.append({"kind": "film", "id": pid, "title": f"膜厚 {PANELS[pid]['name']}: {info['label']}",
                            "detail": " / ".join(info["notes"]), "points": info["points"]})
    for pid, st in points.items():
        if st["points"]:
            reasons.append({"kind": "point", "id": pid, "title": f"{POINTS[pid]['name']}: {LEVEL_LABEL_POINT[st['level']]}",
                            "detail": "自分の見立てで判定" if st["source"] == "user" else "AIの判定", "points": st["points"]})
    for cid, q, _ in VISUAL_CHECKS:
        if visual[cid]["points"]:
            reasons.append({"kind": "visual", "id": cid, "title": "目視チェックで気になる点", "detail": VISUAL_NG[cid], "points": visual[cid]["points"]})
    reasons.sort(key=lambda r: -r["points"])
    score = sum(r["points"] for r in reasons)

    struct_suspect = [pid for pid, st in points.items()
                      if POINTS[pid]["kind"] == "struct" and st["level"] in ("possible", "likely")]
    core_likely = points["core_support"]["level"] == "likely"
    heavy_film = [p for p, i in film["panels"].items()
                  if PANELS[p]["group"] in ("pillar", "roof", "rocker") and i["level"] >= 2]
    skeleton_suspect = bool(struct_suspect or core_likely or heavy_film)

    basic_done = sum(points[p]["done"] for p in BASIC_POINTS)
    answered = sum(1 for v in visual.values() if v["answer"] in ("ok", "ng"))
    coverage = (0.4 * film["measured"] / max(1, film["expected"])
                + 0.5 * basic_done / len(BASIC_POINTS)
                + 0.1 * answered / len(VISUAL_CHECKS))

    likely_struct = any(points[p]["level"] == "likely" for p in struct_suspect)
    if score >= 10 or likely_struct or (struct_suspect and any(
            st["level"] == "likely" for st in points.values())):
        key = "high"
    elif score >= 4 or skeleton_suspect:
        key = "mid"
    elif coverage < 0.6:
        key = "hold"
    else:
        key = "low"
    verdict = {
        "high": ("修復歴の疑いが強い", "骨格(フレーム・ピラー・ルーフなど)まで手が入っている可能性があります。必ず現物で詳しく確認してください。"),
        "mid": ("修復歴の可能性あり", "再塗装や部品の脱着の形跡があります。骨格に及んでいないか、勧めた箇所を追加で確認してください。"),
        "low": ("今のところ修復歴を疑う根拠は少ない", "調べた範囲では目立つ異常はありません。ただし写真で見えない部分は判断できません。"),
        "hold": ("判定保留(確認が足りません)", "調べた箇所が少なく、問題なしとは言えません。膜厚と基本のボルトを撮影すると判定できます。"),
    }[key]

    unconfirmed = []
    for pid in BASIC_POINTS:
        if not points[pid]["done"]:
            unconfirmed.append(f"{POINTS[pid]['name']}(未確認)")
    missing_panels = [PANELS[p]["name"] for p in film["expected_ids"] if p not in film["panels"]]
    if missing_panels:
        unconfirmed.append(f"膜厚 未測定: {'、'.join(missing_panels)}")
    for s in suggestions:
        if s["tier"] in ("must", "high") and not s["done"] and POINTS[s["id"]]["basic"] is False:
            unconfirmed.append(f"{POINTS[s['id']]['name']}(おすすめ・未確認)")

    result = {
        "film": film, "points": points, "visual": visual, "suggestions": suggestions,
        "score": score, "coverage": round(coverage, 2), "skeleton_suspect": skeleton_suspect,
        "verdict": {"key": key, "label": verdict[0], "message": verdict[1]},
        "reasons": reasons, "unconfirmed": unconfirmed,
    }
    result["memo"] = build_memo(case, result)
    return result


def build_memo(case: dict, r: dict) -> str:
    v = case.get("vehicle") or {}
    lim = r["film"]["limits"]
    lines = ["【修復歴チェック メモ】", "※AIと簡易ルールによる補助判定です。最終判断は現物確認で行ってください。"]
    title = " ".join(x for x in [v.get("name"), f"{v['year']}年式" if v.get("year") else ""] if x)
    if title:
        lines.append(f"車両: {title}")
    lines.append(f"総合: {r['verdict']['label']}(疑い度 {r['score']}点 / 確認の充実度 {int(r['coverage'] * 100)}%)")
    if r["skeleton_suspect"]:
        lines.append("★ 骨格部位に影響している疑いがあります")
    lines += ["", f"■ 膜厚(新車の目安 〜{lim['normal']}μm) 測定 {r['film']['measured']}/{r['film']['expected']}部位"]
    flagged = [(p, i) for p, i in r["film"]["panels"].items() if i["level"] != 0]
    for p, i in flagged:
        lines.append(f"・{PANELS[p]['name']}: {i['value']:g}μm {i['label']}")
    if not flagged:
        lines.append("・異常なし")
    lines += ["", "■ ボルト・骨格の確認"]
    any_pt = False
    for pid in POINT_ORDER:
        st = r["points"][pid]
        if st["level"]:
            any_pt = True
            src = "(自分の見立て)" if st["source"] == "user" else "(AI)"
            lines.append(f"・{POINTS[pid]['name']}: {LEVEL_LABEL_POINT[st['level']]}{src}")
    if not any_pt:
        lines.append("・未実施")
    ng = [VISUAL_NG[cid] for cid, _, _ in VISUAL_CHECKS if r["visual"][cid]["points"]]
    if ng:
        lines += ["", "■ 目視で気になった点"] + [f"・{q}" for q in ng]
    if r["unconfirmed"]:
        lines += ["", "■ 未確認"] + [f"・{u}" for u in r["unconfirmed"]]
    return "\n".join(lines)
