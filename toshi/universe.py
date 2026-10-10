"""シグナル判定の対象銘柄(売買代金が大きい大型株)。名前は通知・表示用の目安。"""
from __future__ import annotations

import json
import os

NAMES = {
    "1605": "INPEX", "1925": "大和ハウス工業", "2802": "味の素", "2914": "JT",
    "3382": "セブン&アイ・ホールディングス", "4063": "信越化学工業", "4502": "武田薬品工業", "4519": "中外製薬",
    "4543": "テルモ", "4568": "第一三共", "4661": "オリエンタルランド", "4755": "楽天グループ",
    "5020": "ENEOSホールディングス", "5401": "日本製鉄", "6098": "リクルートホールディングス", "6178": "日本郵政",
    "6301": "コマツ", "6367": "ダイキン工業", "6501": "日立製作所", "6594": "ニデック",
    "6752": "パナソニック ホールディングス", "6758": "ソニーグループ", "6857": "アドバンテスト", "6861": "キーエンス",
    "6902": "デンソー", "6920": "レーザーテック", "6954": "ファナック", "6981": "村田製作所",
    "7011": "三菱重工業", "7012": "川崎重工業", "7013": "IHI", "7201": "日産自動車", "7203": "トヨタ自動車",
    "7267": "本田技研工業", "7741": "HOYA", "7974": "任天堂", "8001": "伊藤忠商事", "8031": "三井物産",
    "8035": "東京エレクトロン", "8058": "三菱商事", "8306": "三菱UFJフィナンシャル・グループ",
    "8316": "三井住友フィナンシャルグループ", "8411": "みずほフィナンシャルグループ", "8591": "オリックス",
    "8604": "野村ホールディングス", "8725": "MS&ADインシュアランスグループ", "8766": "東京海上ホールディングス",
    "8802": "三菱地所", "9020": "東日本旅客鉄道", "9022": "東海旅客鉄道", "9101": "日本郵船", "9104": "商船三井",
    "9432": "NTT", "9433": "KDDI", "9434": "ソフトバンク", "9501": "東京電力ホールディングス",
    "9983": "ファーストリテイリング", "9984": "ソフトバンクグループ", "1306": "TOPIX連動ETF",
}

SIGNAL_UNIVERSE = [c for c in NAMES if c != "1306"]


def name_of(code: str) -> str:
    """表示用の銘柄名。主要銘柄は NAMES、それ以外はかぶミニ一覧の名前、無ければコード。"""
    if code in NAMES:
        return NAMES[code]
    v = _MINI["stocks"].get(code)
    return v[2] if v and len(v) > 2 else code


# --- 楽天証券「かぶミニ®」(単元未満株)の取扱銘柄。1株から売買できるのはこの一覧の銘柄だけ ---
_MINI_PATH = os.path.join(os.path.dirname(__file__), "kabumini.json")
try:
    with open(_MINI_PATH, encoding="utf-8") as _f:
        _MINI = json.load(_f)
except (OSError, ValueError):
    _MINI = {"as_of": "", "stocks": {}}
MINI_AS_OF: str = _MINI.get("as_of", "")


def mini_info(code: str) -> dict | None:
    """{"open": 寄付取引の可否, "realtime": リアルタイム取引の可否}。かぶミニ対象外なら None。"""
    v = _MINI["stocks"].get(code)
    return {"open": bool(v[0]), "realtime": bool(v[1])} if v else None


def filter_mini(codes: list[str]) -> tuple[list[str], list[str]]:
    """(かぶミニ対象の銘柄, 対象外の銘柄)。一覧ファイルが読めないときは絞り込まない。"""
    if not _MINI["stocks"]:
        return list(codes), []
    ok = [c for c in codes if c in _MINI["stocks"]]
    return ok, [c for c in codes if c not in _MINI["stocks"]]


def pool_realtime() -> list[str]:
    """リアルタイム取引ができるかぶミニ銘柄(売買しやすい銘柄の母集団)。一覧が読めなければ空。"""
    return [c for c, v in _MINI["stocks"].items() if v[1]]
