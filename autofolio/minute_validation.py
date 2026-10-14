"""Minute conversion checks and separate forward ledgers for daily candidates.

A daily winner is never silently certified as an intraday winner. Every source
gets an explicit audit record; a distinct minute model uses only observed close
and volume features, purged validation, fees and subsequent observed fills.
"""
import json
import time
from pathlib import Path
from . import store, catalogue, minute_paper as minute
from datetime import datetime

MARKETS = ('kr', 'us', 'crypto', 'timefolio')
CACHE_MAX_AGE = 7 * 86400


def paper_market(row):
    market = row.get('market', 'kr')
    if market not in MARKETS:raise ValueError('지원하지 않는 분봉 시장')
    return 'kr' if market == 'timefolio' else market


def audit_all():
    from .public_routes import owner_id
    from .research import visible
    from .evaluation import selection_metrics
    uid=owner_id();counts={};markets={};now=time.time()
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS minute_validation(strategy_id TEXT PRIMARY KEY,body TEXT NOT NULL,updated REAL NOT NULL)')
        known={r[0]:json.loads(r[1]) for r in db.execute('SELECT strategy_id,body FROM minute_validation')}
        for row in catalogue.rows():
            if row.get('market') not in MARKETS or not visible(row,dict(id=uid)):continue
            g=row.get('genome') or {}
            old=known.get(row['id'])
            if old:
                if old.get('state')=='forward_paper_probation' and old.get('source_target_kind','return')!=g.get('target_kind','return'):
                    body=dict(state='queued',market=row['market'],financial_certified=False,
                              original_daily_certified_as_minute=False,message='원본 학습 목표를 보존한 분봉 재검증 대기')
                    db.execute('UPDATE minute_validation SET body=?,updated=? WHERE strategy_id=?',(json.dumps(body,ensure_ascii=False),now,row['id']))
                continue
            state='queued' if selection_metrics(row) and g.get('engine')=='learned_v1' else 'rejected_daily_gate'
            body=dict(state=state,market=row['market'],original_daily_certified_as_minute=False,financial_certified=False,
                message='분봉 전환 검증 대기' if state=='queued' else '일봉 OS 표본 검증 미통과 · 분봉 운용 제외')
            db.execute('INSERT INTO minute_validation VALUES(?,?,?)',(row['id'],json.dumps(body,ensure_ascii=False),time.time()))
        for state,n in db.execute("SELECT json_extract(body,'$.state'),count(*) FROM minute_validation GROUP BY 1"):counts[state]=n
        for market,state,n in db.execute("SELECT json_extract(body,'$.market'),json_extract(body,'$.state'),count(*) FROM minute_validation GROUP BY 1,2"):
            markets.setdefault(market or 'kr',{})[state]=n
    store.set_setting('minute_validation_status',dict(counts=counts,markets=markets,updated=now))
    return counts


def record(row,proof):
    with store.connect() as db:db.execute('INSERT OR REPLACE INTO minute_validation VALUES(?,?,?)',(row['id'],json.dumps(proof,ensure_ascii=False),time.time()))


def cached_valid(artifact, row, now):
    """Legacy KR v2 remains reusable only while its dated evidence is valid."""
    import pandas as pd
    try:
        market=paper_market(row);version=artifact.get('conversion_version')
        if version not in (2,3) or (version==2 and market!='kr'):return False
        if version==2 and row.get('genome',{}).get('feature_set') in ('liquidity','volume','downside_risk'):return False
        if artifact.get('market','kr')!=market:return False
        if not 0 <= now-float(artifact['created']) <= CACHE_MAX_AGE:return False
        proof=artifact['proof'];g=row.get('genome',{})
        if g.get('news_mode','off')!='off' or g.get('target_kind','return')=='direction':return False
        if artifact.get('target_kind','return')!=g.get('target_kind','return'):return False
        if proof.get('source_strategy')!=row['id'] or proof.get('interval')!='minute':return False
        start=pd.Timestamp(proof['observed_start']);end=pd.Timestamp(proof['observed_end'])
        cutoff=pd.Timestamp(datetime.fromtimestamp(now,minute.market_tz(market)).replace(tzinfo=None))
        if not start<end<cutoff or (cutoff-end).total_seconds()>CACHE_MAX_AGE:return False
        if (end.normalize()-start.normalize()).days<4:return False
        if proof['training_rows']<1000 or proof['validation_rows']<200:return False
        # v2's writer already enforced 1,500 usable rows before the purge.
        # Purged rows are absent from train+validation, so that sum is not fit size.
        if version==3 and proof.get('fit_rows',0)<1500:return False
        if pd.Timestamp(proof['max_training_label_end'])>=pd.Timestamp(proof['validation_start']):return False
        return True
    except (KeyError,TypeError,ValueError,OverflowError):return False


def fit_variant(row, now):
    import fcntl,joblib
    from .frontier import folder
    p=folder(row);p.mkdir(parents=True,exist_ok=True)
    with (p/'minute.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if (p/'minute.joblib').exists():
            old=joblib.load(p/'minute.joblib')
            if cached_valid(old,row,now):return dict(old,market=paper_market(row),genome=row['genome'],target_kind=row['genome'].get('target_kind','return'))
        return _fit_variant(row,now)


def _fit_variant(row, now):
    import joblib,numpy as np,pandas as pd,hashlib
    from threadpoolctl import threadpool_limits
    from .learning import estimator, NEURAL_MODELS
    from .frontier import folder
    g=row['genome'];p=folder(row);p.mkdir(parents=True,exist_ok=True)
    market=paper_market(row);cost=minute.market_config(market);fee=cost['fee'];slippage=cost['slippage']
    with store.connect() as db:r=db.execute('SELECT source FROM strategies WHERE id=?',(row['id'],)).fetchone()
    recipe=json.loads(Path(r['source']).read_text())['recipe']
    from .model_recipe import verify
    verify(recipe)
    if g['model'] in NEURAL_MODELS:
        raise ValueError('신경망 분봉 시퀀스 전환은 별도 등록 필요')
    if g.get('news_mode','off')!='off':raise ValueError('뉴스 입력 전략은 분봉 시각 정합성 검증 대기')
    if g.get('target_kind','return') not in ('return','risk_adjusted'):raise ValueError('수익률 외 학습 목표는 분봉 비용 기준 변환 검증 대기')
    if g['feature_set']=='ohlc' and market=='kr':raise ValueError('OHLC 입력 전략은 실시간 OHLC 분봉 연결 검증 대기')
    # Registered universe stays separate from the collector; no source DB writes.
    import pyarrow.parquet as pq
    raw=pq.ParquetFile(recipe['input']['path']).read(columns=['date','symbol','close','volume'],use_threads=False).to_pandas()
    last=raw[raw.date.eq(raw.date.max())].copy();last['liquidity']=last.close*last.volume
    symbols=last.sort_values('liquidity',ascending=False).symbol.astype(str).head(30).tolist()
    names=['r1','r5','r20','trend']
    if g['feature_set'] in ('price_risk','all','downside_risk'):names+=['volatility']
    if g['feature_set'] in ('price_volume','volume','all','liquidity'):names+=['volume_ratio']
    if g['feature_set']=='downside_risk':names+=['downside_vol']
    if market!='kr':
        names=['return_1','return_w','range','body','trend']
        if g['feature_set'] in ('price_volume','all'):names+=['volume_ratio']
        if g['feature_set'] in ('price_risk','all'):names+=['volatility']
        if g['feature_set']=='ohlc':names=['return_1','return_w','open_gap','high_move','low_move','body','range']
        if g['feature_set']=='all':names+=['open_gap','high_move','low_move','volume_rank','volume_change']
    if g['feature_set']=='volume':names=['volume_ratio','volume_change','volume_rank','return_1']
    if g['feature_set']=='downside_risk':names=['return_1','return_w','trend','downside_vol','drawdown']
    if g['feature_set']=='liquidity':names=['return_1','return_w','volume_ratio','volume_rank','illiquidity']
    horizon=g['holding_sessions'];lookback=g['lookback']
    frame=minute.load_prices(symbols,now,market=market);data=minute.features(frame,lookback,horizon,market=market)
    cutoff=pd.Timestamp(datetime.fromtimestamp(now,minute.market_tz(market)).replace(tzinfo=None)).floor('min')
    data=data[data.ready & np.isfinite(data.target) & np.isfinite(data[names]).all(axis=1) & data.label_end.lt(cutoff)]
    if len(data)<1500 or data.date.dt.normalize().nunique()<5:raise ValueError(f'분봉 기간·표본 부족: {data.date.dt.normalize().nunique()}일/{len(data)}행 (최소 5일/1500행)')
    data=data.copy();data['gross_target']=data.target
    if g.get('target_kind','return')=='risk_adjusted':data['target']=data.target/(data.volatility.clip(lower=.001)*np.sqrt(horizon))
    split=sorted(data.date.unique())[int(data.date.nunique()*.8)]
    train=data[data.label_end.lt(split)];test=data[data.date.ge(split)]
    if len(train)<1000 or len(test)<200:raise ValueError('분봉 시간 분리 검증 표본 부족')
    model=estimator(g,len(names))
    with threadpool_limits(limits=1):
        model.fit(train[names].to_numpy(),train.target.to_numpy())
        predicted=model.predict(test[names].to_numpy())
    if g.get('target_kind','return')=='risk_adjusted':predicted=predicted*test.volatility.clip(lower=.001).to_numpy()*np.sqrt(horizon)
    if not np.isfinite(predicted).all() or np.max(np.abs(predicted))>.5:raise ValueError('분봉 예측값 비정상')
    eligible=predicted>2*(fee+slippage)
    net=test.gross_target.to_numpy()[eligible]-2*(fee+slippage)
    proof=dict(state='forward_paper_probation',interval='minute',market=row['market'],source_strategy=row['id'],symbols=symbols,
        observed_days=int(data.date.dt.normalize().nunique()),converted_at=now,
        model=g['model'],feature_adapter='observed_close_volume_v1' if market=='kr' else 'observed_ohlcv_v1',fields=names,
        target_adapter='next_observed_close_return',source_target_kind=g.get('target_kind','return'),
        original_daily_certified_as_minute=False,financial_certified=False,
        fit_rows=len(data),training_rows=len(train),validation_rows=len(test),validation_signal_rows=int(eligible.sum()),
        validation_mean_net_target=float(net.mean()) if len(net) else None,
        overlapping_labels_not_portfolio_returns=True,validation_mae=float(np.abs(predicted-test.gross_target).mean()),
        max_training_label_end=str(train.label_end.max()),validation_start=str(pd.Timestamp(split)),
        observed_start=str(data.date.min()),observed_end=str(data.date.max()),holding_minutes=horizon,lookback=lookback,
        fee_per_side=fee,slippage_per_side=slippage,
        message='별도 분봉 전환 모델 · 시간 분리 검증 완료 · 비용 반영 전진 페이퍼 검증 중')
    with threadpool_limits(limits=1):model.fit(data[names].to_numpy(),data.target.to_numpy())
    artifact=dict(conversion_version=3,market=market,genome=g,target_kind=g.get('target_kind','return'),model=model,proof=proof,symbols=symbols,day=cutoff.strftime('%Y%m%d'),strategy_id=row['id']+'-minute-v3-'+str(int(now)),created=now,holding_minutes=horizon,lookback=lookback,fields=names,selection_count=g['selection_count'],max_weight=g.get('max_weight',.1))
    temp=p/'minute.tmp';joblib.dump(artifact,temp);temp.replace(p/'minute.joblib');record(row,proof)
    return artifact


def advance_frontier(row,internal):
    from .frontier import save_status
    from .forward_validation import progress
    from .observations import record as observe
    now=time.time();market=paper_market(row)
    artifact=fit_variant(row,now)
    with store.connect() as db:r=db.execute("SELECT body FROM paper_books WHERE user_id=? AND market=?",(internal,market)).fetchone()
    initial=1e8 if market=='kr' else 1e5
    book=json.loads(r['body']) if r else dict(initial=initial,cash=initial,nav=initial,holdings={},trades=[],equity=[],pending=[])
    symbols=list(dict.fromkeys(artifact['symbols']+list(book['holdings'])))
    frame=minute.load_prices(symbols,now,limit=max(100,artifact.get('lookback',20)+5),market=market)
    book=minute.advance_book(book,frame,artifact,now)
    with store.connect() as db:db.execute("INSERT INTO paper_books VALUES(?,?,?,?,?) ON CONFLICT(user_id,market) DO UPDATE SET body=excluded.body,updated=excluded.updated",(internal,market,'frontier-'+row['id'],json.dumps(book,ensure_ascii=False),now))
    from .observations import stock_quotes,mark
    if market=='crypto':
        from integrations.crypto.market import quotes
        raw=quotes(list(book['holdings']));q={k:dict(last=v['last'],ts=v['ts']/1000) for k,v in raw.items()}
    else:q=stock_quotes(book['holdings'],market)
    value=mark(book,q,now)
    if value:observe(internal,market,'deployed',value['nav'],book['initial'],'minute forward paper',value['source_ts'],now)
    save_status(row,state='forward_paper',minute=artifact['proof'],forward=progress(book,market),message=artifact['proof']['message'])


def validation_tick(limit=2):
    audit_all()
    if not store.setting('minute_validation_enabled',True):return
    with store.connect() as db:ids=[r[0] for r in db.execute("SELECT strategy_id FROM minute_validation WHERE json_extract(body,'$.state')='queued' OR (json_extract(body,'$.state')='waiting_data' AND updated<?) ORDER BY updated LIMIT ?",(time.time()-3600,limit))]
    lookup={r['id']:r for r in catalogue.rows()}
    for identity in ids:
        row=lookup.get(identity)
        if not row:continue
        try:fit_variant(row,time.time())
        except Exception as exc:
            waiting=isinstance(exc,ValueError) and '표본 부족' in str(exc)
            record(row,dict(state='waiting_data' if waiting else 'blocked',market=row['market'],financial_certified=False,original_daily_certified_as_minute=False,message=str(exc)[:240]))

if __name__=='__main__':
    while True:
        validation_tick();time.sleep(5)
