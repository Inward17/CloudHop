"""Drives the FastAPI app through its HTTP API, the way the dashboard does, with fake code hosts."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from cloudhop import auth, db
from cloudhop.main import app, scheduled

ORIGIN = {'Origin': 'http://localhost:5173'}
REPO = 'acme/shop'
TOKEN = 'ghp_' + 'x' * 36


@pytest.fixture(autouse=True)
def fresh_db():
    db.connect(':memory:')


@pytest.fixture
def client():
    return TestClient(app, base_url='http://localhost:5173')


def sign_in(client):
    response = client.post('/api/auth/login', json={'token': 'test-admin-token'}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    return client


def github_routes(runs=()):
    return {
        'GET api.github.com/user/repos': [{'full_name': REPO, 'permissions': {'push': True}}],
        'GET api.github.com/user': {'login': 'octo'},
        'GET api.github.com/repos/acme/shop/git/trees/main': {'tree': [{'path': 'package.json', 'type': 'blob', 'size': 50}], 'truncated': False},
        'GET raw.githubusercontent.com/acme/shop/main/package.json': '{"scripts":{"start":"node ."},"dependencies":{"express":"4","pg":"8"}}',
        'GET api.github.com/repos/acme/shop/git/ref/heads/main': {'object': {'sha': 'base'}},
        'GET api.github.com/repos/acme/shop/git/commits/base': {'tree': {'sha': 'tree0'}},
        'POST api.github.com/repos/acme/shop/git/trees': {'sha': 'tree1'},
        'POST api.github.com/repos/acme/shop/git/commits': {'sha': 'abcdef1234'},
        'POST api.github.com/repos/acme/shop/git/refs': {},
        'POST api.github.com/repos/acme/shop/pulls': {'html_url': 'https://github.com/acme/shop/pull/1'},
        'GET api.github.com/repos/acme/shop/actions/workflows/cloudhop.yml/runs': {'workflow_runs': list(runs)},
        'POST api.github.com/repos/acme/shop/actions/workflows/cloudhop.yml/dispatches': '',
        'GET api.github.com/repos/acme/shop': {'default_branch': 'main'},
    }


def test_nothing_works_signed_out_and_writes_from_other_sites_are_refused(client):
    assert client.get('/api/home').status_code == 401
    assert client.post('/api/auth/login', json={'token': 'wrong'}, headers=ORIGIN).status_code == 401
    assert client.post('/api/auth/login', json={'token': 'test-admin-token'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    sign_in(client)
    me = client.get('/api/auth/me').json()
    assert me['user']['login'] == 'admin' and me['methods']['adminToken'] is True
    assert client.post('/api/accounts', json={'kind': 'aws'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/nope').json() == {'error': 'Not found'}


def test_session_cookie_is_http_only_and_logout_ends_it(client):
    response = client.post('/api/auth/login', json={'token': 'test-admin-token'}, headers=ORIGIN)
    assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=Lax' in response.headers['set-cookie']
    stored = db.one('SELECT id_hash FROM sessions')['id_hash']
    assert stored != response.cookies[auth.SESSION_COOKIE], 'only a hash of the session id is stored'
    client.post('/api/auth/logout', headers=ORIGIN)
    assert client.get('/api/home').status_code == 401


def test_full_flow_account_project_cloud_install_run_report_and_metrics(client, fake_hosts):
    sign_in(client)
    hosts = fake_hosts(github_routes())

    # A code host account: checked against GitHub, stored encrypted, never sent back.
    bad = client.post('/api/accounts', json={'kind': 'github_token', 'token': 'short'}, headers=ORIGIN)
    assert bad.status_code == 400
    created = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN)
    assert created.status_code == 201, created.text
    assert created.json()['label'] == 'octo'
    assert TOKEN not in json.dumps(db.all('SELECT * FROM accounts')), 'token is encrypted at rest'
    assert TOKEN not in client.get('/api/accounts').text
    code_account = created.json()['id']
    cloud = client.post('/api/accounts', json={'kind': 'aws', 'accountId': '123456789012', 'region': 'us-east-1', 'label': 'Sandbox'}, headers=ORIGIN).json()
    assert cloud['label'] == 'Sandbox'

    # Project, cloud connection and the install pull request.
    project = client.post('/api/projects', json={'accountId': code_account, 'repo': 'https://github.com/acme/shop'}, headers=ORIGIN)
    assert project.status_code == 201, project.text
    project_id, report_token = project.json()['id'], project.json()['reportToken']
    assert client.post('/api/projects', json={'accountId': code_account, 'repo': REPO}, headers=ORIGIN).status_code == 409
    connected = client.put(f'/api/projects/{project_id}/clouds/aws', json={'accountId': cloud['id']}, headers=ORIGIN).json()
    assert connected['bootstrapName'] == 'cloudhop-aws.yml'
    assert "repo:acme/shop:environment:cloudhop" in connected['bootstrap']
    installed = client.post(f'/api/projects/{project_id}/install', headers=ORIGIN).json()
    assert installed == {'prUrl': 'https://github.com/acme/shop/pull/1', 'portability': 'auto', 'reachable': False}
    tree = hosts.json(lambda call: call['method'] == 'POST' and call['url'].endswith('/git/trees'))
    config = json.loads(next(item['content'] for item in tree['tree'] if item['path'] == '.cloudhop/config.json'))
    assert config['cloudhopUrl'] == 'http://localhost:5173' and config['hasPostgres'] is True

    # Actions need approval, typed confirmation for destroy, and connected clouds only.
    run = f'/api/projects/{project_id}/run'
    assert client.post(run, json={'action': 'deploy', 'cloud': 'aws'}, headers=ORIGIN).status_code == 400
    assert client.post(run, json={'action': 'deploy', 'cloud': 'gcp', 'approved': True}, headers=ORIGIN).status_code == 400
    assert client.post(run, json={'action': 'destroy', 'cloud': 'aws', 'approved': True, 'confirmText': 'nope'}, headers=ORIGIN).status_code == 400
    assert client.post(run, json={'action': 'rm -rf', 'cloud': 'aws', 'approved': True}, headers=ORIGIN).status_code == 400
    started = client.post(run, json={'action': 'deploy', 'cloud': 'aws', 'approved': True}, headers=ORIGIN)
    assert started.status_code == 202, started.text
    assert hosts.json(lambda call: call['url'].endswith('/dispatches')) == {'ref': 'main', 'inputs': {'action': 'deploy', 'cloud': 'aws', 'source': ''}}

    # One action at a time.
    fake_hosts(github_routes(runs=[{'id': 1, 'display_title': 'x', 'status': 'in_progress', 'conclusion': None, 'html_url': 'u', 'run_started_at': 't'}]))
    assert client.post(run, json={'action': 'verify', 'cloud': 'aws', 'approved': True}, headers=ORIGIN).status_code == 409

    # The pipeline reports back with the project's report token.
    assert client.post('/api/report', json={'event': 'deployed', 'cloud': 'aws'}, headers={'Authorization': 'Bearer wrong'}).status_code == 401
    reported = client.post('/api/report', json={'event': 'deployed', 'cloud': 'aws', 'runId': '7', 'url': 'http://203-0-113-10.sslip.io'}, headers={'Authorization': f'Bearer {report_token}'})
    assert reported.status_code == 200
    client.post('/api/report', json={'event': 'access', 'cloud': 'aws', 'checks': [{'action': 'iam:CreateUser', 'allowed': False}]}, headers={'Authorization': f'Bearer {report_token}'})
    client.post('/api/report', json={'event': 'report', 'cloud': 'aws', 'costMonthToDate': 4.2, 'resources': {'cpuLoadPercent': 10}}, headers={'Authorization': f'Bearer {report_token}'})

    status = client.get(f'/api/projects/{project_id}/status').json()
    assert (status['project']['active'], status['project']['url']) == ('aws', 'http://203-0-113-10.sslip.io')
    events = [event['event'] for event in status['events']]
    assert events[:4] == ['report', 'access', 'deployed', 'requested']
    assert next(event for event in status['events'] if event['event'] == 'requested')['by'] == 'admin'
    assert status['latestCost']['costMonthToDate'] == 4.2

    # Uptime checks from the background job feed home and metrics.
    fake_hosts({'GET 203-0-113-10.sslip.io': 'ok'})
    asyncio.run(scheduled())
    home = client.get('/api/home').json()
    assert home['projects'][0]['uptimePercent'] == 100 and home['projects'][0]['costMonthToDate'] == 4.2
    metrics = client.get(f'/api/projects/{project_id}/metrics?range=7d').json()
    assert metrics['range'] == '7d' and metrics['checks'] == 1 and metrics['resources'] == {'cpuLoadPercent': 10}

    # Cloud access view: grants, repos, and the live check from the verify run.
    access = client.get(f"/api/accounts/{cloud['id']}/access").json()
    assert access['repos'] == [REPO] and access['live']['checks'] == [{'action': 'iam:CreateUser', 'allowed': False}]
    accounts = {account['id']: account for account in client.get('/api/accounts').json()['accounts']}
    assert accounts[cloud['id']]['projects'] == 1 and accounts[code_account]['projects'] == 1

    # Commands only propose.
    proposal = client.post(f'/api/projects/{project_id}/command', json={'text': 'destroy aws'}, headers=ORIGIN).json()
    assert proposal['action'] == 'destroy' and proposal['confirmText'] == 'aws'
    assert client.delete(f'/api/accounts/{code_account}', headers=ORIGIN).status_code == 409


def test_users_only_ever_see_their_own_rows(client, fake_hosts):
    sign_in(client)
    fake_hosts(github_routes())
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    project = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()['id']

    other_id, _ = auth.upsert_github_user({'id': 99, 'login': 'mallory'})
    other = TestClient(app, base_url='http://localhost:5173')
    request = type('R', (), {'headers': {'user-agent': 'test'}})()
    other.cookies.set(auth.SESSION_COOKIE, auth.create_session({'PUBLIC_URL': 'http://localhost:5173'}, other_id, request).split(';')[0].split('=', 1)[1])
    assert other.get('/api/home').json()['projects'] == []
    assert other.get(f'/api/projects/{project}/status').status_code == 404
    assert other.get(f'/api/accounts/{account}/access').status_code == 404
    assert other.delete(f'/api/accounts/{account}', headers=ORIGIN).status_code == 404


def test_profile_update_and_delete_removes_everything(client):
    sign_in(client)
    assert client.patch('/api/profile', json={'name': '', 'email': ''}, headers=ORIGIN).status_code == 400
    assert client.patch('/api/profile', json={'name': 'Ada', 'email': 'ada@example.com'}, headers=ORIGIN).status_code == 200
    profile = client.get('/api/profile').json()
    assert profile['user']['name'] == 'Ada' and profile['sessions'][0]['current'] is True
    assert client.request('DELETE', '/api/profile', json={'confirm': 'nope'}, headers=ORIGIN).status_code == 400
    assert client.request('DELETE', '/api/profile', json={'confirm': 'admin'}, headers=ORIGIN).status_code == 200
    assert db.all('SELECT * FROM users') == [] and db.all('SELECT * FROM sessions') == []


def test_reports_are_checked_and_limited(client, fake_hosts):
    sign_in(client)
    fake_hosts(github_routes())
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    token = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()['reportToken']
    send = lambda body: client.post('/api/report', json=body, headers={'Authorization': f'Bearer {token}'})  # noqa: E731
    assert send({'event': 'deployed', 'cloud': 'azure'}).status_code == 400
    assert send({'event': 'x', 'cloud': 'aws', 'pad': 'a' * 30_000}).status_code == 413
    send({'event': 'deployed', 'cloud': 'aws', 'url': 'javascript:alert(1)'})
    assert db.one('SELECT url FROM projects')['url'] is None, 'only plain http(s) addresses are kept'


def test_website_is_served_with_security_headers(client):
    page = client.get('/')
    assert page.status_code == 200 and '<div id="app">' in page.text or 'id="app"' in page.text
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']
    assert page.headers['x-content-type-options'] == 'nosniff'
    assert 'strict-transport-security' not in page.headers, 'HSTS only on https'
    assert client.get('/api/docs').status_code == 200


def test_missing_settings_are_reported_plainly(client, monkeypatch):
    monkeypatch.delenv('ENCRYPTION_KEY')
    assert 'ENCRYPTION_KEY' in client.get('/api/auth/methods').json()['error']


def test_github_sign_in_redirect_sets_state_and_callback_rejects_forged_state(client, monkeypatch):
    monkeypatch.setenv('GITHUB_CLIENT_ID', 'cid')
    monkeypatch.setenv('GITHUB_CLIENT_SECRET', 'secret')
    start = client.get('/api/auth/github', follow_redirects=False)
    assert start.status_code == 302 and start.headers['location'].startswith('https://github.com/login/oauth/authorize?client_id=cid')
    assert 'cloudhop_state=' in start.headers['set-cookie']
    assert client.get('/api/auth/callback?code=x&state=forged', follow_redirects=False).status_code == 400


def test_github_callback_creates_the_user_and_a_session(client, monkeypatch, fake_hosts):
    monkeypatch.setenv('GITHUB_CLIENT_ID', 'cid')
    monkeypatch.setenv('GITHUB_CLIENT_SECRET', 'secret')
    fake_hosts({
        'POST github.com/login/oauth/access_token': {'access_token': 'user-token'},
        'GET api.github.com/user': {'id': 7, 'login': 'octo', 'name': 'Octo Cat', 'avatar_url': 'https://avatars.githubusercontent.com/u/7'},
    })
    state = client.get('/api/auth/github', follow_redirects=False).cookies['cloudhop_state']
    done = client.get(f'/api/auth/callback?code=c&state={state}', follow_redirects=False)
    assert done.status_code == 302 and done.headers['location'] == 'http://localhost:5173/#/?welcome'
    assert client.get('/api/auth/me').json()['user']['login'] == 'octo'


def test_unreachable_site_counts_as_down(fake_hosts):
    db.run("INSERT INTO projects (id, user_id, repo, account_id, report_hash, url, created_at) VALUES ('p', 'u', 'o/r', 'a', 'h', 'http://down.example', 1)")

    def boom(request):
        raise httpx.ConnectError('down', request=request)
    fake_hosts({'GET down.example': boom})
    asyncio.run(scheduled())
    assert db.one('SELECT ok, status, ms FROM checks') == {'ok': 0, 'status': None, 'ms': None}
