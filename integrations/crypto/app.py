"""AutoCrypto — HYFE_QTPA 이미지 CNN(heatf) 암호화폐 롱숏 페이퍼 북 거래창. 읽기 전용.
틱은 HYFE_QTPA 의 cron(hyfe/live.py, 매시 12분, 4h 마감마다 판단)이 돌리고 여기서는 그 산출물과 CryptoBars 시세만 읽는다:
  work/live/state.json["dir"][book]      장부 상태(순자산·보유)
  work/live/dir_<book>_signals.jsonl     틱별 신호(순자산·신규·상하위 점수)
  work/live/dir_<book>_trades.jsonl      청산 기록(보유 기간 뒤 생김)
  work/live/alarms.jsonl · tick.log      급등·급락 경보 · 판단 로그
실행: python3 -m uvicorn web.app:app --host 127.0.0.1 --port 8996  (autocrypto.service, 공개 autocrypto.ai-ve.uk)
"""
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import market, paper

LIVE = Path(os.environ.get("HYFE_LIVE_DIR", "/home/arcosium/projects/HYFE_QTPA/work/live"))
MODELS = Path(os.environ.get("HYFE_MODELS_DIR", "/home/arcosium/projects/HYFE_QTPA/work/models"))
PAPER_DB = Path(os.environ.get("HYFE_PAPER_DB", "/home/arcosium/projects/HYFE_QTPA/work/paper_trade/paper.sqlite3"))
BOOKS = {"cnn": {"label": "CNN 가중", "sub": "발생확률 비중 · 14일", "hold_days": 14},
         "cnn_equal": {"label": "CNN 기본", "sub": "동일 비중 · 14일", "hold_days": 14},
         "v2": {"label": "GBM v2", "sub": "대조군 · 14일", "hold_days": 14},
         "v1": {"label": "GBM v1", "sub": "대조군 · 7일", "hold_days": 7},
         "v2d": {"label": "GBM 1d", "sub": "대조군 · 20일", "hold_days": 20}}
TAPE = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TRX", "HYPE", "SUI"]
CAPITAL = 10_000.0   # 표시용 초기 자본(달러). 장부 상태의 순자산 1.0 = $10,000, 포지션 size = 자본 비중
RESEARCH = {"window": "2023-01 ~ 2026-05", "pooled": 3.53, "pooled_p": "< 0.0001", "pooled_mdd": -4.2, "holdout": 2.49, "holdout_p": 0.019, "funding": 2.95, "gbm": 2.18, "mlp": "2.31 / 2.05",
            "folds": "5.59 · 4.95 · 2.92 · 2.28", "rule": "4h 마감 · 상·하위 10% · 14일 보유 · 달러 중립 · 왕복 0.2%"}
STATIC = Path(__file__).resolve().parent / "static"
app = FastAPI(title="AutoCrypto", docs_url=None, redoc_url=None)


def _jsonl(path, limit=5000):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()[-limit:]
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def _state():
    try:
        return json.loads((LIVE / "state.json").read_text()).get("dir") or {}
    except (OSError, ValueError):
        return {}


def _tick():
    last = None
    try:
        for ln in reversed((LIVE / "tick.log").read_text(encoding="utf-8").splitlines()[-60:]):
            if ln[:4].isdigit() and " UTC" in ln:
                last = ln.split(" UTC")[0].strip(); break
    except OSError:
        pass
    now = int(time.time() * 1000); step = 4 * 3_600_000
    due = now // step * step + 12 * 60_000
    return {"last": last, "next_ms": due if now < due else due + step, "now_ms": now}


def book_view(key, with_quotes=True, history_offset=0, history_limit=80):
    if key not in BOOKS:
        raise HTTPException(404, "없는 장부")
    if key in ("cnn", "cnn_equal"):
        current = paper.view(PAPER_DB, market.quotes, CAPITAL,
                             variant="event" if key == "cnn" else "equal",
                             history_offset=history_offset, history_limit=history_limit)
        if current is not None:
            return current
        if key == "cnn_equal":
            raise HTTPException(503, "CNN 비교 장부 준비 중")
    st = _state(); b = st.get(key) or {}; meta = BOOKS[key]
    pos = b.get("positions") or {}
    q = market.quotes(list(pos)) if (with_quotes and pos) else {}
    positions = []; upnl_frac = 0.0
    for k, v in pos.items():
        last = (q.get(k) or {}).get("last"); sign = 1 if v.get("side") == "long" else -1; size = float(v.get("size") or 0)
        frac = ((last / v["entry"] - 1) * sign) if (last and v.get("entry")) else None      # 포지션 자체 수익률
        if frac is not None:
            upnl_frac += size * frac                                                          # 장부 자본 대비 기여(size = 자본 비중)
        positions.append({"base": k, **v, "last": last, "chg24": (q.get(k) or {}).get("chg24"), "upnl_pct": None if frac is None else frac * 100,
                          "notional_usd": CAPITAL * size, "upnl_usd": None if frac is None else CAPITAL * size * frac})
    positions.sort(key=lambda x: -abs(x.get("score") or 0))
    upnls = [p["upnl_pct"] for p in positions if p["upnl_pct"] is not None]
    eq = b.get("equity"); eq_mtm = (eq + upnl_frac) if eq is not None else None                # 평가 순자산 = 실현 순자산 + 미실현 기여
    sig = _jsonl(LIVE / f"dir_{key}_signals.jsonl", 3000)
    series = [{"ts": s.get("ts"), "equity": s.get("equity"), "n_open": s.get("n_open")} for s in sig if s.get("ts") and s.get("equity") is not None]
    fills = []
    for s in reversed(sig[-40:]):
        for o in (s.get("opened") or []):
            side, base = (o.split(":", 1) + [""])[:2]
            p = pos.get(base) or {}
            fills.append({"ts": s.get("ts"), "base": base, "side": "long" if side == "l" else "short", "entry": p.get("entry"), "score": p.get("score")})
    trades = list(reversed(_jsonl(LIVE / f"dir_{key}_trades.jsonl", 500)))
    rets = [t.get("ret") for t in trades if isinstance(t.get("ret"), (int, float))]
    last = sig[-1] if sig else {}
    return {"key": key, **meta, "active": bool(b), "equity": eq, "equity_mtm": eq_mtm, "capital": CAPITAL, "equity_usd": None if eq is None else eq * CAPITAL, "mtm_usd": None if eq_mtm is None else eq_mtm * CAPITAL,
            "upnl_usd": upnl_frac * CAPITAL, "upnl_pct_book": upnl_frac * 100, "n_open": len(pos), "long": sum(1 for v in pos.values() if v.get("side") == "long"),
            "short": sum(1 for v in pos.values() if v.get("side") == "short"), "n_trades": b.get("n_trades", 0), "started": b.get("started"),
            "avg_ret_bp": (sum(rets) / len(rets) * 1e4) if rets else None, "hit": (sum(1 for r in rets if r > 0) / len(rets)) if rets else None,
            "upnl_avg_pct": (sum(upnls) / len(upnls)) if upnls else None, "upnl_n": len(upnls), "gross": round(sum(float(v.get("size") or 0) for v in pos.values()), 4),
            "positions": positions, "series": series, "fills": fills[:80], "trades": trades[:60],
            "last_signal": {"ts": last.get("ts"), "n": last.get("n"), "opened": last.get("opened"), "top": last.get("top"), "bottom": last.get("bottom")}}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/summary")
def summary():
    books = []
    for k in BOOKS:
        v = book_view(k, with_quotes=(k == "cnn")); [v.pop(x) for x in ("positions", "series", "fills", "trades")]; books.append(v)
    models = sorted(p.stem for p in MODELS.glob("final_i1_heatf_4h_W60_H84_sd*.pt")) if MODELS.exists() else []
    al = _jsonl(LIVE / "alarms.jsonl", 3000); cut = int(time.time() * 1000) - 86_400_000
    recent = [a for a in al if (a.get("ts") or 0) >= cut]
    return {"tick": _tick(), "books": books, "n_models": len(models), "research": RESEARCH, "tape": market.quotes(TAPE),
            "alarms_24h": {"up": sum(1 for a in recent if a.get("kind") == "up"), "dn": sum(1 for a in recent if a.get("kind") == "dn")}}


@app.get("/api/book/{key}")
def book(key: str, history_offset: int = 0, history_limit: int = 80):
    return book_view(key, history_offset=max(0,history_offset), history_limit=max(1,min(history_limit,200)))


@app.get("/api/candles/{base}")
def candles(base: str, tf: int = 60, n: int = 160):
    if tf not in (15, 60, 240, 1440) or not base.replace("1000", "").isalnum() or len(base) > 16:
        raise HTTPException(400, "tf 는 15/60/240/1440, base 는 티커")
    c = market.candles(base, tf, max(30, min(n, 400))); q = market.quotes([base]).get(base)
    return {"base": base, "tf": tf, "candles": c, "quote": q}


@app.get("/api/alarms")
def alarms(limit: int = 40):
    return {"alarms": list(reversed(_jsonl(LIVE / "alarms.jsonl", max(1, min(limit, 500)))))}


@app.get("/api/log")
def log(limit: int = 30):
    try:
        lines = [l for l in (LIVE / "tick.log").read_text(encoding="utf-8").splitlines() if l.strip()][-max(1, min(limit, 200)):]
    except OSError:
        lines = []
    return {"lines": list(reversed(lines))}



