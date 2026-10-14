"""시세 — CryptoBars 1분봉(버퍼 date=*/ + history/base=*/part-YYYY-MM.parquet)에서 현재가·24h 변동·캔들. 60초 캐시, 읽기 전용."""
import glob
import os
import threading
import time

import numpy as np
import pandas as pd

BUF = os.environ.get("CRYPTOBARS_BUF", "/home/arcosium/projects/CryptoBars/data/bars")
HIST = os.environ.get("CRYPTOBARS_HIST", "/home/arcosium/projects/CryptoBars/data/history")
COLS = ["ts", "open", "high", "low", "close", "volume", "quote_volume"]
_lock = threading.Lock(); _cache = {"at": 0.0, "buf": None}


def buffer(days=2, ttl=60):
    """최근 days 일 버퍼 전부 → {base: DataFrame(ts 정렬)}. 60초 캐시."""
    with _lock:
        if _cache["buf"] is not None and time.time() - _cache["at"] < ttl:
            return _cache["buf"]
        fs = []
        for d in sorted(glob.glob(f"{BUF}/date=*"))[-days:]:
            fs += sorted(glob.glob(f"{d}/*.parquet"))
        out = {}
        if fs:
            df = pd.concat(pd.read_parquet(f, columns=["base"] + COLS) for f in fs)
            df = df.drop_duplicates(["base", "ts"]).sort_values("ts")
            out = {b: g.drop(columns="base").reset_index(drop=True) for b, g in df.groupby("base")}
        _cache.update(at=time.time(), buf=out)
        return out


def quotes(bases):
    """{base: {last, ts, chg24(%), high24, low24, qv24}} — 버퍼(≈2일)에서 24h 창."""
    buf = buffer(); out = {}
    for b in bases:
        g = buf.get(b)
        if g is None or g.empty:
            continue
        last_ts = int(g.ts.iloc[-1]); w = g[g.ts > last_ts - 86_400_000]
        ref = g[g.ts <= last_ts - 86_400_000]
        base_px = float(ref.close.iloc[-1]) if len(ref) else float(w.open.iloc[0])
        last = float(g.close.iloc[-1])
        out[b] = {"last": last, "ts": last_ts, "chg24": (last / base_px - 1) * 100 if base_px else None, "high24": float(w.high.max()), "low24": float(w.low.min()),
                  "qv24": float(w.quote_volume.fillna(w.close * w.volume).sum())}
    return out


def candles(base, tf=60, n=150):
    """tf 분 봉 n 개 — history 최근 2개월 + 버퍼. [{t, o, h, l, c, v}]"""
    now = pd.Timestamp.utcnow(); parts = []
    for i in range(2, -1, -1):
        p = f"{HIST}/base={base}/part-{(now - pd.DateOffset(months=i)).strftime('%Y-%m')}.parquet"
        if os.path.exists(p):
            parts.append(pd.read_parquet(p, columns=COLS))
    g = buffer().get(base)
    if g is not None:
        parts.append(g[COLS])
    if not parts:
        return []
    df = pd.concat(parts).drop_duplicates("ts").sort_values("ts")
    df["quote_volume"] = df.quote_volume.fillna(df.close * df.volume)
    step = tf * 60_000; df["bin"] = (df.ts // step) * step
    a = df.groupby("bin").agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"), v=("quote_volume", "sum")).tail(n)
    return [{"t": int(t), "o": float(r.o), "h": float(r.h), "l": float(r.l), "c": float(r.c), "v": float(r.v)} for t, r in a.iterrows()]
