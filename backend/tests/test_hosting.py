"""Platform projects: find where a site runs, plan a move, run it, and stop for the user where needed.
Vercel and Cloudflare are fakes here; the request shapes follow their REST API docs."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from cloudhop import db, hosting
from cloudhop.main import app

ORIGIN = {'Origin': 'http://localhost:5173'}
REPO = 'acme/shop'
TOKEN = 'ghp_' + 'x' * 36
CF_TOKEN = 'cloudflare-token-0123456789'
VERCEL_TOKEN = 'vercel-token-0123456789abc'


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    db.connect(':memory:')

    async def no_wait(*_args, **_kwargs):
        return None
    monkeypatch.setattr(hosting.asyncio, 'sleep', no_wait)


@pytest.fixture
def client():
    client = TestClient(app, base_url='http://localhost:5173')
    assert client.post('/api/auth/login', json={'token': 'test-admin-token'}, headers=ORIGIN).status_code == 200
    return client


PAGES_PROJECT = {
    'name': 'shop', 'subdomain': 'shop.pages.dev', 'domains': ['shop.pages.dev', 'shop.example.com'],
    'source': {'type': 'github', 'config': {'owner': 'acme', 'repo_name': 'shop', 'production_branch': 'main'}},
    'build_config': {'build_command': 'npm run build', 'destination_dir': 'dist', 'root_dir': 'web'},
    'deployment_configs': {'production': {'env_vars': {'API_URL': {'type': 'plain_text', 'value': 'https://api.example.com'}, 'STRIPE_KEY': {'type': 'secret_text'}}}},
}


def routes(create=None, env_keys=()):
    return {
        'GET api.github.com/user': {'login': 'octo'},
        'GET api.github.com/repos/acme/shop': {'default_branch': 'main'},
        'GET api.cloudflare.com/client/v4/accounts/a1/pages/projects': {'result': [PAGES_PROJECT, {**PAGES_PROJECT, 'name': 'other', 'source': {'type': 'github', 'config': {'owner': 'acme', 'repo_name': 'other'}}}]},
        'GET api.cloudflare.com/client/v4/accounts': {'result': [{'id': 'a1', 'name': 'Acme'}]},
        'GET api.vercel.com/v2/user': {'user': {'username': 'octo'}},
        'GET api.vercel.com/v5/user/tokens/current': {'token': {'scopes': [{'type': 'user'}]}},
        'GET api.vercel.com/v10/projects': {'projects': []},
        'POST api.vercel.com/v11/projects': create or {'id': 'prj_1', 'name': 'shop'},
        'GET api.vercel.com/v9/projects/prj_1/env': {'envs': [{'key': key, 'target': ['production']} for key in env_keys]},
        'POST api.vercel.com/v13/deployments': {'id': 'dpl_1', 'readyState': 'QUEUED'},
        'GET api.vercel.com/v13/deployments/dpl_1': {'readyState': 'READY'},
        'GET shop.vercel.app': '<html><title>Shop</title></html>',
        'GET shop.pages.dev': '<html><title>Shop</title></html>',
        'POST api.vercel.com/v1/projects/prj_1/pause': {},
    }


def setup_project(client):
    """A platform project from a GitHub repo, with Cloudflare and Vercel accounts saved."""
    code = client.post('/api/accounts', json={'kind': 'github_token', 'token': TOKEN}, headers=ORIGIN).json()['id']
    cloudflare = client.post('/api/accounts', json={'kind': 'cloudflare', 'token': CF_TOKEN}, headers=ORIGIN).json()['id']
    vercel = client.post('/api/accounts', json={'kind': 'vercel', 'token': VERCEL_TOKEN}, headers=ORIGIN).json()['id']
    project = client.post('/api/projects', json={'accountId': code, 'repo': REPO, 'kind': 'platform'}, headers=ORIGIN).json()['id']
    return project, cloudflare, vercel


def test_finds_where_the_site_runs_and_records_it(client, fake_hosts):
    fake_hosts(routes())
    project, cloudflare, _ = setup_project(client)
    assert client.get(f'/api/projects/{project}/status').json()['project']['kind'] == 'platform'
    found = client.get(f'/api/projects/{project}/hosting/find').json()
    assert [site['name'] for site in found['sites']] == ['shop'], 'only sites built from this repo'
    site = found['sites'][0]
    assert site['platform'] == 'cloudflare_pages' and site['url'] == 'https://shop.pages.dev' and site['domains'] == ['shop.example.com']
    assert site['settings'] == {'buildCommand': 'npm run build', 'outputDirectory': 'dist', 'rootDirectory': 'web', 'framework': None}
    assert {item['name']: item['secret'] for item in site['variables']} == {'API_URL': False, 'STRIPE_KEY': True}
    saved = client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN).json()['current']
    assert saved['name'] == 'shop'
    status = client.get(f'/api/projects/{project}/status').json()['project']
    assert status['url'] == 'https://shop.example.com' and status['active'] == 'cloudflare_pages'
    assert client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'not-mine'}, headers=ORIGIN).status_code == 404


def test_plan_then_move_with_cloudhop_adding_the_secrets(client, fake_hosts):
    hosts = fake_hosts(routes())
    project, cloudflare, vercel = setup_project(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)

    plan = client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': vercel}, headers=ORIGIN).json()
    assert plan['status'] == 'planned'
    assert (plan['plan']['copy'], plan['plan']['secrets'], plan['plan']['missingSecrets']) == (['API_URL'], ['STRIPE_KEY'], ['STRIPE_KEY'])
    assert any(step['id'] == 'domain' and step['comingSoon'] for step in plan['plan']['steps'])
    assert not any(call['method'] == 'POST' and 'vercel.com' in call['url'] for call in hosts.calls), 'planning changes nothing'

    approve = f"/api/projects/{project}/moves/{plan['id']}/approve"
    assert client.post(approve, json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN).status_code == 400, 'secret missing'
    client.put(f'/api/projects/{project}/secrets', json={'values': {'STRIPE_KEY': 'sk_test_value'}}, headers=ORIGIN)
    assert 'sk_test_value' not in json.dumps(db.all('SELECT * FROM project_secrets')), 'stored encrypted'
    assert client.post(approve, json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN).status_code == 202

    move = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert move['status'] == 'done', move
    created = hosts.json(lambda call: call['method'] == 'POST' and '/v11/projects' in call['url'])
    assert created['gitRepository'] == {'type': 'github', 'repo': REPO}
    assert (created['buildCommand'], created['outputDirectory'], created['rootDirectory']) == ('npm run build', 'dist', 'web')
    assert {item['key']: item['value'] for item in created['environmentVariables']} == {'API_URL': 'https://api.example.com', 'STRIPE_KEY': 'sk_test_value'}
    assert hosts.json(lambda call: '/v13/deployments' in call['url'] and call['method'] == 'POST')['gitSource'] == {'type': 'github', 'org': 'acme', 'repo': 'shop', 'ref': 'main'}
    assert 'Page title matches' in move['state']['steps']['verify']['detail']
    assert move['state']['steps']['domain']['status'] == 'skipped'

    project_now = client.get(f'/api/projects/{project}/status').json()
    assert (project_now['project']['url'], project_now['project']['active']) == ('https://shop.vercel.app', 'vercel')
    assert project_now['project']['hosting']['previous']['name'] == 'shop', 'the old site is remembered, not touched'
    assert [event['event'] for event in project_now['events']][:2] == ['moved', 'move_started']
    assert not any(call['method'] == 'DELETE' for call in hosts.calls), 'nothing is ever deleted'


def test_waits_for_the_platforms_github_access_then_continues(client, fake_hosts):
    fake_hosts(routes(create=httpx.Response(400, json={'error': {'code': 'repo_not_found', 'message': 'Repository not found or GitHub integration missing'}})))
    project, cloudflare, vercel = setup_project(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    client.put(f'/api/projects/{project}/secrets', json={'values': {'STRIPE_KEY': 'sk'}}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': vercel}, headers=ORIGIN).json()
    client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN)
    waiting = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert waiting['status'] == 'waiting'
    assert waiting['state']['steps']['create']['action'] == 'git' and waiting['state']['steps']['create']['link'] == hosting.VERCEL_GIT_APP

    fake_hosts(routes())  # The user installed Vercel's GitHub app.
    assert client.post(f"/api/projects/{project}/moves/{move['id']}/continue", headers=ORIGIN).status_code == 202
    assert client.get(f'/api/projects/{project}/moves/latest').json()['move']['status'] == 'done'


def test_user_adds_secrets_themselves_and_cloudhop_checks_before_deploying(client, fake_hosts):
    hosts = fake_hosts(routes(env_keys=['API_URL']))
    project, cloudflare, vercel = setup_project(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': vercel}, headers=ORIGIN).json()
    client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'manual'}, headers=ORIGIN)
    waiting = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert waiting['status'] == 'waiting' and waiting['state']['steps']['variables']['missing'] == ['STRIPE_KEY']
    assert not any('/v13/deployments' in call['url'] for call in hosts.calls), 'no deploy before the secrets are there'
    created = hosts.json(lambda call: call['method'] == 'POST' and '/v11/projects' in call['url'])
    assert [item['key'] for item in created['environmentVariables']] == ['API_URL'], 'only readable settings were copied'

    fake_hosts(routes(env_keys=['API_URL', 'STRIPE_KEY']))
    client.post(f"/api/projects/{project}/moves/{move['id']}/continue", headers=ORIGIN)
    done = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert done['status'] == 'done' and done['state']['steps']['create']['status'] == 'done'


def test_pause_where_the_platform_can_and_point_to_delete_where_it_cannot(client, fake_hosts):
    hosts = fake_hosts(routes())
    project, cloudflare, vercel = setup_project(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    client.put(f'/api/projects/{project}/secrets', json={'values': {'STRIPE_KEY': 'sk'}}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': vercel}, headers=ORIGIN).json()
    client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN)

    old = client.post(f'/api/projects/{project}/hosting/pause', json={'which': 'previous', 'approved': True}, headers=ORIGIN)
    assert old.status_code == 400 and 'cannot pause' in old.json()['error'] and 'dash.cloudflare.com' in old.json()['error']
    assert client.post(f'/api/projects/{project}/hosting/pause', json={'which': 'current'}, headers=ORIGIN).status_code == 400, 'needs approval'
    assert client.post(f'/api/projects/{project}/hosting/pause', json={'which': 'current', 'approved': True}, headers=ORIGIN).json() == {'paused': True}
    assert any(call['method'] == 'POST' and call['url'].endswith('/v1/projects/prj_1/pause') for call in hosts.calls)


def test_secrets_are_checked_and_moves_need_a_supported_target(client, fake_hosts):
    fake_hosts(routes())
    project, cloudflare, vercel = setup_project(client)
    assert client.put(f'/api/projects/{project}/secrets', json={'values': {'bad name': 'x'}}, headers=ORIGIN).status_code == 400
    assert client.put(f'/api/projects/{project}/secrets', json={'values': {'OK': ''}}, headers=ORIGIN).status_code == 400
    assert client.get(f'/api/projects/{project}/secrets').json() == {'names': []}
    assert client.post(f'/api/projects/{project}/moves', json={'to': 'cloudflare_pages', 'accountId': cloudflare}, headers=ORIGIN).status_code == 400, 'coming soon'
    assert client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': cloudflare}, headers=ORIGIN).status_code == 400, 'wrong account type'


def test_domain_records_and_cloudflare_dns_plan_change_nothing(fake_hosts):
    assert hosting.dns_records_for('vercel', 'example.com') == [{'type': 'A', 'name': '@', 'content': '76.76.21.21'}]
    assert hosting.dns_records_for('vercel', 'www.example.com') == [{'type': 'CNAME', 'name': 'www', 'content': 'cname.vercel-dns.com'}]
    hosts = fake_hosts({
        'GET api.cloudflare.com/client/v4/zones/z1/dns_records': {'result': [{'id': 'r1', 'type': 'CNAME', 'content': 'shop.pages.dev'}, {'id': 'r2', 'type': 'TXT', 'content': 'x'}]},
        'GET api.cloudflare.com/client/v4/zones': {'result': [{'id': 'z1', 'name': 'example.com'}]},
    })
    import asyncio
    plan = asyncio.run(hosting.cloudflare_dns_plan('t', 'shop.example.com', hosting.dns_records_for('vercel', 'shop.example.com')))
    assert plan['managedHere'] and plan['replace'] == [{'id': 'r1', 'type': 'CNAME', 'content': 'shop.pages.dev'}]
    assert all(call['method'] == 'GET' for call in hosts.calls)


def test_logs_show_move_steps_approvals_and_uptime_changes_newest_first(client, fake_hosts):
    fake_hosts(routes())
    project, cloudflare, vercel = setup_project(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    client.put(f'/api/projects/{project}/secrets', json={'values': {'STRIPE_KEY': 'sk'}}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': vercel}, headers=ORIGIN).json()
    client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN)
    for at, ok in [(1000, 1), (2000, 0), (3000, 0), (4000, 1)]:
        db.run('INSERT INTO checks (project_id, at, ok, status, ms) VALUES (?, ?, ?, ?, ?)', project, at, ok, 200 if ok else 503, 50 if ok else None)

    entries = client.get(f'/api/logs?project={project}').json()['entries']
    events = [entry['event'] for entry in entries]
    assert 'move_started' in events and 'moved' in events and 'hosting_found' in events
    steps = [(entry['data']['step'], entry['data']['status']) for entry in entries if entry['event'] == 'move_step']
    assert ('create', 'done') in steps and ('verify', 'done') in steps
    assert [entry['event'] for entry in entries if entry['source'] == 'uptime'] == ['site_up', 'site_down']
    assert entries == sorted(entries, key=lambda entry: entry['at'], reverse=True)
    assert all(entry['level'] == 'error' for entry in client.get(f'/api/logs?project={project}&level=error').json()['entries'])
    assert {entry['source'] for entry in client.get('/api/logs?source=you').json()['entries']} == {'you'}
    assert 'move_step' not in [event['event'] for event in client.get(f'/api/projects/{project}/status').json()['events']], 'activity stays short'
    assert client.get('/api/logs?project=someone-else').status_code == 404


def test_domain_check_reads_only_and_says_what_record_is_needed(client, fake_hosts):
    hosts = fake_hosts({**routes(),
                        'GET api.cloudflare.com/client/v4/zones/z1/dns_records': {'result': [{'id': 'r1', 'type': 'CNAME', 'content': 'shop.pages.dev'}]},
                        'GET api.cloudflare.com/client/v4/zones': {'result': [{'id': 'z1', 'name': 'example.com'}]}})
    project, cloudflare, vercel = setup_project(client)
    assert client.put(f'/api/projects/{project}/domain', json={'domain': 'not a domain'}, headers=ORIGIN).status_code == 400
    assert client.put(f'/api/projects/{project}/domain', json={'domain': 'Shop.Example.com.'}, headers=ORIGIN).json() == {'domain': 'shop.example.com'}
    assert client.get(f'/api/projects/{project}/domain/check').status_code == 400, 'needs to know where the site runs'
    client.put(f'/api/projects/{project}/secrets', json={'values': {'STRIPE_KEY': 'sk'}}, headers=ORIGIN)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    assert client.get(f'/api/projects/{project}/domain/check').json()['supported'] is False, 'Cloudflare Pages domains are coming soon'
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': vercel}, headers=ORIGIN).json()
    client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN)
    calls_before = len(hosts.calls)
    check = client.get(f'/api/projects/{project}/domain/check').json()
    assert check['records'] == [{'type': 'CNAME', 'name': 'shop', 'content': 'cname.vercel-dns.com'}]
    assert check['cloudflare']['managedHere'] and check['cloudflare']['replace'][0]['content'] == 'shop.pages.dev'
    assert all(call['method'] == 'GET' for call in hosts.calls[calls_before:]), 'checking changes nothing'


def _drop(request):
    raise httpx.ConnectError('connection dropped', request=request)


def _start_move(client, mode='cloudhop'):
    project, cloudflare, vercel = setup_project(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    client.put(f'/api/projects/{project}/secrets', json={'values': {'STRIPE_KEY': 'sk_test_value'}}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': vercel}, headers=ORIGIN).json()
    assert client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': mode}, headers=ORIGIN).status_code == 202
    return project, move['id']


@pytest.mark.parametrize(('route', 'step', 'mode'), [
    ('POST api.vercel.com/v11/projects', 'create', 'cloudhop'),
    ('GET api.vercel.com/v9/projects/prj_1/env', 'variables', 'manual'),
    ('POST api.vercel.com/v13/deployments', 'deploy', 'cloudhop'),
    ('GET api.vercel.com/v13/deployments/dpl_1', 'deploy', 'cloudhop'),
])
def test_a_dropped_connection_fails_the_move_safely_and_continue_finishes_it(client, fake_hosts, route, step, mode):
    hosts = fake_hosts({**routes(env_keys=['API_URL', 'STRIPE_KEY']), route: _drop})
    project, move_id = _start_move(client, mode)
    failed = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert failed['status'] == 'failed', failed
    detail = failed['state']['steps'][step]['detail']
    assert 'Could not reach api.vercel.com' in detail and VERCEL_TOKEN not in json.dumps(failed)
    assert not any(value['status'] == 'running' for value in failed['state']['steps'].values())

    # The platform is back. Continue picks up where it stopped. A create whose answer was lost may still have
    # happened, so the retry looks for that project before creating one.
    fixed = routes(env_keys=['API_URL', 'STRIPE_KEY'])
    if step == 'create':
        fixed['GET api.vercel.com/v10/projects'] = {'projects': [{'id': 'prj_1', 'name': 'shop', 'link': {'type': 'github', 'org': 'acme', 'repo': 'shop'}}]}
    hosts = fake_hosts(fixed)
    assert client.post(f'/api/projects/{project}/moves/{move_id}/continue', headers=ORIGIN).status_code == 202
    done = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert done['status'] == 'done', done
    if step == 'create':
        creates = [call for call in hosts.calls if call['method'] == 'POST' and '/v11/projects' in call['url']]
        assert creates == [], 'the project from the lost answer is reused, not created twice'


def test_a_cancelled_move_is_not_swallowed(client, fake_hosts, monkeypatch):
    import asyncio

    from cloudhop import main
    fake_hosts({**routes(), 'POST api.vercel.com/v11/projects': _drop})
    project, move_id = _start_move(client)

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(hosting.SITES['vercel'], 'create', cancelled)
    db.run("UPDATE moves SET status = 'running' WHERE id = ?", move_id)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.run_move(project, move_id))
    assert db.one('SELECT status FROM moves WHERE id = ?', move_id)['status'] == 'running', 'cancellation is passed on, not recorded as a failure'
