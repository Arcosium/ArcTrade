"""Anonymous, read-only paper performance. Never expose research or account data."""
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request

from . import config

router = APIRouter(prefix='/api/public')
Market = Literal['kr', 'us', 'crypto', 'timefolio']
Interval = Literal['minute', 'hour', 'day']
CRYPTO_TITLES = {'cnn': 'CNN 가중', 'cnn_equal': 'CNN 기본', 'v1': 'GBM v1',
                 'v2': 'GBM v2', 'v2d': 'GBM 1d'}
TITLES = {'kr': '한국주식 운용 전략', 'us': '미국주식 운용 전략', 'crypto': '크립토 운용 전략', 'timefolio': '타임폴리오 전략 페이퍼'}


@contextmanager
def readonly(path):
    # Unlike store.connect/auth.connect this does not create directories, tables,
    # sessions or change journal mode. All public performance reads are SELECTs.
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def owner_id():
    directory = Path(os.environ.get('QUANTINSIGHT_AUTH_DIR', Path.home() / 'vault/QuantInSight/auth'))
    try:
        with readonly(directory / 'accounts.sqlite3') as db:
            row = db.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        return row['id'] if row else None
    except (OSError, sqlite3.Error):
        return None


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def timestamp(value):
    numeric = number(value)
    if numeric is not None:
        return numeric / 1000 if numeric > 100_000_000_000 else numeric
    if isinstance(value, str):
        try:
            if len(value)==12 and value.isdigit():
                return datetime.strptime(value,'%Y%m%d%H%M').replace(tzinfo=ZoneInfo('Asia/Seoul')).timestamp()
            stamp = datetime.strptime(value, '%Y%m%d') if len(value) == 8 and value.isdigit() else datetime.fromisoformat(value.replace('Z', '+00:00'))
            return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
        except (ValueError, OverflowError):
            pass
    return None


def sampled(points, interval):
    """Last observed value per bucket, with original timestamp; no interpolation."""
    width = {'minute': 60, 'hour': 3600, 'day': 86400}[interval]
    buckets = {}
    for row in sorted(points, key=lambda item: item['ts']):
        buckets[int(row['ts'] // width)] = row
    return list(buckets.values())[-10000:]


def observation_points(uid, market, strategy):
    if uid is None:
        return []
    try:
        with readonly(config.DB) as db:
            columns={r['name'] for r in db.execute('PRAGMA table_info(paper_observations)')}
            source='source_ts' if 'source_ts' in columns else 'NULL AS source_ts'
            rows = db.execute('SELECT ts,nav,initial,'+source+' FROM paper_observations WHERE user_id=? AND market=? '
                              'AND strategy_id=? AND stale=0 ORDER BY ts DESC LIMIT 20000',
                              (uid, market, strategy)).fetchall()
    except (OSError, sqlite3.Error):
        return []
    points = []
    for row in rows:
        ts, nav, initial = timestamp(row['ts']), number(row['nav']), number(row['initial'])
        if ts is not None and nav is not None and initial and initial > 0 and nav >= 0:
            point={'ts': ts, 'equity': nav, 'net_return': nav / initial - 1}
            if number(row['source_ts']) is not None:point['source_ts']=number(row['source_ts'])
            points.append(point)
    return points


def freshness(result):
    points=result.get('series',[])
    last=max(points,key=lambda p:p['ts']) if points else {}
    source=last.get('source_ts',result.get('updated'))
    age=max(0,int(time.time()-source)) if source is not None else None
    result['source_age_seconds']=age
    result['delayed']=age is None or age>120
    if age is not None:
        result['message']+=f' · 시세·평가 기준 {age//60}분 전'
    return result


def safe_rows(rows, fields):
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        return []
    return [{key: (number(value) if isinstance(value, (int, float)) else value[:120])
             for key, value in row.items() if key in fields and isinstance(value, (str, int, float))
             and not isinstance(value, bool)} for row in rows[-200:] if isinstance(row, dict)]


HOLDING_FIELDS = {'code', 'name', 'qty', 'entry', 'value', 'pnl_ratio', 'side', 'price'}
TRADE_FIELDS = {'date', 'ts', 'code', 'name', 'qty', 'price', 'side', 'fee', 'ret'}


def empty(market, key, interval):
    return {'market': market, 'strategy': {'key': key, 'title': CRYPTO_TITLES.get(key, TITLES[market])},
            'connected': False, 'currency': 'KRW' if market in ('kr','timefolio') else 'USD', 'interval': interval,
            'series': [], 'equity': [], 'total_eval': None, 'cash': None, 'pnl_ratio': None,
            'updated': None, 'holdings': [], 'trades': [], 'message': '아직 공개할 페이퍼 운용 기록이 없습니다.'}


def deployed(market, interval, uid):
    result = empty(market, 'deployed', interval)
    if uid is None:
        return result
    try:
        with readonly(config.DB) as db:
            row = db.execute('SELECT body,updated,deployment FROM paper_books WHERE user_id=? AND market=?', (uid, market)).fetchone()
            if not row:
                return result
            book = json.loads(row['body'])
            strategy_id = book.get('minute_strategy_id') or 'deployed'
            try:
                deployment = db.execute('SELECT strategy_id FROM model_deployments WHERE id=? AND user_id=?', (row['deployment'], uid)).fetchone()
                if deployment and not book.get('minute_strategy_id'):
                    strategy_id = deployment['strategy_id']
            except sqlite3.Error:
                pass
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return result
    if not isinstance(book, dict):
        return result
    initial, nav = number(book.get('initial')), number(book.get('nav'))
    points = observation_points(uid, market, strategy_id)
    if not points and strategy_id != 'deployed':
        points = observation_points(uid, market, 'deployed')
    # Revalue holdings and the final graph point from the same read-only quote set.
    # This does not execute the pending daily plan or alter the settlement ledger.
    if market in ('kr','us','crypto') and initial and isinstance(book.get('holdings'),dict):
        from .observations import stock_quotes,mark
        try:
            now=time.time();quotes=stock_quotes(book['holdings'],market)
            if market=='crypto':
                from integrations.crypto.market import quotes as crypto_quotes
                quotes={k:dict(last=v['last'],ts=v['ts']/1000) for k,v in crypto_quotes(list(book['holdings'])).items()}
            value=mark(book,quotes,now) if quotes else None
            if value:
                nav=value['nav']
                points.append(dict(ts=now,equity=nav,net_return=nav/initial-1,source_ts=value['source_ts']))
                for symbol,holding in book['holdings'].items():
                    holding['value']=holding['qty']*quotes[symbol]['last']
                    if holding.get('entry'):holding['pnl_ratio']=quotes[symbol]['last']/holding['entry']-1
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):pass
    for item in book.get('observations', []) if isinstance(book.get('observations', []), list) else []:
        if not isinstance(item, dict):
            continue
        ts, value = timestamp(item.get('ts')), number(item.get('nav'))
        if ts is not None and value is not None and value >= 0 and initial and initial > 0:
            points.append({'ts': ts, 'equity': value, 'net_return': value / initial - 1})
    observed = bool(points)
    # Book writes are worker activity, not evidence of a fresh market valuation.
    daily_as_of = max((stamp for item in book.get('equity', []) if isinstance(item, dict)
                       and (stamp := timestamp(item.get('date'))) is not None), default=None)
    # Daily closing returns cannot truthfully supply a minute/hour chart.
    if interval == 'day' and initial and initial > 0:
        for item in book.get('equity', []):
            if not isinstance(item, dict):
                continue
            ts, ret = timestamp(item.get('date')), number(item.get('net_return'))
            if ts is not None and ret is not None and ret >= -1:
                points.append({'ts': ts, 'equity': initial * (1 + ret), 'net_return': ret})
    series = sampled(points, interval)
    if observed and series:
        nav = series[-1]['equity']
    minute = book.get('interval')=='minute'
    if minute:
        result['strategy']['title']={'kr':'한국주식','us':'미국주식','crypto':'크립토'}.get(market,market)+' 분봉 전략'
        result['execution_interval']='minute'
        result['execution_price']='next_observed_close'
        from .forward_validation import progress
        result['validation']=dict(minute=book.get('minute_model'),progress=progress(book,market),paper_only=True)
        result['signal']={k:book.get('minute',{}).get(k) for k in ('last_observation','last_prediction','prediction_count','ready_symbols')}
    def trade_stamp(trade):
        date=str(trade.get('date',''))
        if len(date)==12 and date.isdigit():
            zone='UTC' if market=='crypto' else 'America/New_York' if market=='us' else 'Asia/Seoul'
            return datetime.strptime(date,'%Y%m%d%H%M').replace(tzinfo=ZoneInfo(zone)).timestamp()
        return trade.get('ts')
    result.update(connected=True, total_eval=nav, cash=number(book.get('cash')),
                  pnl_ratio=nav / initial - 1 if nav is not None and initial and initial > 0 else None,
                  updated=max((p['ts'] for p in points), default=daily_as_of),
                  series=series, equity=series,
                  holdings=safe_rows(book.get('holdings', []), HOLDING_FIELDS),
                  trades=list(reversed(safe_rows([{**t,'ts':trade_stamp(t)} for t in book.get('trades', []) if isinstance(t,dict)], TRADE_FIELDS))),
                  message=('분봉 신호 · 다음 관측 종가 체결 · 분봉 전진 검증 중' if minute else '실제 관측 평가 기록' if observed else '일봉 확정 후 다음 시가 체결 모의 · 장중 관측 기록 없음'))
    return freshness(result)


def crypto(key, interval, uid):
    result = empty('crypto', key, interval)
    try:
        from integrations.crypto.app import book_view
        book = book_view(key)
    except (ImportError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, HTTPException):
        return result
    capital, nav = number(book.get('capital')), number(book.get('equity_usd'))
    points = observation_points(uid, 'crypto', key)
    latest_observation = max(points, key=lambda point: point['ts']) if points else None
    if latest_observation:
        nav = latest_observation['equity']
    # Stored native series are 4-hour observations, not minute valuations.
    if interval in {'hour', 'day'} and capital and capital > 0:
        raw = book.get('series', [])
        if key in {'cnn', 'cnn_equal'}:
            from .crypto_view import paper_series
            raw, _ = paper_series(key)
        for item in raw:
            ts, value = timestamp(item.get('ts')), number(item.get('equity'))
            if ts is not None and value is not None and value >= 0:
                points.append({'ts': ts, 'equity': value * capital, 'net_return': value - 1})
    series = sampled(points, interval)
    holdings = []
    for position in book.get('positions', []):
        holdings.append({'code': position.get('base'), 'side': position.get('side'),
                         'entry': position.get('entry'), 'price': position.get('last'),
                         'value': position.get('notional_usd'),
                         'pnl_ratio': number(position.get('upnl_pct')) / 100 if number(position.get('upnl_pct')) is not None else None})
    result.update(connected=bool(book.get('active')), total_eval=nav,
                  pnl_ratio=nav / capital - 1 if nav is not None and capital and capital > 0 else None,
                  updated=max((p['ts'] for p in points), default=None), series=series, equity=series,
                  holdings=safe_rows(holdings, HOLDING_FIELDS),
                  trades=safe_rows([{**item, 'code': item.get('base'), 'price':item.get('exit_px',item.get('entry')), 'ts': timestamp(item.get('exit_ts'))} for item in book.get('trades', [])], TRADE_FIELDS),
                  message='실제 기록 기준 · 수수료 반영 · 펀딩비 제외' if series else '이 간격의 관측 기록이 아직 없습니다.')
    return freshness(result)


@router.get('/paper/{market}')
def paper(market: Market, interval: Interval = 'minute', strategy: str | None = Query(default=None, max_length=64)):
    from .frontier import members, snapshot
    uid=owner_id()
    if strategy is None or strategy=='account':return account(market,interval,uid)
    rows=members(market,uid)
    row=next((r for r in rows if r['id']==strategy),None) if strategy else (rows[0] if rows else None)
    if row is None:
        if strategy:raise HTTPException(404,'현재 파레토 운용 목록에 없는 전략입니다.')
        return empty(market,'deployed',interval)
    return snapshot(row,uid,interval)


@router.get('/strategies')
def strategies(market: Market):
    from .frontier import members, snapshot
    uid=owner_id();items=[account(market,'minute',uid)]+[snapshot(row,uid,'minute') for row in members(market,uid)]
    return dict(market=market, selection='latest_cohort_sharpe_mdd_frontier',
                strategies=[dict(item['strategy'],market=market,connected=item['connected'],
                    pnl_ratio=item['pnl_ratio'],updated=item['updated'],message=item['message'],
                    validation=item.get('validation'),status_label='운용 계좌' if item['strategy']['key']=='account' else '샤프/MDD 파레토') for item in items])


@router.get('/briefing')
def briefing():
    from .briefing import chart, news, MARKETS, iso
    from concurrent.futures import ThreadPoolExecutor
    from .frontier import members
    with ThreadPoolExecutor(max_workers=3) as pool:charts=list(pool.map(chart,MARKETS))
    discoveries=[];uid=owner_id()
    for market in ('kr','us','crypto','timefolio'):
        for r in members(market,uid):
            discoveries.append(dict(id=r['id'],market=market,title=r['title'],net_return=r['net_return'],
                sharpe=r['sharpe'],negative_months=r['negative_months'],discovered_at=r.get('updated')))
    return dict(as_of=iso(time.time()),charts=charts,news=news(),discoveries=dict(items=discoveries[:12],
        criteria='현재 샤프/MDD 파레토 전략',message='연구 평가이며 페이퍼 운용 성과와 구분합니다.',status='ok'))


@router.get('/timefolio-account')
def timefolio_account():
    """Read an already-observed contest account; never login or order from public reads."""
    result=dict(connected=False,message='실제 대회 계좌 관측 대기',holdings=[])
    uid=owner_id()
    try:
        with readonly(config.DB) as db:
            row=db.execute('SELECT body FROM timefolio_model_state WHERE user_id=?',(uid,)).fetchone()
        state=json.loads(row['body']) if row else {}
        value=state.get('public_account',{})
        if value.get('connected'):
            result.update({k:value.get(k) for k in ('connected','updated','total_eval','cash','pnl_ratio')})
            result['holdings']=safe_rows(value.get('holdings',[]),HOLDING_FIELDS)
            result['message']='RFM 13회 실제 모의대회 계좌 · 연구 전략별 페이퍼와 별도'
            if state.get('interval')=='minute':
                result['execution_interval']='minute'
                result['execution_price']='contest_observed_fills'
                result['message']+=' · 분봉 모델 신호'
    except (OSError,sqlite3.Error,ValueError,TypeError):pass
    return result


def account(market, interval, uid):
    """The sole real paper account, not a newly seeded strategy comparison book."""
    if market=='timefolio':
        result=empty(market,'account',interval);value=timefolio_account()
        result.update(value);result['strategy']=dict(key='account',title='타임폴리오 모의매매 계좌')
        points=observation_points(uid,'timefolio','account')
        if number(value.get('updated')) is not None and number(value.get('total_eval')) is not None and number(value.get('pnl_ratio')) is not None:
            points.append(dict(ts=value['updated'],equity=value['total_eval'],net_return=value['pnl_ratio']))
        result['series']=result['equity']=sampled(points,interval)
        return result
    result=deployed(market,interval,uid)
    result['strategy']=dict(key='account',title={'kr':'한국주식 분봉 운용 계좌','us':'미국주식 운용 계좌','crypto':'크립토 운용 계좌'}[market])
    try:
        with readonly(config.DB) as db:
            dep=db.execute('SELECT status,updated,strategy_id FROM model_deployments WHERE user_id=? AND target=? ORDER BY updated DESC LIMIT 1',(uid,market+'-paper')).fetchone()
            bookrow=db.execute('SELECT body FROM paper_books WHERE user_id=? AND market=?',(uid,market)).fetchone()
        if dep:
            result['training']=dict(status=dep['status'],updated=dep['updated'],strategy_id=dep['strategy_id'])
            status={'queued':'재학습 대기','training':'재학습 중','ready':'재학습 완료','failed':'재학습 실패 · 관리자 확인 필요'}.get(dep['status'],dep['status'])
            result['message']=status+' · '+result['message']
        if bookrow:
            book=json.loads(bookrow['body'])
            if book.get('pending') and not book.get('holdings'):
                result['message']+=' · 첫 체결 대기 (신호 생성 후 다음 확정 세션 시가)'
                result['execution_status']='waiting_next_session'
    except (OSError,sqlite3.Error,ValueError,TypeError):pass
    return result


def published_request(identity):
    """Only an owner's validated published discovery can cross this boundary."""
    from .frontier import members
    uid=owner_id()
    if not any(row['id']==identity for market in ('kr','us','crypto','timefolio') for row in members(market,uid)):
        raise HTTPException(404,'현재 공개된 연구 결과가 아닙니다.')
    return Request({'type':'http','method':'GET','path':'/','headers':[], 'state':{'user':{'id':uid,'role':'admin'}}})


@router.get('/backtest/{identity}')
def backtest(identity:str,phase:int=Query(0,ge=0),months:int=Query(36,ge=36,le=36),symbol:str=Query('',max_length=12),scope:str=Query('os',pattern='^(is|os|ros|all)$')):
    from .app import detail
    result=detail(identity,published_request(identity),phase,months,symbol,scope)
    allowed={'id','title','family','market','cases','training_summary','phase_count','contest_certified','protocol_origin'}
    summary={k:v for k,v in result['summary'].items() if k in allowed}
    summary['cases']=[{'warm_start':c.get('warm_start')} for c in summary.get('cases',[])]
    if summary.get('training_summary'):
        summary['training_summary']={k:v for k,v in summary['training_summary'].items() if k in ('model','model_size','parameters','folds')}
    summary.update(source_name='연구 백테스트',verification='IS / OS / ROS 평가',limitations=[],genome={})
    result['summary']=summary;result['record_type']='backtest'
    return result


@router.get('/backtest/{identity}/trades')
def backtest_trades(identity:str,phase:int=Query(0,ge=0),months:int=Query(36,ge=36,le=36),symbol:str=Query('',max_length=12),offset:int=Query(0,ge=0),limit:int=Query(25,ge=1,le=200),scope:str=Query('os',pattern='^(is|os|ros|all)$')):
    from .app import transactions
    result=transactions(identity,published_request(identity),phase,months,symbol,offset,limit,scope)
    result['trades']=safe_rows(result['trades'],TRADE_FIELDS|{'pnl','gross','proceeds','cost'})
    return result
