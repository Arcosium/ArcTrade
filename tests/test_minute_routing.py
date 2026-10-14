import copy
from autofolio import paper,store
from autofolio.forward_validation import progress


def test_enabled_minute_accounts_never_run_daily_executor(monkeypatch):
    monkeypatch.setattr(store,'setting',lambda name,default=False:name.endswith('_minute_paper_enabled'))
    monkeypatch.setattr(paper,'_advance',lambda dep:(_ for _ in ()).throw(AssertionError('daily execution')))
    for market in ('kr','us','crypto'):
        assert paper.advance({'target':market+'-paper'}) is None


def test_progress_excludes_daily_and_partial_exits_and_does_not_certify():
    b=dict(minute={'activated':100},trades=[dict(date='20261001',side='sell',qty=1,code='X')])
    def trade(side,qty,day):return dict(date=day,code='X',side=side,qty=qty,signal_ts=101,execution='next_observed_close')
    b['trades'] += [trade('buy',1,'202610020900'),trade('sell',.5,'202610021000')]
    assert progress(b,'crypto')['closed_positions']==0
    b['trades'].append(trade('sell',.5,'202610031000'))
    p=progress(b,'crypto')
    assert p['closed_positions']==1 and p['active_days']==2
    assert p['state']=='collecting' and p['financial_certified'] is False
