import sqlite3
import pandas as pd
from autofolio import minute_data as d


def test_us_observed_ohlc_closed_minutes_override_archive(tmp_path,monkeypatch):
    root=tmp_path;monkeypatch.setattr(d,'ROOT',root)
    p=root/'USA/1m/AAPL';p.mkdir(parents=True)
    t=int(pd.Timestamp('2026-10-14 08:01',tz='UTC').timestamp()*1000)
    pd.DataFrame(dict(t=[t,t+60000],o=[100.,100.],h=[101.,101.],l=[99.,99.],c=[100.,100.],v=[10.,10.])).to_parquet(p/'2026-10.parquet')
    dbpath=root/'USA/realtime.db'
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE bars(symbol TEXT,ts INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL)')
        db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?)',[
            ('AAPL',t//1000,101,102,100,101,20),('AAPL',t//1000+60,102,103,101,102,20)])
    result=d.load_us(['AAPL'],t/1000+90,None,1000)
    assert len(result)==1 and result.iloc[0].close==101
    assert result.iloc[0].open==101 and result.iloc[0].volume==20
    assert result.iloc[0].source=='KIS observed executions'
    assert str(result.iloc[0].date)=='2026-10-14 04:01:00'


def test_crypto_venue_and_base_volume_preserved_and_unclosed_excluded(tmp_path):
    t=int(pd.Timestamp('2026-10-17 08:00',tz='UTC').timestamp()*1000)
    p=tmp_path/'bars/date=2026-10-17';p.mkdir(parents=True)
    pd.DataFrame(dict(ts=[t,t+60000,t],base=['BTC','BTC','ETH'],venue=['binance']*3,
        open=[100.,101.,50.],high=[100.,101.,50.],low=[100.,101.,50.],close=[100.,101.,50.],volume=[.123,.2,2.])).to_parquet(p/'part.parquet')
    result=d.load_crypto(['BTC'],t/1000+90,tmp_path,80)
    assert len(result)==1 and result.iloc[0].volume==.123
    assert result.iloc[0].source=='binance' and result.iloc[0].symbol=='BTC'
