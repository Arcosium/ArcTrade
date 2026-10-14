"""OS-only minimum activity evidence. Passing is not statistical certification."""
import math
from collections import defaultdict
from .metrics import normalized_date
from .evaluation import periods

VERSION = 'os_activity_v1'
MIN_CLOSED_TRADES = 20
MIN_EXIT_DAYS = 10
MIN_ACTIVE_MONTHS = 6
MIN_ACTIVE_DAYS = 30


def assess(case, start, end):
    span = periods(start, end)['os']
    first, last = span['start'], span['end']
    held = defaultdict(float)
    closed = 0
    exit_days = set()
    invalid = False
    # Completed positions, not partial fills. Carry IS positions into OS.
    for trade in sorted(case.get('trades', []), key=lambda t: normalized_date(t.get('date', ''))):
        day = normalized_date(trade.get('date', ''))
        if day > last:
            continue  # ROS must not influence selection.
        symbol = str(trade.get('code', trade.get('symbol', '')))
        side = str(trade.get('side', '')).lower()
        try:
            qty = float(trade.get('qty', 0))
        except (TypeError, ValueError):
            invalid = True
            continue
        if len(day) != 8 or not symbol or side not in ('buy', 'sell') or not math.isfinite(qty) or qty <= 0:
            invalid = True
            continue
        if side == 'buy':
            held[symbol] += qty
        else:
            previous = held[symbol]
            if qty > previous + max(1e-8, previous * 1e-8):
                invalid = True
            held[symbol] = max(0., previous - qty)
            if previous > 0 and held[symbol] <= max(1e-8, previous * 1e-8) and first <= day <= last:
                closed += 1
                exit_days.add(day)
    active = []
    sessions = 0
    for row in case.get('daily', []):
        day = normalized_date(row.get('date', ''))
        if not first <= day <= last:
            continue
        sessions += 1
        try:
            nav, cash = float(row['nav']), float(row['cash'])
            if not all(math.isfinite(v) for v in (nav, cash)) or nav <= 0:
                invalid = True
            elif (nav - cash) / nav >= .01:
                active.append(day)
        except (KeyError, TypeError, ValueError):
            invalid = True
    months = len({d[:6] for d in active})
    required_days = max(MIN_ACTIVE_DAYS, math.ceil(sessions * .1))
    reasons = []
    if invalid: reasons.append('invalid_activity_ledger')
    if closed < MIN_CLOSED_TRADES: reasons.append('insufficient_closed_trades')
    if len(exit_days) < MIN_EXIT_DAYS: reasons.append('insufficient_exit_days')
    if months < MIN_ACTIVE_MONTHS: reasons.append('insufficient_active_months')
    if len(active) < required_days: reasons.append('insufficient_active_days')
    return dict(version=VERSION, passed=not reasons, scope='os', reasons=reasons,
                closed_trades=closed, exit_days=len(exit_days), active_months=months,
                active_days=len(active), sessions=sessions,
                thresholds=dict(closed_trades=MIN_CLOSED_TRADES, exit_days=MIN_EXIT_DAYS,
                                active_months=MIN_ACTIVE_MONTHS, active_days=required_days),
                statistical_certification=False)


def aggregate(cases):
    return dict(version=VERSION, scope='os', passed=bool(cases) and all(c['passed'] for c in cases),
                reasons=sorted({reason for c in cases for reason in c['reasons']}),
                cases=cases, statistical_certification=False)


def eligible(summary):
    evidence = summary.get('activity_validation') or {}
    return evidence.get('version') == VERSION and evidence.get('passed') is True
