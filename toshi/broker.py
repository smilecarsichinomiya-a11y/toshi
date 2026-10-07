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
    """仮想売買。取得できた最新値(無料データのため遅延あり) ± スリッページ0.05% で即約定。手数料は0円と仮定。"""

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


def make_broker(cfg, db: DB) -> Broker:
    """証券口座を使わない仮想売買のみ。将来実売買する場合は Broker を実装して差し替える。"""
    return PaperBroker(db, cfg.initial_cash)
