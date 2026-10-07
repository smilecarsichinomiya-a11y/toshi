from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

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


class KabuStationBroker(Broker):
    """auカブコム証券「kabuステーション API」経由の実発注 (現物・成行・特定口座)。

    ※ 実環境での検証は未実施です。必ず検証用ポート(18081)・少額で動作確認してください。
    """

    name = "kabustation"

    def __init__(self, base_url: str, password: str):
        self.base, self.password, self._token = base_url.rstrip("/"), password, ""

    def _h(self) -> dict:
        if not self._token:
            r = requests.post(f"{self.base}/token", json={"APIPassword": self.password}, timeout=10)
            r.raise_for_status()
            self._token = r.json()["Token"]
        return {"X-API-KEY": self._token}

    def _req(self, method: str, path: str, **kw):
        r = requests.request(method, self.base + path, headers=self._h(), timeout=15, **kw)
        if r.status_code == 401:  # トークン失効
            self._token = ""
            r = requests.request(method, self.base + path, headers=self._h(), timeout=15, **kw)
        r.raise_for_status()
        return r.json()

    def cash(self) -> float:
        return float(self._req("GET", "/wallet/cash").get("StockAccountWallet", 0))

    def positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for p in self._req("GET", "/positions", params={"product": 1}):
            qty = int(p["LeavesQty"])
            if qty > 0:
                out[p["Symbol"]] = Position(p["Symbol"], qty, float(p["Price"]))
        return out

    def order(self, symbol: str, side: str, qty: int, ref_price: float) -> Fill:
        body = {
            "Password": self.password, "Symbol": symbol, "Exchange": 1, "SecurityType": 1,
            "Side": "2" if side == "buy" else "1", "CashMargin": 1,
            "DelivType": 2 if side == "buy" else 0, "FundType": "AA" if side == "buy" else "  ",
            "AccountType": 4, "Qty": qty, "FrontOrderType": 10, "Price": 0, "ExpireDay": 0,
        }
        try:
            res = self._req("POST", "/sendorder", json=body)
        except Exception as e:  # noqa: BLE001
            return Fill(False, ref_price, message=str(e))
        if res.get("Result") == 0:
            return Fill(True, ref_price, ref=str(res.get("OrderId")), message="発注受付(約定価格は証券会社側で確定)")
        return Fill(False, ref_price, message=str(res))


def make_broker(cfg, db: DB) -> Broker:
    if cfg.live:
        return KabuStationBroker(cfg.kabu_url, cfg.kabu_password)
    return PaperBroker(db, cfg.initial_cash)
