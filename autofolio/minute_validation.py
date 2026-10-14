"""Minute conversion checks and separate forward ledgers for daily candidates.

A daily winner is never silently certified as an intraday winner. Every source
gets an explicit audit record; a distinct minute model uses only observed close
and volume features, purged validation, fees and subsequent observed fills.
"""
import json
import time
from pathlib import Path
from . import store, catalogue, minute_paper as minute
from .config import RUNS


def audit_all():
    from .public_routes import owner_id
    from .research import visible
    from .evaluation import selection_metrics
    uid=owner_id();counts={}
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS minute_validation(strategy_id TEXT PRIMARY KEY,body TEXT NOT NULL,updated REAL NOT NULL)')
        known={r[0] for r in db.execute('SELECT strategy_id FROM minute_validation')}
        for row in catalogue.rows():
            if not visible(row,dict(id=uid),'kr'):continue
            if row['id'] in known:continue
            g=row.get('genome') or {}
            state='queued' if selection_metrics(row) and g.get('engine')=='learned_v1' else 'rejected_daily_gate'
            body=dict(state=state,original_daily_certified_as_minute=False,
                message='분봉 전환 검증 대기' if state=='queued' else '일봉 OS 표본 검증 미통과 · 분봉 운용 제외')
            db.execute('INSERT INTO minute_validation VALUES(?,?,?)',(row['id'],json.dumps(body,ensure_ascii=False),time.time()))
        for state,n in db.execute("SELECT json_extract(body,'$.state'),count(*) FROM minute_validation GROUP BY 1"):counts[state]=n
    store.set_setting('minute_validation_status',dict(counts=counts,updated=time.time()))
    return counts


def record(row,proof):
    with store.connect() as db:db.execute('INSERT OR REPLACE INTO minute_validation VALUES(?,?,?)',(row['id'],json.dumps(proof,ensure_ascii=False),time.time()))


def fit_variant(row, now):
    import fcntl,joblib
    from .frontier import folder
    p=folder(row);p.mkdir(parents=True,exist_ok=True)
    with (p/'minute.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if (p/'minute.joblib').exists():
            old=joblib.load(p/'minute.joblib')
            if old.get('conversion_version')==2:return old
        return _fit_variant(row,now)


def _fit_variant(row, now):
    import joblib,numpy as np,pandas as pd,hashlib
    from threadpoolctl import threadpool_limits
    from .learning import estimator, NEURAL_MODELS
    from .frontier import folder
    g=row['genome'];p=folder(row);p.mkdir(parents=True,exist_ok=True)
    with store.connect() as db:r=db.execute('SELECT source FROM strategies WHERE id=?',(row['id'],)).fetchone()
    recipe=json.loads(Path(r['source']).read_text())['recipe']
    from .model_recipe import verify
    verify(recipe)
    # Registered universe stays separate from the collector; no source DB writes.
    import pyarrow.parquet as pq
    raw=pq.ParquetFile(recipe['input']['path']).read(columns=['date','symbol','close','volume'],use_threads=False).to_pandas()
    last=raw[raw.date.eq(raw.date.max())].copy();last['liquidity']=last.close*last.volume
    symbols=last.sort_values('liquidity',ascending=False).symbol.astype(str).head(30).tolist()
    if g['model'] in NEURAL_MODELS:
        raise ValueError('신경망 분봉 시퀀스 전환은 별도 등록 필요')
    if g['feature_set']=='ohlc':raise ValueError('OHLC 입력 전략은 실시간 OHLC 분봉 연결 검증 대기')
    names=['r1','r5','r20','trend']
    if g['feature_set'] in ('price_risk','all','downside_risk'):names+=['volatility']
    if g['feature_set'] in ('price_volume','volume','all','liquidity'):names+=['volume_ratio']
    if g['feature_set']=='downside_risk':names+=['downside_vol']
    horizon=g['holding_sessions'];lookback=g['lookback']
    frame=minute.load_prices(symbols,now);data=minute.features(frame,lookback,horizon)
    cutoff=pd.Timestamp.now(tz=minute.TZ).tz_localize(None).floor('min')
    data=data[data.ready & np.isfinite(data.target) & np.isfinite(data[names]).all(axis=1) & data.label_end.lt(cutoff)]
    if len(data)<1500 or data.date.dt.normalize().nunique()<5:raise ValueError('분봉 기간·표본 부족')
    split=sorted(data.date.unique())[int(data.date.nunique()*.8)]
    train=data[data.label_end.lt(split)];test=data[data.date.ge(split)]
    if len(train)<1000 or len(test)<200:raise ValueError('분봉 시간 분리 검증 표본 부족')
    model=estimator(g,len(names))
    with threadpool_limits(limits=1):
        model.fit(train[names].to_numpy(),train.target.to_numpy())
        predicted=model.predict(test[names].to_numpy())
    if not np.isfinite(predicted).all() or np.max(np.abs(predicted))>.5:raise ValueError('분봉 예측값 비정상')
    eligible=predicted>2*(minute.FEE+minute.SLIPPAGE)
    net=test.target.to_numpy()[eligible]-2*(minute.FEE+minute.SLIPPAGE)
    proof=dict(state='forward_paper_probation',interval='minute',source_strategy=row['id'],
        model=g['model'],feature_adapter='observed_close_volume_v1',fields=names,
        original_daily_certified_as_minute=False,financial_certified=False,
        training_rows=len(train),validation_rows=len(test),validation_signal_rows=int(eligible.sum()),
        validation_mean_net_target=float(net.mean()) if len(net) else None,
        overlapping_labels_not_portfolio_returns=True,validation_mae=float(np.abs(predicted-test.target).mean()),
        max_training_label_end=str(train.label_end.max()),validation_start=str(pd.Timestamp(split)),
        observed_start=str(data.date.min()),observed_end=str(data.date.max()),holding_minutes=horizon,lookback=lookback,
        fee_per_side=minute.FEE,slippage_per_side=minute.SLIPPAGE,
        message='별도 분봉 전환 모델 · 시간 분리 검증 완료 · 비용 반영 전진 페이퍼 검증 중')
    with threadpool_limits(limits=1):model.fit(data[names].to_numpy(),data.target.to_numpy())
    artifact=dict(conversion_version=2,model=model,proof=proof,symbols=symbols,day=cutoff.strftime('%Y%m%d'),strategy_id=row['id']+'-minute-v2',created=now,holding_minutes=horizon,lookback=lookback,fields=names,selection_count=g['selection_count'],max_weight=g['max_weight'])
    temp=p/'minute.tmp';joblib.dump(artifact,temp);temp.replace(p/'minute.joblib');record(row,proof)
    return artifact


def advance_frontier(row,internal):
    import joblib
    from .frontier import folder,save_status
    from .observations import record as observe
    now=time.time();p=folder(row);path=p/'minute.joblib'
    artifact=fit_variant(row,now)
    with store.connect() as db:r=db.execute("SELECT body FROM paper_books WHERE user_id=? AND market='kr'",(internal,)).fetchone()
    book=json.loads(r['body']) if r else dict(initial=1e8,cash=1e8,nav=1e8,holdings={},trades=[],equity=[],pending=[])
    frame=minute.load_prices(artifact['symbols'],now,limit=100)
    book=minute.advance_book(book,frame,artifact,now)
    with store.connect() as db:db.execute("INSERT INTO paper_books VALUES(?,'kr',?,?,?) ON CONFLICT(user_id,market) DO UPDATE SET body=excluded.body,updated=excluded.updated",(internal,'frontier-'+row['id'],json.dumps(book,ensure_ascii=False),now))
    from .observations import stock_quotes,mark
    value=mark(book,stock_quotes(book['holdings'],'kr'),now)
    if value:observe(internal,'kr','deployed',value['nav'],book['initial'],'minute forward paper',value['source_ts'],now)
    save_status(row,state='forward_paper',minute=artifact['proof'],message=artifact['proof']['message'])


def validation_tick(limit=2):
    audit_all()
    if not store.setting('minute_validation_enabled',True):return
    with store.connect() as db:ids=[r[0] for r in db.execute("SELECT strategy_id FROM minute_validation WHERE json_extract(body,'$.state')='queued' ORDER BY updated LIMIT ?",(limit,))]
    lookup={r['id']:r for r in catalogue.rows()}
    for identity in ids:
        row=lookup.get(identity)
        if not row:continue
        try:fit_variant(row,time.time())
        except Exception as exc:record(row,dict(state='blocked',financial_certified=False,message=str(exc)[:240]))

if __name__=='__main__':
    while True:
        validation_tick();time.sleep(5)
