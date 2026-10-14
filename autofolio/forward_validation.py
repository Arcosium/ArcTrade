"""Forward sample progress; reaching sample targets is a review, not a pass."""
from datetime import datetime
from zoneinfo import ZoneInfo


def progress(book,market):
    timezone=ZoneInfo('UTC' if market=='crypto' else 'America/New_York' if market=='us' else 'Asia/Seoul')
    started=(book.get('minute') or {}).get('activated')
    trades=[t for t in book.get('trades',[]) if t.get('execution')=='next_observed_close'
            and t.get('signal_ts',0)>=(started or float('inf'))]
    dates=sorted({str(t.get('date',''))[:8] for t in trades if len(str(t.get('date','')))>=8})
    # Partial sells are not round trips: track units purchased since activation.
    inventory={};closed=0
    for t in trades:
        code=t.get('code');qty=float(t.get('qty',0))
        if t.get('side')=='buy':inventory[code]=inventory.get(code,0)+qty
        elif t.get('side')=='sell' and inventory.get(code,0)>0:
            inventory[code]=max(0,inventory[code]-qty)
            if inventory[code]<1e-10:closed+=1
    enough=len(dates)>=20 and closed>=100
    return dict(state='ready_for_review' if enough else 'collecting',financial_certified=False,
                started_at=started,started_date=datetime.fromtimestamp(started,timezone).date().isoformat() if started else None,
                active_days=len(dates),closed_positions=closed,first_review_active_days=5,
                review_min_active_days=20,review_min_closed_positions=100,
                message='표본 목표 충족 · 비용·수익·위험 검토 대기' if enough else '분봉 전진 표본 수집 중')
