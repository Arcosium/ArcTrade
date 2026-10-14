"""Causal KR minute paper trading on observed prices, never brokerage orders.

The live collector supplies close and cumulative volume, NOT OHLC. Signals at
minute t execute at a subsequently observed minute's close, never a made-up open.
A separately fitted ridge predicts a 30-minute holding return. Daily research
certification is not inherited; this is a forward paper probation model.
"""
import copy
import fcntl
import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import store

TZ = ZoneInfo('Asia/Seoul')
HOLD_MINUTES = 30
MAX_AGE_SECONDS = 600
FEE = .002  # Conservative per-side fees/tax allowance, separate slippage.
SLIPPAGE = .0005
PARTICIPATION = .01
MAX_SYMBOLS = 30
BAR_LIMIT = 7000
FEATURES = ['r1', 'r5', 'r20', 'trend', 'volatility']


def source_path():
    return Path.home()/'vault/CryptoBars/data/KRX/bars.db'


def load_prices(symbols, now, path=None, limit=BAR_LIMIT):
    """Indexed bounded reads. Keep actual observed minutes and volume resets."""
    import pandas as pd
    import numpy as np
    frames=[]
    with closing(sqlite3.connect((path or source_path()).as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        for symbol in symbols:
            rows=db.execute('SELECT ts,close,vol_cum FROM bars WHERE code=? AND ts<? ORDER BY ts DESC LIMIT ?',
                            (symbol.split('@')[0],datetime.fromtimestamp(now,TZ).strftime('%Y%m%d%H%M'),min(BAR_LIMIT,limit))).fetchall()
            if not rows:continue
            d=pd.DataFrame(rows,columns=['ts','close','vol_cum']).sort_values('ts')
            d['date']=pd.to_datetime(d.ts,format='%Y%m%d%H%M',errors='coerce')
            d=d[d.date.notna() & d.close.gt(0) & np.isfinite(d.close) & d.date.dt.dayofweek.lt(5)]
            clock=d.date.dt.hour*60+d.date.dt.minute
            d=d[(clock>=540)&(clock<930)].copy()
            contiguous=d.date.diff().eq(pd.Timedelta(minutes=1))
            delta=d.vol_cum.diff()
            d['volume']=delta.where(contiguous & delta.ge(0) & np.isfinite(delta),0).fillna(0)
            d['symbol']=symbol;frames.append(d[['date','symbol','close','volume']])
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=['date','symbol','close','volume'])


def features(prices, window=20, horizon=HOLD_MINUTES):
    import numpy as np
    import pandas as pd
    p=prices.sort_values(['symbol','date']).copy()
    group=p.groupby('symbol',sort=False)
    for n in (1,5,20):p['r'+str(n)]=group.close.pct_change(n,fill_method=None)
    p['trend']=p.close/group.close.transform(lambda x:x.rolling(window).mean())-1
    p['volatility']=p.groupby('symbol').r1.transform(lambda x:x.rolling(window).std())
    p['r20']=group.close.pct_change(window,fill_method=None)
    p['volume_ratio']=p.volume/group.volume.transform(lambda x:x.rolling(window).mean()).replace(0,np.nan)-1
    p['downside_vol']=p.r1.clip(upper=0).pow(2).groupby(p.symbol).transform(lambda x:x.rolling(window).mean()).pow(.5)
    history=max(20,window)
    p['ready']=((p.date-group.date.shift(history)).eq(pd.Timedelta(minutes=history)) & np.isfinite(p[FEATURES]).all(axis=1))
    entry=group.close.shift(-1);last=group.close.shift(-horizon-1)
    p['label_end']=group.date.shift(-horizon-1)
    p['target']=last/entry-1
    causal=(group.date.shift(-1)-p.date).eq(pd.Timedelta(minutes=1)) & (p.label_end-p.date).eq(pd.Timedelta(minutes=horizon+1))
    p.loc[~causal,'target']=np.nan
    return p


def fit(prices, cutoff):
    """Purged chronological validation; all labels strictly precede cutoff."""
    import numpy as np
    import pandas as pd
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import Ridge
    from threadpoolctl import threadpool_limits
    p=features(prices)
    rows=p[p.ready & np.isfinite(p.target) & p.label_end.lt(cutoff)].copy()
    dates=sorted(rows.date.dt.normalize().unique())
    if len(rows)<1500 or len(dates)<5:raise ValueError('분봉 학습 표본 부족: 최소 5거래일·1500행')
    split=sorted(rows.date.unique())[int(rows.date.nunique()*.8)]
    train=rows[rows.label_end.lt(split)];valid=rows[rows.date.ge(split)]
    if len(train)<1000 or len(valid)<200:raise ValueError('분봉 시간 분리 검증 표본 부족')
    model=make_pipeline(StandardScaler(),Ridge(alpha=10.))
    with threadpool_limits(limits=1):
        model.fit(train[FEATURES].to_numpy(),train.target.to_numpy())
        prediction=model.predict(valid[FEATURES].to_numpy())
    if not np.isfinite(prediction).all() or np.max(np.abs(prediction))>.2:raise ValueError('분봉 검증 예측값 비정상')
    proof=dict(interval='minute',execution='next_observed_close',holding_minutes=HOLD_MINUTES,
        train_rows=len(train),validation_rows=len(valid),train_max_label_end=str(train.label_end.max()),
        validation_start=str(pd.Timestamp(split)),validation_max_label_end=str(valid.label_end.max()),
        train_cutoff=str(cutoff),validation_mae=float(np.mean(np.abs(prediction-valid.target.to_numpy()))),
        validation_status='forward_paper_probation',daily_os_certified=False)
    # Refit only after held-out predictions have been recorded, still purged at cutoff.
    with threadpool_limits(limits=1):model.fit(rows[FEATURES].to_numpy(),rows.target.to_numpy())
    proof['fit_rows']=len(rows);proof['fit_max_label_end']=str(rows.label_end.max())
    return model,proof


def universe(dep, book):
    import pyarrow.parquet as pq
    artifact=Path(dep['artifact'])
    recipe=json.loads((artifact.parent/'recipe.json').read_text())
    data=pq.ParquetFile(recipe['input']['path']).read(columns=['date','symbol','close','volume'],use_threads=False).to_pandas()
    last=data[data.date.eq(data.date.max())].copy();last['liquidity']=last.close*last.volume
    candidates=last.sort_values(['liquidity','symbol'],ascending=[False,True]).symbol.astype(str).tolist()
    held=list(book['holdings'])
    return list(dict.fromkeys(held+candidates))[:max(MAX_SYMBOLS,len(held))]


def model_for(dep,book,now,retrain=False):
    import joblib
    import pandas as pd
    from .config import RUNS
    folder=RUNS/'minute_paper'/str(dep['user_id']);folder.mkdir(parents=True,exist_ok=True)
    path=folder/'model.joblib';day=datetime.fromtimestamp(now,TZ).strftime('%Y%m%d')
    if path.exists() and not retrain:
        artifact=joblib.load(path)
        if artifact['day']==day:return artifact
    symbols=universe(dep,book);prices=load_prices(symbols,now)
    cutoff=pd.Timestamp(datetime.fromtimestamp(now,TZ).replace(tzinfo=None)).floor('min')
    model,proof=fit(prices,cutoff)
    identity='kr-minute-'+hashlib.sha256(json.dumps(dict(proof=proof,symbols=symbols),sort_keys=True).encode()).hexdigest()[:16]
    artifact=dict(model=model,proof=proof,symbols=symbols,day=day,strategy_id=identity,created=now)
    temp=path.with_suffix('.tmp');joblib.dump(artifact,temp);temp.replace(path)
    (folder/'proof.json').write_text(json.dumps(dict(strategy_id=identity,**proof),ensure_ascii=False,indent=2))
    return artifact


def advance_book(original, frame, artifact, now):
    """Pure book transition. No replay, stale/missing holds freeze all fills."""
    import pandas as pd
    book=copy.deepcopy(original)
    local=datetime.fromtimestamp(now,TZ)
    if local.weekday()>4 or not (540<=local.hour*60+local.minute<920):return book
    horizon=artifact.get('holding_minutes',HOLD_MINUTES)
    p=features(frame,artifact.get('lookback',20),horizon)
    if p.empty:return book
    latest=p.sort_values('date').groupby('symbol',sort=False).tail(1).set_index('symbol')
    def stamp(row):return row['date'].to_pydatetime().replace(tzinfo=TZ).timestamp()
    def fresh(row):return 60<=now-stamp(row)<=MAX_AGE_SECONDS and math.isfinite(float(row.close)) and row.close>0
    if any(s not in latest.index or not fresh(latest.loc[s]) for s in book['holdings']):
        book['message']='분봉 페이퍼 · 보유종목 시세 누락/지연으로 체결 대기';return book
    state=book.setdefault('minute',{})
    if not state:
        state.update(activated=now,last_processed={},pending=None,last_signal=0,entered={s:now for s in book['holdings']})
        book['pending']=[]  # Retire daily orders while retaining balances/history.
    elif book.get('minute_strategy_id')!=artifact['strategy_id']:
        state['pending']=None;state['last_signal']=0
    book['minute_strategy_id']=artifact['strategy_id'];book['minute_model']=artifact['proof'];book['interval']='minute'
    pending=state.get('pending')
    # A close observed before the decision was actually recorded cannot fill it.
    if pending:
        required=set(pending['sells'])|{x['symbol'] for x in pending['buys']}
        available=all(s in latest.index and fresh(latest.loc[s]) and stamp(latest.loc[s])>pending['created'] for s in required)
        if now-pending['created']>MAX_AGE_SECONDS+120:
            state['pending']=None  # Expired or overnight instructions never replay.
        elif available:
            for side,picks in [('sell',pending['sells']),('buy',pending['buys'])]:
                for pick in picks:
                    symbol=pick if side=='sell' else pick['symbol'];row=latest.loc[symbol];ts=stamp(row)
                    px=float(row.close)*(1-SLIPPAGE if side=='sell' else 1+SLIPPAGE)
                    volume=float(row.volume)
                    if not math.isfinite(volume) or volume<0:continue
                    cap=min(int(pending['caps'].get(symbol,0)),int(volume*PARTICIPATION))
                    if side=='sell':
                        if symbol not in book['holdings']:continue
                        h=book['holdings'][symbol];qty=min(int(h['qty']),cap)
                        if qty<=0:continue
                        book['cash']+=qty*px*(1-FEE);h['qty']-=qty
                        if h['qty']<=0:del book['holdings'][symbol];state['entered'].pop(symbol,None)
                    else:
                        if symbol in book['holdings']:continue
                        budget=min(book['cash']/(1+FEE),book['nav']*artifact.get('max_weight',.1))
                        qty=min(cap,int(budget/px))
                        if qty<=0:continue
                        book['cash']-=qty*px*(1+FEE)
                        book['holdings'][symbol]=dict(code=symbol,name=symbol,qty=qty,entry=px,value=qty*px,pnl_ratio=0.)
                        state['entered'][symbol]=ts
                    book['trades'].append(dict(date=datetime.fromtimestamp(ts,TZ).strftime('%Y%m%d%H%M'),code=symbol,
                        side=side,qty=qty,price=px,fee=qty*px*FEE,signal_ts=pending['created'],execution='next_observed_close',strategy_id=pending['strategy_id']))
            state['pending']=None
    for symbol,h in book['holdings'].items():
        px=float(latest.loc[symbol].close);h['value']=h['qty']*px;h['pnl_ratio']=px/h['entry']-1
    book['nav']=book['cash']+sum(h['value'] for h in book['holdings'].values())
    if not math.isfinite(book['nav']) or book['cash']<-.000001:raise ValueError('분봉 장부 잔고 오류')
    ready=latest[latest.ready].copy()
    ready=ready[[fresh(row) and stamp(row)>state['activated'] and stamp(row)>state['last_processed'].get(s,0) for s,row in ready.iterrows()]]
    if len(ready) and state['pending'] is None and now-state['last_signal']>=horizon*60 and local.hour*60+local.minute<900:
        ready['score']=artifact['model'].predict(ready[artifact.get('fields',FEATURES)].to_numpy())
        # Expected gross return must clear both sides' cost and slippage.
        picks=ready[ready.score.gt(2*(FEE+SLIPPAGE))].sort_values('score',ascending=False).head(artifact.get('selection_count',10))
        selected=set(picks.index)
        sells=[s for s in book['holdings'] if s not in selected and now-state['entered'].get(s,now)>=horizon*60]
        buys=[dict(symbol=s) for s in sorted(selected) if s not in book['holdings']]
        caps={s:max(0,int(float(latest.loc[s,'volume'])*PARTICIPATION))
              if math.isfinite(float(latest.loc[s,'volume'])) else 0 for s in set(sells)|selected}
        state['pending']=dict(created=now,sells=sells,buys=buys,caps=caps,strategy_id=artifact['strategy_id'])
        state['last_signal']=now
        state['last_processed'].update({s:stamp(row) for s,row in ready.iterrows()})
    book['message']=f'분봉 페이퍼 · 별도 분봉 학습/다음 관측 종가 체결 · 최소 보유 {horizon}분 · 일봉 OS 인증 미승계'
    book['last_minute']=local.strftime('%Y%m%d%H%M')
    day=local.strftime('%Y%m%d');point=dict(date=day,net_return=book['nav']/book['initial']-1)
    if book['equity'] and book['equity'][-1]['date']==day:book['equity'][-1]=point
    else:book['equity'].append(point)
    return book


def advance(dep, now=None, retrain=False, train_only=False):
    from .paper import initialize
    from .config import DATA
    now=time.time() if now is None else now
    initialize();DATA.mkdir(parents=True,exist_ok=True)
    with (DATA/f"paper-{int(dep['user_id'])}-kr-paper.lock").open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with store.connect() as db:row=db.execute("SELECT * FROM paper_books WHERE user_id=? AND market='kr'",(dep['user_id'],)).fetchone()
        if not row:raise ValueError('분봉 전환에는 기존 국장 페이퍼 장부가 필요합니다.')
        book=json.loads(row['body']);artifact=model_for(dep,book,now,retrain)
        if train_only:return artifact['proof']
        prices=load_prices(list(dict.fromkeys(artifact['symbols']+list(book['holdings']))),now,limit=80)
        result=advance_book(book,prices,artifact,now)
        with store.connect() as db:
            db.execute("UPDATE paper_books SET body=?,updated=? WHERE user_id=? AND market='kr'",
                       (json.dumps(result,ensure_ascii=False),now,dep['user_id']))
        return result


def run(retrain=False,train_only=False):
    with store.connect() as db:
        rows=db.execute("SELECT d.* FROM model_deployments d JOIN paper_books b ON b.deployment=d.id WHERE b.market='kr'").fetchall()
    for dep in rows:advance(dict(dep),retrain=retrain,train_only=train_only)
    store.set_setting('minute_paper_heartbeat',dict(timestamp=time.time(),accounts=len(rows)))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--retrain',action='store_true');parser.add_argument('--train-only',action='store_true')
    args=parser.parse_args()
    if args.train_only or store.setting('kr_minute_paper_enabled',False):run(args.retrain,args.train_only)
