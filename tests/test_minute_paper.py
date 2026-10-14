import copy
import datetime
import json
import sqlite3
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from autofolio import minute_paper as m


class Model:
    def predict(self,x):return np.full(len(x),.02)


def prices(end='2026-10-14 10:00',symbol='A',n=80):
    dates=pd.date_range(end=end,periods=n,freq='min')
    return pd.DataFrame(dict(date=dates,symbol=symbol,close=100+np.arange(n)*.1,volume=100000.))


def book():
    return dict(initial=100000.,cash=50000.,nav=100000.,holdings={'A':dict(code='A',name='A',qty=500,entry=100.,value=50000.,pnl_ratio=0.)},trades=[dict(date='20261013',side='buy')],equity=[dict(date='20261013',net_return=0)],pending=['daily'])


def artifact():return dict(strategy_id='kr-minute-test',model=Model(),proof={'interval':'minute','daily_os_certified':False})

def epoch(stamp):return pd.Timestamp(stamp).to_pydatetime().replace(tzinfo=m.TZ).timestamp()


def test_activation_preserves_history_and_does_not_replay():
    original=book();result=m.advance_book(original,prices(),artifact(),epoch('2026-10-14 10:04'))
    assert result['trades']==original['trades'] and result['cash']==original['cash']
    assert result['holdings']['A']['qty']==500 and result['pending']==[]
    assert result['minute']['pending'] is None
    assert result['minute_model']['daily_os_certified'] is False
    assert original==book()


def test_fill_requires_observed_after_decision_and_is_idempotent():
    b=book();now=epoch('2026-10-14 10:04')
    b=m.advance_book(b,prices(),artifact(),now)
    b['minute']['pending']=dict(created=now,sells=['A'],buys=[],caps={'A':100},strategy_id='kr-minute-test')
    b['minute']['last_signal']=now
    same=m.advance_book(b,prices('2026-10-14 10:03'),artifact(),now+120)
    assert len(same['trades'])==1
    filled=m.advance_book(b,prices('2026-10-14 10:05'),artifact(),now+240)
    assert len(filled['trades'])==2 and filled['holdings']['A']['qty']==400
    assert filled['trades'][-1]['price']==pytest.approx(107.9*(1-m.SLIPPAGE))
    assert filled['trades'][-1]['fee']>0
    again=m.advance_book(filled,prices('2026-10-14 10:05'),artifact(),now+241)
    assert len(again['trades'])==2 and again['cash']==filled['cash']


def test_stale_or_missing_holding_blocks_all_fills():
    b=book();now=epoch('2026-10-14 10:04')
    b=m.advance_book(b,prices(),artifact(),now)
    b['minute']['pending']=dict(created=now,sells=['A'],buys=[],caps={'A':500},strategy_id='s')
    for frame in (prices('2026-10-14 09:00'),prices(symbol='B')):
        result=m.advance_book(b,frame,artifact(),now+300)
        assert result['cash']==b['cash'] and result['trades']==b['trades']


def test_expired_pending_and_after_session_do_not_replay():
    b=m.advance_book(book(),prices(),artifact(),epoch('2026-10-14 10:04'))
    b['minute']['pending']=dict(created=epoch('2026-10-14 10:04'),sells=['A'],buys=[],caps={'A':500},strategy_id='s')
    result=m.advance_book(b,prices('2026-10-14 11:00'),artifact(),epoch('2026-10-14 11:04'))
    assert result['trades']==b['trades']
    result=m.advance_book(b,prices('2026-10-14 15:29'),artifact(),epoch('2026-10-14 15:31'))
    assert result==b


def test_training_purges_future_labels_and_overnight_gaps():
    blocks=[prices(str(day.date())+' 14:00',n=250) for day in pd.bdate_range('2026-09-21',periods=10)]
    p=pd.concat(blocks,ignore_index=True)
    p['close']=100+np.sin(np.arange(len(p))*.05)+np.arange(len(p))*.0001
    cutoff=pd.Timestamp('2026-10-02 12:00')
    model,proof=m.fit(p,cutoff)
    assert pd.Timestamp(proof['train_max_label_end'])<pd.Timestamp(proof['validation_start'])
    assert pd.Timestamp(proof['fit_max_label_end'])<cutoff
    assert not proof['daily_os_certified']
    f=m.features(p)
    end=f[f.date.dt.hour.eq(14)]
    assert end.target.isna().all()
    changed=p.copy();changed.loc[changed.date.ge(cutoff),'close']=1e9
    _,again=m.fit(changed,cutoff)
    assert proof==again


def test_close_only_source_read_has_no_invented_ohlc_and_resets_volume(tmp_path):
    path=tmp_path/'bars.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE bars(code TEXT,ts TEXT,close REAL,vol_cum REAL,PRIMARY KEY(code,ts))')
        db.executemany('INSERT INTO bars VALUES(?,?,?,?)',[
            ('A','202610130959',100,1000),('A','202610131000',101,1100),
            ('A','202610140959',102,300),('A','202610141000',103,350),('A','202610141004',104,400)])
    p=m.load_prices(['A'],epoch('2026-10-14 10:04'),path)
    assert len(p)==4 and 'open' not in p.columns
    assert list(p.volume)==[0,100,0,50]


def test_minimum_holding_and_positive_net_signal_gate():
    b=m.advance_book(book(),prices(),artifact(),epoch('2026-10-14 10:04'))
    class Negative:
        def predict(self,x):return np.full(len(x),-.01)
    a=dict(artifact(),model=Negative())
    early=m.advance_book(b,prices('2026-10-14 10:06'),a,epoch('2026-10-14 10:10'))
    assert early['minute']['pending']['sells']==[] and early['minute']['pending']['buys']==[]
    later=m.advance_book(b,prices('2026-10-14 10:36'),a,epoch('2026-10-14 10:40'))
    assert later['minute']['pending']['sells']==['A']


def test_execution_volume_caps_fills_and_replacement_cancels_old_orders():
    now=epoch('2026-10-14 10:04')
    b=m.advance_book(book(),prices(),artifact(),now)
    b['minute']['pending']=dict(created=now,sells=['A'],buys=[],caps={'A':500},strategy_id='kr-minute-test')
    b['minute']['last_signal']=now
    thin=prices('2026-10-14 10:05');thin.loc[thin.index[-1],'volume']=1000
    result=m.advance_book(b,thin,artifact(),now+240)
    assert result['trades'][-1]['qty']==10
    replaced=m.advance_book(b,thin,dict(artifact(),strategy_id='replacement'),now+240)
    assert replaced['trades']==b['trades']


def test_nonfinite_collector_values_do_not_create_liquidity(tmp_path):
    path=tmp_path/'bars.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE bars(code TEXT,ts TEXT,close REAL,vol_cum REAL,PRIMARY KEY(code,ts))')
        db.executemany('INSERT INTO bars VALUES(?,?,?,?)',[
            ('A','202610140959',100,100),('A','202610141000',101,float('inf')),
            ('A','202610141001',float('inf'),200),('A','202610141002',102,250)])
    result=m.load_prices(['A'],epoch('2026-10-14 10:04'),path)
    assert np.isfinite(result[['close','volume']]).all().all()
    assert result.volume.sum()==0


def market_prices(market,end,symbol='A',volume=100000.):
    p=prices(end,symbol);p['open']=p.close;p['high']=p.close;p['low']=p.close;p['volume']=volume;p['source']='test observed'
    return p


def market_epoch(stamp,market):
    return pd.Timestamp(stamp).to_pydatetime().replace(tzinfo=m.market_tz(market)).timestamp()


def test_crypto_weekend_fractional_fills_and_conservative_fees():
    a=dict(artifact(),market='crypto');now=market_epoch('2026-10-17 10:04','crypto')
    b=book();b['holdings']={};b['cash']=b['nav']=100000
    b=m.advance_book(b,market_prices('crypto','2026-10-17 10:03'),a,now)
    b['minute']['pending']=dict(created=now,sells=[],buys=[dict(symbol='A')],caps={'A':.25},strategy_id=a['strategy_id'])
    p=market_prices('crypto','2026-10-17 10:05',volume=10.)
    r=m.advance_book(b,p,a,now+120)
    assert r['holdings']['A']['qty']==pytest.approx(.1)
    t=r['trades'][-1]
    assert t['fee']==pytest.approx(.1*t['price']*.001)
    assert r['cash']+r['holdings']['A']['value']==pytest.approx(r['nav'])


def test_us_extended_hours_and_closed_weekend():
    a=dict(artifact(),market='us')
    for when in ['2026-10-14 04:04','2026-10-14 18:04','2026-10-18 21:04']:
        now=market_epoch(when,'us');p=market_prices('us',str(pd.Timestamp(when)-pd.Timedelta(minutes=1)))
        r=m.advance_book(book(),p,a,now)
        assert r['interval']=='minute'
    when='2026-10-17 10:04'
    assert m.advance_book(book(),market_prices('us',when),a,market_epoch(when,'us'))==book()


def test_signals_re_evaluated_each_new_minute_not_holding_horizon():
    now=epoch('2026-10-14 10:04');a=artifact()
    b=m.advance_book(book(),prices(),a,now)
    first=m.advance_book(b,prices('2026-10-14 10:05'),a,now+120)
    assert first['minute']['last_signal']==now+120
    second=m.advance_book(first,prices('2026-10-14 10:06'),a,now+180)
    assert second['minute']['last_signal']==now+180
    assert second['trades']==book()['trades']


def test_observed_source_boundary_purges_features_and_targets():
    p=prices(n=200);p['source']='historical'
    p.loc[p.index[100:],'source']='live'
    f=m.features(p)
    assert not f.loc[100:119,'ready'].any()
    assert f.loc[69:99,'target'].isna().all()
    assert f.loc[120,'ready']


def test_risk_adjusted_prediction_is_not_treated_as_gross_return():
    class RiskModel:
        def predict(self,x):return np.ones(len(x))*.1
    a=dict(artifact(),market='crypto',target_kind='risk_adjusted',model=RiskModel())
    now=market_epoch('2026-10-17 10:04','crypto');b=book();b['holdings']={};b['cash']=b['nav']=100000
    b=m.advance_book(b,market_prices('crypto','2026-10-17 10:03'),a,now)
    b=m.advance_book(b,market_prices('crypto','2026-10-17 10:05'),a,now+120)
    assert b['minute']['pending']['buys']==[]


def test_stop_take_checks_every_minute_before_minimum_holding():
    a=dict(artifact(),market='crypto',genome=dict(holding_sessions=30,rebalance_sessions=20,
        selection_count=10,sell_rule='stop_take',stop_loss=.01,take_profit=.01))
    now=market_epoch('2026-10-17 10:04','crypto')
    b=m.advance_book(book(),market_prices('crypto','2026-10-17 10:03'),a,now)
    b=m.advance_book(b,market_prices('crypto','2026-10-17 10:05'),a,now+120)
    assert b['minute']['pending']['sells']==['A']
    assert b['minute']['pending']['buys']==[]


def test_kr_close_volume_has_liquidity_and_downside_fields_without_fake_ohlc():
    f=m.features(prices(n=220),window=180)
    row=f[f.ready].iloc[-1]
    assert np.isfinite(row[['return_1','return_w','volume_change','adv20','volume_rank','drawdown','illiquidity','downside_vol']].astype(float)).all()
    assert 'open_gap' not in f and 'open' not in f


def test_run_uses_current_ready_assignment_and_isolates_market_failure(tmp_path,monkeypatch):
    path=tmp_path/'state.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE model_deployments(id TEXT,user_id INTEGER,target TEXT,strategy_id TEXT,status TEXT)')
        db.execute('CREATE TABLE strategy_assignments(user_id INTEGER,target TEXT,strategy_id TEXT)')
        db.executemany('INSERT INTO model_deployments VALUES(?,?,?,?,?)',[
            ('old',1,'us-paper','old-strategy','ready'),('us',1,'us-paper','current','ready'),
            ('crypto',1,'crypto-paper','coin','ready'),('kr',1,'kr-paper','stock','ready')])
        db.executemany('INSERT INTO strategy_assignments VALUES(?,?,?)',[
            (1,'us-paper','current'),(1,'crypto-paper','coin'),(1,'kr-paper','stock')])
    def connect():
        db=sqlite3.connect(path);db.row_factory=sqlite3.Row;return db
    monkeypatch.setattr(m.store,'connect',connect)
    monkeypatch.setattr(m.store,'setting',lambda key,default=False:key!='kr_minute_paper_enabled')
    called=[];status={}
    def advance(dep,**kwargs):
        called.append(dep['id'])
        if dep['id']=='us':raise ValueError('sample missing')
    monkeypatch.setattr(m,'advance',advance)
    monkeypatch.setattr(m.store,'event',lambda *args:None)
    monkeypatch.setattr(m.store,'set_setting',lambda k,v:status.update(v))
    m.run()
    assert called==['us','crypto'] and status['accounts']==1
    assert status['errors']==[dict(market='us',error='sample missing')]
