"""Guards added after the security review: outbound address checks, repo scope for GitHub App accounts,
one-at-a-time runs and moves, admin sign-in limits, body limits and the agent's secret-file filter."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from cloudhop import agent, auth, db, net
from cloudhop.errors import RepoError
from cloudhop.main import app, check_repo_allowed, interrupted_moves

ORIGIN = {'Origin': 'http://localhost:5173'}


@pytest.fixture(autouse=True)
def fresh_db():
    db.connect(':memory:')
    auth._admin_failures.clear()


@pytest.fixture
def client():
    return TestClient(app, base_url='http://localhost:5173')


def test_site_checks_refuse_private_addresses_and_private_redirects():
    def handler(request):
        return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest/meta-data/'})
    net.use(httpx.MockTransport(handler))
    net.check_addresses = True
    try:
        for url in ['http://127.0.0.1:6379/', 'http://10.0.0.5/', 'http://169.254.169.254/', 'http://[::1]/', 'file:///etc/passwd', 'http://localhost/']:
            with pytest.raises(net.UnsafeAddress):
                asyncio.run(net.public_get(url))
        # A public address that redirects to the metadata service is stopped at the redirect.
        with pytest.raises(net.UnsafeAddress):
            asyncio.run(net.public_get('http://93.184.215.14/'))
    finally:
        net.check_addresses = False


def test_github_app_accounts_reach_only_repos_the_user_confirmed():
    account = {'kind': 'github_app', 'data': json.dumps({'installationId': '1', 'repos': ['acme/shop']})}
    check_repo_allowed(account, 'Acme/Shop')
    with pytest.raises(RepoError) as refused:
        check_repo_allowed(account, 'acme/payroll')
    assert refused.value.status == 404
    with pytest.raises(RepoError) as old:
        check_repo_allowed({'kind': 'github_app', 'data': json.dumps({'installationId': '1'})}, 'acme/shop')
    assert old.value.status == 403 and 'Reconnect' in old.value.message
    check_repo_allowed({'kind': 'github_token', 'data': '{}'}, 'anything/at-all')


def test_a_planned_move_starts_once_and_a_restart_leaves_running_moves_failed():
    db.run("INSERT INTO moves (id, project_id, created_at, status, plan, state) VALUES ('m', 'p', 1, 'planned', '{}', '{}')")
    assert db.run("UPDATE moves SET status = 'running' WHERE id = 'm' AND status = 'planned'") == 1
    assert db.run("UPDATE moves SET status = 'running' WHERE id = 'm' AND status = 'planned'") == 0
    db.run("UPDATE moves SET state = ? WHERE id = 'm'", json.dumps({'steps': {'create': {'status': 'done'}, 'deploy': {'status': 'running'}}}))
    interrupted_moves()
    move = db.one("SELECT status, state FROM moves WHERE id = 'm'")
    steps = json.loads(move['state'])['steps']
    assert move['status'] == 'failed' and steps['deploy']['status'] == 'failed' and steps['create']['status'] == 'done'


def test_admin_sign_in_is_off_on_https_and_pauses_after_many_wrong_tokens(client):
    assert auth.admin_enabled({'ADMIN_TOKEN': 'x', 'PUBLIC_URL': 'https://cloudhop.example'}) is False
    assert auth.admin_enabled({'ADMIN_TOKEN': 'x', 'PUBLIC_URL': 'http://localhost:5173'}) is True
    for _ in range(auth.ADMIN_FAILURE_LIMIT):
        assert client.post('/api/auth/login', json={'token': 'wrong'}, headers=ORIGIN).status_code == 401
    assert client.post('/api/auth/login', json={'token': 'test-admin-token'}, headers=ORIGIN).status_code == 429


def test_large_bodies_are_refused_before_they_are_read(client):
    big = client.post('/api/report', content=b'{"x":"' + b'a' * 70_000 + b'"}', headers={'Content-Type': 'application/json', 'Authorization': 'Bearer x'})
    assert big.status_code == 413
    assert client.post('/api/auth/login', content=b'a' * 1_000_001, headers={**ORIGIN, 'Content-Type': 'application/json'}).status_code == 413


def test_agent_never_reads_likely_secret_files():
    for path in ['.env', 'config/.env.production', '.git-credentials', 'deploy/key.jks', 'infra/prod.tfvars.json', 'terraform.tfstate.backup',
                 '.docker/config.json', 'gcp/service-account-prod.json', '.pgpass', 'certs/server.pem']:
        assert agent.SECRET_NAME.search(path), path
    for path in ['.env.example', 'src/app.py', 'package.json', 'docs/keys.md']:
        assert not agent.SECRET_NAME.search(path), path


def test_old_github_app_account_asks_for_reconnect_and_reconnecting_saves_its_repos(client, monkeypatch, fake_hosts):
    from cloudhop import main
    monkeypatch.setenv('GITHUB_CLIENT_ID', 'cid')
    monkeypatch.setenv('GITHUB_CLIENT_SECRET', 'secret')
    monkeypatch.setenv('GITHUB_APP_ID', '1')
    monkeypatch.setenv('GITHUB_APP_SLUG', 'cloudhop')
    monkeypatch.setenv('GITHUB_APP_PRIVATE_KEY', 'unused')

    async def owner(_env, _installation):
        return 'acme'
    monkeypatch.setattr(main, 'installation_owner', owner)
    assert client.post('/api/auth/login', json={'token': 'test-admin-token'}, headers=ORIGIN).status_code == 200
    db.run("INSERT INTO accounts (id, user_id, kind, label, data, created_at) VALUES ('a1', ?, 'github_app', 'acme', ?, 1)",
           auth.LOCAL_ADMIN, json.dumps({'installationId': '42', 'owner': 'acme'}))

    listed = client.get('/api/accounts').json()['accounts']
    assert listed[0]['needsReconnect'] is True
    repos = client.get('/api/accounts/a1/repos')
    assert repos.status_code == 403 and 'Reconnect' in repos.json()['error']

    setup = client.get('/api/github/setup?installation_id=42', follow_redirects=False)
    assert setup.status_code == 302 and setup.headers['location'].startswith('https://github.com/login/oauth/authorize')
    fake_hosts({
        'POST github.com/login/oauth/access_token': {'access_token': 'user-token'},
        'GET api.github.com/user/installations/42/repositories': {'repositories': [{'full_name': 'Acme/Shop'}]},
        'GET api.github.com/user/installations': {'installations': [{'id': 42}]},
    })
    done = client.get(f"/api/auth/callback?code=c&state={client.cookies['cloudhop_state']}", follow_redirects=False)
    assert done.status_code == 302 and done.headers['location'].endswith('#/accounts?install=updated')
    account = client.get('/api/accounts').json()['accounts'][0]
    assert account['needsReconnect'] is False and account['data']['repos'] == ['acme/shop']
    assert len(client.get('/api/accounts').json()['accounts']) == 1
