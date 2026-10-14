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
