from autofolio.observations import mark


def test_complete_fresh_quotes_required_and_original_book_preserved():
    book=dict(cash=50,holdings={'A':{'qty':2},'B':{'qty':1}})
    quotes={'A':dict(ts=999,last=20),'B':dict(ts=998,last=30)}
    assert mark(book,quotes,1000)==dict(nav=120,source_ts=998)
    assert mark(book,{'A':quotes['A']},1000) is None
    assert mark(book,quotes,3000) is None
    assert book['cash']==50 and book['holdings']['A']['qty']==2
