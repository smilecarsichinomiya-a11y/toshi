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


def _pct(a: float, b: float) -> float:
    return round((a / b - 1) * 100, 2)


def intraday_features(bars: pd.DataFrame | None, daily: pd.DataFrame | None) -> dict | None:
    """5分足(複数日)と日足から、デイトレ判断用の指標を作る。当日の足が3本未満なら None。"""
    if bars is None or len(bars) < 20:
        return None
    day = bars.index[-1].date()
    today = bars[[d == day for d in bars.index.date]]
    prev = bars[[d != day for d in bars.index.date]]
    if len(today) < 3:
        return None
    c = bars["Close"]
    last = float(today["Close"].iloc[-1])
    if len(prev):
        prev_close = float(prev["Close"].iloc[-1])
    elif daily is not None and len(daily) > 1:
        prev_close = float(daily["Close"].iloc[-2])
    else:
        prev_close = float(today["Open"].iloc[0])
    tp = (today["High"] + today["Low"] + today["Close"]) / 3
    vol = today["Volume"].astype(float)
    vwap = float((tp * vol).sum() / vol.sum()) if vol.sum() > 0 else last
    orng = today.head(3)  # 寄り付き後15分のレンジ
    base_vol = float(prev["Volume"].median()) if len(prev) else float(vol.median())
    recent_vol = float(vol.tail(3).mean())
    sma9, sma21 = float(c.tail(9).mean()), float(c.tail(21).mean())
    a = float(atr(bars).iloc[-1])
    out = {
        "price": round(last, 1),
        "prev_close": round(prev_close, 1),
        "gap_pct": _pct(float(today["Open"].iloc[0]), prev_close),
        "day_change_pct": _pct(last, prev_close),
        "day_high": round(float(today["High"].max()), 1),
        "day_low": round(float(today["Low"].min()), 1),
        "vwap": round(vwap, 1),
        "vs_vwap_pct": _pct(last, vwap),
        "opening_range_high": round(float(orng["High"].max()), 1),
        "opening_range_low": round(float(orng["Low"].min()), 1),
        "rsi14_5m": round(float(rsi(c).iloc[-1]), 1),
        "sma9_5m": round(sma9, 1),
        "sma21_5m": round(sma21, 1),
        "atr_5m_pct": round(a / last * 100, 3),
        "volume_ratio": round(recent_vol / base_vol, 2) if base_vol > 0 else None,
        "ret_last_30m_pct": _pct(last, float(today["Close"].iloc[-7])) if len(today) > 6 else None,
        "bars_today": len(today),
    }
    if daily is not None and len(daily) >= 25:
        dc = daily["Close"]
        out["daily_trend"] = "up" if dc.tail(25).mean() > dc.tail(75).mean() else "down"
        out["daily_ret_5d_pct"] = _pct(float(dc.iloc[-1]), float(dc.iloc[-6]))
        out["daily_rsi14"] = round(float(rsi(dc).iloc[-1]), 1)
    return out
