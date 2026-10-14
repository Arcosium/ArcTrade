import json
import time
import pandas as pd
import pytest
from autofolio import minute_validation as v, frontier, store

@pytest.fixture
def database(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DB',tmp_path/'state.db');monkeypatch.setattr(store,'DATA',tmp_path)
    store.initialize();return tmp_path

def artifact(now,market='kr'):
    cutoff=pd.Timestamp.fromtimestamp(now,v.minute.market_tz(market)).tz_localize(None)
    return dict(conversion_version=3,market=market,created=now-60,
        proof=dict(source_strategy='a',interval='minute',fit_rows=2000,training_rows=1600,validation_rows=400,
          observed_start=str(cutoff-pd.Timedelta(days=7)),observed_end=str(cutoff-pd.Timedelta(minutes=5)),
          max_training_label_end=str(cutoff-pd.Timedelta(days=2)),validation_start=str(cutoff-pd.Timedelta(days=1))))

@pytest.mark.parametrize('market',['kr','us','crypto','timefolio'])
def test_cache_market_age_and_causal_boundaries(market):
    now=time.time();row=dict(id='a',market=market);a=artifact(now,v.paper_market(row))
    assert v.cached_valid(a,row,now)
    assert not v.cached_valid(dict(a,market='crypto' if market!='crypto' else 'us'),row,now)
    assert not v.cached_valid(dict(a,created=now+1),row,now)
    assert not v.cached_valid(dict(a,created=now-v.CACHE_MAX_AGE-1),row,now)
    assert not v.cached_valid(dict(a,proof=dict(a['proof'],observed_end='2099-01-01')),row,now)
    assert not v.cached_valid(dict(a,proof=dict(a['proof'],max_training_label_end=a['proof']['validation_start'])),row,now)

def test_legacy_kr_reuse_does_not_certify_other_market_or_changed_target():
    now=time.time();a=artifact(now);a.pop('market');a['conversion_version']=2
    assert v.cached_valid(a,dict(id='a',market='kr'),now)
    assert not v.cached_valid(a,dict(id='a',market='us'),now)
    assert not v.cached_valid(a,dict(id='a',market='kr',genome={'target_kind':'risk_adjusted'}),now)

def test_audit_enqueues_all_markets_and_reports_daily_as_uncertified(database,monkeypatch):
    from autofolio import public_routes,evaluation
    rows=[dict(id=m,market=m,owner_id=1,genome={'engine':'learned_v1'}) for m in v.MARKETS]
    rows.append(dict(id='other',market='us',owner_id=2,genome={'engine':'learned_v1'}))
    monkeypatch.setattr(public_routes,'owner_id',lambda:1);monkeypatch.setattr(v.catalogue,'rows',lambda:rows)
    monkeypatch.setattr(evaluation,'selection_metrics',lambda row:{'sharpe':1})
    assert v.audit_all()=={'queued':4}
    with store.connect() as db:proofs=[json.loads(r[0]) for r in db.execute('SELECT body FROM minute_validation')]
    assert {p['market'] for p in proofs}==set(v.MARKETS)
    assert all(not p['financial_certified'] and not p['original_daily_certified_as_minute'] for p in proofs)
    assert v.audit_all()=={'queued':4}

def test_frontier_routes_every_market_through_minute_engine(database,monkeypatch):
    from autofolio import public_routes,paper
    monkeypatch.setattr(public_routes,'owner_id',lambda:1);monkeypatch.setattr(paper,'initialize',lambda:None)
    monkeypatch.setattr(v,'audit_all',lambda:None)
    monkeypatch.setattr(frontier,'members',lambda market,uid:[dict(id=market,market=market)])
    monkeypatch.setattr(frontier,'folder',lambda row:database/row['id']);calls=[]
    monkeypatch.setattr(v,'advance_frontier',lambda row,uid:calls.append(row['market']))
    monkeypatch.setattr(paper,'_advance',lambda dep:pytest.fail('daily engine must not run'))
    frontier.tick();assert set(calls)==set(v.MARKETS)

@pytest.mark.parametrize('market,feature,target',[('us','ohlc','return'),('crypto','all','risk_adjusted')])
def test_fit_preserves_registered_model_features_target_and_market(database,monkeypatch,market,feature,target):
    import numpy as np
    from autofolio import model_recipe
    days=pd.date_range('2026-09-20',periods=7,freq='D')
    dates=pd.DatetimeIndex(np.concatenate([pd.date_range(str(d.date())+' 10:00',periods=350,freq='min').values for d in days]))
    close=100+np.sin(np.arange(len(dates))*.13)
    frame=pd.DataFrame(dict(date=dates,symbol='A',close=close,open=close-.03,high=close+.1,low=close-.1,volume=1000+np.arange(len(dates))%150))
    parquet=database/'prices.parquet';frame.to_parquet(parquet)
    source=database/'source.json';source.write_text(json.dumps({'recipe':{'input':{'path':str(parquet)}}}))
    with store.connect() as db:
        db.execute('INSERT INTO strategies VALUES(?,?,?,?,?,?,?)',('a','a','a','a','{}',str(source),time.time()))
        db.execute('CREATE TABLE minute_validation(strategy_id TEXT PRIMARY KEY,body TEXT,updated REAL)')
    monkeypatch.setattr(model_recipe,'verify',lambda recipe:None)
    monkeypatch.setattr(frontier,'folder',lambda row:database/'model')
    seen=[]
    def load(symbols,now,**kwargs):seen.append(kwargs['market']);return frame
    monkeypatch.setattr(v.minute,'load_prices',load)
    row=dict(id='a',market=market,genome=dict(model='ridge',regularization=1,feature_set=feature,
        holding_sessions=3,lookback=20,selection_count=2,target_kind=target))
    now=pd.Timestamp('2026-09-28 12:00',tz=v.minute.market_tz(market)).timestamp()
    a=v._fit_variant(row,now)
    assert seen==[market] and a['market']==market and a['target_kind']==target
    assert a['proof']['model']=='ridge' and a['genome']==row['genome']
    assert a['proof']['observed_days']==7 and not a['proof']['financial_certified']
    assert {'body','range','high_move','low_move'} <= set(a['fields'])
    assert pd.Timestamp(a['proof']['max_training_label_end'])<pd.Timestamp(a['proof']['validation_start'])
    assert a['proof']['validation_mae']<.1
    assert v.cached_valid(a,row,now)
