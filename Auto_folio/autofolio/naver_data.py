from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _num(text: str) -> float | None:
    m = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except Exception:
        return None


def fetch_daily_ohlcv(code: str, *, pages: int = 3, timeout: float = 8.0) -> list[dict[str, Any]]:
    code = str(code).strip().zfill(6)
    rows: list[dict[str, Any]] = []
    for page in range(1, max(1, int(pages)) + 1):
        url = f"https://finance.naver.com/item/sise_day.nhn?code={code}&page={page}"
        res = requests.get(url, headers=_HEADERS, timeout=timeout)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "lxml")
        valid = 0
        for tr in soup.select("table.type2 tr"):
            cols = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cols) != 7 or not cols[0]:
                continue
            try:
                rows.append({
                    "date": datetime.strptime(cols[0], "%Y.%m.%d").date().isoformat(),
                    "close": int(cols[1].replace(",", "")),
                    "open": int(cols[3].replace(",", "")),
                    "high": int(cols[4].replace(",", "")),
                    "low": int(cols[5].replace(",", "")),
                    "volume": int(cols[6].replace(",", "")),
                })
                valid += 1
            except Exception:
                continue
        if valid == 0 and page > 1:
            break
    dedup = {r["date"]: r for r in rows}
    return [dedup[k] for k in sorted(dedup)]


def fetch_security_meta(code: str, *, stored: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
    code = str(code).strip().zfill(6)
    meta: dict[str, Any] = dict(stored or {})
    meta["ticker"] = code
    daily = fetch_daily_ohlcv(code, pages=2, timeout=timeout)
    if daily:
        recent = daily[-5:]
        meta["last_price"] = float(daily[-1]["close"])
        meta["listed_business_days"] = max(int(meta.get("listed_business_days") or 0), len(daily))
        if recent:
            meta["avg_5d_trading_value_krw"] = sum(float(r["close"]) * float(r["volume"]) for r in recent) / len(recent)

    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        res = requests.get(url, headers=_HEADERS, timeout=timeout)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "lxml")
        text = soup.get_text(" ", strip=True)
        name = soup.select_one("div.wrap_company h2 a") or soup.select_one("div.wrap_company h2")
        if name and not meta.get("name"):
            meta["name"] = name.get_text(" ", strip=True)
        if not meta.get("market"):
            if "코스닥" in text or "KOSDAQ" in text.upper():
                meta["market"] = "KOSDAQ"
            elif "코스피" in text or "KOSPI" in text.upper():
                meta["market"] = "KOSPI"
        if not meta.get("market_cap_krw"):
            cap = _extract_market_cap_krw(soup, text)
            if cap:
                meta["market_cap_krw"] = cap
        flags = list(meta.get("flags") or []) if not isinstance(meta.get("flags"), str) else [x.strip() for x in meta.get("flags", "").split(",") if x.strip()]
        for word in ("투자주의", "투자경고", "투자위험", "투자주의환기", "관리종목", "거래정지"):
            if word in text and word not in flags:
                flags.append(word)
        meta["flags"] = flags
        if meta.get("is_common_stock") is None:
            nm = str(meta.get("name") or "")
            meta["is_common_stock"] = not any(x in nm.upper() for x in ("ETF", "ETN", "스팩", "우B")) and not nm.endswith("우")
    except Exception:
        pass
    return meta


def _extract_market_cap_krw(soup: BeautifulSoup, text: str) -> float | None:
    for th in soup.find_all(["th", "dt"]):
        label = th.get_text(" ", strip=True)
        if "시가총액" not in label:
            continue
        target = th.find_next(["td", "dd"])
        if target:
            raw = target.get_text(" ", strip=True)
            value = _num(raw)
            if value:
                return value * 100_000_000
    m = re.search(r"시가총액\s*([\d,]+)\s*억원", text)
    if m:
        return float(m.group(1).replace(",", "")) * 100_000_000
    return None
