"""Render, Vercel and Cloudflare accounts: checked against the platform, stored encrypted, access shown."""
import httpx
import pytest
from fastapi.testclient import TestClient

from cloudhop import db
from cloudhop.main import app

ORIGIN = {'Origin': 'http://localhost:5173'}
TOKEN = 'platform-token-0123456789abcdef'


@pytest.fixture(autouse=True)
def fresh_db():
    db.connect(':memory:')


@pytest.fixture
def client():
    client = TestClient(app, base_url='http://localhost:5173')
    assert client.post('/api/auth/login', json={'token': 'test-admin-token'}, headers=ORIGIN).status_code == 200
    return client


ROUTES = {
    'GET api.render.com/v1/owners': [{'owner': {'id': 'tea-1', 'name': 'Acme team', 'type': 'team'}, 'cursor': 'x'}, {'owner': {'id': 'own-1', 'name': 'Me', 'type': 'user'}, 'cursor': 'y'}],
    'GET api.vercel.com/v2/user': {'user': {'username': 'octo'}},
    'GET api.vercel.com/v5/user/tokens/current': {'token': {'expiresAt': 1798761600000, 'scopes': [{'type': 'team', 'teamId': 'team_1'}]}},
    'GET api.vercel.com/v2/teams': {'teams': [{'id': 'team_1', 'name': 'Acme'}, {'id': 'team_2', 'name': 'Other'}]},
    'GET api.cloudflare.com/client/v4/accounts': {'result': [{'id': 'a1', 'name': 'Acme account'}]},
    'GET api.cloudflare.com/client/v4/user/tokens/verify': httpx.Response(401, json={'success': False}),  # Account-owned token.
}


@pytest.mark.parametrize('kind,label,scopes', [
    ('render', 'Acme team', ['Acme team', 'Me']),
    ('vercel', 'octo', ['Acme']),
    ('cloudflare', 'Acme account', ['Acme account']),
])
def test_platform_accounts_are_checked_stored_encrypted_and_show_access(client, fake_hosts, kind, label, scopes):
    hosts = fake_hosts(ROUTES)
    created = client.post('/api/accounts', json={'kind': kind, 'token': TOKEN}, headers=ORIGIN)
    assert created.status_code == 201, created.text
    assert created.json()['label'] == label
    assert TOKEN not in str(db.all('SELECT * FROM accounts')), 'token is encrypted at rest'
    assert all(call['auth'] == f'Bearer {TOKEN}' for call in hosts.calls)
    access = client.get(f"/api/accounts/{created.json()['id']}/access").json()
    assert access['kind'] == 'platform' and [scope['name'] for scope in access['scopes']] == scopes
    if kind == 'vercel':
        assert access['expires'].startswith('2027-01-01')
    assert client.get(f"/api/accounts/{created.json()['id']}/repos").status_code == 400


def test_a_rejected_platform_token_gives_a_clear_message(client, fake_hosts):
    fake_hosts({'GET api.render.com/v1/owners': httpx.Response(401, json={'message': 'unauthorized'})})
    refused = client.post('/api/accounts', json={'kind': 'render', 'token': TOKEN}, headers=ORIGIN)
    assert refused.status_code == 400 and 'Render did not accept that token' in refused.json()['error']
    assert client.post('/api/accounts', json={'kind': 'vercel', 'token': 'short'}, headers=ORIGIN).status_code == 400
    assert db.all('SELECT * FROM accounts') == []
