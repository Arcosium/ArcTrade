from datetime import date,timedelta
from copy import deepcopy
import pytest
from autofolio.validation import assess,aggregate
from autofolio.evaluation import selection_metrics,PROTOCOL
from autofolio.metrics import pareto_front,statistics_for


def case():
    days=[(date(2025,10,8)+timedelta(days=i)).strftime('%Y%m%d') for i in range(273)]
    trades=[]
    for i in range(0,len(days)-1,10):
        trades += [dict(date=days[i],code='TEST',side='buy',qty=10),
                   dict(date=days[i+1],code='TEST',side='sell',qty=10)]
    return dict(daily=[dict(date=d,nav=100,cash=50) for d in days],trades=trades)


def test_sparse_and_concentrated_trading_rejected():
    book=case();book['trades']=book['trades'][:4]
    evidence=assess(book,'20231008','20261007')
    assert not evidence['passed'] and evidence['closed_trades']==2
    assert 'insufficient_closed_trades' in evidence['reasons']
    book=case()
    for trade in book['trades']:trade['date']='20251009'
    assert 'insufficient_exit_days' in assess(book,'20231008','20261007')['reasons']


def test_partial_exits_not_counted_as_independent_trades_and_ros_not_selected():
    book=case();baseline=assess(book,'20231008','20261007')
    assert baseline['passed']
    split=[]
    for trade in book['trades']:
        split.extend([dict(trade,qty=5),dict(trade,qty=5)] if trade['side']=='sell' else [trade])
    book['trades']=split
    assert assess(book,'20231008','20261007')==baseline
    book['trades'].append(dict(date='20260709',code='TEST',side='sell',qty=100000))
    book['daily'].append(dict(date='20260709',nav=-1,cash=-1))
    assert assess(book,'20231008','20261007')==baseline


def test_unknown_evidence_and_inactive_cash_not_eligible():
    book=case()
    for row in book['daily']:row['cash']=row['nav']
    evidence=assess(book,'20231008','20261007')
    assert not evidence['passed'] and evidence['active_days']==0
    summary=dict(evaluation_protocol=PROTOCOL,performance={'os':dict(months=9,net_return=.5,mdd=-.1,negative_months=2,sharpe=2.)})
    assert selection_metrics(summary) is None
    summary['activity_validation']=aggregate([evidence]);assert selection_metrics(summary) is None
    summary['activity_validation']=aggregate([assess(case(),'20231008','20261007')])
    assert selection_metrics(summary)['net_return']==.5


def test_mdd_magnitude_minimized_and_tied_frontier_retained():
    rows=[dict(id='deep',net_return=.2,sharpe=1,mdd=-.5),dict(id='shallow',net_return=.2,sharpe=2,mdd=-.1),dict(id='tie',net_return=.2,sharpe=2,mdd=-.1)]
    assert {x['id'] for x in pareto_front(rows,loss_key='mdd')}=={'shallow','tie'}
    assert {x['id'] for x in pareto_front(rows,return_key='sharpe',loss_key='mdd')}=={'shallow','tie'}


def test_crypto_sharpe_annualization():
    rows=[dict(date='20260101',nav=101,previous_nav=100,net_daily_return=.01),dict(date='20260102',nav=100,previous_nav=101,net_daily_return=-1/101)]
    assert statistics_for(rows,365)['sharpe']==pytest.approx(statistics_for(rows,252)['sharpe']*(365/252)**.5)
