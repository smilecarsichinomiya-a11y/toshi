from __future__ import annotations

import logging
from dataclasses import dataclass

from .db import DB, ts

log = logging.getLogger("toshi.broker")


@dataclass
class Position:
    symbol: str
    qty: int
    avg_price: float


@dataclass
class Fill:
    ok: bool
    price: float
    ref: str = ""
    message: str = ""


class Broker:
    name = "base"

    def cash(self) -> float: ...
    def positions(self) -> dict[str, Position]: ...
    def order(self, symbol: str, side: str, qty: int, ref_price: float) -> Fill: ...


class PaperBroker(Broker):
    """仮想売買。現値 ± スリッページ(0.05%)で即約定、手数料は無料と仮定。"""

    name = "paper"
    SLIPPAGE = 0.0005

    def __init__(self, db: DB, initial_cash: float):
        self.db = db
        if db.get("paper_cash") is None:
            db.set("paper_cash", str(initial_cash))

    def cash(self) -> float:
        return float(self.db.get("paper_cash"))

    def positions(self) -> dict[str, Position]:
        return {r["symbol"]: Position(r["symbol"], r["qty"], r["avg_price"])
                for r in self.db.query("SELECT * FROM positions WHERE qty>0")}

    def order(self, symbol: str, side: str, qty: int, ref_price: float) -> Fill:
        px = ref_price * (1 + self.SLIPPAGE if side == "buy" else 1 - self.SLIPPAGE)
        px = round(px, 1)
        cash, pos = self.cash(), self.positions().get(symbol)
        if side == "buy":
            cost = px * qty
            if cost > cash:
                return Fill(False, px, message="資金不足")
            nq = qty + (pos.qty if pos else 0)
            avg = (px * qty + (pos.avg_price * pos.qty if pos else 0)) / nq
            self.db.execute("INSERT INTO positions(symbol,qty,avg_price) VALUES(?,?,?) "
                            "ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price",
                            (symbol, nq, avg))
            self.db.set("paper_cash", str(cash - cost))
        else:
            if not pos or pos.qty < qty:
                return Fill(False, px, message="保有数量不足")
            self.db.execute("UPDATE positions SET qty=? WHERE symbol=?", (pos.qty - qty, symbol))
            self.db.set("paper_cash", str(cash + px * qty))
        return Fill(True, px, ref=f"paper-{ts()}")


class RakutenRssBroker(Broker):
    """楽天証券 マーケットスピードII RSS 経由の実発注 (現物・成行)。

    楽天証券に個人向けの公式 REST API は無いため、Windows 上の Excel(RSS + VBA) とファイルで連携する:
      Python → {bridge}/orders/<id>.txt  (注文指示)   → VBA が RSS で発注
      VBA    → {bridge}/fills/<id>.txt   (約定結果)   → Python が読む
      VBA    → {bridge}/state.txt        (余力・建玉。定期更新) → Python が読む
    VBA 側の雛形は bridge/RakutenBridge.bas。RSS 発注関数の呼び出し部分は未実装(要・公式マニュアル参照)。
    ※ 実環境で未検証。必ず少額で動作確認してください。
    """

    name = "rakuten-rss"
    STATE_MAX_AGE_SEC = 180

    def __init__(self, bridge_dir: str, fill_timeout: int = 90):
        import os

        self.dir, self.timeout = bridge_dir, fill_timeout
        for sub in ("orders", "fills"):
            os.makedirs(os.path.join(bridge_dir, sub), exist_ok=True)

    def _state(self) -> dict:
        import os
        from datetime import datetime

        path = os.path.join(self.dir, "state.txt")
        if not os.path.exists(path):
            raise RuntimeError("RSSブリッジの state.txt がありません(Excelブリッジ未起動)")
        kv: dict = {"pos": []}
        for line in open(path, encoding="utf-8"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                kv["pos"].append(v) if k == "pos" else kv.__setitem__(k, v)
        age = (datetime.now() - datetime.strptime(kv["updated"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        if age > self.STATE_MAX_AGE_SEC:
            raise RuntimeError(f"RSSブリッジの状態が{age:.0f}秒更新されていません(Excel停止?)")
        return kv

    def cash(self) -> float:
        return float(self._state()["cash"])

    def positions(self) -> dict[str, Position]:
        out = {}
        for row in self._state()["pos"]:
            sym, qty, avg = row.split(",")
            if int(qty) > 0:
                out[sym] = Position(sym, int(qty), float(avg))
        return out

    def order(self, symbol: str, side: str, qty: int, ref_price: float) -> Fill:
        import os
        import time
        import uuid

        oid = uuid.uuid4().hex[:12]
        tmp = os.path.join(self.dir, "orders", oid + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"id={oid}\nsymbol={symbol}\nside={side}\nqty={qty}\ntype=market\n")
        os.replace(tmp, tmp[:-4] + ".txt")
        fpath = os.path.join(self.dir, "fills", oid + ".txt")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if os.path.exists(fpath):
                kv = dict(l.strip().split("=", 1) for l in open(fpath, encoding="utf-8") if "=" in l)
                ok = kv.get("status") == "filled"
                return Fill(ok, float(kv.get("price") or ref_price), ref=oid, message=kv.get("message", ""))
            time.sleep(1)
        return Fill(False, ref_price, ref=oid, message="約定確認タイムアウト(注文が出ている可能性あり。楽天証券で要確認)")


def make_broker(cfg, db: DB) -> Broker:
    if cfg.live:
        return RakutenRssBroker(cfg.bridge_dir)
    return PaperBroker(db, cfg.initial_cash)
