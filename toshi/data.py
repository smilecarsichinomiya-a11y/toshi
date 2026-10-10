from __future__ import annotations

import hashlib
import logging
import time

import numpy as np
import pandas as pd

from .db import JST, now

log = logging.getLogger("toshi.data")


def naive_daily(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """日足の index を、時刻・タイムゾーンなしの日付にそろえる。"""
    if df is None or df.empty:
        return None
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert(JST).tz_localize(None)
    out = df.copy()
    out.index = idx.normalize()
    return out[~out.index.duplicated(keep="last")].sort_index()


class DataProvider:
    def history(self, symbol: str, days: int = 200) -> pd.DataFrame | None:
        """日足"""
        raise NotImplementedError

    def intraday(self, symbol: str) -> pd.DataFrame | None:
        """5分足(直近数営業日, JST tz-aware index)"""
        raise NotImplementedError

    def daily(self, symbol: str, years: int = 3) -> pd.DataFrame | None:
        """日足(index は日付のみ)。シグナル判定・バックテスト用。"""
        return naive_daily(self.history(symbol, years * 250))

    def last_price(self, symbol: str) -> float | None:
        df = self.intraday(symbol)
        if df is not None and len(df):
            return float(df["Close"].iloc[-1])
        df = self.history(symbol, 40)
        return float(df["Close"].iloc[-1]) if df is not None and len(df) else None

    def daily_many(self, symbols: list[str], years: int = 3) -> dict[str, pd.DataFrame]:
        """多数の銘柄の日足をまとめて取得する。取得できなかった銘柄は含めない。"""
        out = {}
        for s in symbols:
            try:
                df = self.daily(s, years)
            except Exception as e:  # noqa: BLE001
                log.warning("daily %s failed: %s", s, e)
                continue
            if df is not None and len(df):
                out[s] = df
        return out

    def day_return(self, symbol: str, date: str) -> float | None:
        """指定日の始値→終値の騰落率(%)。ベンチマーク比較用。取得不能なら None。"""
        df = self.intraday(symbol)
        if df is None or df.empty:
            return None
        d = df[[str(x) == date for x in df.index.date]]
        if len(d) < 3:
            return None
        return round((float(d["Close"].iloc[-1]) / float(d["Open"].iloc[0]) - 1) * 100, 2)


class YFinanceProvider(DataProvider):
    """Yahoo Finance (東証は 7203.T)。遅延・欠損があり得る参考値。実運用は RSS 等のリアルタイム値へ。"""

    def __init__(self):
        self._cache: dict[tuple, tuple[float, pd.DataFrame]] = {}

    def _get(self, symbol: str, period: str, interval: str, ttl: int) -> pd.DataFrame | None:
        import yfinance as yf

        key = (symbol, interval, period)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            df = yf.Ticker(f"{symbol}.T").history(period=period, interval=interval, auto_adjust=False)
        except Exception as e:  # noqa: BLE001
            log.warning("yfinance %s failed: %s", symbol, e)
            return hit[1] if hit else None
        if df is None or df.empty:
            return hit[1] if hit else None
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        if interval != "1d":
            df.index = df.index.tz_convert(JST)
        self._cache[key] = (time.time(), df)
        return df

    def history(self, symbol: str, days: int = 200):
        df = self._get(symbol, "1y", "1d", 1800)
        return df.tail(days) if df is not None else None

    def intraday(self, symbol: str):
        return self._get(symbol, "5d", "5m", 60)

    def daily(self, symbol: str, years: int = 3):
        return naive_daily(self._get(symbol, f"{years}y", "1d", 600))

    def daily_many(self, symbols: list[str], years: int = 3, chunk: int = 100) -> dict[str, pd.DataFrame]:
        """yf.download で100銘柄ずつまとめて取得する(1銘柄ずつより圧倒的に速い)。失敗した銘柄は含めない。"""
        import yfinance as yf

        period, out, todo = f"{years}y", {}, []
        for s in symbols:
            hit = self._cache.get((s, "1d", period))
            if hit and time.time() - hit[0] < 600:
                out[s] = naive_daily(hit[1])
            else:
                todo.append(s)
        cols = ["Open", "High", "Low", "Close", "Volume"]
        for i in range(0, len(todo), chunk):
            part = todo[i:i + chunk]
            try:
                raw = yf.download([f"{s}.T" for s in part], period=period, interval="1d", auto_adjust=False,
                                  group_by="ticker", threads=True, progress=False)
            except Exception as e:  # noqa: BLE001
                log.warning("yfinance download failed: %s", e)
                continue
            if raw is None or raw.empty:
                continue
            for s in part:
                try:
                    sub = raw[f"{s}.T"] if isinstance(raw.columns, pd.MultiIndex) else raw
                    df = sub[cols].dropna()
                except (KeyError, TypeError):
                    continue
                if df.empty:
                    continue
                self._cache[(s, "1d", period)] = (time.time(), df)
                out[s] = naive_daily(df)
        return out


class SyntheticProvider(DataProvider):
    """オフライン検証用の擬似株価(銘柄コードで決定的)。"""

    def history(self, symbol: str, days: int = 200) -> pd.DataFrame:
        seed = int(hashlib.md5(symbol.encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        base = 1000 + seed % 9000
        close = base * np.exp(np.cumsum(rng.normal(0.0004, 0.015, days)))
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
        hi = close * (1 + np.abs(rng.normal(0, 0.006, days)))
        lo = close * (1 - np.abs(rng.normal(0, 0.006, days)))
        op = np.r_[close[0], close[:-1]]
        return pd.DataFrame({"Open": op, "High": hi, "Low": lo, "Close": close,
                             "Volume": rng.integers(500_000, 3_000_000, days)}, index=idx)

    def intraday(self, symbol: str) -> pd.DataFrame:
        seed = int(hashlib.md5(("i" + symbol).encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        base = 1000 + seed % 9000
        stamps = []
        for d in pd.bdate_range(end=pd.Timestamp(now().date()), periods=5):
            for start, end in (("09:00", "11:30"), ("12:30", "15:30")):
                stamps += list(pd.date_range(f"{d.date()} {start}", f"{d.date()} {end}", freq="5min",
                                             inclusive="left", tz=JST))
        n = len(stamps)
        close = base * np.exp(np.cumsum(rng.normal(0, 0.0015, n)))
        op = np.r_[close[0], close[:-1]]
        hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.0007, n)))
        lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.0007, n)))
        return pd.DataFrame({"Open": op, "High": hi, "Low": lo, "Close": close,
                             "Volume": rng.integers(20_000, 200_000, n)}, index=pd.DatetimeIndex(stamps))


def make_provider(kind: str) -> DataProvider:
    return SyntheticProvider() if kind == "synthetic" else YFinanceProvider()
