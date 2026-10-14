"""Current validated Pareto membership and isolated forward paper accounts.

Daily research scores never become paper returns. Removed members are hidden
immediately; their historical accounts stay intact and stop advancing.
"""
import hashlib
import json
import time
from pathlib import Path
from . import store, catalogue, research
from .evaluation import selection_metrics, valid_result
from .metrics import pareto_front
from .config import RUNS


def members(market, uid):
    if uid is None:return []
    eligible=[]
    for row in catalogue.rows():
        if not research.visible(row,dict(id=uid),market):continue
        if (row.get('genome') or {}).get('engine')!='learned_v1':continue
        metric=selection_metrics(row)
        if not metric or not valid_result(row):continue
        eligible.append(dict(row,**{k:v for k,v in metric.items() if k!='monthly'}))
    cohorts={}
    for row in eligible:
        c=cohorts.setdefault(row['cohort'],[tuple(row.get('evaluation_window') or []),0,row.get('sessions',0)])
        c[1]+=1
    if not cohorts:return []
    cohort=max(cohorts,key=lambda c:(cohorts[c][0],cohorts[c][1],cohorts[c][2]))
    rows=[r for r in eligible if r['cohort']==cohort]
    ids={r['id'] for r in pareto_front(rows,return_key='sharpe',loss_key='mdd')}
    return sorted([r for r in rows if r['id'] in ids],key=lambda r:(-r['sharpe'],abs(r['mdd']),r['id']))


def identity(uid, market, strategy):
    # Isolated, non-user IDs in paper_books only; never passed to brokers/auth.
    return -int(hashlib.sha256(f'frontier:{uid}:{market}:{strategy}'.encode()).hexdigest()[:14],16)


def folder(row):return RUNS/'frontier_paper'/row.get('market','kr')/row['id']


def status(row):
    try:return json.loads((folder(row)/'status.json').read_text())
    except (OSError,ValueError):return dict(state='queued',message='분봉 검증·운용 모델 준비 대기')


def save_status(row, **data):
    p=folder(row);p.mkdir(parents=True,exist_ok=True)
    tmp=p/'status.tmp';tmp.write_text(json.dumps(dict(data,updated=time.time()),ensure_ascii=False));tmp.replace(p/'status.json')


def snapshot(row, uid, interval):
    from .public_routes import deployed, empty
    market=row['market'];paper_market='kr' if market=='timefolio' else market
    internal=identity(uid,market,row['id'])
    result=deployed(paper_market,interval,internal)
    s=status(row)
    result['market']=market;result['strategy']=dict(key=row['id'],title=row['title'])
    g=row.get('genome',{})
    if s.get('minute') and g.get('holding_sessions'):
        result['strategy']['title']=f"{g.get('model','모델')} · {g.get('lookback','')} · {g.get('feature_set','')} · {g['holding_sessions']}분"
    elif not result.get('execution_interval'):
        result['strategy']['title']+=' · 분봉 전환 대기'
    if not result['connected']:result['message']=s.get('message','운용 준비 중')
    result['validation']=dict(state=s.get('state'),minute=s.get('minute'),forward=s.get('forward'),paper_only=True)
    return result


def tick():
    from .public_routes import owner_id
    from .paper import initialize
    initialize();uid=owner_id()
    from .minute_validation import audit_all
    audit_all()
    for market in ('kr','timefolio','us','crypto'):
        for row in members(market,uid):
            p=folder(row);internal=identity(uid,market,row['id']);paper_market='kr' if market=='timefolio' else market
            try:
                from .minute_validation import advance_frontier
                advance_frontier(row,internal)
            except Exception as exc:
                save_status(row,state='waiting_validation',message='검증·데이터 확인 필요: '+type(exc).__name__)
    store.set_setting('frontier_heartbeat',dict(timestamp=time.time()))


def main():
    import fcntl
    with (store.DATA/'frontier.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        while True:
            try:tick()
            except Exception as exc:store.event('frontier_error',dict(error=type(exc).__name__))
            time.sleep(30)

if __name__=='__main__':main()
