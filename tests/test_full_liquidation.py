from playwright.sync_api import sync_playwright
from integrations.timefolio.Auto_folio.autofolio.timefolio_browser import TimefolioBrowser
from autofolio.timefolio_live import order_payload


def test_residual_shares_use_explicit_full_liquidation():
    out=order_payload('005930','sell',0,{'total_eval':1e9,'positions':[dict(ticker='005930',qty=2,weight_pct=0)]},100000,{'default':.15})
    assert out['qty']==2 and out['full_liquidation'] and out['weight_pct']>0
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page();page.set_content('<div role="dialog"><input id="exitAll" type="checkbox"><label for="exitAll">전량 청산</label></div>')
        TimefolioBrowser()._select_full_liquidation(page)
        assert page.locator('#exitAll').is_checked()
        browser.close()
