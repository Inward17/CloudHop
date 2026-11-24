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
PUBLIC_HOST = '93-184-215-14.sslip.io'  # A public address, in the shape the pipeline reports.


def approved_run(project_id, action, cloud, source='', at=None):
    """A run row as POST /run records it, for tests that send pipeline reports directly."""
    run_id = f'req{len(db.all("SELECT id FROM runs")) + 1}'
    db.run("INSERT INTO runs (id, project_id, action, cloud, source, requested_by, at, status) VALUES (?, ?, ?, ?, ?, 'admin', ?, 'requested')",
           run_id, project_id, action, cloud, source, at if at is not None else len(run_id))
    return run_id
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
    request_id = started.json()['requestId']
    assert hosts.json(lambda call: call['url'].endswith('/dispatches')) == {'ref': 'main', 'inputs': {'action': 'deploy', 'cloud': 'aws', 'source': '', 'request': request_id}}

    # One action at a time: right after a start (before the code host lists the run), and while a run is in progress.
    just = client.post(run, json={'action': 'verify', 'cloud': 'aws', 'approved': True}, headers=ORIGIN)
    assert just.status_code == 409 and 'just started' in just.json()['error']
    fake_hosts(github_routes(runs=[{'id': 1, 'display_title': 'x', 'status': 'in_progress', 'conclusion': None, 'html_url': 'u', 'run_started_at': 't'}]))
    busy = client.post(run, json={'action': 'verify', 'cloud': 'aws', 'approved': True}, headers=ORIGIN)
    assert busy.status_code == 409 and 'still running' in busy.json()['error']

    # The pipeline reports back with the project's report token.
    assert client.post('/api/report', json={'event': 'deployed', 'cloud': 'aws'}, headers={'Authorization': 'Bearer wrong'}).status_code == 401
    reported = client.post('/api/report', json={'event': 'deployed', 'cloud': 'aws', 'runId': '7', 'requestId': request_id, 'url': f'http://{PUBLIC_HOST}'},
                           headers={'Authorization': f'Bearer {report_token}'})
    assert reported.json() == {'ok': True, 'applied': True}
    client.post('/api/report', json={'event': 'access', 'cloud': 'aws', 'checks': [{'action': 'iam:CreateUser', 'allowed': False}]}, headers={'Authorization': f'Bearer {report_token}'})
    client.post('/api/report', json={'event': 'report', 'cloud': 'aws', 'costMonthToDate': 4.2, 'resources': {'cpuLoadPercent': 10}}, headers={'Authorization': f'Bearer {report_token}'})

    status = client.get(f'/api/projects/{project_id}/status').json()
    assert (status['project']['active'], status['project']['url']) == ('aws', f'http://{PUBLIC_HOST}')
    events = [event['event'] for event in status['events']]
    assert events[:4] == ['report', 'access', 'deployed', 'requested']
    assert next(event for event in status['events'] if event['event'] == 'requested')['by'] == 'admin'
    assert status['latestCost']['costMonthToDate'] == 4.2

    # Uptime checks from the background job feed home and metrics.
    fake_hosts({f'GET {PUBLIC_HOST}': 'ok'})
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


def test_regenerating_the_report_token_retires_the_old_one(client, fake_hosts):
    sign_in(client)
    fake_hosts(github_routes())
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    created = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()
    old = created['reportToken']
    fresh = client.post(f"/api/projects/{created['id']}/report-token", headers=ORIGIN).json()['reportToken']
    assert fresh != old and len(fresh) >= 40
    send = lambda token: client.post('/api/report', json={'event': 'verified', 'cloud': 'aws'}, headers={'Authorization': f'Bearer {token}'}).status_code  # noqa: E731
    assert send(old) == 401, 'the old token stops working at once'
    assert send(fresh) == 200
    events = [event['event'] for event in client.get(f"/api/projects/{created['id']}/status").json()['events']]
    assert 'report_token_rotated' in events
    assert client.post(f"/api/projects/{created['id']}/report-token", headers={'Origin': 'https://evil.example'}).status_code == 403


def test_deleting_a_project_needs_its_name_and_removes_its_data_only(client, fake_hosts):
    sign_in(client)
    fake_hosts(github_routes())
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    created = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()
    token = created['reportToken']
    client.post('/api/report', json={'event': 'verified', 'cloud': 'aws'}, headers={'Authorization': f'Bearer {token}'})
    url = f"/api/projects/{created['id']}"
    assert client.request('DELETE', url, json={'confirm': 'wrong/name'}, headers=ORIGIN).status_code == 400
    assert client.request('DELETE', url, json={'confirm': REPO}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.request('DELETE', url, json={'confirm': REPO}, headers=ORIGIN).status_code == 200
    assert client.get(f'{url}/status').status_code == 404
    assert db.all('SELECT * FROM reports') == [] and db.all('SELECT * FROM projects') == []
    assert client.post('/api/report', json={'event': 'verified', 'cloud': 'aws'}, headers={'Authorization': f'Bearer {token}'}).status_code == 401
    assert len(db.all('SELECT * FROM accounts')) == 1, 'the code host account stays'
    assert client.delete(f'/api/accounts/{account}', headers=ORIGIN).status_code == 200, 'and can now be removed'


def test_cloud_project_domain_check_points_at_the_live_server(client, fake_hosts):
    sign_in(client)
    fake_hosts(github_routes())
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    created = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()
    client.put(f"/api/projects/{created['id']}/domain", json={'domain': 'example.com'}, headers=ORIGIN)
    assert client.get(f"/api/projects/{created['id']}/domain/check").status_code == 400, 'deploy first'
    client.post('/api/report', json={'event': 'deployed', 'cloud': 'aws', 'requestId': approved_run(created['id'], 'deploy', 'aws'), 'url': f'http://{PUBLIC_HOST}'},
                headers={'Authorization': f"Bearer {created['reportToken']}"})
    check = client.get(f"/api/projects/{created['id']}/domain/check").json()
    assert check['records'] == [{'type': 'A', 'name': '@', 'content': '93.184.215.14'}]


def test_only_the_newest_approved_run_moves_the_app_and_only_once(client, fake_hosts):
    sign_in(client)
    fake_hosts(github_routes())
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    created = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()
    project = created['id']
    send = lambda body: client.post('/api/report', json=body, headers={'Authorization': f"Bearer {created['reportToken']}"}).json()  # noqa: E731
    active = lambda: db.one('SELECT active, previous, url FROM projects WHERE id = ?', project)  # noqa: E731

    assert send({'event': 'deployed', 'cloud': 'aws'})['applied'] is False, 'no approved run'
    assert send({'event': 'deployed', 'cloud': 'aws', 'requestId': 'made-up'})['applied'] is False
    deploy = approved_run(project, 'deploy', 'aws', at=1)
    assert send({'event': 'deployed', 'cloud': 'gcp', 'requestId': deploy})['applied'] is False, 'wrong cloud'
    assert send({'event': 'destroyed', 'cloud': 'aws', 'requestId': deploy})['applied'] is False, 'wrong action'
    assert active()['active'] is None

    assert send({'event': 'deployed', 'cloud': 'aws', 'requestId': deploy, 'url': f'http://{PUBLIC_HOST}'})['applied'] is True
    assert send({'event': 'deployed', 'cloud': 'aws', 'requestId': deploy})['applied'] is False, 'a duplicate changes nothing'
    assert active()['active'] == 'aws'

    # A switch is approved, then a newer action. The switch's late report is stale and must not move the app.
    switch = approved_run(project, 'switch', 'gcp', 'aws', at=2)
    approved_run(project, 'destroy', 'gcp', at=3)
    late = send({'event': 'switched', 'cloud': 'gcp', 'from': 'aws', 'requestId': switch})
    assert late['applied'] is False and 'newer action' in late['reason']
    assert dict(active()) == {'active': 'aws', 'previous': None, 'url': f'http://{PUBLIC_HOST}'}
    ignored = [json.loads(row['data']) for row in db.all("SELECT data FROM reports WHERE event = 'report_ignored'")]
    assert len(ignored) == 6 and all(item['reason'] for item in ignored), 'ignored reports stay in the history'

    # A failed switch reports the source cloud, and belongs to the switch run.
    switch = approved_run(project, 'switch', 'gcp', 'aws', at=4)
    assert send({'event': 'switch_failed', 'cloud': 'aws', 'requestId': switch})['applied'] is True
    assert active()['active'] == 'aws'


def test_reported_addresses_must_be_a_public_server_address():
    from cloudhop.main import deployed_url
    assert deployed_url(f'http://{PUBLIC_HOST}') == f'http://{PUBLIC_HOST}'
    assert deployed_url(f'https://{PUBLIC_HOST}/') == f'https://{PUBLIC_HOST}/'
    for url in ['http://127-0-0-1.sslip.io', 'http://10-0-0-5.sslip.io', 'http://169-254-169-254.sslip.io', 'http://203-0-113-10.sslip.io',
                'http://example.com', 'http://93-184-215-14.sslip.io.evil.example', 'http://999-1-1-1.sslip.io', None, 7]:
        assert deployed_url(url) is None, url


def test_site_checks_refuse_private_addresses_and_private_redirects():
    from cloudhop import net

    def handler(request):
        return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest/meta-data/'})
    net.use(httpx.MockTransport(handler))
    net.check_addresses = True
    try:
        for url in ['http://127.0.0.1:6379/', 'http://10.0.0.5/', 'http://169.254.169.254/', 'http://[::1]/', 'file:///etc/passwd', 'http://localhost/']:
            with pytest.raises(net.UnsafeAddress):
                asyncio.run(net.public_get(url))
        with pytest.raises(net.UnsafeAddress):
            asyncio.run(net.public_get('http://93.184.215.14/'))  # Public, but it redirects to the metadata service.
    finally:
        net.check_addresses = False


def test_uptime_checks_the_health_path_and_only_2xx_or_3xx_count_as_up(client, fake_hosts):
    sign_in(client)
    fake_hosts(github_routes())
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    project = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()['id']
    db.run("UPDATE projects SET url = 'http://app.example' WHERE id = ?", project)
    for bad in ['health', '//evil.example/x', 'http://evil.example', '/a b', '/' + 'x' * 300]:
        assert client.put(f'/api/projects/{project}/health', json={'path': bad}, headers=ORIGIN).status_code == 400, bad
    assert client.put(f'/api/projects/{project}/health', json={'path': '/healthz?full=1'}, headers=ORIGIN).json() == {'healthPath': '/healthz?full=1'}

    hosts = fake_hosts({'GET app.example/healthz': httpx.Response(401)})
    asyncio.run(scheduled())
    assert hosts.calls[-1]['url'] == 'http://app.example/healthz?full=1'
    fake_hosts({'GET app.example/healthz': httpx.Response(204)})
    asyncio.run(scheduled())
    checks = [(row['ok'], row['status']) for row in db.all('SELECT ok, status FROM checks ORDER BY at, rowid')]
    assert checks == [(0, 401), (1, 204)], 'a 401 is not a healthy app'
    status = client.get(f'/api/projects/{project}/status').json()
    assert status['project']['healthPath'] == '/healthz?full=1' and status['uptime']['checks'] == 2 and status['uptime']['last']['at']


def test_home_cost_says_what_it_covers_its_currency_and_when(client, fake_hosts):
    sign_in(client)
    fake_hosts(github_routes())
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    created = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()
    client.post('/api/report', json={'event': 'report', 'cloud': 'aws', 'costMonthToDate': 12.5, 'currency': 'USD', 'costScope': 'aws-account'},
                headers={'Authorization': f"Bearer {created['reportToken']}"})
    card = client.get('/api/home').json()['projects'][0]
    assert (card['costMonthToDate'], card['costCurrency'], card['costScope']) == (12.5, 'USD', 'aws-account') and card['costAt']
    cost = client.get(f"/api/projects/{created['id']}/metrics").json()['costs'][0]
    assert (cost['currency'], cost['scope']) == ('USD', 'aws-account')


def test_log_filters_apply_before_the_page_limit_and_pages_never_skip_shared_timestamps(client, fake_hosts):
    sign_in(client)
    fake_hosts(github_routes())
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    project = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()['id']
    # 600 ordinary events, then (older) 3 errors: the errors are past any first page of 100 unfiltered entries.
    for index in range(600):
        db.run("INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, 'verified', '{}')", project, 10_000 + index)
    for _ in range(3):
        db.run("INSERT INTO reports (project_id, at, event, data) VALUES (?, 5000, 'switch_failed', '{}')", project)
    errors = client.get('/api/logs?level=error&limit=100').json()
    assert [entry['event'] for entry in errors['entries']] == ['switch_failed'] * 3 and errors['next'] is None

    # Seven events share one timestamp; pages of two must return each exactly once.
    for _ in range(7):
        db.run("INSERT INTO reports (project_id, at, event, data) VALUES (?, 4000, 'paused', '{}')", project)
    seen, cursor = [], None
    while True:
        page = client.get('/api/logs', params={'source': 'you', 'limit': 2, **({'before': cursor} if cursor else {})}).json()
        seen += page['entries']
        cursor = page['next']
        if not cursor:
            break
    assert len(seen) == 7 and all(entry['event'] == 'paused' for entry in seen)
    assert client.get('/api/logs?before=1.x.2').status_code == 400


def test_log_pages_never_jump_past_unread_reports_when_the_scan_limit_is_hit(client, fake_hosts, monkeypatch):
    from cloudhop import main
    monkeypatch.setattr(main, 'LOG_BATCH', 5)
    monkeypatch.setattr(main, 'LOG_SCAN_LIMIT', 10)
    sign_in(client)
    fake_hosts(github_routes())
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    project = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()['id']
    for index in range(20):  # More recent rows than one scan reads, none of them errors.
        db.run("INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, 'verified', '{}')", project, 10_000 + index)
    db.run("INSERT INTO reports (project_id, at, event, data) VALUES (?, 5000, 'switch_failed', '{}')", project)
    for at, ok in [(1000, 1), (2000, 0)]:  # An even older outage: a site_down line at 2000.
        db.run('INSERT INTO checks (project_id, at, ok, status, ms) VALUES (?, ?, ?, ?, ?)', project, at, ok, 200 if ok else 503, None)

    seen, cursor = [], None
    for _ in range(20):
        page = client.get('/api/logs', params={'level': 'error', **({'before': cursor} if cursor else {})}).json()
        seen += [(entry['event'], entry['at']) for entry in page['entries']]
        cursor = page['next']
        if not cursor:
            break
    assert seen == [('switch_failed', 5000), ('site_down', 2000)], 'both errors, newest first, none skipped'


def test_home_spend_counts_each_reporting_account_once_even_after_a_reconnect(client, fake_hosts):
    sign_in(client)
    fake_hosts({**github_routes(), 'GET api.github.com/repos/acme/blog': {'default_branch': 'main', 'full_name': 'acme/blog'}})
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    shop = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()
    blog = client.post('/api/projects', json={'accountId': account, 'repo': 'acme/blog'}, headers=ORIGIN).json()
    report = lambda created, amount, aws: client.post('/api/report', json={  # noqa: E731
        'event': 'report', 'cloud': 'aws', 'costMonthToDate': amount, 'currency': 'USD', 'costScope': 'aws-account', 'costAccount': aws},
        headers={'Authorization': f"Bearer {created['reportToken']}"})
    report(shop, 20, '111111111111')
    report(blog, 30, '222222222222')
    assert client.get('/api/home').json()['spend'] == {'total': 50, 'currency': 'USD', 'currencies': ['USD'], 'accounts': 2}

    # The shop project is reconnected to the blog's AWS account before its next report: its last report still came
    # from the first account, so both totals stay.
    db.run('UPDATE projects SET clouds = ? WHERE id = ?', json.dumps({'aws': {'accountId': '222222222222', 'region': 'us-east-1'}}), shop['id'])
    assert client.get('/api/home').json()['spend']['total'] == 50

    # Its next report comes from the shared account: now one account, counted once, latest figure.
    report(shop, 31, '222222222222')
    assert client.get('/api/home').json()['spend'] == {'total': 31, 'currency': 'USD', 'currencies': ['USD'], 'accounts': 1}


def test_a_matching_report_exactly_at_the_scan_boundary_is_returned_once(client, fake_hosts, monkeypatch):
    from cloudhop import main
    monkeypatch.setattr(main, 'LOG_BATCH', 5)
    monkeypatch.setattr(main, 'LOG_SCAN_LIMIT', 10)
    sign_in(client)
    fake_hosts(github_routes())
    account = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    project = client.post('/api/projects', json={'accountId': account, 'repo': REPO}, headers=ORIGIN).json()['id']
    for index in range(9):
        db.run("INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, 'verified', '{}')", project, 10_000 + index)
    db.run("INSERT INTO reports (project_id, at, event, data) VALUES (?, 9000, 'switch_failed', '{}')", project)  # The 10th row read: the boundary.
    db.run("INSERT INTO reports (project_id, at, event, data) VALUES (?, 5000, 'switch_failed', '{}')", project)

    seen, cursor = [], None
    for _ in range(20):
        page = client.get('/api/logs', params={'level': 'error', **({'before': cursor} if cursor else {})}).json()
        seen += [entry['at'] for entry in page['entries']]
        cursor = page['next']
        if not cursor:
            break
    assert seen == [9000, 5000], 'both errors, each exactly once'
