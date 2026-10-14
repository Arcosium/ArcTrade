"""Authenticated market research and user-scoped account views."""
import asyncio
import contextlib
import calendar
import hashlib
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from .config import ROOT, RUNS, ARC
from .store import initialize, connect, setting
from .catalogue import rows, strategy_case
from .metrics import pareto_front, ledger, statistics_for, normalized_date, evaluation_scope

initialize()
from .auth import user,admin_required,AuthMiddleware,router as auth_router
from . import research
research.initialize()
@contextlib.asynccontextmanager
async def lifespan(app):
    async def automatic_deployments():
        from .auto_apply import tick
        from .store import event
        last_timefolio_run=0.0
        while True:
            try:
                await asyncio.to_thread(tick)
            except Exception as exc:
                event('auto_apply_error',dict(message='자동 적용 점검 실패',error=str(exc)[:180]))
            now=time.monotonic()
            if now-last_timefolio_run>=60:
                last_timefolio_run=now
                try:
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    from .timefolio_live import market_open,run
                    if market_open(datetime.now(ZoneInfo('Asia/Seoul'))):
                        await asyncio.to_thread(run)
                except Exception as exc:
                    event('timefolio_execution_error',dict(message='타임폴리오 실행 점검 실패',error=type(exc).__name__))
            await asyncio.sleep(30)
    task=asyncio.create_task(automatic_deployments())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

app=FastAPI(title='ArcTrade',docs_url=None,redoc_url=None,lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.include_router(auth_router)
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')


@app.middleware('http')
async def headers(request,call_next):
    if request.url.hostname=='quantinsight.ai-ve.uk':
        return HTMLResponse('<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>QuantInSight</title></head><body></body></html>',
                            status_code=200 if request.url.path=='/' else 404,
                            headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    if request.url.hostname=='autofolio.ai-ve.uk':
        return RedirectResponse('https://arctrade.ai-ve.uk'+request.url.path+('?' + request.url.query if request.url.query else ''),status_code=308)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    response.headers.setdefault('Content-Security-Policy',"default-src 'self'; script-src 'self' https://static.cloudflareinsights.com; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'")
    return response


# Added last so it wraps every response; the tunnel uplink otherwise carries plain JSON.
app.add_middleware(GZipMiddleware,minimum_size=1024,compresslevel=6)


@app.get('/')
@app.get('/strategy/{identity}')
def index(identity=None):return FileResponse(ROOT/'static/index.html')


@app.get('/api/leaderboard')
def leaderboard(request:Request,cohort: str|None=None,market:str=Query("kr",pattern="^(kr|us|crypto|timefolio)$"),known:str|None=Query(None,max_length=40),x_axis:str=Query('negative_months',pattern='^(negative_months|mdd)$'),y_axis:str=Query('net_return',pattern='^(net_return|sharpe)$')):
    from .evaluation import selection_metrics
    all_rows=[];learned_only=setting('labs_v2_enabled',False)
    for r in rows():
        if not research.visible(r,user(request),market):continue
        span=qualified_window(r);metric=selection_metrics(r)
        if not span or not metric:continue
        if learned_only and (r.get('genome') or {}).get('engine')!='learned_v1':continue
        all_rows.append(dict(r,**{k:v for k,v in metric.items() if k!='monthly'},
                             evaluation_window=list(span),evaluation_months=36,selection_scope='os'))
    cohorts={}
    for r in all_rows:
        cohorts.setdefault(r['cohort'],dict(id=r['cohort'],start=r['start'],end=r['end'],sessions=r['sessions'],months=r['months'],evaluation_window=r['evaluation_window'],count=0))['count']+=1
    cohorts=sorted(cohorts.values(),key=lambda c:(c['evaluation_window'][1],c['count'],c['sessions']),reverse=True)
    selected=cohort if cohort in {c['id'] for c in cohorts} else (cohorts[0]['id'] if cohorts else None)
    chosen=[r for r in all_rows if r['cohort']==selected]
    # Explicitly validated axes; automatic selection keeps its fixed OS policy.
    frontier=lambda values:pareto_front(values,return_key=y_axis,loss_key=x_axis)
    front={r['id'] for r in frontier(chosen)}
    screened=[r for r in chosen if r.get('rule_screen_pass')]
    valid_front={r['id'] for r in frontier(screened)}
    # The strategy page reads segment tables and contest checks from /api/strategy;
    # the genome stays so the board can tell same-named trials apart.
    points=[{k:v for k,v in r.items() if k not in ['cases','source_digest','limitations','performance','contest_validation']} |
            dict(pareto=r['id'] in front,screened_pareto=r['id'] in valid_front) for r in chosen]
    points.sort(key=lambda r:(-r['net_return'],r['negative_months']))
    from .map_references import references
    mapped=references(market,user(request),chosen)
    if market=='crypto':mapped['points']=[]
    for point in points:point['applied_targets']=mapped['assignments'].get(point['id'],[])
    board=dict(cohort=selected,cohorts=cohorts,strategies=points,total=len(all_rows),
                references=mapped['points'],
                frontier=[r['id'] for r in frontier(chosen)],x_axis=x_axis,y_axis=y_axis,
                screened_frontier=[r['id'] for r in frontier(screened)],
                period_label=((' ~ '.join([chosen[0]['start'],chosen[0]['end']])+' · OS 9개월') if chosen else 'OS 9개월 · 첫 검증 대기'), comparison='OS 9개월',selection_scope='os',protocol=research.protocol(market))
    # The map polls every few seconds; an unchanged board is answered without its body.
    if market=='kr':
        with connect() as db:
            try:minute_checks={r[0]:json.loads(r[1]) for r in db.execute('SELECT strategy_id,body FROM minute_validation')}
            except Exception:minute_checks={}
        for point in board['strategies']:point['minute_validation']=minute_checks.get(point['id'],dict(state='queued'))
        board['minute_validation_status']=setting('minute_validation_status',{})
    version=hashlib.sha256(json.dumps(board,sort_keys=True,default=str).encode()).hexdigest()[:20]
    return dict(unchanged=True,version=version) if known==version else dict(board,version=version)


def qualified_window(summary):
    """A complete, pinned 36-month result remains inspectable after the daily roll."""
    from .period import window
    from .evaluation import valid_result
    span=summary.get('evaluation_window') or window()
    try:
        return tuple(span) if valid_result(summary) and span[1]<=window()[1] else None
    except (ValueError,TypeError,IndexError):return None


def selected_book(identity,phase):
    try:
        summary,case=strategy_case(identity,phase)
        if not qualified_window(summary):raise HTTPException(410,'완전한 36개월 평가 기록이 아닙니다.')
        return summary,case
    except KeyError:raise HTTPException(404,'전략을 찾을 수 없습니다.')
    except ValueError:raise HTTPException(400,'시작일을 확인해 주세요.')
    except (OSError,TypeError,IndexError):raise HTTPException(409,'원본 결과를 확인할 수 없습니다.')


def period_rows(case,months,span=None,scope='all'):
    if span:
        from .evaluation import periods
        bounds=dict(start=span[0],end=span[1]) if scope=='all' else periods(*span)[scope]
        return ledger(case,bounds['start'],bounds['end'])
    start,end,_=evaluation_scope(case)
    selected=ledger(case,start,end)
    if months and selected:
        keys=sorted({r['date'][:6] for r in selected})[-months:]
        selected=[r for r in selected if r['date'][:6] in keys]
    return selected


@app.get('/api/strategy/{identity}')
def detail(identity: str,request:Request,phase:int=Query(0,ge=0),months:int=Query(36,ge=36,le=36),symbol:str=Query('',max_length=12),scope:str=Query('os',pattern='^(is|os|ros|all)$')):
    summary,case=selected_book(identity,phase)
    if not research.visible(summary,user(request)):raise HTTPException(404,'전략을 찾을 수 없습니다.')
    from .evaluation import periods,performance,segment_metrics
    span=qualified_window(summary)
    selected=period_rows(case,months,span,scope)
    if not selected:raise HTTPException(409,'이 기간에 계좌 장부가 없습니다.')
    split=performance(period_rows(case,months,span),*span,market=summary.get('market','kr'))
    metrics=segment_metrics(selected,*span,36,market=summary.get('market','kr')) if scope=='all' else split[scope]
    if not metrics:raise HTTPException(409,'선택 구간의 완전한 평가 기록이 없습니다.')
    trades=[t for t in case['trades'] if selected[0]['date']<=normalized_date(t.get('date',''))<=selected[-1]['date']]
    symbols=Counter(str(t.get('code','')) for t in trades)
    filtered=[t for t in trades if not symbol or t.get('code')==symbol]
    flow=defaultdict(lambda:dict(buy=0.,sell=0.,buy_count=0,sell_count=0,fees=0.))
    for t in filtered:
        f=flow[normalized_date(t['date'])]
        side=t.get('side')
        if side not in ['buy','sell']:continue
        f[side]+=float(t['qty'])*float(t['price'])
        f[side+'_count']+=1
        f['fees']+=float(t.get('fee',0))
    daily=[dict(date=r['date'],net_return=r['nav']/selected[0]['previous_nav']-1,
                nav=r['nav'],cash=r.get('cash'),gross=r.get('gross'),**flow[r['date']]) for r in selected]
    fills=[dict(date=normalized_date(t['date']),price=t['price'],side=t['side'],qty=t['qty'],code=t['code']) for t in filtered] if symbol else []
    return dict(summary=summary,phase=phase,metrics=metrics,daily=daily,trade_count=len(filtered),
                scope=scope,selection_scope='os',performance=split,evaluation_periods=periods(*span),
                symbols=[dict(code=k,count=v) for k,v in sorted(symbols.items())],fills=fills,
                total_fees=sum(float(t.get('fee',0)) for t in trades),
                trades_note='체결 가격에는 슬리피지가 포함됩니다. 수수료는 별도입니다.')


@app.get('/api/strategy/{identity}/trades')
def transactions(identity: str,request:Request,phase:int=Query(0,ge=0),months:int=Query(36,ge=36,le=36),
                 symbol:str=Query('',max_length=12),offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=200),scope:str=Query('os',pattern='^(is|os|ros|all)$')):
    summary,case=selected_book(identity,phase)
    if not research.visible(summary,user(request)):raise HTTPException(404,'전략을 찾을 수 없습니다.')
    selected=period_rows(case,months,qualified_window(summary),scope)
    if not selected:return dict(total=0,trades=[])
    trades=[t for t in case['trades'] if selected[0]['date']<=normalized_date(t.get('date',''))<=selected[-1]['date']
            and (not symbol or t.get('code')==symbol)]
    trades.sort(key=lambda t:normalized_date(t['date']),reverse=True)
    return dict(total=len(trades),trades=trades[offset:offset+limit],offset=offset,limit=limit)


@app.get('/api/status')
def status(request:Request):
    admin_required(request)
    with connect() as db:
        counts=dict((r['status'],r['n']) for r in db.execute('SELECT status,count(*) n FROM jobs GROUP BY status'))
        coverage=dict((r['status'],r['n']) for r in db.execute('SELECT status,count(*) n FROM sources GROUP BY status'))
        active=[dict(r) for r in db.execute("SELECT id,generation,operator,status,started,genome FROM jobs WHERE status IN ('running','queued') ORDER BY created LIMIT 10")]
        recent=[dict(r) for r in db.execute('SELECT id,generation,operator,status,finished,error FROM jobs ORDER BY created DESC LIMIT 8')]
    for r in active:
        r['genome']=json.loads(r['genome'])
        path=RUNS/'experiments'/r['id']/'progress.json'
        try:r['progress']=json.loads(path.read_text())
        except (OSError,ValueError):r['progress']=None
    heartbeat=setting('heartbeat',{})
    return dict(market_running=setting('market_running',0),enabled=setting('enabled'),worker_alive=time.time()-heartbeat.get('timestamp',0)<60,
                heartbeat=heartbeat,generation=setting('generation',0),jobs=counts,active=active,recent=recent,
                coverage=coverage,baseline_verified=bool(setting('baseline_verified')),
                validation_candidate=setting('validation_candidate'),
                storage_guard=setting('storage_guard',{}),
                resources=dict(cpu_cores=setting('cpu_cores',4),memory_gb=setting('memory_gb',8),parallel=setting('concurrency',2),cloud=False),
                research_only=True,orders_enabled=False)


from .controls import router as controls_router
app.include_router(controls_router)

from .market_routes import router as market_router
from .crypto_view import router as crypto_router
app.include_router(market_router)
app.include_router(crypto_router)

from .briefing import router as briefing_router
from .auto_apply import router as auto_apply_router
app.include_router(briefing_router)
app.include_router(auto_apply_router)

from .public_routes import router as public_router
app.include_router(public_router)
