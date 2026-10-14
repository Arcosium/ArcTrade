"""Preserve repository identities while moving to ArcTrade. Default: dry run.

This only renames/archives repositories and reserves a private blank repository.
Source uploads belong to project_copies.py. The private journal permits safe
resumption after interruption; never remove it to bypass an identity conflict.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time

OWNER = 'Arcosium'
OLD = 'ArcTrade-legacy'
NAMES = ('ArcTrade', 'QuantInSight', OLD, 'AutoCrypto')


class Migration:
    def __init__(self, api, journal):
        self.api = api
        self.journal = Path(journal)

    def request(self, method, path, body=None):
        response = self.api(method, path, json=body)
        if method == 'GET' and response.status_code == 404:
            return None
        if response.status_code not in (200, 201):
            # Do not expose tokens, headers, request details or API response text.
            raise RuntimeError(f'GitHub {method} failed: HTTP {response.status_code}')
        return response.json()

    def exact(self, name):
        repo = self.request('GET', f'/repos/{OWNER}/{name}')
        # GitHub keeps redirects after a rename; redirected repositories are not
        # occupants of the requested name and must never be patched by that name.
        if repo and repo.get('full_name', '').casefold() == f'{OWNER}/{name}'.casefold():
            return repo
        return None

    def save(self, state):
        self.journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.journal.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.journal)

    def initial(self):
        if self.journal.exists():
            state = json.loads(self.journal.read_text())
            if state.get('version') != 1 or state.get('owner') != OWNER:
                raise RuntimeError('Unexpected migration journal')
            return state
        repos = {name: self.exact(name) for name in NAMES}
        if repos[OLD] is not None:
            raise RuntimeError('ArcTrade-legacy already exists without a migration journal')
        if any(repos[name] is None for name in ('ArcTrade', 'QuantInSight', 'AutoCrypto')):
            raise RuntimeError('Expected original repositories absent; restore migration journal if already started')
        if repos['ArcTrade']['archived'] or repos['QuantInSight']['archived']:
            raise RuntimeError('Original active repositories must be writable')
        return dict(version=1, owner=OWNER, started_ns=time.time_ns(),
                    original={name: {key: repo.get(key) for key in
                              ('id', 'full_name', 'private', 'archived', 'default_branch', 'description')}
                              for name, repo in repos.items() if repo}, blank_id=None)

    def identity(self, state, original, allowed):
        expected = state['original'][original]
        found = [repo for name in allowed if (repo := self.exact(name)) and repo['id'] == expected['id']]
        if len(found) != 1:
            raise RuntimeError(f'Repository identity conflict: {original}')
        repo = found[0]
        if repo['private'] != expected['private']:
            raise RuntimeError(f'Unexpected visibility change: {original}')
        return repo

    def patch(self, repo, body):
        name = repo['full_name'].split('/')[1]
        current = self.exact(name)
        if not current or current['id'] != repo['id']:
            raise RuntimeError('Repository identity changed concurrently')
        result = self.request('PATCH', '/repos/' + repo['full_name'], body)
        if result['id'] != repo['id']:
            raise RuntimeError('Repository identity changed during update')
        return result

    def run(self, apply=False):
        state = self.initial()
        legacy = self.identity(state, 'ArcTrade', ('ArcTrade', OLD))
        source = self.identity(state, 'QuantInSight', ('QuantInSight', 'ArcTrade'))
        crypto = self.identity(state, 'AutoCrypto', ('AutoCrypto',))
        occupant = self.exact(OLD)
        if occupant and occupant['id'] != legacy['id']:
            raise RuntimeError('ArcTrade-legacy name collision')
        # Validate every name conflict before the first mutation.
        arc = self.exact('ArcTrade')
        if arc and arc['id'] not in (legacy['id'], source['id']):
            raise RuntimeError('ArcTrade name collision')
        blank = self.exact('QuantInSight')
        marker = f'QuantInSight reserved blank site; migration {state["started_ns"]}'
        if blank and blank['id'] != source['id']:
            if not blank['private'] or (blank['id'] != state.get('blank_id') and blank.get('description') != marker):
                raise RuntimeError('QuantInSight name collision')
        plan = ['ArcTrade → ArcTrade-legacy (archive)', 'QuantInSight → ArcTrade (preserve history and visibility)',
                'Create private QuantInSight blank repository', 'Archive AutoCrypto (retain history)',
                'Upload inspected copies separately with project_copies.py']
        if not apply:
            return dict(apply=False, plan=plan)
        self.save(state)  # Persist original immutable IDs before any mutation.
        if legacy['full_name'].split('/')[1] != OLD:
            legacy = self.patch(legacy, {'name': OLD})
        if not legacy['archived']:
            self.patch(legacy, {'archived': True})
        if source['full_name'].split('/')[1] != 'ArcTrade':
            source = self.patch(source, {'name': 'ArcTrade'})
        blank = self.exact('QuantInSight')
        if blank is None:
            blank = self.request('POST', '/user/repos', {'name': 'QuantInSight', 'private': True,
                                 'auto_init': False, 'description': marker})
        if not blank['private'] or blank['full_name'] != f'{OWNER}/QuantInSight':
            raise RuntimeError('Blank repository creation verification failed')
        state['blank_id'] = blank['id']
        self.save(state)
        if not crypto['archived']:
            self.patch(crypto, {'archived': True})
        final = {name: self.exact(name) for name in NAMES}
        expected = {'ArcTrade': source['id'], OLD: legacy['id'],
                    'QuantInSight': blank['id'], 'AutoCrypto': crypto['id']}
        if any(not final[name] or final[name]['id'] != repo_id for name, repo_id in expected.items()):
            raise RuntimeError('Final repository identity verification failed')
        if not final[OLD]['archived'] or not final['AutoCrypto']['archived']:
            raise RuntimeError('Archive verification failed')
        state['completed_ns'] = time.time_ns()
        self.save(state)
        return dict(apply=True, repositories={name: {'id': repo['id'], 'private': repo['private'],
                    'archived': repo['archived']} for name, repo in final.items()},
                    upload='pending project_copies.py')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    home = Path.home()
    sys.path.insert(0, str(home / 'projects/ArcAI.ve'))
    from dotenv import dotenv_values
    from github_manage import gh_api
    token = dotenv_values(home / 'vault/projects-root/.env').get('GITHUB_TOKEN')
    if not token:
        raise RuntimeError('GitHub token unavailable')
    def api(method, path, json=None):
        return gh_api(method, path, token, json=json)
    account = api('GET', '/user')
    if account.status_code != 200 or account.json().get('login', '').casefold() != OWNER.casefold():
        raise RuntimeError('Unexpected GitHub account')
    journal = home / 'vault/ArcTrade/migration/github.json'
    journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with journal.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = Migration(api, journal).run(args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
