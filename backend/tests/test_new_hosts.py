"""Northflank, DigitalOcean App Platform, GitHub Pages and Coolify as hosting platforms, against recording fakes.
Request shapes follow each platform's API reference or OpenAPI file; none has run on a real account yet."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from test_hosting import ORIGIN, setup_project
from test_more_platforms import HTML, from_cloudflare, merged, move

from cloudhop import db, hosting
from cloudhop.main import app


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    db.connect(':memory:')

    async def no_wait(*_args, **_kwargs):
        return None
    monkeypatch.setattr(hosting.asyncio, 'sleep', no_wait)


@pytest.fixture
def client():
    signed_in = TestClient(app, base_url='http://localhost:5173')
    assert signed_in.post('/api/auth/login', json={'token': 'test-admin-token'}, headers=ORIGIN).status_code == 200
    return signed_in

NF = 'api.northflank.com/v1'


def test_northflank_moves_a_site_into_a_new_project_and_pauses_it(client, fake_hosts):
    hosts = fake_hosts(merged({
        f'GET {NF}/auth': {'data': {'entityId': 'acme-team', 'entityType': 'team'}},
        f'GET {NF}/projects/shop/services/svc1/ports': {'data': {'ports': [{'public': True, 'dns': 'p01--shop--acme.code.run'}]}},
        f'GET {NF}/projects/shop/services/svc1/runtime-environment/details': {'data': {'runtimeEnvironment': {'STRIPE_KEY': {'value': 'x'}}}},
        f'GET {NF}/projects/shop/services/svc1': {'data': {'id': 'svc1', 'status': {'build': {'status': 'SUCCESS'}, 'deployment': {'status': 'COMPLETED'}}}},
        f'POST {NF}/projects/shop/services/combined': {'data': {'id': 'svc1'}},
        f'POST {NF}/projects/shop/services/svc1/pause': {'data': {}},
        f'GET {NF}/projects': {'data': {'projects': []}, 'pagination': {'hasNextPage': False}},
        f'POST {NF}/projects': {'data': {'id': 'shop'}},
        'GET p01--shop--acme.code.run': HTML,
    }))
    project, northflank = from_cloudflare(client, 'northflank', 'nf-token-0123456789abcdefghij')
    plan, done = move(client, project, 'northflank', northflank)
    assert any('runs your app as a web service' in warning for warning in plan['plan']['warnings'])
    assert done['status'] == 'done', done
    body = hosts.json(lambda call: call['url'].endswith('/services/combined'))
    assert body['vcsData'] == {'projectUrl': 'https://github.com/acme/shop', 'projectType': 'github', 'projectBranch': 'main'}
    assert body['buildSettings'] == {'buildpack': {'buildContext': '/web'}} and body['billing'] == {'deploymentPlan': 'nf-compute-10'}
    assert body['runtimeEnvironment'] == {'PORT': '8080', 'API_URL': 'https://api.example.com', 'STRIPE_KEY': 'sk_test_value'}
    assert not any(call['url'].endswith('/build') for call in hosts.calls), 'follows the build Northflank started on create'
    assert client.get(f'/api/projects/{project}/status').json()['project']['url'] == 'https://p01--shop--acme.code.run'
    assert client.post(f'/api/projects/{project}/hosting/pause', json={'which': 'current', 'approved': True}, headers=ORIGIN).json() == {'paused': True}


def test_northflank_finds_services_built_from_the_repo_and_asks_for_every_variable(client, fake_hosts):
    service = {'id': 'web', 'name': 'web', 'vcsData': {'projectUrl': 'https://github.com/acme/shop', 'projectBranch': 'main'}, 'buildSettings': {'buildpack': {'buildContext': '/web'}}}
    fake_hosts(merged({
        f'GET {NF}/auth': {'data': {'entityId': 'acme-team'}},
        f'GET {NF}/projects/p1/services/web/ports': {'data': {'ports': [{'public': True, 'dns': 'web--p1.code.run'}]}},
        f'GET {NF}/projects/p1/services/web/runtime-environment/details': {'data': {'runtimeEnvironment': {'DATABASE_URL': {'value': 'postgres://secret'}}}},
        f'GET {NF}/projects/p1/services/web': {'data': service},
        f'GET {NF}/projects/p1/services/other': {'data': {**service, 'id': 'other', 'vcsData': {'projectUrl': 'https://github.com/acme/other'}}},
        f'GET {NF}/projects/p1/services': {'data': {'services': [{'id': 'web', 'serviceType': 'combined'}, {'id': 'other', 'serviceType': 'combined'}, {'id': 'db', 'serviceType': 'deployment'}]}, 'pagination': {'hasNextPage': False}},
        f'GET {NF}/projects': {'data': {'projects': [{'id': 'p1', 'name': 'Shop'}]}, 'pagination': {'hasNextPage': False}},
    }))
    project, _ = from_cloudflare(client, 'northflank', 'nf-token-0123456789abcdefghij')
    found = [site for site in client.get(f'/api/projects/{project}/hosting/find').json()['sites'] if site['platform'] == 'northflank']
    assert [site['id'] for site in found] == ['web']
    assert found[0]['url'] == 'https://web--p1.code.run' and found[0]['settings']['rootDirectory'] == 'web'
    assert found[0]['variables'] == [{'name': 'DATABASE_URL', 'secret': True, 'value': None}], 'values are never copied as plain text'


DO = 'api.digitalocean.com/v2'


def test_digitalocean_moves_to_a_free_static_site_and_archives_to_pause(client, fake_hosts):
    hosts = fake_hosts(merged({
        f'GET {DO}/account': {'account': {'email': 'octo@example.com'}},
        f'GET {DO}/apps/app1/deployments/dep1': {'deployment': {'phase': 'ACTIVE'}},
        f'GET {DO}/apps/app1': {'app': {'id': 'app1', 'live_url': 'https://shop-abc.ondigitalocean.app', 'spec': {'name': 'shop', 'static_sites': [{'name': 'shop'}]}}},
        f'PUT {DO}/apps/app1': {'app': {'id': 'app1'}},
        f'GET {DO}/apps': {'apps': []},
        f'POST {DO}/apps': {'app': {'id': 'app1', 'pending_deployment': {'id': 'dep1'}}},
        'GET shop-abc.ondigitalocean.app': HTML,
    }))
    project, account = from_cloudflare(client, 'do_apps', 'dop_v1_' + 'a' * 40)
    plan, done = move(client, project, 'do_apps', account)
    assert done['status'] == 'done', done
    spec = hosts.json(lambda call: call['method'] == 'POST' and call['url'].endswith('/v2/apps'))['spec']
    site = spec['static_sites'][0]
    assert site['github'] == {'repo': 'acme/shop', 'branch': 'main', 'deploy_on_push': True}
    assert (site['build_command'], site['output_dir'], site['source_dir']) == ('npm run build', 'dist', 'web')
    assert {env['key']: (env['value'], env['type'], env['scope']) for env in site['envs']} == {
        'API_URL': ('https://api.example.com', 'SECRET', 'BUILD_TIME'), 'STRIPE_KEY': ('sk_test_value', 'SECRET', 'BUILD_TIME')}
    assert not any(call['method'] == 'POST' and '/deployments' in call['url'] for call in hosts.calls), 'follows the first deployment'
    assert client.get(f'/api/projects/{project}/status').json()['project']['url'] == 'https://shop-abc.ondigitalocean.app'
    assert client.post(f'/api/projects/{project}/hosting/pause', json={'which': 'current', 'approved': True}, headers=ORIGIN).json() == {'paused': True}
    archived = hosts.json(lambda call: call['method'] == 'PUT')['spec']
    assert archived['maintenance'] == {'enabled': True, 'archive': True} and archived['static_sites'] == [{'name': 'shop'}], 'keeps the rest of the spec'


def test_digitalocean_waits_for_its_github_app(client, fake_hosts):
    fake_hosts(merged({
        f'GET {DO}/account': {'account': {'email': 'octo@example.com'}},
        f'GET {DO}/apps': {'apps': []},
        f'POST {DO}/apps': httpx.Response(400, json={'id': 'invalid_argument', 'message': 'GitHub user not authenticated'}),
    }))
    project, account = from_cloudflare(client, 'do_apps', 'dop_v1_' + 'a' * 40)
    _plan, waiting = move(client, project, 'do_apps', account)
    step = waiting['state']['steps']['create']
    assert (waiting['status'], step['action'], step['link']) == ('waiting', 'git', hosting.DO_GIT_APP)


def cloudflare_project(client):
    """A project whose site runs on Cloudflare Pages, with STRIPE_KEY saved in Cloudhop, and no other platform account."""
    project, cloudflare, _vercel = setup_project(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    client.put(f'/api/projects/{project}/secrets', json={'values': {'STRIPE_KEY': 'sk_test_value'}}, headers=ORIGIN)
    return project


GH = 'api.github.com/repos/acme/shop'
PAGES_WORKFLOW = 'raw.githubusercontent.com/acme/shop/main/.github/workflows/cloudhop-pages.yml'


def pages_routes(state):
    return merged({
        f'POST {GH}/pages': lambda request: httpx.Response(403, json={'message': 'Resource not accessible by integration'}) if state.get('no_pages_access') else httpx.Response(201, json={}),
        f'GET {GH}/pages': {'html_url': 'https://acme.github.io/shop/'},
        f'GET {GH}/actions/secrets': lambda request: {'secrets': [{'name': 'STRIPE_KEY'}] if state.get('secret_added') else []},
        f'GET {GH}/actions/variables': {'variables': []},
        f'GET {PAGES_WORKFLOW}': lambda request: '# Generated by Cloudhop for GitHub Pages. x\n' if state.get('merged') else httpx.Response(404),
        f'GET {GH}/git/ref/heads/main': {'object': {'sha': 'abc'}},
        f'GET {GH}/git/commits/abc': {'tree': {'sha': 't0'}},
        f'POST {GH}/git/trees': {'sha': 't1'},
        f'POST {GH}/git/commits': {'sha': 'c1c1c1c1c1'},
        f'POST {GH}/git/refs': {},
        f'POST {GH}/pulls': {'html_url': 'https://github.com/acme/shop/pull/9'},
        f'POST {GH}/actions/workflows/cloudhop-pages.yml/dispatches': httpx.Response(204),
        f'GET {GH}/actions/workflows/cloudhop-pages.yml/runs': {'workflow_runs': [{'created_at': '2999-01-01T00:00:00Z', 'status': 'completed', 'conclusion': 'success'}]},
        'GET acme.github.io/shop/': HTML,
    })


def test_github_pages_uses_the_projects_github_access_and_a_workflow_pull_request(client, fake_hosts):
    state = {}
    hosts = fake_hosts(pages_routes(state))
    project = cloudflare_project(client)
    plan = client.post(f'/api/projects/{project}/moves', json={'to': 'github_pages'}, headers=ORIGIN).json()
    assert any('/shop/' in warning and 'base path' in warning for warning in plan['plan']['warnings'])
    refused = client.post(f"/api/projects/{project}/moves/{plan['id']}/approve", json={'approved': True, 'secretsMode': 'cloudhop'}, headers=ORIGIN)
    assert refused.status_code == 400 and 'add them yourself' in refused.json()['error']

    assert client.post(f"/api/projects/{project}/moves/{plan['id']}/approve", json={'approved': True, 'secretsMode': 'manual'}, headers=ORIGIN).status_code == 202
    waiting = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert waiting['state']['steps']['variables']['missing'] == ['STRIPE_KEY'], waiting
    enable = next(call for call in hosts.calls if call['method'] == 'POST' and call['url'].endswith('/repos/acme/shop/pages'))
    assert enable['auth'] == 'Bearer ' + 'ghp_' + 'x' * 36, 'the project GitHub token, not a platform token'

    state['secret_added'] = True
    client.post(f"/api/projects/{project}/moves/{plan['id']}/continue", headers=ORIGIN)
    waiting = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert (waiting['status'], waiting['state']['steps']['deploy']['link']) == ('waiting', 'https://github.com/acme/shop/pull/9'), waiting
    workflow = {item['path']: item['content'] for item in hosts.json(lambda call: call['url'].endswith('/git/trees'))['tree']}['.github/workflows/cloudhop-pages.yml']
    assert 'path: web/dist' in workflow and 'working-directory: web' in workflow and 'run: npm run build' in workflow
    assert 'API_URL: "https://api.example.com"' in workflow and 'STRIPE_KEY: ${{ secrets.STRIPE_KEY }}' in workflow
    assert 'sk_test_value' not in workflow and '@368f82528645a54fb793d4d04e342629a3f51346' in workflow

    state['merged'] = True
    client.post(f"/api/projects/{project}/moves/{plan['id']}/continue", headers=ORIGIN)
    done = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert done['status'] == 'done', done
    assert any(call['url'].endswith('/cloudhop-pages.yml/dispatches') for call in hosts.calls)
    assert client.get(f'/api/projects/{project}/status').json()['project']['url'] == 'https://acme.github.io/shop/'
    paused = client.post(f'/api/projects/{project}/hosting/pause', json={'which': 'current', 'approved': True}, headers=ORIGIN)
    assert paused.status_code == 400 and 'settings/pages' in paused.json()['error']


def test_github_pages_asks_the_user_to_turn_pages_on_once(client, fake_hosts):
    state = {'no_pages_access': True}
    hosts = fake_hosts(pages_routes(state))
    project = cloudflare_project(client)
    plan = client.post(f'/api/projects/{project}/moves', json={'to': 'github_pages'}, headers=ORIGIN).json()
    client.post(f"/api/projects/{project}/moves/{plan['id']}/approve", json={'approved': True, 'secretsMode': 'manual'}, headers=ORIGIN)
    waiting = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    step = waiting['state']['steps']['create']
    assert (step['action'], step['link']) == ('git', 'https://github.com/acme/shop/settings/pages') and 'GitHub Actions' in step['detail']
    client.post(f"/api/projects/{project}/moves/{plan['id']}/continue", headers=ORIGIN)
    again = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert again['state']['steps']['create']['status'] == 'done', again
    assert sum(call['method'] == 'POST' and call['url'].endswith('/pages') for call in hosts.calls) == 1


def test_github_pages_needs_a_github_repo():
    files = hosting.SITES['github_pages'].repo_files({'id': 'acme/acme.github.io'}, {}, 'main')
    assert 'path: .\n' in files[0]['content'] and 'npm' not in files[0]['content'], 'a site with no build command publishes the repo as it is'
    assert hosting.GitHubPages.address('Acme/acme.github.io') == 'https://acme.github.io/'


CF = 'coolify.example.com/api/v1'
COOLIFY_TOKEN = '3|coolify-token-0123456789abcdef'


def coolify_routes():
    return merged({
        f'GET {CF}/team': {'name': 'Acme'},
        f'GET {CF}/applications/app1/envs': [],
        f'GET {CF}/applications/app1': {'uuid': 'app1', 'fqdn': 'https://shop.coolify.example.com'},
        f'PATCH {CF}/applications/app1/envs/bulk': [],
        f'POST {CF}/applications/app1/stop': {'message': 'Application stopping request queued.'},
        f'GET {CF}/applications': [],
        f'POST {CF}/applications/public': httpx.Response(201, json={'uuid': 'app1'}),
        f'GET {CF}/projects': [{'uuid': 'p1'}],
        f'GET {CF}/servers': [{'uuid': 's1'}],
        f'GET {CF}/github-apps': [],
        f'POST {CF}/deploy': {'deployments': [{'deployment_uuid': 'd1', 'resource_uuid': 'app1'}]},
        f'GET {CF}/deployments/d1': {'status': 'finished'},
        'GET shop.coolify.example.com': HTML,
    })


def test_coolify_saves_its_address_and_moves_a_site(client, fake_hosts):
    hosts = fake_hosts(coolify_routes())
    project = cloudflare_project(client)
    for url in ('http://coolify.example.com', 'https://coolify.example.com/path', ''):
        assert client.post('/api/accounts', json={'kind': 'coolify', 'token': COOLIFY_TOKEN, 'url': url}, headers=ORIGIN).status_code == 400
    saved = client.post('/api/accounts', json={'kind': 'coolify', 'token': COOLIFY_TOKEN, 'url': 'https://coolify.example.com/'}, headers=ORIGIN).json()
    assert saved['label'] == 'Coolify, Acme'
    assert COOLIFY_TOKEN not in str(client.get('/api/accounts').json()), 'the token never goes back to the browser'

    plan, done = move(client, project, 'coolify', saved['id'])
    assert done['status'] == 'done', done
    body = hosts.json(lambda call: call['url'].endswith('/applications/public'))
    assert (body['project_uuid'], body['server_uuid'], body['git_repository'], body['build_pack'], body['is_static']) == ('p1', 's1', 'https://github.com/acme/shop', 'nixpacks', True)
    assert (body['base_directory'], body['build_command'], body['publish_directory']) == ('/web', 'npm run build', '/dist')
    env = hosts.json(lambda call: call['url'].endswith('/envs/bulk'))['data']
    assert {item['key']: item['value'] for item in env} == {'API_URL': 'https://api.example.com', 'STRIPE_KEY': 'sk_test_value'}
    assert all(call['auth'] == f'Bearer {COOLIFY_TOKEN}' for call in hosts.calls if 'coolify.example.com/api' in call['url'])
    assert client.get(f'/api/projects/{project}/status').json()['project']['url'] == 'https://shop.coolify.example.com'
    assert client.post(f'/api/projects/{project}/hosting/pause', json={'which': 'current', 'approved': True}, headers=ORIGIN).json() == {'paused': True}


def test_coolify_refuses_private_addresses(monkeypatch):
    from cloudhop import net
    monkeypatch.setattr(net, 'check_addresses', True)
    with pytest.raises(hosting.RepoError, match='public https address'):
        asyncio.run(hosting.SITES['coolify'].find('{"url": "https://127.0.0.1:8000", "token": "x"}', 'acme/shop'))
