import pytest
from autofolio import frontier


def test_current_frontier_hides_dominated_and_old_cohorts(monkeypatch):
    rows=[dict(id='a',market='kr',owner_id=1,genome={'engine':'learned_v1'},cohort='new',evaluation_window=['20231014','20261013'],sessions=200,net_return=.3,sharpe=2.,mdd=-.1,negative_months=2),
          dict(id='b',market='kr',owner_id=1,genome={'engine':'learned_v1'},cohort='new',evaluation_window=['20231014','20261013'],sessions=200,net_return=.1,sharpe=1.,mdd=-.2,negative_months=3)]
    rows.append(dict(rows[0],id='old',cohort='old',evaluation_window=['20221014','20251013']))
    rows.append(dict(rows[0],id='other-owner',owner_id=2))
    monkeypatch.setattr(frontier.catalogue,'rows',lambda:rows)
    monkeypatch.setattr(frontier,'valid_result',lambda r:True)
    monkeypatch.setattr(frontier,'selection_metrics',lambda r:{k:r[k] for k in ['net_return','sharpe','mdd','negative_months']})
    assert [r['id'] for r in frontier.members('kr',1)]==['a']
    rows[1].update(net_return=.4,sharpe=3.,mdd=-.05,negative_months=1)
    assert [r['id'] for r in frontier.members('kr',1)]==['b']


def test_paper_id_isolated_from_user_and_market():
    assert frontier.identity(1,'kr','a')<0
    assert len({frontier.identity(u,m,s) for u in [1,2] for m in ['kr','us'] for s in ['a','b']})==8
