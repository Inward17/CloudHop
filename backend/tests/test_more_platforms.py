"""Netlify, Koyeb, Railway and Heroku as hosting platforms: find, plan, move and pause against recording fakes.
Request shapes follow each platform's API docs (Koyeb: its generated API client); none has run on a real account yet."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_hosting import ORIGIN, routes, setup_project

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

HTML = '<html><title>Shop</title></html>'


def merged(mine):
    """The test's own routes first (first match wins), then the shared ones it does not replace."""
    return {**mine, **{key: value for key, value in routes().items() if key not in mine}}


def move(client, project, target, account, secrets='cloudhop'):
    plan = client.post(f'/api/projects/{project}/moves', json={'to': target, 'accountId': account}, headers=ORIGIN).json()
    assert client.post(f"/api/projects/{project}/moves/{plan['id']}/approve", json={'approved': True, 'secretsMode': secrets}, headers=ORIGIN).status_code == 202
    return plan, client.get(f'/api/projects/{project}/moves/latest').json()['move']


def from_cloudflare(client, kind, token):
    """A platform project whose site runs on Cloudflare Pages today, with the STRIPE_KEY secret saved in Cloudhop."""
    project, cloudflare, vercel = setup_project(client)
    account = client.post('/api/accounts', json={'kind': kind, 'token': token}, headers=ORIGIN).json()['id']
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    client.put(f'/api/projects/{project}/secrets', json={'values': {'STRIPE_KEY': 'sk_test_value'}}, headers=ORIGIN)
    return project, account


NETLIFY = 'api.netlify.com/api/v1'


def netlify_routes(sites=()):
    return merged({
        f'GET {NETLIFY}/user': {'full_name': 'Octo Cat'},
        f'POST {NETLIFY}/sites/site1/builds': {'id': 'b1', 'deploy_id': 'dep1'},
        f'GET {NETLIFY}/deploys/dep1': {'state': 'ready'},
        f'PUT {NETLIFY}/sites/site9/disable': '',
        f'POST {NETLIFY}/accounts/acc1/env': [],
        f'GET {NETLIFY}/accounts/acc1/env': [{'key': 'API_URL', 'values': [{'value': 'https://api.example.com', 'context': 'all'}], 'is_secret': False},
                                             {'key': 'STRIPE_KEY', 'values': [], 'is_secret': True}],
        f'POST {NETLIFY}/team/sites': {'id': 'site1', 'name': 'shop', 'ssl_url': 'https://shop.netlify.app', 'admin_url': 'https://app.netlify.com/projects/shop'},
        f'GET {NETLIFY}/accounts': [{'id': 'acc1', 'slug': 'team', 'name': 'Team'}],
        f'GET {NETLIFY}/sites': list(sites),
        'GET shop.netlify.app': HTML,
    })


def test_netlify_finds_sites_moves_a_site_and_pauses_the_old_one(client, fake_hosts):
    site = {'id': 'site9', 'name': 'docs', 'account_id': 'acc1', 'account_slug': 'team', 'ssl_url': 'https://docs.netlify.app', 'admin_url': 'https://app.netlify.com/projects/docs',
            'build_settings': {'provider': 'github', 'repo_path': 'acme/shop', 'repo_branch': 'main', 'cmd': 'npm run build', 'dir': 'dist'}}
    fake_hosts(netlify_routes([site, {**site, 'id': 'site8', 'build_settings': {'repo_path': 'acme/other'}}]))
    project, netlify = from_cloudflare(client, 'netlify', 'nfp_netlify-token-0123456789')
    found = [item for item in client.get(f'/api/projects/{project}/hosting/find').json()['sites'] if item['platform'] == 'netlify']
    assert [item['id'] for item in found] == ['site9'], 'only sites built from this repo'
    assert {item['name']: item['secret'] for item in found[0]['variables']} == {'API_URL': False, 'STRIPE_KEY': True}

    hosts = fake_hosts(netlify_routes())
    plan, done = move(client, project, 'netlify', netlify)
    assert done['status'] == 'done', done
    created = hosts.json(lambda call: call['method'] == 'POST' and call['url'].endswith('/team/sites'))
    assert created['repo'] == {'provider': 'github', 'repo_path': 'acme/shop', 'repo_branch': 'main', 'cmd': 'npm run build', 'dir': 'dist', 'base': 'web'}
    env = hosts.json(lambda call: call['method'] == 'POST' and '/env?site_id=site1' in call['url'])
    assert {item['key']: item['values'][0]['value'] for item in env} == {'API_URL': 'https://api.example.com', 'STRIPE_KEY': 'sk_test_value'}

    # Netlify as the old site: it can be disabled (paused) after moving away.
    fake_hosts(netlify_routes([site]))
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': netlify, 'siteId': 'site9'}, headers=ORIGIN)
    status = client.get(f'/api/projects/{project}/status').json()['project']
    assert status['active'] == 'netlify'


KOYEB = 'app.koyeb.com/v1'


def test_koyeb_moves_a_site_to_a_free_web_service_and_warns_about_the_start_command(client, fake_hosts):
    hosts = fake_hosts(merged({
        f'GET {KOYEB}/account/profile': {'user': {'name': 'Octo Cat', 'email': 'octo@example.com'}},
        f'GET {KOYEB}/deployments/dep1': {'deployment': {'status': 'HEALTHY'}},
        f'POST {KOYEB}/services/svc1/pause': {},
        f'POST {KOYEB}/services': {'service': {'id': 'svc1', 'app_id': 'app1', 'latest_deployment_id': 'dep1'}},
        f'GET {KOYEB}/services': {'services': []},  # Projects that exist before the move (none).
        f'POST {KOYEB}/apps': {'app': {'id': 'app1', 'domains': [{'name': 'shop-acme.koyeb.app'}]}},
        'GET shop-acme.koyeb.app': HTML,
    }))
    project, koyeb = from_cloudflare(client, 'koyeb', 'koyeb-token-0123456789abcdef')
    plan, done = move(client, project, 'koyeb', koyeb)
    assert any('runs your app as a web service' in warning for warning in plan['plan']['warnings'])
    assert done['status'] == 'done', done
    body = hosts.json(lambda call: call['method'] == 'POST' and call['url'].endswith('/v1/services'))
    definition = body['definition']
    assert (body['app_id'], definition['type'], definition['instance_types']) == ('app1', 'WEB', [{'type': 'free'}])
    assert definition['git'] == {'repository': 'github.com/acme/shop', 'branch': 'main', 'workdir': 'web', 'build_command': 'npm run build'}
    assert {item['key']: item['value'] for item in definition['env']} == {'PORT': '8000', 'API_URL': 'https://api.example.com', 'STRIPE_KEY': 'sk_test_value'}
    assert not any(call['url'].endswith('/redeploy') for call in hosts.calls), 'follows the deployment Koyeb started on create'
    assert client.get(f'/api/projects/{project}/status').json()['project']['url'] == 'https://shop-acme.koyeb.app'
    assert client.post(f'/api/projects/{project}/hosting/pause', json={'which': 'current', 'approved': True}, headers=ORIGIN).json() == {'paused': True}
    assert any(call['method'] == 'POST' and call['url'].endswith('/services/svc1/pause') for call in hosts.calls)


def test_railway_creates_a_project_and_service_from_the_repo(client, fake_hosts):
    sent = []

    def graphql(request):
        query = json.loads(request.content)
        sent.append(query)
        text = query['query']
        answers = [
            ('me {', {'me': {'name': 'Octo Cat', 'email': 'octo@example.com'}}),
            ('projectCreate', {'projectCreate': {'id': 'p1'}}),
            ('project(id', {'project': {'environments': {'edges': [{'node': {'id': 'e1', 'name': 'production'}}]}}}),
            ('serviceCreate', {'serviceCreate': {'id': 's1'}}),
            ('serviceInstanceUpdate', {'serviceInstanceUpdate': True}),
            ('serviceDomainCreate', {'serviceDomainCreate': {'domain': 'shop-production.up.railway.app'}}),
            ('deployments(', {'deployments': {'edges': [{'node': {'id': 'd1', 'status': 'BUILDING'}}]}}),
            ('deployment(id', {'deployment': {'status': 'SUCCESS'}}),
        ]
        return httpx.Response(200, json={'data': next(data for marker, data in answers if marker in text)})
    fake_hosts(merged({'POST backboard.railway.com/graphql/v2': graphql, 'GET shop-production.up.railway.app': HTML}))
    project, railway = from_cloudflare(client, 'railway', 'railway-token-0123456789abc')
    plan, done = move(client, project, 'railway', railway)
    assert done['status'] == 'done', done
    service = next(item for item in sent if 'serviceCreate' in item['query'])['variables']['input']
    assert (service['projectId'], service['source'], service['branch']) == ('p1', {'repo': 'acme/shop'}, 'main')
    assert service['variables'] == {'API_URL': 'https://api.example.com', 'STRIPE_KEY': 'sk_test_value'}
    update = next(item for item in sent if 'serviceInstanceUpdate' in item['query'])['variables']
    assert update['input'] == {'rootDirectory': 'web', 'buildCommand': 'npm run build'} and update['environmentId'] == 'e1'
    assert not any('serviceInstanceDeployV2' in item['query'] for item in sent), 'follows the build Railway started for the new service'
    assert client.get(f'/api/projects/{project}/status').json()['project']['url'] == 'https://shop-production.up.railway.app'


def test_railway_reports_graphql_errors_sent_with_http_200(client, fake_hosts):
    fake_hosts(merged({'POST backboard.railway.com/graphql/v2': lambda request: httpx.Response(200, json={'errors': [{'message': 'Not Authorized'}]})}))
    project, cloudflare, vercel = setup_project(client)
    refused = client.post('/api/accounts', json={'kind': 'railway', 'token': 'railway-workspace-token-0123'}, headers=ORIGIN)
    assert refused.status_code == 400 and 'Railway did not accept' in refused.json()['error']


HEROKU = 'api.heroku.com'
ARCHIVE = 'https://codeload.github.com/acme/shop/legacy.tar.gz/refs/heads/main?token=short-lived'


def test_heroku_builds_from_a_short_lived_github_archive_and_pauses_with_maintenance(client, fake_hosts):
    hosts = fake_hosts(merged({
        f'GET {HEROKU}/account': {'email': 'octo@example.com'},
        'GET api.github.com/repos/acme/shop/tarball/main': httpx.Response(302, headers={'location': ARCHIVE}),
        f'GET {HEROKU}/apps/shop/builds/b1': {'status': 'succeeded'},
        f'POST {HEROKU}/apps/shop/builds': {'id': 'b1', 'status': 'pending'},
        f'PATCH {HEROKU}/apps/shop/config-vars': {},
        f'PATCH {HEROKU}/apps/shop': {},
        f'POST {HEROKU}/apps': {'id': 'h1', 'name': 'shop', 'web_url': 'https://shop-1234.herokuapp.com/'},
        'GET shop-1234.herokuapp.com': HTML,
    }))
    project, heroku = from_cloudflare(client, 'heroku', 'heroku-api-key-0123456789')
    plan, done = move(client, project, 'heroku', heroku)
    assert done['status'] == 'done', done
    build_call = next(call for call in hosts.calls if call['method'] == 'POST' and call['url'].endswith('/apps/shop/builds'))
    assert json.loads(build_call['body'])['source_blob'] == {'url': ARCHIVE, 'version': 'main'}
    assert build_call['headers']['accept'] == 'application/vnd.heroku+json; version=3'
    assert hosts.json(lambda call: call['method'] == 'PATCH' and call['url'].endswith('/config-vars')) == {'API_URL': 'https://api.example.com', 'STRIPE_KEY': 'sk_test_value'}
    assert client.post(f'/api/projects/{project}/hosting/pause', json={'which': 'current', 'approved': True}, headers=ORIGIN).json() == {'paused': True}
    assert hosts.json(lambda call: call['method'] == 'PATCH' and call['url'].endswith('/apps/shop')) == {'maintenance': True}


FLY = 'api.machines.dev/v1'
FLY_WORKFLOW = 'raw.githubusercontent.com/acme/shop/main/.github/workflows/cloudhop-fly.yml'


def test_flyio_opens_a_workflow_pull_request_waits_for_it_then_deploys(client, fake_hosts):
    sent, merged_yet = [], {'value': False}

    def graphql(request):
        query = json.loads(request.content)
        sent.append(query)
        answers = [('organizations', {'organizations': {'nodes': [{'slug': 'acme', 'name': 'Acme'}]}}),
                   ('setSecrets', {'setSecrets': {'app': {'name': 'shop'}}}),
                   ('secrets {', {'app': {'secrets': [{'name': 'STRIPE_KEY'}]}})]
        return httpx.Response(200, json={'data': next(data for marker, data in answers if marker in query['query'])})

    def workflow(request):
        return 'name: Cloudhop Fly deploy' if merged_yet['value'] else httpx.Response(404)
    hosts = fake_hosts(merged({
        'POST api.fly.io/graphql': graphql,
        f'POST {FLY}/apps': httpx.Response(201, json={'id': 'app1'}),
        f'GET {FLY}/apps/shop/machines': [{'id': 'm1', 'state': 'started', 'updated_at': '2999-01-01T00:00:00Z'}],
        f'GET {FLY_WORKFLOW}': workflow,
        'GET raw.githubusercontent.com/acme/shop/main/web/Dockerfile': httpx.Response(404),
        'GET api.github.com/repos/acme/shop/git/ref/heads/main': {'object': {'sha': 'abc'}},
        'GET api.github.com/repos/acme/shop/git/commits/abc': {'tree': {'sha': 't0'}},
        'POST api.github.com/repos/acme/shop/git/trees': {'sha': 't1'},
        'POST api.github.com/repos/acme/shop/git/commits': {'sha': 'c1c1c1c1c1'},
        'POST api.github.com/repos/acme/shop/git/refs': {},
        'POST api.github.com/repos/acme/shop/pulls': {'html_url': 'https://github.com/acme/shop/pull/7'},
        'POST api.github.com/repos/acme/shop/actions/workflows/cloudhop-fly.yml/dispatches': httpx.Response(204),
        'GET shop.fly.dev': HTML,
    }))
    project, fly = from_cloudflare(client, 'flyio', 'FlyV1 fm2_org-token-0123456789')
    plan, waiting = move(client, project, 'flyio', fly)
    assert waiting['status'] == 'waiting', waiting
    step = waiting['state']['steps']['deploy']
    assert (step['action'], step['link']) == ('pipeline', 'https://github.com/acme/shop/pull/7')
    assert 'FLY_API_TOKEN' in step['detail'] and any('Cloudhop never sees that token' in warning for warning in waiting['plan']['warnings'])
    tree = hosts.json(lambda call: call['url'].endswith('/git/trees'))['tree']
    files = {item['path']: item['content'] for item in tree}
    assert set(files) == {'web/Dockerfile', 'web/fly.toml', '.github/workflows/cloudhop-fly.yml'}
    assert "app = 'shop'" in files['web/fly.toml'] and 'internal_port = 80' in files['web/fly.toml']
    assert 'working-directory: web' in files['.github/workflows/cloudhop-fly.yml'] and '@ed8efb33836e8b2096c7fd3ba1c8afe303ebbff1' in files['.github/workflows/cloudhop-fly.yml']
    assert 'RUN npm run build' in files['web/Dockerfile'] and 'COPY --from=build /app/dist' in files['web/Dockerfile']
    assert next(call for call in hosts.calls if call['url'].endswith('/v1/apps'))['auth'] == 'FlyV1 fm2_org-token-0123456789'
    secrets = next(item for item in sent if 'setSecrets' in item['query'])['variables']['input']
    assert secrets['appId'] == 'shop' and {item['key'] for item in secrets['secrets']} == {'API_URL', 'STRIPE_KEY'}
    assert not any('dispatches' in call['url'] for call in hosts.calls)

    merged_yet['value'] = True
    assert client.post(f"/api/projects/{project}/moves/{plan['id']}/continue", headers=ORIGIN).status_code == 202
    done = client.get(f'/api/projects/{project}/moves/latest').json()['move']
    assert done['status'] == 'done', done
    assert sum(call['url'].endswith('/git/trees') for call in hosts.calls) == 1, 'the pull request is opened once'
    assert any(call['url'].endswith('/cloudhop-fly.yml/dispatches') for call in hosts.calls)
    assert client.get(f'/api/projects/{project}/status').json()['project']['url'] == 'https://shop.fly.dev'


def test_flyio_uses_the_repos_own_dockerfile_and_its_port(client):
    files = {item['path']: item['content'] for item in hosting.SITES['flyio'].repo_files({'id': 'api'}, {}, 'main', 'FROM python:3.12\nEXPOSE 9000\n')}
    assert set(files) == {'fly.toml', '.github/workflows/cloudhop-fly.yml'}
    assert 'internal_port = 9000' in files['fly.toml'] and 'working-directory' not in files['.github/workflows/cloudhop-fly.yml']
