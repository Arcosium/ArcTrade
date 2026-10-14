from autofolio.observations import mark


def test_complete_fresh_quotes_required_and_original_book_preserved():
    book=dict(cash=50,holdings={'A':{'qty':2},'B':{'qty':1}})
    quotes={'A':dict(ts=999,last=20),'B':dict(ts=998,last=30)}
    assert mark(book,quotes,1000)==dict(nav=120,source_ts=998)
    assert mark(book,{'A':quotes['A']},1000) is None
    assert mark(book,quotes,3000) is None
    assert book['cash']==50 and book['holdings']['A']['qty']==2


def test_us_and_research_crypto_books_record_observed_minute_nav(tmp_path,monkeypatch):
    import json,sys,types
    from autofolio import observations as o,store,public_routes,paper,deployment
    monkeypatch.setattr(store,'DATA',tmp_path);monkeypatch.setattr(store,'DB',tmp_path/'test.db')
    store.initialize();paper.initialize();deployment.initialize()
    monkeypatch.setattr(public_routes,'owner_id',lambda:1)
    monkeypatch.setattr(o,'stock_quotes',lambda symbols,market:{'A':dict(last=110,ts=999)})
    monkeypatch.setitem(sys.modules,'integrations.crypto.app',types.SimpleNamespace(BOOKS={},book_view=lambda key:None))
    monkeypatch.setitem(sys.modules,'integrations.crypto.market',types.SimpleNamespace(quotes=lambda symbols:{'BTC':dict(last=120,ts=999000)}))
    with store.connect() as db:
        for market,symbol in [('us','A'),('crypto','BTC')]:
            book=dict(initial=100,cash=10,holdings={symbol:{'qty':1}})
            db.execute('INSERT INTO paper_books VALUES(?,?,?,?,?)',(1,market,market,json.dumps(book),999))
            db.execute('INSERT INTO model_deployments VALUES(?,?,?,?,?,?,?,?,?,?)',(market,1,market+'-paper','strategy-'+market,'source','ready',1,1,'artifact','ready'))
    assert o.tick(now=1000)==2
    with store.connect() as db:rows={r['market']:dict(r) for r in db.execute('SELECT * FROM paper_observations')}
    assert rows['us']['nav']==120 and rows['crypto']['nav']==130
    assert rows['crypto']['strategy_id']=='strategy-crypto'
    assert rows['us']['source_ts']==999
