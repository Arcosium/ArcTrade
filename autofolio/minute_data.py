"""Read canonical observed minute bars; never synthesize missing candles."""
import sqlite3
from contextlib import closing
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path.home()/'vault/CryptoBars/data'
COLUMNS = ['date','symbol','close','volume','source','open','high','low']


def _clean(data, symbol, now, market, limit):
    if data.empty:return pd.DataFrame(columns=COLUMNS)
    tz='America/New_York' if market=='us' else 'UTC'
    data=data.copy()
    data['date']=pd.to_datetime(data.ts,unit='ms',utc=True).dt.tz_convert(tz).dt.tz_localize(None)
    cutoff=pd.Timestamp(now,unit='s',tz='UTC').tz_convert(tz).tz_localize(None).floor('min')
    data=data[data.date.lt(cutoff) & np.isfinite(data.close) & data.close.gt(0)]
    data['volume']=data.volume.where(np.isfinite(data.volume) & data.volume.ge(0),0.)

    data=data[np.isfinite(data[['open','high','low']]).all(axis=1) & data[['open','high','low']].gt(0).all(axis=1)]
    data=data[data.high.ge(data[['open','close','low']].max(axis=1)) & data.low.le(data[['open','close','high']].min(axis=1))]
    data['symbol']=symbol
    return data.drop_duplicates('date',keep='last').sort_values('date').tail(limit)[COLUMNS]


def load_us(symbols, now, path, limit):
    import pyarrow.parquet as pq
    dbpath=Path(path) if path is not None else ROOT/'USA/realtime.db'
    frames=[]
    with closing(sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        for symbol in symbols:
            base=symbol.split('@')[0];parts=[];count=0
            # A small live read must never resurrect an older archival minute.
            if limit>100:
                for file in sorted((ROOT/'USA/1m'/base).glob('*.parquet'),reverse=True):
                    if file.stem>pd.Timestamp(now,unit='s',tz='UTC').strftime('%Y-%m'):continue
                    d=pq.ParquetFile(file).read(columns=['t','o','h','l','c','v'],use_threads=False).to_pandas().rename(columns={'t':'ts','o':'open','h':'high','l':'low','c':'close','v':'volume'})
                    d['source']='Polygon historical adjusted';parts.insert(0,d)
                    count+=len(_clean(d,symbol,now,'us',limit))
                    if count>=limit:break
            rows=db.execute('SELECT ts,close,volume,open,high,low FROM bars WHERE symbol=? AND ts<? ORDER BY ts DESC LIMIT ?',
                            (base,int(now)//60*60,limit)).fetchall()
            if rows:
                d=pd.DataFrame(rows,columns=['ts','close','volume','open','high','low']);d.ts*=1000;d['source']='KIS observed executions';parts.append(d)
            if parts:frames.append(_clean(pd.concat(parts,ignore_index=True),symbol,now,'us',limit))
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)


def load_crypto(symbols, now, path, limit):
    import pyarrow.parquet as pq
    root=Path(path) if path is not None else ROOT
    frames=[];wanted={s.split('@')[0]:s for s in symbols};buffers=[]
    # Partition and row filtering bounds memory; retain source venue to avoid
    # treating venue switches as one continuous historical price path.
    days=max(2,int(np.ceil(limit/1440))+1)
    cutoff=pd.Timestamp(now,unit='s',tz='UTC')
    for folder in sorted((root/'bars').glob('date=*'))[-days:]:
        if folder.name[5:]>cutoff.strftime('%Y-%m-%d'):continue
        for file in sorted(folder.glob('*.parquet')):
            d=pq.read_table(file,columns=['ts','base','close','volume','venue','open','high','low'],filters=[('base','in',list(wanted))]).to_pandas()
            if len(d):buffers.append(d)
    buf=pd.concat(buffers,ignore_index=True) if buffers else pd.DataFrame()
    for base,symbol in wanted.items():
        parts=[];count=0
        if limit>100:
            for file in sorted((root/'history'/('base='+base)).glob('part-*.parquet'),reverse=True):
                if file.stem[5:]>cutoff.strftime('%Y-%m'):continue
                d=pq.ParquetFile(file).read(columns=['ts','close','volume','venue','open','high','low'],use_threads=False).to_pandas()
                d['source']=d.venue;parts.insert(0,d)
                count+=len(_clean(d,symbol,now,'crypto',limit))
                if count>=limit:break
        if len(buf):
            d=buf[buf.base.eq(base)].copy();d['source']=d.venue;parts.append(d)
        if parts:frames.append(_clean(pd.concat(parts,ignore_index=True),symbol,now,'crypto',limit))
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
