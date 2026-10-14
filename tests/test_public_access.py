"""Security boundaries and real-observation chart regressions (temporary DBs only)."""
import hashlib
import json
import secrets
import sqlite3
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from autofolio import auth, public_routes as public


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('QUANTINSIGHT_AUTH_DIR', str(tmp_path / 'auth'))
    monkeypatch.setattr(public.config, 'DB', tmp_path / 'paper.sqlite3')
    password = secrets.token_urlsafe(24)
    auth.bootstrap_admin('owner', password)
    with auth.connect() as db:
        db.execute('INSERT INTO users VALUES(2,?,?,?,?)', ('member', auth.password_hash(password), 'user', time.time()))
        db.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(b'old-member-session').hexdigest(), 2, time.time() + 3600))
    with sqlite3.connect(public.config.DB) as db:
        db.executescript('''CREATE TABLE paper_books(user_id,market,deployment,body,updated);
            CREATE TABLE paper_observations(user_id,market,strategy_id,ts,nav,initial,source,stale);''')
        book = dict(initial=100, nav=110, cash=20, message='/private/deployment-secret',
                    holdings={'A': dict(code='A', qty=2, value=90, account_no='private-account')},
                    trades=[dict(date='20261012', code='A', side='buy', qty=2, price=40, secret='private')],
                    equity=[dict(date='20261012', net_return=0), dict(date='20261013', net_return=.1)])
        db.execute('INSERT INTO paper_books VALUES(?,?,?,?,?)', (1, 'kr', 'private-deployment', json.dumps(book), time.time()))
        db.execute('INSERT INTO paper_books VALUES(?,?,?,?,?)', (2, 'us', 'member-only', json.dumps(book), time.time()))
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(auth.router)
    app.include_router(public.router)

    @app.get('/')
    def home():
        return {'ok': True}

    @app.get('/api/research')
    @app.get('/api/accounts/timefolio')
    def private(request: Request):
        return auth.user(request)

    @app.post('/api/public/write')
    def write():
        return {'unsafe': True}

    with TestClient(app) as client:
        yield client, password


def test_guest_only_sees_paper_and_existing_member_sessions_are_rejected(setup):
    client, password = setup
    assert client.get('/').status_code == 200
    assert client.get('/api/auth/me').json() == {'role': 'guest', 'username': None}
    for endpoint in ['/api/research', '/api/accounts/timefolio', '/api/auth/connections', '/crypto/api/book/cnn']:
        assert client.get(endpoint).status_code == 401
    assert client.post('/api/public/write', headers={'X-Requested-With': 'ArcTrade'}).status_code == 401
    client.cookies.set(auth.COOKIE, 'old-member-session')
    assert client.get('/api/research').status_code == 401
    assert client.post('/api/auth/login', headers={'X-Requested-With': 'ArcTrade'},
                       json={'username': 'member', 'password': password}).status_code == 401
    assert client.post('/api/auth/register', json={'username': 'new-member', 'password': password}).status_code == 410
    with auth.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 2
    assert client.post('/api/auth/login', headers={'X-Requested-With': 'ArcTrade'},
                       json={'username': 'owner', 'password': password}).status_code == 200
    assert client.get('/api/research').json()['role'] == 'admin'


def test_public_whitelist_owner_scope_and_daily_not_faked_as_intraday(setup):
    client, _ = setup
    result = client.get('/api/public/paper/kr').json()
    assert result['connected'] and len(result['series']) == 2
    assert result['series'][-1]['net_return'] == .1
    assert result['total_eval'] == 110
    assert 'account_no' not in result['holdings'][0]
    assert not any(value in json.dumps(result) for value in ['private-', 'deployment', 'assignment', 'user_id'])
    assert client.get('/api/public/paper/us').json()['connected'] is False
    for interval in ['minute', 'hour']:
        intraday = client.get('/api/public/paper/kr', params={'interval': interval}).json()
        assert intraday['series'] == []
        assert intraday['updated'] == result['updated']
        assert intraday['source_age_seconds'] >= result['source_age_seconds']
        assert intraday['delayed'] is True
    for suffix in ['/kr?interval=week', '/kr?strategy=cnn', '/invalid']:
        assert client.get('/api/public/paper' + suffix).status_code == 422


def test_observations_are_bucketed_without_interpolation_and_stale_values_excluded(setup):
    client, _ = setup
    start = 1_800_000_000
    with sqlite3.connect(public.config.DB) as db:
        for ts, nav, stale in [(start, 110, 0), (start + 20, 111, 0), (start + 180, 114, 0), (start + 240, 999, 1)]:
            db.execute('INSERT INTO paper_observations VALUES(?,?,?,?,?,?,?,?)', (1, 'kr', 'deployed', ts, nav, 100, 'quotes', stale))
        db.execute('INSERT INTO paper_observations VALUES(?,?,?,?,?,?,?,?)', (2, 'kr', 'deployed', start + 260, 888, 100, 'quotes', 0))
    result = client.get('/api/public/paper/kr?interval=minute').json()
    assert [row['ts'] for row in result['series']] == [start + 20, start + 180]
    assert result['total_eval'] == 114
    assert len(client.get('/api/public/paper/kr?interval=hour').json()['series']) == 1


def test_public_queries_do_not_create_database_or_mutate_book(setup):
    client, _ = setup
    before = public.config.DB.read_bytes()
    for _ in range(2):
        assert client.get('/api/public/paper/kr').status_code == 200
    assert public.config.DB.read_bytes() == before
    public.config.DB.unlink()
    assert not client.get('/api/public/paper/kr').json()['connected']
    assert not public.config.DB.exists()


def test_crypto_public_adapter_filters_research_and_internal_metadata(setup, monkeypatch):
    import sys
    import types
    from autofolio import crypto_view
    client, _ = setup
    raw = dict(active=True, capital=10000, equity_usd=11000, research_sharpe=99,
               last_signal={'top': ['private-model-output']}, source='/private/source',
               positions=[dict(base='BTC', side='long', entry=100, last=110, notional_usd=1000, upnl_pct=10,
                               secret='private')],
               trades=[dict(base='BTC', exit_ts=1_800_000_000_000, side='long', ret=.1, secret='private')],
               series=[dict(ts=1_800_000_000_000, equity=1.1)])
    monkeypatch.setitem(sys.modules, 'integrations.crypto.app', types.SimpleNamespace(book_view=lambda key: raw))
    monkeypatch.setattr(crypto_view, 'paper_series', lambda key: (raw['series'], ''))
    for key in public.CRYPTO_TITLES:
        result = client.get('/api/public/paper/crypto', params={'strategy': key, 'interval': 'hour'}).json()
        assert result['connected']
        assert result['series'] == [{'ts': 1_800_000_000, 'equity': 11000, 'net_return': pytest.approx(.1)}]
        assert not any(value in json.dumps(result) for value in ['private', 'research_sharpe', 'last_signal'])
        assert result['holdings'][0]['pnl_ratio'] == .1
        assert result['trades'][0]['ts'] == 1_800_000_000
    assert client.get('/api/public/paper/crypto?interval=minute').json()['series'] == []
    listing = client.get('/api/public/strategies?market=crypto').json()
    assert {row['key'] for row in listing['strategies']} == set(public.CRYPTO_TITLES) | {'deployed'}
