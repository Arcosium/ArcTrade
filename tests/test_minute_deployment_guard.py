import pytest
from autofolio import deployment,store
from autofolio.public_routes import timestamp


def test_daily_model_cannot_activate_when_minute_conversion_fails(monkeypatch,tmp_path):
    from autofolio import research,learning,catalogue,minute_validation
    monkeypatch.setattr(store,'DATA',tmp_path);monkeypatch.setattr(store,'DB',tmp_path/'state.sqlite')
    monkeypatch.setattr(deployment,'RUNS',tmp_path)
    store.initialize();research.initialize();deployment.initialize()
    store.set_setting('us_minute_paper_enabled',True)
    with store.connect() as db:
        db.execute("INSERT INTO model_deployments VALUES('new',1,'us-paper','s','source','queued',1,1,NULL,'')")
        db.execute("INSERT INTO strategy_assignments VALUES(1,'us-paper','old',1,'paper_ready')")
    def retrain(source,dest):
        dest.mkdir(parents=True);p=dest/'model.joblib';p.write_text('fixture');return p
    monkeypatch.setattr(learning,'retrain',retrain)
    monkeypatch.setattr(catalogue,'rows',lambda:[{'id':'s'}])
    monkeypatch.setattr(minute_validation,'fit_variant',lambda *a:(_ for _ in ()).throw(ValueError('분봉 검증 실패')))
    with pytest.raises(ValueError,match='분봉 검증'):deployment.run('new')
    with store.connect() as db:
        assert db.execute('SELECT strategy_id FROM strategy_assignments').fetchone()[0]=='old'
        assert db.execute('SELECT status FROM model_deployments').fetchone()[0]=='failed'


def test_minute_trade_timestamp_is_korean_exchange_time():
    assert timestamp('202610141100')==timestamp('2026-10-14T11:00:00+09:00')
