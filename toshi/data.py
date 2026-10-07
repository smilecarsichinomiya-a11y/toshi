from __future__ import annotations

import hashlib
import logging

import numpy as np
import pandas as pd

log = logging.getLogger("toshi.data")


class DataProvider:
    def history(self, symbol: str, days: int = 200) -> pd.DataFrame | None:
        raise NotImplementedError

    def last_price(self, symbol: str) -> float | None:
        df = self.history(symbol, 40)
        return float(df["Close"].iloc[-1]) if df is not None and len(df) else None


class YFinanceProvider(DataProvider):
    """Yahoo Finance (東証は 7203.T 形式)。遅延・欠損があり得るため参考値扱い。"""

    def __init__(self):
        self._cache: dict[str, tuple[float, pd.DataFrame]] = {}

    def history(self, symbol: str, days: int = 200) -> pd.DataFrame | None:
        import time

        import yfinance as yf

        hit = self._cache.get(symbol)
        if hit and time.time() - hit[0] < 120:
            return hit[1]
        try:
            df = yf.Ticker(f"{symbol}.T").history(period="1y", interval="1d", auto_adjust=False)
        except Exception as e:  # noqa: BLE001
            log.warning("yfinance %s failed: %s", symbol, e)
            return None
        if df is None or df.empty:
            return None
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna().tail(days)
        self._cache[symbol] = (time.time(), df)
        return df


class SyntheticProvider(DataProvider):
    """オフライン検証用の擬似株価(銘柄コードで決定的)。"""

    def history(self, symbol: str, days: int = 200) -> pd.DataFrame:
        seed = int(hashlib.md5(symbol.encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        base = 1000 + seed % 9000
        r = rng.normal(0.0004, 0.015, days)
        close = base * np.exp(np.cumsum(r))
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
        hi = close * (1 + np.abs(rng.normal(0, 0.006, days)))
        lo = close * (1 - np.abs(rng.normal(0, 0.006, days)))
        op = np.r_[close[0], close[:-1]]
        vol = rng.integers(500_000, 3_000_000, days)
        return pd.DataFrame({"Open": op, "High": hi, "Low": lo, "Close": close, "Volume": vol}, index=idx)


def make_provider(kind: str) -> DataProvider:
    return SyntheticProvider() if kind == "synthetic" else YFinanceProvider()
