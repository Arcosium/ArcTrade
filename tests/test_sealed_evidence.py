import hashlib,json
import pandas as pd
import pytest
from autofolio import research_input as r


def saved(root,market='kr',day='2026-10-08'):
    folder=root/'research_inputs/20231011-20261010';folder.mkdir(parents=True)
    p=folder/f'{market}.parquet'
    row=dict(date=pd.Timestamp(day),symbol='A',open=10,high=11,low=9,close=10,volume=100,
             eligible=True,sector='UNKNOWN',tradable_buy=True,tradable_sell=True)
    pd.DataFrame([row]).to_parquet(p,index=False)
    info=dict(ready=True,version=r.VERSION,market=market,end=day,snapshot_hash=hashlib.sha256(p.read_bytes()).hexdigest())
    (folder/f'{market}.audit.json').write_text(json.dumps(info));return p


def test_sealed_recovery_preserves_dated_eligibility(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'RUNS',tmp_path);monkeypatch.setattr(r,'window',lambda:('20231014','20261013'))
    p=saved(tmp_path);frame,path=r.sealed_evidence('kr')
    assert path==p and frame.date.max()==pd.Timestamp('2026-10-08')
    assert bool(frame.eligible.iloc[0])


def test_modified_or_future_evidence_rejected(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'RUNS',tmp_path);monkeypatch.setattr(r,'window',lambda:('20231014','20261013'))
    p=saved(tmp_path);p.write_bytes(p.read_bytes()+b'changed')
    with pytest.raises(ValueError,match='hash_mismatch'):r.sealed_evidence('kr')
    monkeypatch.setattr(r,'window',lambda:('20231001','20261007'))
    with pytest.raises(FileNotFoundError):r.sealed_evidence('kr')


def test_missing_evidence_remains_closed(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'RUNS',tmp_path)
    with pytest.raises(FileNotFoundError):r.sealed_evidence('us')
