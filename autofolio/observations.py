"""Minute NAV observations independent of model training; never submits orders."""
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import store


def initialize():
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS paper_observations(
            user_id INTEGER NOT NULL,market TEXT NOT NULL,strategy_id TEXT NOT NULL,
            ts INTEGER NOT NULL,nav REAL NOT NULL,initial REAL NOT NULL,
            source TEXT NOT NULL,stale INTEGER NOT NULL,source_ts REAL,
            PRIMARY KEY(user_id,market,strategy_id,ts))''')


def stock_quotes(symbols, market):
    """Use indexed per-symbol queries; do not scan or write collector databases."""
    if market != 'kr':
        return {}  # US history is a delayed archive, not an intraday quote feed.
    path = Path.home() / 'vault/CryptoBars/data/KRX/bars.db'
    result = {}
    if not path.exists(): return result
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        for symbol in symbols:
            row = db.execute('SELECT ts,close FROM bars WHERE code=? ORDER BY ts DESC LIMIT 1',
                             (symbol.split('@')[0],)).fetchone()
            if not row: continue
            try:
                stamp = datetime.strptime(row[0], '%Y%m%d%H%M').replace(tzinfo=ZoneInfo('Asia/Seoul')).timestamp()
                px = float(row[1])
                if math.isfinite(px) and px > 0: result[symbol] = dict(ts=stamp, last=px)
            except (ValueError, TypeError): continue
    return result


def mark(book, quotes, now, max_age=1200):
    """Never publish a mixed fresh/stale portfolio as a complete current NAV."""
    nav = float(book['cash']);stamps=[]
    for symbol, holding in book['holdings'].items():
        quote = quotes.get(symbol)
        if not quote or not 0 <= now - quote['ts'] <= max_age:
            return None
        nav += float(holding['qty']) * quote['last'];stamps.append(quote['ts'])
    if not math.isfinite(nav) or nav <= 0:return None
    return dict(nav=nav, source_ts=min(stamps,default=now))


def record(uid, market, strategy, nav, initial, source, source_ts, now=None):
    now = time.time() if now is None else now
    if not all(math.isfinite(float(v)) and float(v)>0 for v in (nav, initial)): return
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO paper_observations VALUES(?,?,?,?,?,?,?,?,?)',
                   (uid,market,strategy,int(now)//60*60,nav,initial,source,0,source_ts))


def tick(now=None):
    now = time.time() if now is None else now
    initialize();count=0
    from .public_routes import owner_id
    uid=owner_id()
    if uid is None:return 0
    with store.connect() as db:
        rows=db.execute('''SELECT p.market,p.body,d.strategy_id FROM paper_books p
            JOIN model_deployments d ON d.id=p.deployment
            WHERE p.user_id=?''',(uid,)).fetchall()
    for row in rows:
        market=row['market'];book=json.loads(row['body'])
        local=datetime.fromtimestamp(now,ZoneInfo('Asia/Seoul'))
        if market=='kr' and not (local.weekday()<5 and (9,0)<=(local.hour,local.minute)<=(15,35)):
            continue
        if market!='kr':continue
        value=mark(book,stock_quotes(book['holdings'],market),now)
        if value:
            record(uid,market,book.get('minute_strategy_id') or row['strategy_id'],value['nav'],book['initial'],'CryptoBars KRX 1m',value['source_ts'],now);count+=1
    from integrations.crypto.app import book_view,BOOKS
    from integrations.crypto.market import quotes
    for key in BOOKS:
        try:
            book=book_view(key,history_limit=1)
            nav=book.get('mtm_usd')
            if nav is None or book.get('stale'):continue
            symbols={p['base'] for p in book.get('positions',[])}
            current=quotes(sorted(symbols))
            if any(s not in current or not 0<=now-current[s]['ts']/1000<=1200 for s in symbols):continue
            stamp=min((current[s]['ts']/1000 for s in symbols),default=now)
            record(uid,'crypto',key,nav,book['capital'],'CryptoBars crypto 1m',stamp,now);count+=1
        except (OSError,ValueError,KeyError,sqlite3.Error):continue
    store.set_setting('observations_heartbeat',dict(timestamp=now,recorded=count))
    return count


def main():
    import fcntl
    initialize()
    with (store.DATA/'observations.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        while True:
            try:tick()
            except Exception as exc:store.event('observations_error',dict(error=type(exc).__name__))
            time.sleep(max(1,60-time.time()%60))


if __name__=='__main__':main()
