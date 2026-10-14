"""Allowlisted operational view. Internal experiments/reports have no API route."""
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


VARIANTS = {"event": ("cnn", "CNN 가중", "발생확률 비중 · 14일", 3.87),
            "equal": ("cnn_equal", "CNN 기본", "동일 비중 · 14일", 3.53)}


def view(database, quotes, capital=10000.0, variant="event", history_offset=0, history_limit=80):
    key, label, sub, research_sharpe = VARIANTS[variant]
    history_offset = max(0, int(history_offset))
    history_limit = max(1, min(int(history_limit), 200))
    database = Path(database)
    if not database.exists():
        return None
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True, timeout=5) as con:
        con.execute("BEGIN")  # State and history must come from the same commit.
        r = con.execute("SELECT payload FROM state WHERE id=1").fetchone()
        if not r:
            return None
        state = json.loads(r[0])
        closed = [json.loads(p) for p, in con.execute("SELECT payload FROM closed ORDER BY id DESC LIMIT 20")]
        hr = con.execute("SELECT payload FROM health WHERE id=1").fetchone()
        status = json.loads(hr[0]) if hr else {}
        fill_total = con.execute("SELECT COALESCE(SUM(json_array_length(payload,'$.cohort.positions')),0) FROM decisions").fetchone()[0]
        fill_rows = con.execute("""SELECT d.ts,d.generated_ms,
            COALESCE(json_extract(d.payload,'$.origin'),json_extract(d.payload,'$.cohort.origin'),'forward'),p.value
            FROM decisions d,json_each(d.payload,'$.cohort.positions') p
            ORDER BY d.ts DESC,CAST(p.key AS INTEGER) LIMIT ? OFFSET ?""",
            (history_limit,history_offset)).fetchall()
    book = state["books"][variant]
    eq = book["net"]
    active = state["cohorts"]
    n = len(active)
    grouped = defaultdict(list)
    for c in active:
        for p in c["positions"]:
            grouped[(p["base"], p["side"])].append((p, c))
    q = quotes(sorted({base for base, side in grouped}))
    positions, delta = [], 0.0
    valid_quotes = []
    for (base, side), items in grouped.items():
        sign = 1 if side == "long" else -1
        weight = sum(p[variant] for p, _ in items) / n
        units = sum(p[variant] / p["entry"] for p, _ in items) / n
        entry = weight / units
        mark = items[0][0].get("mark", items[0][0]["entry"])
        quote = q.get(base) or {}
        px = quote.get("last")
        fresh = (px is not None and px > 0 and quote.get("ts", 0) >= state["last_ts"] - 60_000
                 and quote.get("ts", 0) >= int(time.time()*1000) - 20*60_000)
        if fresh:
            delta += sign * eq * units * (px - mark)
            valid_quotes.append(quote["ts"])
        else:
            px = None
        ret = sign * (px / entry - 1) if px is not None else None
        positions.append({"base": base, "side": side, "entry": entry,
            "entry_ts": min(c["id"] for _, c in items), "exit_ts": min(c["exit_ts"] for _, c in items),
            "score": sum(p[variant] * p["score"] for p, _ in items) / sum(p[variant] for p, _ in items),
            "size": eq * units * (px if px is not None else mark), "last": px, "chg24": quote.get("chg24"),
            "upnl_pct": None if ret is None else ret * 100,
            "upnl_usd": None if ret is None else capital * eq * weight * ret,
            "notional_usd": capital * eq * units * (px if px is not None else mark)})
    positions.sort(key=lambda p: -abs(p["score"]))
    # Interpolate the paper's fixed fee between recorded 4h closes, using the
    # oldest displayed quote. Never count a fee twice at the next close.
    fraction = max(0.0, min(1.0, (min(valid_quotes, default=state["last_ts"]) - state["last_ts"]) / state["step_ms"]))
    accrued = eq * 2 * state["cost"] / state["hold"] * fraction if n else 0.0
    complete = len(valid_quotes) == len(positions)
    mtm = eq + delta - accrued if complete else None
    trades = []
    for cohort in closed:
        for p in cohort["positions"]:
            sign = 1 if p["side"] == "long" else -1
            ret = sign * (p["mark"] / p["entry"] - 1) - 2 * state["cost"]
            trades.append({"base": p["base"], "side": p["side"], "entry": p["entry"],
                           "exit_px": p["mark"], "exit_ts": cohort["exit_ts"], "ret": ret,
                           "origin": cohort.get("origin", "forward")})
    fills = []
    for ts, generated, origin, payload in fill_rows:
        p = json.loads(payload)
        fills.append({"ts": ts, "base": p["base"], "side": p["side"], "entry": p["entry"],
                      "score": p["score"], "cohort_weight": p[variant], "origin": origin,
                      "generated_ms": generated})
    returns = [p["upnl_pct"] for p in positions if p["upnl_pct"] is not None]
    signal = state.get("last_signal", {})
    history = state.get("history_import")
    history_note = ""
    if history:
        start = datetime.fromtimestamp(history["replay_start_ms"]/1000,timezone.utc).astimezone(ZoneInfo('Asia/Seoul'))
        cutover = datetime.fromtimestamp(history["forward_since_ms"]/1000,timezone.utc).astimezone(ZoneInfo('Asia/Seoul'))
        history_note = f"{start:%m/%d %H:%M}부터 소급 재계산 · {cutover:%m/%d %H:%M}부터 운영 기록 연결 (KST)"
    return {"key": key, "label": label, "sub": sub, "hold_days": 14,
            "active": True, "mode": "cohort", "equity": eq, "equity_mtm": mtm, "capital": capital,
            "equity_usd": capital * eq, "mtm_usd": None if mtm is None else capital * mtm,
            "upnl_usd": None if mtm is None else capital * (mtm - 1),
            "upnl_pct_book": None if mtm is None else (mtm - 1) * 100,
            "n_open": len(positions), "long": sum(p["side"] == "long" for p in positions),
            "short": sum(p["side"] == "short" for p in positions), "n_trades": state["n_closed_positions"],
            "started": datetime.fromtimestamp(state["started"] / 1000, timezone.utc).isoformat(),
            "avg_ret_bp": sum(t["ret"] for t in trades) / len(trades) * 10000 if trades else None,
            "hit": sum(t["ret"] > 0 for t in trades) / len(trades) if trades else None,
            "upnl_avg_pct": sum(returns) / len(returns) if returns else None, "upnl_n": len(returns),
            "gross": sum(p["size"] for p in positions), "positions": positions,
            "series": [], "fills": fills, "trades": trades[:60],
            "fills_total": fill_total, "history_offset": history_offset, "history_limit": history_limit,
            "history_note": history_note, "research_sharpe": research_sharpe,
            "valuation_basis": "fees_only", "settled_return_pct": (eq-1)*100,
            "last_signal": {"ts": signal.get("ts"), "n": signal.get("n"),
                            "opened": list(range(signal.get("n_positions", 0))),
                            "top": signal.get("top", []), "bottom": signal.get("bottom", [])},
            "valuation_note": "마감가 기준 · 왕복 수수료 0.2% 반영 · 펀딩비 제외",
            "stale": status.get("status") == "error" or int(time.time()*1000) - signal.get("ts", state["started"]) > state["step_ms"] + 45*60_000,
            }
