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
    assert client.post(f'/api/projects/{project}/moves', json={'to': 'netlify', 'accountId': cloudflare}, headers=ORIGIN).status_code == 400, 'coming soon'
    assert client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': cloudflare}, headers=ORIGIN).status_code == 400, 'wrong account type'
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    same = client.post(f'/api/projects/{project}/moves', json={'to': 'cloudflare_pages', 'accountId': cloudflare}, headers=ORIGIN)
    assert same.status_code == 400 and 'already runs' in same.json()['error']


VERCEL_SITE = {'id': 'prj_old', 'name': 'shop', 'accountId': 'team', 'link': {'type': 'github', 'org': 'acme', 'repo': 'shop', 'productionBranch': 'main'},
               'buildCommand': 'npm run build', 'outputDirectory': 'dist', 'rootDirectory': 'web', 'framework': 'vite'}
CF = 'api.cloudflare.com/client/v4/accounts/a1/pages/projects'


def cloudflare_routes(create=None):
    return {
        f'POST {CF}/shop/deployments': {'result': {'id': 'd1'}},
        f'GET {CF}/shop/deployments/d1': {'result': {'latest_stage': {'name': 'deploy', 'status': 'success'}}},
        f'POST {CF}': create or {'result': {'name': 'shop', 'subdomain': 'shop-x.pages.dev'}},
        'GET shop-x.pages.dev': '<html><title>Shop</title></html>',
        'GET api.vercel.com/v9/projects/prj_old/env': {'envs': [
            {'key': 'API_URL', 'target': ['production'], 'type': 'plain', 'value': 'https://api.example.com'},
            {'key': 'STRIPE_KEY', 'target': ['production'], 'type': 'encrypted'}]},
        **routes(),
        'GET api.vercel.com/v10/projects': {'projects': [VERCEL_SITE]},
        'POST api.vercel.com/v1/projects/prj_old/pause': {},
    }


def test_move_from_vercel_to_cloudflare_pages(client, fake_hosts):
    hosts = fake_hosts(cloudflare_routes())
    project, cloudflare, vercel = setup_project(client)
    assert client.put(f'/api/projects/{project}/hosting/current', json={'accountId': vercel, 'siteId': 'prj_old'}, headers=ORIGIN).status_code == 200
    client.put(f'/api/projects/{project}/secrets', json={'values': {'STRIPE_KEY': 'sk_test_value'}}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'cloudflare_pages', 'accountId': cloudflare}, headers=ORIGIN).json()
    assert move['plan']['to'] == 'cloudflare_pages' and move['plan']['steps'][0]['title'].startswith('Create the Cloudflare Pages project')
    assert client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN).status_code == 202

    done = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert done['status'] == 'done', done
    created = hosts.json(lambda call: call['method'] == 'POST' and call['url'].endswith('/pages/projects'))
    assert created['source'] == {'type': 'github', 'config': {'owner': 'acme', 'repo_name': 'shop', 'production_branch': 'main', 'deployments_enabled': True,
                                                              'production_deployments_enabled': True, 'pr_comments_enabled': False, 'preview_deployment_setting': 'none'}}
    assert created['build_config'] == {'build_command': 'npm run build', 'destination_dir': 'dist', 'root_dir': 'web'}
    assert created['deployment_configs']['production']['env_vars'] == {
        'API_URL': {'type': 'secret_text', 'value': 'https://api.example.com'}, 'STRIPE_KEY': {'type': 'secret_text', 'value': 'sk_test_value'}}
    deploy = next(call for call in hosts.calls if call['method'] == 'POST' and call['url'].endswith('/shop/deployments'))
    assert 'name="branch"' in deploy['body'] and 'main' in deploy['body'] and deploy['headers']['content-type'].startswith('multipart/form-data')

    now = client.get(f'/api/projects/{project}/status').json()['project']
    assert (now['active'], now['url']) == ('cloudflare_pages', 'https://shop-x.pages.dev')
    assert now['hosting']['current']['manageUrl'].startswith('https://dash.cloudflare.com/a1/pages/view/shop')
    assert now['hosting']['previous']['platform'] == 'vercel'
    assert client.get(f'/api/projects/{project}/secrets').json() == {'names': []}, 'secrets now live on Cloudflare'
    assert client.post(f'/api/projects/{project}/hosting/pause', json={'which': 'previous', 'approved': True}, headers=ORIGIN).json() == {'paused': True}


def test_cloudflare_move_waits_for_its_github_app_and_reports_failed_builds(client, fake_hosts):
    no_git = httpx.Response(400, json={'success': False, 'errors': [{'code': 8000011, 'message': 'There is an internal issue with your Cloudflare Pages Git installation.'}]})
    fake_hosts(cloudflare_routes(create=no_git))
    project, cloudflare, vercel = setup_project(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': vercel, 'siteId': 'prj_old'}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'cloudflare_pages', 'accountId': cloudflare}, headers=ORIGIN).json()
    client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'manual'}, headers=ORIGIN)
    waiting = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert waiting['status'] == 'waiting' and waiting['state']['steps']['create']['link'] == hosting.CLOUDFLARE_GIT_APP
    assert 'Cloudflare Pages cannot see this repo' in waiting['state']['steps']['create']['detail']

    failing = cloudflare_routes()
    failing[f'GET {CF}/shop'] = {'result': {'deployment_configs': {'production': {'env_vars': {'API_URL': {}, 'STRIPE_KEY': {}}}}}}
    failing[f'GET {CF}/shop/deployments/d1'] = {'result': {'latest_stage': {'name': 'build', 'status': 'failure'}}}
    fake_hosts(dict(sorted(failing.items(), key=lambda item: -len(item[0]))))  # Most specific route first.
    client.post(f"/api/projects/{project}/moves/{move['id']}/continue", headers=ORIGIN)
    failed = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert failed['status'] == 'failed' and 'build stage failed' in failed['state']['steps']['deploy']['detail']


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


RENDER_TOKEN = 'rnd_render-key-0123456789'
RENDER = 'api.render.com/v1'
RENDER_WEB = {'id': 'srv-old', 'name': 'shop-api', 'type': 'web_service', 'ownerId': 'tea-1', 'repo': 'https://github.com/acme/shop', 'branch': 'main',
              'suspended': 'not_suspended', 'dashboardUrl': 'https://dashboard.render.com/web/srv-old',
              'serviceDetails': {'url': 'https://shop-api.onrender.com', 'envSpecificDetails': {'buildCommand': 'npm ci', 'startCommand': 'node server.js'}}}


def render_routes(create=None, deploy_status='live'):
    return {
        f'GET {RENDER}/services/srv-old/env-vars': [{'cursor': 'c1', 'envVar': {'key': 'API_URL', 'value': 'https://api.example.com'}}],
        f'GET {RENDER}/services/srv-new/deploys/dep-1': {'id': 'dep-1', 'status': deploy_status},
        f'POST {RENDER}/services/srv-new/deploys': {'id': 'dep-2', 'status': 'created'},
        f'POST {RENDER}/services/srv-old/suspend': '',
        f'POST {RENDER}/services': create or {'service': {'id': 'srv-new', 'name': 'shop', 'dashboardUrl': 'https://dashboard.render.com/static/srv-new',
                                                          'serviceDetails': {'url': 'https://shop.onrender.com'}}, 'deployId': 'dep-1'},
        f'GET {RENDER}/services': [{'cursor': 'a', 'service': RENDER_WEB}, {'cursor': 'b', 'service': {**RENDER_WEB, 'id': 'srv-x', 'repo': 'https://github.com/acme/other'}}],
        f'GET {RENDER}/owners': [{'cursor': 'o', 'owner': {'id': 'tea-1', 'name': 'Acme', 'type': 'team'}}],
        'GET shop.onrender.com': '<html><title>Shop</title></html>',
        **routes(),
    }


def setup_render(client):
    project, cloudflare, vercel = setup_project(client)
    render = client.post('/api/accounts', json={'kind': 'render', 'token': RENDER_TOKEN}, headers=ORIGIN).json()['id']
    return project, cloudflare, vercel, render


def test_finds_render_services_and_warns_when_a_server_moves_to_a_static_host(client, fake_hosts):
    fake_hosts(render_routes())
    project, cloudflare, vercel, render = setup_render(client)
    found = [site for site in client.get(f'/api/projects/{project}/hosting/find').json()['sites'] if site['platform'] == 'render']
    assert [site['id'] for site in found] == ['srv-old'], 'only services built from this repo'
    site = found[0]
    assert (site['kind'], site['url'], site['settings']['buildCommand']) == ('server', 'https://shop-api.onrender.com', 'npm ci')
    assert site['variables'] == [{'name': 'API_URL', 'secret': False, 'value': 'https://api.example.com'}], 'Render returns values'
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': render, 'siteId': 'srv-old'}, headers=ORIGIN)
    plan = client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': vercel}, headers=ORIGIN).json()['plan']
    assert any('running server on Render' in warning for warning in plan['warnings'])
    assert plan['copy'] == ['API_URL'] and plan['secrets'] == []


def test_move_from_cloudflare_pages_to_a_render_static_site(client, fake_hosts):
    hosts = fake_hosts(render_routes())
    project, cloudflare, vercel, render = setup_render(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    client.put(f'/api/projects/{project}/secrets', json={'values': {'STRIPE_KEY': 'sk_test_value'}}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'render', 'accountId': render}, headers=ORIGIN).json()
    assert any('Render static site' in warning for warning in move['plan']['warnings'])
    assert client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN).status_code == 202

    done = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert done['status'] == 'done', done
    created = hosts.json(lambda call: call['method'] == 'POST' and call['url'].endswith('/v1/services'))
    assert (created['type'], created['ownerId'], created['repo'], created['branch'], created['autoDeploy']) == ('static_site', 'tea-1', 'https://github.com/acme/shop', 'main', 'yes')
    assert created['serviceDetails'] == {'buildCommand': 'npm install && npm run build', 'publishPath': 'dist'} and created['rootDir'] == 'web', 'Render needs the install'
    assert {item['key']: item['value'] for item in created['envVars']} == {'API_URL': 'https://api.example.com', 'STRIPE_KEY': 'sk_test_value'}
    assert not any(call['method'] == 'POST' and call['url'].endswith('/deploys') for call in hosts.calls), 'follows the deploy Render started, no second one'
    now = client.get(f'/api/projects/{project}/status').json()['project']
    assert (now['active'], now['url']) == ('render', 'https://shop.onrender.com')
    assert 'firstDeploy' not in now['hosting']['current']


def test_render_move_waits_for_its_github_app_and_reports_a_failed_build(client, fake_hosts):
    no_git = httpx.Response(400, json={'id': 'invalid', 'message': 'Could not access the repository. Connect your GitHub account to Render.'})
    fake_hosts(render_routes(create=no_git))
    project, cloudflare, vercel, render = setup_render(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'render', 'accountId': render}, headers=ORIGIN).json()
    client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'manual'}, headers=ORIGIN)
    waiting = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert waiting['status'] == 'waiting' and waiting['state']['steps']['create']['link'] == hosting.RENDER_GIT_APP

    failing = render_routes()
    failing[f'GET {RENDER}/services/srv-new/env-vars'] = [{'cursor': 'x', 'envVar': {'key': 'API_URL', 'value': 'v'}}, {'cursor': 'y', 'envVar': {'key': 'STRIPE_KEY', 'value': 'v'}}]
    failing[f'GET {RENDER}/services/srv-new/deploys/dep-2'] = {'id': 'dep-2', 'status': 'build_failed'}
    hosts = fake_hosts(dict(sorted(failing.items(), key=lambda item: -len(item[0]))))
    client.post(f"/api/projects/{project}/moves/{move['id']}/continue", headers=ORIGIN)
    failed = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert failed['status'] == 'failed' and 'build failed' in failed['state']['steps']['deploy']['detail']
    assert any(call['method'] == 'POST' and call['url'].endswith('/srv-new/deploys') for call in hosts.calls), 'a fresh build with the added secrets'


def test_a_render_site_can_be_paused_after_moving_away(client, fake_hosts):
    hosts = fake_hosts(render_routes())
    project, cloudflare, vercel, render = setup_render(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': render, 'siteId': 'srv-old'}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': vercel}, headers=ORIGIN).json()
    client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN)
    assert client.get(f'/api/projects/{project}/moves/latest').json()['move']['status'] == 'done'
    assert client.post(f'/api/projects/{project}/hosting/pause', json={'which': 'previous', 'approved': True}, headers=ORIGIN).json() == {'paused': True}
    assert any(call['method'] == 'POST' and call['url'].endswith('/services/srv-old/suspend') for call in hosts.calls)


def test_a_plan_with_no_current_site_takes_build_settings_from_the_repo(client, fake_hosts):
    # Found in the end-to-end run: with no site to copy from, the plan had no build settings, so the platform would
    # publish the repo unbuilt. The static site in web/ (Vite) now sets them.
    fake_hosts({
        'GET api.github.com/repos/acme/shop/git/trees/main': {'tree': [{'path': 'web/package.json', 'type': 'blob', 'size': 50}, {'path': 'api/main.py', 'type': 'blob', 'size': 50}], 'truncated': False},
        'GET raw.githubusercontent.com/acme/shop/main/web/package.json': '{"scripts":{"build":"vite build"},"devDependencies":{"vite":"6"}}',
        **routes(),
    })
    project, cloudflare, vercel = setup_project(client)
    plan = client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': vercel}, headers=ORIGIN).json()['plan']
    assert plan['settings'] == {'buildCommand': 'npm run build', 'outputDirectory': 'dist', 'rootDirectory': 'web'}
    assert any('the repo supplies them' in warning for warning in plan['warnings'])


def test_the_new_site_must_serve_its_page_not_just_answer():
    import asyncio
    for status, ok in [(200, True), (301, True), (404, False), (401, False), (503, False)]:
        net_transport = httpx.MockTransport(lambda request, status=status: httpx.Response(status, text='<title>x</title>'))
        from cloudhop import net
        net.use(net_transport)
        assert asyncio.run(hosting.check_site('https://new.example'))['ok'] is ok, status


def test_render_builds_install_dependencies_first():
    render = hosting.Render()
    assert render.build_command('npm run build') == 'npm install && npm run build'
    assert render.build_command('yarn build') == 'yarn install && yarn build'
    assert render.build_command('npm ci && npm run build') == 'npm ci && npm run build'
    assert render.build_command('npm install && npm run build') == 'npm install && npm run build'
    assert render.build_command('hugo --minify') == 'hugo --minify'
    assert render.build_command(None) == 'npm install && npm run build'


def test_moves_build_the_repos_default_branch_when_no_site_names_one(client, fake_hosts):
    # Review of #14: with no current site, moves built "main" even when the repo's default branch is something else.
    hosts = fake_hosts({f'GET {RENDER}/services/srv-new/env-vars': [], **render_routes(), 'GET api.github.com/repos/acme/shop': {'default_branch': 'master'},
                        'GET api.github.com/repos/acme/shop/git/trees/master': {'tree': [], 'truncated': False}})
    project, cloudflare, vercel, render = setup_render(client)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'render', 'accountId': render}, headers=ORIGIN).json()
    assert move['plan']['branch'] == 'master' and 'master branch' in move['plan']['steps'][2]['title']
    client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN)
    created = hosts.json(lambda call: call['method'] == 'POST' and call['url'].endswith('/v1/services'))
    assert created['branch'] == 'master'


def test_a_render_move_with_secrets_added_by_hand_builds_again_with_them(client, fake_hosts):
    # Review of #14: Render starts a build on create, before hand-added secrets exist; continuing must not reuse it.
    routes_now = render_routes()
    routes_now[f'GET {RENDER}/services/srv-new/env-vars'] = [{'cursor': 'x', 'envVar': {'key': 'API_URL', 'value': 'v'}}]
    fake_hosts(dict(sorted(routes_now.items(), key=lambda item: -len(item[0]))))
    project, cloudflare, vercel, render = setup_render(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'render', 'accountId': render}, headers=ORIGIN).json()
    client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'manual'}, headers=ORIGIN)
    assert client.get(f'/api/projects/{project}/moves/latest').json()['move']['state']['steps']['variables']['status'] == 'waiting'

    added = render_routes()
    added[f'GET {RENDER}/services/srv-new/env-vars'] = [{'cursor': 'x', 'envVar': {'key': 'API_URL', 'value': 'v'}}, {'cursor': 'y', 'envVar': {'key': 'STRIPE_KEY', 'value': 'v'}}]
    added[f'GET {RENDER}/services/srv-new/deploys/dep-2'] = {'id': 'dep-2', 'status': 'live'}
    hosts = fake_hosts(dict(sorted(added.items(), key=lambda item: -len(item[0]))))
    client.post(f"/api/projects/{project}/moves/{move['id']}/continue", headers=ORIGIN)
    assert client.get(f'/api/projects/{project}/moves/latest').json()['move']['status'] == 'done'
    assert any(call['method'] == 'POST' and call['url'].endswith('/srv-new/deploys') for call in hosts.calls), 'a fresh build, not the one from before the secrets'


def test_a_site_that_leaves_settings_to_its_framework_gets_them_from_the_repo(client, fake_hosts):
    # Review of #14: a Vercel site with only a framework set gave Render the "dist" default; Create React App outputs "build".
    cra_site = {'id': 'prj_cra', 'name': 'shop', 'accountId': 'team', 'framework': 'create-react-app', 'link': {'type': 'github', 'org': 'acme', 'repo': 'shop'}}
    hosts = fake_hosts({
        'GET api.github.com/repos/acme/shop/git/trees/main': {'tree': [{'path': 'package.json', 'type': 'blob', 'size': 50}], 'truncated': False},
        'GET raw.githubusercontent.com/acme/shop/main/package.json': '{"scripts":{"build":"react-scripts build"},"dependencies":{"react-scripts":"5"}}',
        'GET api.vercel.com/v9/projects/prj_cra/env': {'envs': []},
        **render_routes(),
        'GET api.vercel.com/v10/projects': {'projects': [cra_site]},
    })
    project, cloudflare, vercel, render = setup_render(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': vercel, 'siteId': 'prj_cra'}, headers=ORIGIN)
    move = client.post(f'/api/projects/{project}/moves', json={'to': 'render', 'accountId': render}, headers=ORIGIN).json()
    assert move['plan']['settings'] == {'framework': 'create-react-app', 'buildCommand': 'npm run build', 'outputDirectory': 'build'}, 'root stays the repo root'
    client.post(f"/api/projects/{project}/moves/{move['id']}/approve", json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN)
    created = hosts.json(lambda call: call['method'] == 'POST' and call['url'].endswith('/v1/services'))
    assert created['serviceDetails']['publishPath'] == 'build' and 'rootDir' not in created
