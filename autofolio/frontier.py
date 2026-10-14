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
    except (OSError,ValueError):return dict(state='queued',message='분봉 검증·운용 모델 준비 대기' if row.get('market')=='kr' else '독립 페이퍼 계좌 준비 대기')


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
    if not result['connected']:result['message']=s.get('message','운용 준비 중')
    result['validation']=dict(state=s.get('state'),minute=s.get('minute'),paper_only=True)
    return result


def tick():
    from .public_routes import owner_id
    from .paper import initialize, _advance
    from .learning import retrain
    from .observations import record, stock_quotes, mark
    initialize();uid=owner_id()
    from .minute_validation import audit_all
    audit_all()
    for market in ('kr','timefolio','us','crypto'):
        for row in members(market,uid):
            p=folder(row);internal=identity(uid,market,row['id']);paper_market='kr' if market=='timefolio' else market
            try:
                if market=='kr':
                    from .minute_validation import advance_frontier
                    advance_frontier(row,internal)
                    continue
                model=p/'model.joblib'
                if not model.exists():
                    with store.connect() as db:r=db.execute('SELECT source FROM strategies WHERE id=?',(row['id'],)).fetchone()
                    save_status(row,state='training',message='등록된 학습 조건으로 페이퍼 모델 준비 중')
                    retrain(r['source'],p)
                dep=dict(id='frontier-'+row['id'],user_id=internal,target=paper_market+'-paper',artifact=str(model))
                _advance(dep)
                save_status(row,state='forward_paper',message='독립 페이퍼 운용 · 신규 관측 이후 성과만 기록')
                with store.connect() as db:r=db.execute('SELECT body FROM paper_books WHERE user_id=? AND market=?',(internal,paper_market)).fetchone()
                if r:
                    b=json.loads(r['body']);q=stock_quotes(b['holdings'],paper_market)
                    if market=='crypto':
                        from integrations.crypto.market import quotes
                        raw=quotes(list(b['holdings']));q={k:dict(last=v['last'],ts=v['ts']/1000) for k,v in raw.items()}
                    value=mark(b,q,time.time())
                    if value:record(internal,paper_market,'deployed',value['nav'],b['initial'],'live quotes',value['source_ts'])
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
