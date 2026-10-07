"""機械的なリスク管理。Claude の判断より常に優先される (最後の砦)。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Order:
    symbol: str
    side: str  # buy / sell
    qty: int
    price: float
    source: str  # claude / risk-stop / risk-trailing / risk-takeprofit
    reason: str


def exit_signals(cfg, positions, prices: dict[str, float], high_water: dict[str, float]) -> list[Order]:
    """損切り・トレーリング・利確は Claude の判断と無関係に強制執行する。"""
    out = []
    for sym, p in positions.items():
        px = prices.get(sym)
        if not px:
            continue
        hw = max(high_water.get(sym, px), px)
        if px <= p.avg_price * (1 - cfg.stop_loss_pct):
            out.append(Order(sym, "sell", p.qty, px, "risk-stop",
                             f"損切り: 取得{p.avg_price:.0f}→現在{px:.0f} ({(px / p.avg_price - 1) * 100:.1f}%)"))
        elif px >= p.avg_price * (1 + cfg.take_profit_pct):
            out.append(Order(sym, "sell", p.qty, px, "risk-takeprofit",
                             f"利確: 取得{p.avg_price:.0f}→現在{px:.0f} (+{(px / p.avg_price - 1) * 100:.1f}%)"))
        elif hw > p.avg_price and px <= hw * (1 - cfg.trailing_stop_pct):
            out.append(Order(sym, "sell", p.qty, px, "risk-trailing",
                             f"トレーリング: 高値{hw:.0f}から{(px / hw - 1) * 100:.1f}%"))
    return out


class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg

    def review(self, decisions: list[dict], positions, prices: dict[str, float], cash: float,
               equity: float, orders_today: int, day_start_equity: float, halted: bool
               ) -> tuple[list[Order], dict[str, str]]:
        """Claude の売買案を検査し、実行可能な注文だけ返す。(orders, {symbol: 却下/調整理由})"""
        cfg, lot = self.cfg, self.cfg.lot_size
        notes: dict[str, str] = {}
        orders: list[Order] = []
        budget = cash
        held = {s: p.qty for s, p in positions.items()}
        n_orders = orders_today
        loss_hit = day_start_equity > 0 and equity <= day_start_equity * (1 - cfg.daily_loss_limit_pct)

        # 売りを先に処理して資金を確保
        ordered = sorted(decisions, key=lambda d: 0 if d["action"] == "sell" else 1)
        for d in ordered:
            sym, act, px = d["symbol"], d["action"], prices.get(d["symbol"])
            if act == "hold":
                continue
            if not px:
                notes[sym] = "価格取得不可"
                continue
            if n_orders >= cfg.max_orders_per_day:
                notes[sym] = "1日の最大注文数に到達"
                continue
            if act == "sell":
                have = held.get(sym, 0)
                if have <= 0:
                    notes[sym] = "未保有のため売り不可(空売り禁止)"
                    continue
                qty = min(have, max(1, d["lots"]) * lot) if d["lots"] else have
                orders.append(Order(sym, "sell", qty, px, "claude", d["reason"]))
                held[sym] = have - qty
                budget += px * qty
                n_orders += 1
            elif act == "buy":
                if halted:
                    notes[sym] = "キルスイッチ作動中(新規買い停止)"
                    continue
                if loss_hit:
                    notes[sym] = "日次損失上限に到達(新規買い停止)"
                    continue
                n_pos = sum(1 for q in held.values() if q > 0)
                if held.get(sym, 0) == 0 and n_pos >= cfg.max_positions:
                    notes[sym] = f"最大保有銘柄数({cfg.max_positions})に到達"
                    continue
                cur_val = held.get(sym, 0) * px
                room = cfg.max_position_pct * equity - cur_val
                spendable = budget - cfg.cash_reserve_pct * equity
                qty = int(min(room, spendable, max(1, d["lots"]) * lot * px) // (px * lot)) * lot
                if qty < lot:
                    notes[sym] = f"1単元({lot}株={px * lot:,.0f}円)が上限/資金余力に収まらない"
                    continue
                orders.append(Order(sym, "buy", qty, px, "claude", d["reason"]))
                held[sym] = held.get(sym, 0) + qty
                budget -= px * qty
                n_orders += 1
        return orders, notes
