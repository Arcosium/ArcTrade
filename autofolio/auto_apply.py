"""Opt-in, user-scoped automatic paper deployment of newly observed Pareto models."""
import contextlib
import fcntl
import json
import math
import time
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from . import store, research
from .auth import user, mutation_guard
from .catalogue import rows
from .period import accepts
from .metrics import pareto_front
from .evaluation import selection_metrics

router = APIRouter()
TARGETS = {'kr': 'kr-paper', 'us': 'us-paper', 'crypto': 'crypto-paper', 'timefolio': 'timefolio'}
LABELS = {'kr': '한국주식 페이퍼매매', 'us': '미국주식 페이퍼매매', 'crypto': '크립토 페이퍼매매', 'timefolio':'타임폴리오 13회 모의매매'}


def initialize():
    with store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS auto_apply_policies(
            user_id INTEGER NOT NULL,market TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 0,seen TEXT NOT NULL DEFAULT '[]',
            updated REAL NOT NULL,message TEXT NOT NULL DEFAULT '',PRIMARY KEY(user_id,market));
            CREATE TABLE IF NOT EXISTS auto_apply_jobs(
            deployment_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,market TEXT NOT NULL,revision INTEGER NOT NULL);''')


@contextlib.contextmanager
def lock():
    store.DATA.mkdir(parents=True, exist_ok=True)
    with (store.DATA / 'auto-apply.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def eligible(person, market, candidates=None):
    selected=[]
    for r in (rows() if candidates is None else candidates):
        if not research.visible(r,person,market) or not accepts(r):continue
        if (r.get('genome') or {}).get('engine')!='learned_v1':continue
        if market=='timefolio':
            assessment=r.get('contest_validation') or {}
            if assessment.get('status') not in ('missing','passed') or not assessment.get('rule_profile'):continue
        metrics=selection_metrics(r)
        if metrics is not None:
            selected.append(dict(r,**{k:v for k,v in metrics.items() if k!='monthly'}))
    return selected


def current_front(candidates):
    cohorts={}
    for row in candidates:
        c=cohorts.setdefault(row['cohort'],[tuple(row.get('evaluation_window') or []),0,row.get('sessions',0)])
        c[1]+=1
    if not cohorts:return []
    cohort=max(cohorts,key=lambda c:(cohorts[c][0],cohorts[c][1],cohorts[c][2]))
    return pareto_front([r for r in candidates if r['cohort']==cohort],return_key='sharpe',loss_key='mdd')


def awaiting_paper_session(uid,market):
    """Let an activated daily model reach one eligible session before replacing it.

    Replacing it sooner resets the pending plan's activation date every day and
    can indefinitely prevent fills. Empty plans need no protection; after the
    first eligible completed session, even unfilled plans may be replaced.
    """
    if market not in ('kr','us','crypto'):return False
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_books'").fetchone():return False
        row=db.execute('SELECT body FROM paper_books WHERE user_id=? AND market=?',(uid,market)).fetchone()
    if not row:return False
    book=json.loads(row['body'])
    if book.get('interval')=='minute':
        pending=(book.get('minute') or {}).get('pending') or {}
        return bool(pending.get('buys') or pending.get('sells')) and time.time()-pending.get('created',0)<=720
    pending=book.get('pending') or []
    orders=bool(pending.get('buys') or pending.get('sells')) if isinstance(pending,dict) else bool(pending)
    return orders and book.get('last_day','')<=book.get('not_before','')


def state(uid, market):
    if market not in TARGETS:
        raise HTTPException(404)
    initialize()
    with store.connect() as db:
        policy = db.execute('SELECT * FROM auto_apply_policies WHERE user_id=? AND market=?', (uid, market)).fetchone()
    from .auth import get_connection
    available = market != 'timefolio' or bool(get_connection(uid,'timefolio'))
    minute = store.setting(market+'_minute_paper_enabled',False)
    message = (f'{LABELS.get(market)} · 켠 이후의 새 OS 9개월 샤프/MDD 파레토 후보 중 샤프 최대, 동률이면 MDD 최소 · 재학습 검증 후 전환'
               if available else '타임폴리오 계정을 먼저 연결해 주세요.')
    if market=='timefolio' and available:
        message+=' · 과거 대회 적합성 인증과 별개로 실제 주문마다 13회 규칙 확인'
    if market == 'kr':
        message += ' · 한투 실매매 정지 유지'
    if minute:
        message='새 샤프/MDD 파레토 후보를 분봉 재학습·검증한 뒤 자동 전환합니다.'
    return dict(enabled=bool(policy and policy['enabled']), available=available, target=TARGETS[market],
                message=message, last_action=policy['message'] if policy else '', updated=policy['updated'] if policy else None)


class Toggle(BaseModel):
    enabled: bool


@router.get('/api/research/{market}/auto-apply')
def get_policy(market: str, request: Request):
    return state(user(request)['id'], market)


@router.post('/api/research/{market}/auto-apply')
def set_policy(market: str, value: Toggle, request: Request):
    mutation_guard(request)
    return configure(user(request), market, value.enabled)


def configure(person, market, enabled):
    current = state(person['id'], market)
    if enabled and not current['available']:
        raise HTTPException(409, current['message'])
    with lock():
        # A repeated enabled request must not erase the observation watermark.
        current = state(person['id'], market)
        if current['enabled'] == enabled:
            return current
        seen = json.dumps([r['id'] for r in eligible(person, market)])
        with store.connect() as db:
            db.execute('''INSERT INTO auto_apply_policies VALUES(?,?,?,1,?,?,?)
                ON CONFLICT(user_id,market) DO UPDATE SET enabled=excluded.enabled,
                revision=auto_apply_policies.revision+1,seen=excluded.seen,updated=excluded.updated,message=excluded.message''',
                (person['id'], market, int(enabled), seen, time.time(), '새 파레토 후보 대기' if enabled else '자동 적용 중지'))
            if not enabled:
                db.execute("""UPDATE model_deployments SET status='cancelled',message='자동 적용 해제',updated=?
                    WHERE status='queued' AND id IN (SELECT deployment_id FROM auto_apply_jobs WHERE user_id=? AND market=?)""",
                    (time.time(), person['id'], market))
    return state(person['id'], market)


@contextlib.contextmanager
def activation(identity):
    """Hold the same cross-process lock as disable through the deployment commit."""
    initialize()
    with lock():
        with store.connect() as db:
            row = db.execute('''SELECT j.revision job_revision,j.user_id,j.market,d.strategy_id,p.revision,p.enabled FROM auto_apply_jobs j
                JOIN model_deployments d ON d.id=j.deployment_id
                LEFT JOIN auto_apply_policies p ON p.user_id=j.user_id AND p.market=j.market
                WHERE j.deployment_id=?''', (identity,)).fetchone()
        if row and (not row['enabled'] or row['revision'] != row['job_revision']):
            raise ValueError('자동 적용 해제로 모델 전환을 취소했습니다.')
        if row and row['strategy_id'] not in {r['id'] for r in current_front(eligible({'id':row['user_id']},row['market']))}:
            raise ValueError('현재 샤프/MDD 파레토에서 벗어나 자동 전환을 취소했습니다.')
        yield


def tick():
    initialize()
    from .auth import connect as auth_connect
    from .deployment import request_retrain, initialize as deployment_initialize
    deployment_initialize()
    with lock():
        with store.connect() as db:
            policies = [dict(p) for p in db.execute('SELECT * FROM auto_apply_policies WHERE enabled=1')]
        if not policies:
            return
        with auth_connect() as db:
            people = {r['id']: dict(r) for r in db.execute("SELECT id,role FROM users WHERE role='admin'")}
        all_rows = rows()
        for policy in policies:
            uid, market = policy['user_id'], policy['market']
            if uid not in people or market not in LABELS:
                continue
            candidates = eligible(people[uid], market, all_rows)
            seen = set(json.loads(policy['seen']))
            with store.connect() as db:
                busy = db.execute("SELECT 1 FROM model_deployments WHERE user_id=? AND target=? AND status IN ('queued','training')", (uid, TARGETS[market])).fetchone()
            if busy:
                continue
            if awaiting_paper_session(uid,market):
                # Keep the watermark unchanged, so the current frontier is
                # reconsidered after the account processes its next session.
                with store.connect() as db:
                    db.execute('UPDATE auto_apply_policies SET updated=?,message=? WHERE user_id=? AND market=?',
                               (time.time(),'현재 모델의 다음 확정 세션 처리 대기 · 새 파레토 자동전환 보류',uid,market))
                continue
            # Do not compare different session calendars as one Pareto population.
            front = current_front(candidates)
            fresh = sorted((r for r in front if r['id'] not in seen),
                           key=lambda r: (-r['sharpe'], abs(r['mdd']), r['id']))
            message = policy['message']
            retry=set()
            if fresh:
                chosen = fresh[0]
                try:
                    request_retrain(uid, chosen['id'], TARGETS[market], auto_policy=(market, policy['revision']))
                    message = f"새 파레토 전략 재학습 요청 · {chosen['title']}"
                except ValueError as exc:
                    message = f'자동 적용 보류 · {exc}'[:250]
                    retry.add(chosen['id'])
                store.event('auto_apply', dict(user_id=uid, market=market, strategy=chosen['id'], message=message))
            with store.connect() as db:
                db.execute('UPDATE auto_apply_policies SET seen=?,updated=?,message=? WHERE user_id=? AND market=?',
                           (json.dumps([r['id'] for r in candidates if r['id'] not in retry]), time.time(), message, uid, market))
