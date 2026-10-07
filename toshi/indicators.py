from __future__ import annotations

import pandas as pd


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(100.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def features(df: pd.DataFrame) -> dict | None:
    """日足 DataFrame(Open/High/Low/Close/Volume) から判断材料を作る。"""
    if df is None or len(df) < 30:
        return None
    c = df["Close"]
    last = float(c.iloc[-1])

    def sma(n: int):
        return float(c.tail(n).mean()) if len(c) >= n else None

    def ret(n: int):
        return round(float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 2) if len(c) > n else None

    vol_ratio = None
    if "Volume" in df and len(df) >= 21 and df["Volume"].tail(20).mean() > 0:
        vol_ratio = round(float(df["Volume"].iloc[-1] / df["Volume"].tail(20).mean()), 2)
    a = float(atr(df).iloc[-1])
    return {
        "price": round(last, 1),
        "sma5": round(sma(5), 1),
        "sma25": round(sma(25), 1),
        "sma75": round(sma(75), 1) if sma(75) else None,
        "rsi14": round(float(rsi(c).iloc[-1]), 1),
        "atr14_pct": round(a / last * 100, 2),
        "ret_5d_pct": ret(5),
        "ret_20d_pct": ret(20),
        "high_20d": round(float(df["High"].tail(20).max()), 1),
        "low_20d": round(float(df["Low"].tail(20).min()), 1),
        "volume_ratio_20d": vol_ratio,
    }
