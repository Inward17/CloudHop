"""Cloudhop as an MCP server: tokens, the protocol, per-user data, and that nothing reached over MCP can run a change."""
import json

import pytest
from fastapi.testclient import TestClient

from cloudhop import auth, db
from cloudhop.main import app
from test_hosting import routes, setup_project

ORIGIN = {'Origin': 'http://localhost:5173'}


@pytest.fixture(autouse=True)
def fresh_db():
    db.connect(':memory:')


@pytest.fixture
def client():
    return TestClient(app, base_url='http://localhost:5173')


def sign_in(client):
    assert client.post('/api/auth/login', json={'token': 'test-admin-token'}, headers=ORIGIN).status_code == 200


def new_token(client, name='Claude Desktop'):
    created = client.post('/api/profile/mcp-tokens', json={'name': name}, headers=ORIGIN)
    assert created.status_code == 201, created.text
    return created.json()


def rpc(client, token, method, params=None, message_id=1, headers=None):
    body = {'jsonrpc': '2.0', 'method': method, **({'id': message_id} if message_id is not None else {}), **({'params': params} if params is not None else {})}
    # An MCP client has no cookies; clear them so only the bearer token counts.
    client.cookies.clear()
    return client.post('/api/mcp', content=json.dumps(body), headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                                                                         'Accept': 'application/json, text/event-stream', **(headers or {})})


def call(client, token, tool, **arguments):
    answer = rpc(client, token, 'tools/call', {'name': tool, 'arguments': arguments}).json()
    return answer['result']


def test_tokens_are_shown_once_stored_hashed_listed_and_revoked(client):
    sign_in(client)
    created = new_token(client)
    assert created['token'].startswith('chmcp_') and created['url'] == 'http://localhost:5173/api/mcp'
    assert created['token'] not in json.dumps(db.all('SELECT * FROM mcp_tokens')), 'only the hash is stored'
    listed = client.get('/api/profile/mcp-tokens').json()
    assert [token['name'] for token in listed['tokens']] == ['Claude Desktop'] and 'token' not in listed['tokens'][0]
    assert client.post('/api/profile/mcp-tokens', json={'name': 'x'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/profile/mcp-tokens', json={'name': ' '}, headers=ORIGIN).status_code == 400

    assert rpc(client, created['token'], 'ping').status_code == 200
    client.cookies.clear()
    sign_in(client)
    assert client.delete(f"/api/profile/mcp-tokens/{created['id']}", headers=ORIGIN).status_code == 200
    assert rpc(client, created['token'], 'ping').status_code == 401, 'a revoked token stops working at once'


def test_endpoint_needs_a_token_ignores_cookies_and_refuses_other_origins(client):
    sign_in(client)
    token = new_token(client)['token']
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
    # Signed in with a cookie but no token: a web page cannot use the visitor's session here.
    with_cookie = client.post('/api/mcp', content=body, headers={'Content-Type': 'application/json'})
    assert with_cookie.status_code == 401 and with_cookie.headers['www-authenticate'].startswith('Bearer')
    assert rpc(client, 'chmcp_' + 'x' * 43, 'ping').status_code == 401
    assert rpc(client, token, 'ping', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert rpc(client, token, 'ping', headers=ORIGIN).status_code == 200
    assert client.get('/api/mcp').status_code == 405
    client.cookies.clear()
    assert client.post('/api/mcp', content='{not json', headers={'Authorization': f'Bearer {token}'}).json()['error']['code'] == -32700


def test_protocol_handshake_tools_and_errors(client):
    sign_in(client)
    token = new_token(client)['token']
    hello = rpc(client, token, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}).json()['result']
    assert hello['protocolVersion'] == '2025-06-18' and hello['serverInfo']['name'] == 'cloudhop' and 'approve' in hello['instructions']
    assert rpc(client, token, 'initialize', {'protocolVersion': '1999-01-01'}).json()['result']['protocolVersion'] == '2025-06-18'
    assert rpc(client, token, 'notifications/initialized', message_id=None).status_code == 202

    tools = {tool['name']: tool for tool in rpc(client, token, 'tools/list').json()['result']['tools']}
    assert set(tools) == {'list_projects', 'get_project', 'get_activity', 'list_accounts', 'propose_action', 'plan_move'}
    assert all(tool['annotations']['destructiveHint'] is False for tool in tools.values())
    assert tools['plan_move']['annotations']['readOnlyHint'] is False and tools['get_project']['annotations']['readOnlyHint'] is True
    assert not any(word in name for name in tools for word in ('run', 'approve', 'deploy', 'destroy')), 'no tool starts or approves anything'

    assert rpc(client, token, 'tools/call', {'name': 'run_action', 'arguments': {}}).json()['error']['code'] == -32602
    assert rpc(client, token, 'tools/call', {'name': 'get_project', 'arguments': {'project_id': 1}}).json()['error']['code'] == -32602
    assert rpc(client, token, 'tools/call', {'name': 'get_project', 'arguments': {}}).json()['error']['code'] == -32602
    assert rpc(client, token, 'resources/list').json()['error']['code'] == -32601


def test_tools_see_only_the_token_owners_data_and_proposals_never_run(client, fake_hosts):
    hosts = fake_hosts(routes())
    sign_in(client)
    project, cloudflare, vercel = setup_project(client)
    token = new_token(client)['token']
    # Someone else's project, which the token must not reach.
    db.run("INSERT INTO users (id, github_id, login, name, created_at, last_login_at) VALUES ('other', 99, 'mallory', 'M', 1, 1)")
    db.run("INSERT INTO projects (id, user_id, provider, repo, account_id, report_hash, created_at) VALUES ('theirs', 'other', 'github', 'evil/app', 'x', 'h', 1)")

    listed = call(client, token, 'list_projects')['structuredContent']['projects']
    assert [item['id'] for item in listed] == [project] and listed[0]['link'].endswith(f'#/p/{project}')
    assert call(client, token, 'get_project', project_id='theirs') == {'content': [{'type': 'text', 'text': 'Project not found'}], 'isError': True}
    assert call(client, token, 'get_activity', project_id='theirs')['isError'] is True
    accounts = call(client, token, 'list_accounts')['structuredContent']['accounts']
    assert {item['kind'] for item in accounts} == {'github_token', 'cloudflare', 'vercel'} and 'secret' not in json.dumps(accounts)

    detail = call(client, token, 'get_project', project_id=project)['structuredContent']
    assert detail['repo'] == 'acme/shop' and detail['kind'] == 'platform' and detail['latestMove'] is None

    # A cloud-style request on this project: the answer is a proposal and a link, never a started run.
    calls_before = len(hosts.calls)
    proposed = call(client, token, 'propose_action', project_id=project, request='deploy to aws')['structuredContent']
    assert proposed['proposed'] is False and 'not connected' in proposed['reply'], proposed
    assert db.all('SELECT * FROM runs') == [] and len(hosts.calls) == calls_before, 'proposing changes nothing'

    # Moves: planned only, approval stays in the dashboard.
    client.cookies.clear()
    sign_in(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    planned = call(client, token, 'plan_move', project_id=project, to='vercel', account_id=vercel)['structuredContent']
    assert planned['status'] == 'planned' and planned['copy'] == ['API_URL'] and planned['approve'].endswith(f'#/p/{project}/hosting')
    assert db.one('SELECT status FROM moves WHERE id = ?', planned['moveId'])['status'] == 'planned'
    assert not any(call_['method'] == 'POST' and 'vercel.com' in call_['url'] for call_ in hosts.calls), 'planning creates nothing on the platform'
    activity = call(client, token, 'get_activity', project_id=project, limit='5')['structuredContent']['entries']
    assert all(entry['projectId'] == project for entry in activity)


def test_cloud_project_proposals_link_to_the_dashboard_and_deleting_the_profile_removes_tokens(client):
    sign_in(client)
    token = new_token(client)['token']
    db.run("""INSERT INTO projects (id, user_id, provider, repo, account_id, report_hash, kind, clouds, active, created_at)
              VALUES ('p1', ?, 'github', 'acme/shop', 'a', 'h', 'cloud', ?, 'aws', 1)""", auth.LOCAL_ADMIN, json.dumps({'aws': {}, 'gcp': {}}))
    proposed = call(client, token, 'propose_action', project_id='p1', request='switch to gcp')['structuredContent']
    assert proposed['proposed'] is True and proposed['action'] == 'switch' and proposed['cloud'] == 'gcp'
    assert proposed['approve'] == 'http://localhost:5173/#/p/p1?ask=switch%20to%20gcp' and proposed['warnings']
    assert db.all('SELECT * FROM runs') == []
    assert call(client, token, 'plan_move', project_id='p1', to='vercel')['isError'] is True

    client.cookies.clear()
    sign_in(client)
    assert client.request('DELETE', '/api/profile', json={'confirm': 'admin'}, headers=ORIGIN).status_code == 200
    assert db.all('SELECT * FROM mcp_tokens') == []
    assert rpc(client, token, 'ping').status_code == 401


def test_approval_link_keeps_an_action_that_comes_after_a_long_request(client):
    sign_in(client)
    token = new_token(client)['token']
    db.run("""INSERT INTO projects (id, user_id, provider, repo, account_id, report_hash, kind, clouds, active, created_at)
              VALUES ('p1', ?, 'github', 'acme/shop', 'a', 'h', 'cloud', ?, 'aws', 1)""", auth.LOCAL_ADMIN, json.dumps({'aws': {}, 'gcp': {}}))
    request = 'We reviewed the bill and the latency numbers from last month, ' * 5 + 'so please switch to gcp'
    assert len(request) > 260
    proposed = call(client, token, 'propose_action', project_id='p1', request=request)['structuredContent']
    assert proposed['action'] == 'switch' and proposed['approve'].endswith('?ask=switch%20to%20gcp'), 'the link carries the action, not a cut request'
    from cloudhop.intent import proposal_for
    from cloudhop.store import public_project
    again = proposal_for('switch to gcp', public_project(db.one("SELECT * FROM projects WHERE id = 'p1'")))
    assert (again['action'], again['cloud']) == ('switch', 'gcp'), 'the dashboard rebuilds the same proposal from the link'


def test_no_new_plan_hides_a_move_that_still_needs_the_user(client, fake_hosts):
    fake_hosts(routes())
    sign_in(client)
    project, cloudflare, vercel = setup_project(client)
    token = new_token(client)['token']
    client.cookies.clear()
    sign_in(client)
    client.put(f'/api/projects/{project}/hosting/current', json={'accountId': cloudflare, 'siteId': 'shop'}, headers=ORIGIN)
    for status in ('waiting', 'failed', 'running'):
        db.run('DELETE FROM moves')
        db.run('INSERT INTO moves VALUES (?, ?, ?, ?, ?, ?)', 'm1', project, 1, status, json.dumps({'to': 'netlify'}), '{}')
        refused = call(client, token, 'plan_move', project_id=project, to='vercel', account_id=vercel)
        assert refused['isError'] is True and 'Netlify is not finished' in refused['content'][0]['text'], status
        sign_in(client)  # The MCP call above sent no cookies; the dashboard route needs the session.
        assert client.post(f'/api/projects/{project}/moves', json={'to': 'vercel', 'accountId': vercel}, headers=ORIGIN).status_code == 409
        assert [row['id'] for row in db.all('SELECT id FROM moves')] == ['m1'], 'the unfinished move stays the newest'
    db.run("UPDATE moves SET status = 'done'")
    assert call(client, token, 'plan_move', project_id=project, to='vercel', account_id=vercel)['structuredContent']['status'] == 'planned'
