import copy
from types import SimpleNamespace

import pytest

from deploy.migrate_github import Migration


class FakeGitHub:
    def __init__(self):
        self.repos = {name: dict(id=i, full_name='Arcosium/' + name, private=name == 'AutoCrypto',
                                 archived=False, description='', default_branch='main')
                      for i, name in enumerate(('ArcTrade', 'QuantInSight', 'AutoCrypto'), 1)}
        self.redirects = {}
        self.writes = []
        self.fail_next_create = False

    def __call__(self, method, path, json=None):
        name = path.split('/')[-1]
        if method == 'GET':
            repo = self.repos.get(name) or self.repos.get(self.redirects.get(name))
            return SimpleNamespace(status_code=200 if repo else 404, json=lambda: copy.deepcopy(repo))
        self.writes.append((method, path, json))
        if method == 'POST':
            if self.fail_next_create:
                self.fail_next_create = False
                return SimpleNamespace(status_code=503)
            assert json['name'] not in self.repos
            repo = dict(id=10, full_name='Arcosium/' + json['name'], archived=False, **json)
            self.repos[json['name']] = repo
        else:
            repo = self.repos[name]
            if 'name' in json:
                assert json['name'] not in self.repos
                self.repos[json['name']] = self.repos.pop(name)
                self.redirects[name] = json['name']
                repo['full_name'] = 'Arcosium/' + json['name']
            repo.update(json)
        return SimpleNamespace(status_code=200, json=lambda: copy.deepcopy(repo))


def test_default_is_read_only_and_does_not_write_journal(tmp_path):
    api = FakeGitHub()
    journal = tmp_path / 'journal.json'
    result = Migration(api, journal).run()
    assert not result['apply'] and not api.writes and not journal.exists()


def test_preserves_ids_visibility_and_history_then_is_idempotent(tmp_path):
    api = FakeGitHub()
    migration = Migration(api, tmp_path / 'journal.json')
    result = migration.run(True)
    assert result['repositories']['ArcTrade']['id'] == 2
    assert not result['repositories']['ArcTrade']['private']
    assert result['repositories']['ArcTrade-legacy'] == {'id': 1, 'private': False, 'archived': True}
    assert result['repositories']['AutoCrypto']['archived']
    assert result['repositories']['QuantInSight']['private']
    writes = len(api.writes)
    migration.run(True)
    assert len(api.writes) == writes
    assert all(method != 'DELETE' for method, _, _ in api.writes)


def test_redirect_is_not_an_existing_blank_repository(tmp_path):
    api = FakeGitHub()
    migration = Migration(api, tmp_path / 'journal.json')
    migration.run(True)
    assert any(method == 'POST' and body['name'] == 'QuantInSight' for method, _, body in api.writes)


def test_resume_after_renames_and_before_private_creation(tmp_path):
    api = FakeGitHub()
    api.fail_next_create = True
    migration = Migration(api, tmp_path / 'journal.json')
    with pytest.raises(RuntimeError, match='HTTP 503'):
        migration.run(True)
    assert migration.exact('QuantInSight') is None
    migration.run(True)
    assert api.repos['ArcTrade']['id'] == 2
    assert api.repos['QuantInSight']['private']


def test_collision_fails_before_mutation(tmp_path):
    api = FakeGitHub()
    api.repos['ArcTrade-legacy'] = dict(id=999, full_name='Arcosium/ArcTrade-legacy')
    with pytest.raises(RuntimeError, match='already exists'):
        Migration(api, tmp_path / 'journal.json').run(True)
    assert not api.writes


def test_changed_identity_fails_before_resuming(tmp_path):
    api = FakeGitHub()
    migration = Migration(api, tmp_path / 'journal.json')
    migration.save(migration.initial())
    api.repos['QuantInSight']['id'] = 999
    with pytest.raises(RuntimeError, match='identity conflict'):
        migration.run(True)
    assert not api.writes


def test_unknown_blank_name_collision_on_resume_is_rejected(tmp_path):
    api = FakeGitHub()
    api.fail_next_create = True
    migration = Migration(api, tmp_path / 'journal.json')
    with pytest.raises(RuntimeError):
        migration.run(True)
    api.repos['QuantInSight'] = dict(id=999, full_name='Arcosium/QuantInSight', private=True, description='unrelated')
    writes = len(api.writes)
    with pytest.raises(RuntimeError, match='name collision'):
        migration.run(True)
    assert len(api.writes) == writes
