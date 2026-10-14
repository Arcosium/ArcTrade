import pytest
from autofolio import deployment,store
from autofolio.public_routes import timestamp


def test_daily_research_cannot_replace_minute_paper(monkeypatch):
    monkeypatch.setattr(store,'setting',lambda key,default=None:key=='kr_minute_paper_enabled')
    with pytest.raises(ValueError,match='분봉 검증'):
        deployment.request_retrain(1,'daily-candidate','kr-paper')


def test_minute_trade_timestamp_is_korean_exchange_time():
    assert timestamp('202610141100')==timestamp('2026-10-14T11:00:00+09:00')
