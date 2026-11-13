"""Cloudhop API: FastAPI on one server, with SQLite and a 5-minute background job for uptime checks.

Run locally: uvicorn cloudhop.main:app --port 8787 (from backend/). Settings come from the
environment, or from backend/.env for local use. See README.
"""
import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import db, net
from .analyze import analyze_files
from .auth import (INSTALL_COOKIE, SESSION_COOKIE, STATE_COOKIE, admin_login, allowed_login, check_origin, check_state, clear_cookie, cookie,
                   create_session, current_session, end_session, exchange_code, github_as_user, github_authorize, login_methods, read_cookie,
                   redirect, upsert_github_user, with_cookies)
from .errors import RepoError
from .generate import (bootstrap_for, cloud_access, cloud_config, deploy_issues, install_files, projects_using_cloud, trust_for,
                       validate_cloud_input)
from .github import fetch_manifests, installation_owner, parse_repo
from . import hosting
from .intent import proposal_for
from .plans import rank_plans
from .platforms import PLATFORMS
from .portability import portability_report
from .providers import ADAPTERS, PROVIDERS, credentials_for, parse_repo_for, provider_of, read_manifests
from .store import decrypt, decrypt_with_env, encrypt, next_state, public_account, public_project, public_user, random_token, sha256, summarize_checks, uptime_summary
from .timeutil import js_round, now_ms, parse_date_ms

log = logging.getLogger('cloudhop')
BACKEND_DIR = Path(__file__).resolve().parent.parent
DAY = 24 * 3600 * 1000
ACTIONS = {'verify', 'plan', 'deploy', 'switch', 'rollback', 'destroy', 'report'}
CLOUDS = {'aws', 'gcp'}
RANGES = {'24h': (DAY, 48), '7d': (7 * DAY, 56)}


def load_env_file(path=BACKEND_DIR / '.env'):
    """Local settings file (gitignored). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and key and not key.startswith('#'):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()
env = os.environ


def json_ok(body, status=200, cookies=()):
    return with_cookies(JSONResponse(body, status_code=status), cookies)


async def read_json(request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise RepoError(400, 'Request body must be JSON')
    return body


def get_row(table, row_id, user_id):
    """Another user's row looks exactly like a missing one, so ids reveal nothing."""
    row = db.one(f'SELECT * FROM {table} WHERE id = ? AND user_id = ?', row_id, user_id)
    if not row:
        raise RepoError(404, 'Project not found' if table == 'projects' else 'Account not found')
    return row


async def project_access(row):
    """Everything needed to talk to a project's code host: the adapter, credentials and repo details."""
    account = get_row('accounts', row['account_id'], row['user_id'])
    adapter = ADAPTERS[row['provider']]
    cred = await credentials_for(env, account)
    return adapter, cred, await adapter.repo_info(cred, row['repo'])


def parse_credits(raw):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 20:
        raise RepoError(400, 'Credits must be a list of at most 20 items')
    out = []
    for credit in raw:
        amount = credit.get('amount') if isinstance(credit, dict) else None
        valid = isinstance(credit, dict) and credit.get('cloud') in CLOUDS and isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0 \
            and parse_date_ms(credit.get('expiresOn')) is not None
        if not valid:
            raise RepoError(400, 'Each credit needs a cloud, a positive amount and an expiry date')
        out.append({'cloud': credit['cloud'], 'amount': amount, 'expiresOn': credit['expiresOn']})
    return out


def _token(body):
    token = body.get('token')
    return token if isinstance(token, str) and len(token) >= 20 else None


def _bitbucket_secret(body):
    token, email = _token(body), body.get('email')
    if token and isinstance(email, str) and re.fullmatch(r'[^\s@]+@[^\s@]+', email.strip()):
        return json.dumps({'email': email.strip(), 'token': token})
    return None


ACCOUNT_SECRETS = {'github_token': _token, 'gitlab_token': _token, 'bitbucket_token': _bitbucket_secret, 'azure_token': _token}
# Hosting platforms: saved and checked now; deploying to them is not built yet.
PLATFORM_KINDS = set(PLATFORMS)

SECRET_PLACES = {
    'github': 'repo Settings > Secrets and variables > Actions',
    'gitlab': 'project Settings > CI/CD > Variables, marked masked',
    'bitbucket': 'repository settings > Repository variables, marked secured. Pipelines must be enabled there too',
    'azure': 'the cloudhop pipeline > Edit > Variables, marked secret',
}


def install_description(provider, notes):
    lines = [
        'Adds the pipeline, infrastructure and Compose files Cloudhop uses. Nothing runs until you merge this and start an action from Cloudhop.',
        '',
        f'Set CLOUDHOP_REPORT_TOKEN and CLOUDHOP_ENV in {SECRET_PLACES[provider]}.',
    ]
    if provider == 'github':
        lines += ['', 'Cloud access is limited to the `cloudhop` GitHub environment. Add required reviewers to it for a second approval on every run.']
    if provider == 'gitlab':
        lines += ['', 'Cloud access is limited to pipelines on the default branch. Protect that branch.']
    if provider == 'azure':
        lines += ['', 'Create a service connection named `cloudhop` that uses workload identity federation, then add its id as pipeline variable CLOUDHOP_SERVICE_CONNECTION_ID.']
    if notes:
        lines += ['', *(f'**Action needed:** {note}' for note in notes)]
    lines += ['', 'Check generated Dockerfiles carefully: start commands are best guesses.']
    return '\n'.join(lines)


# Background job: uptime checks every 5 minutes, a daily cost report, and cleanup.

async def check_site(project):
    started = now_ms()
    ok, code = 0, None
    try:
        response = await net.client.get(project['url'], follow_redirects=True, timeout=10)
        code = response.status_code
        ok = 1 if code < 500 else 0
    except Exception:  # Unreachable counts as down.
        pass
    db.run('INSERT INTO checks (project_id, at, ok, status, ms) VALUES (?, ?, ?, ?, ?)', project['id'], now_ms(), ok, code, now_ms() - started if ok else None)


async def scheduled(when_ms=None):
    when = datetime.fromtimestamp((when_ms or now_ms()) / 1000, tz=timezone.utc)

    async def one(row):
        await check_site(row)
        # Once a day, ask the user's pipeline for cost and server data.
        if row['active'] and when.hour == 6 and when.minute < 5:
            adapter, cred, info = await project_access(row)
            await adapter.trigger(cred, row['repo'], info, {'action': 'report', 'cloud': row['active'], 'source': ''})

    results = await asyncio.gather(*(one(row) for row in db.all('SELECT * FROM projects WHERE url IS NOT NULL')), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            log.warning('scheduled job: %s', result)
    db.run('DELETE FROM checks WHERE at < ?', now_ms() - 7 * DAY)
    db.run('DELETE FROM sessions WHERE expires_at < ?', now_ms())


async def scheduler():
    while True:
        await asyncio.sleep(300 - (now_ms() / 1000) % 300)  # On the next 5-minute mark, like a cron.
        try:
            await scheduled()
        except Exception:
            log.exception('scheduled job failed')


@asynccontextmanager
async def lifespan(_app):
    db.conn()
    task = None if env.get('CLOUDHOP_SCHEDULER') == 'off' else asyncio.create_task(scheduler())
    yield
    if task:
        task.cancel()


app = FastAPI(title='Cloudhop API', lifespan=lifespan, docs_url='/api/docs', openapi_url='/api/openapi.json', redoc_url=None)


@app.exception_handler(RepoError)
async def repo_error(_request, error):
    return JSONResponse({'error': error.message}, status_code=error.status)


@app.exception_handler(HTTPException)
async def http_error(_request, error):
    return JSONResponse({'error': 'Not found' if error.status_code == 404 else str(error.detail)}, status_code=error.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, _error):
    return JSONResponse({'error': 'Bad request'}, status_code=400)


@app.exception_handler(Exception)
async def unexpected(_request, error):
    log.exception('unexpected error', exc_info=error)
    return JSONResponse({'error': 'Something went wrong'}, status_code=500)


@app.middleware('http')
async def settings_check(request, call_next):
    if request.url.path.startswith('/api/') and not (env.get('ENCRYPTION_KEY') and env.get('PUBLIC_URL')):
        return JSONResponse({'error': 'Server is missing ENCRYPTION_KEY or PUBLIC_URL. See README.'}, status_code=500)
    return await call_next(request)


def signed_in(request: Request):
    """Everything behind this needs a session, only ever touches that user's rows, and refuses cross-site writes."""
    session = current_session(request)
    if not session:
        raise RepoError(401, 'Sign in required')
    check_origin(request, env)
    return session


Session = Depends(signed_in)


# Public routes.

@app.post('/api/report')
async def receive_report(request: Request):
    """Reached by the pipeline in the user's repo, so it uses that repo's report token, not a session."""
    token = request.headers.get('authorization', '').removeprefix('Bearer ')
    row = db.one('SELECT * FROM projects WHERE report_hash = ?', sha256(token)) if token else None
    if not row:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = await read_json(request)
    if not isinstance(body.get('event'), str) or body.get('cloud') not in CLOUDS:
        raise RepoError(400, 'Bad report')
    raw = json.dumps(body, separators=(',', ':'))
    if len(raw) > 20_000:
        raise RepoError(413, 'Report too large')
    url = body.get('url') if isinstance(body.get('url'), str) and re.fullmatch(r'https?://[\w.-]+(:\d+)?/?', body['url'], re.ASCII) else None
    following = next_state(public_project(row), {**body, 'url': url})
    db.batch([
        ('INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, ?, ?)', (row['id'], now_ms(), body['event'], raw)),
        ('UPDATE projects SET active = ?, previous = ?, url = ? WHERE id = ?', (following['active'], following['previous'], following['url'], row['id'])),
    ])
    return {'ok': True}


@app.get('/api/auth/methods')
def auth_methods():
    return login_methods(env)


@app.get('/api/auth/github')
def auth_github():
    return github_authorize(env)


@app.get('/api/auth/callback')
async def auth_callback(request: Request):
    """Handles both sign-up/sign-in and confirming a GitHub App install."""
    session = current_session(request)
    check_state(request)
    token = await exchange_code(env, request.query_params.get('code'))
    pending_install = read_cookie(request, INSTALL_COOKIE)
    cleared = [clear_cookie(env, STATE_COOKIE), clear_cookie(env, INSTALL_COOKIE)]
    public_url = env['PUBLIC_URL']

    if pending_install:
        # The installation id in GitHub's redirect can be forged, so only accept installations this GitHub user can see.
        if not session:
            return redirect(f'{public_url}/#/', cleared)
        installations = (await github_as_user(token, '/user/installations?per_page=100'))['installations']
        if not any(str(item['id']) == pending_install for item in installations):
            return redirect(f'{public_url}/#/accounts?install=denied', cleared)
        owner = await installation_owner(env, pending_install)
        existing = db.one("SELECT id FROM accounts WHERE user_id = ? AND kind = 'github_app' AND json_extract(data, '$.installationId') = ?", session['user']['id'], pending_install)
        if not existing:
            db.run('INSERT INTO accounts (id, user_id, kind, label, data, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                   str(uuid.uuid4()), session['user']['id'], 'github_app', owner, json.dumps({'installationId': pending_install, 'owner': owner}), now_ms())
        return redirect(f'{public_url}/#/accounts', cleared)

    profile = await github_as_user(token, '/user')
    if not allowed_login(env, profile['login']):
        return redirect(f'{public_url}/#/denied', cleared)
    user_id, is_new = upsert_github_user(profile)
    return redirect(f"{public_url}/#/{'?welcome' if is_new else ''}", [*cleared, create_session(env, user_id, request)])


@app.post('/api/auth/login')
async def auth_login(request: Request):
    check_origin(request, env)
    return await admin_login(request, env)


# Signed-in routes.

@app.get('/api/auth/me')
def auth_me(session=Session):
    return {'user': public_user(session['user']), 'methods': login_methods(env)}


@app.post('/api/auth/logout')
def auth_logout(request: Request, _session=Session):
    return json_ok({'ok': True}, cookies=[end_session(env, request)])


@app.get('/api/profile')
def profile(session=Session):
    user_id = session['user']['id']
    rows = db.all('SELECT id_hash, agent, created_at, expires_at FROM sessions WHERE user_id = ? AND expires_at > ? ORDER BY created_at DESC', user_id, now_ms())
    counts = db.one('SELECT (SELECT COUNT(*) FROM projects WHERE user_id = ?1) AS projects, (SELECT COUNT(*) FROM accounts WHERE user_id = ?1) AS accounts', user_id)
    return {
        'user': public_user(session['user']),
        'counts': counts,
        # A short prefix of the hash identifies a session without exposing anything usable.
        'sessions': [{'id': row['id_hash'][:12], 'agent': row['agent'], 'createdAt': row['created_at'], 'expiresAt': row['expires_at'], 'current': row['id_hash'] == session['sessionHash']} for row in rows],
    }


@app.patch('/api/profile')
async def update_profile(request: Request, session=Session):
    body = await read_json(request)
    name = body['name'].strip()[:80] if isinstance(body.get('name'), str) else ''
    email = body['email'].strip()[:254] if isinstance(body.get('email'), str) else ''
    if not name:
        raise RepoError(400, 'Enter a name')
    if email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise RepoError(400, 'Enter a valid email or leave it empty')
    db.run('UPDATE users SET name = ?, email = ? WHERE id = ?', name, email or None, session['user']['id'])
    return {'ok': True}


@app.delete('/api/profile')
async def delete_profile(request: Request, session=Session):
    """Removes everything Cloudhop stores for this user. Cloud resources and repo files are untouched."""
    body = await read_json(request)
    if body.get('confirm') != session['user']['login']:
        raise RepoError(400, 'Type your username to confirm')
    user_id = session['user']['id']
    mine = 'SELECT id FROM projects WHERE user_id = ?'
    db.batch([
        (f'DELETE FROM project_secrets WHERE project_id IN ({mine})', (user_id,)),
        (f'DELETE FROM moves WHERE project_id IN ({mine})', (user_id,)),
        (f'DELETE FROM checks WHERE project_id IN ({mine})', (user_id,)),
        (f'DELETE FROM reports WHERE project_id IN ({mine})', (user_id,)),
        ('DELETE FROM projects WHERE user_id = ?', (user_id,)),
        ('DELETE FROM accounts WHERE user_id = ?', (user_id,)),
        ('DELETE FROM sessions WHERE user_id = ?', (user_id,)),
        ('DELETE FROM users WHERE id = ?', (user_id,)),
    ])
    return json_ok({'ok': True}, cookies=[cookie(env, SESSION_COOKIE, '', 0)])


@app.delete('/api/profile/sessions')
def end_other_sessions(session=Session):
    db.run('DELETE FROM sessions WHERE user_id = ? AND id_hash != ?', session['user']['id'], session['sessionHash'])
    return {'ok': True}


@app.get('/api/github/install')
def github_install(_session=Session):
    if not login_methods(env)['githubApp']:
        raise RepoError(404, 'GitHub App is not set up. See README.')
    return redirect(f"https://github.com/apps/{quote(env['GITHUB_APP_SLUG'], safe='')}/installations/new")


@app.get('/api/github/setup')
def github_setup(request: Request, _session=Session):
    """After GitHub installs the app, confirm through a GitHub sign-in that this user really owns that installation."""
    installation_id = request.query_params.get('installation_id') or ''
    if not re.fullmatch(r'\d+', installation_id):
        raise RepoError(400, 'Missing installation')
    return github_authorize(env, [cookie(env, INSTALL_COOKIE, installation_id, 600)])


@app.post('/api/analyze')
async def analyze(request: Request, _session=Session):
    body = await read_json(request)
    repo = parse_repo(body.get('repo'))
    credits = parse_credits(body.get('credits'))
    requests_per_day = body.get('requestsPerDay', 1000)
    if not isinstance(requests_per_day, (int, float)) or isinstance(requests_per_day, bool) or not 0 <= requests_per_day <= 1e9:
        raise RepoError(400, 'Requests per day is out of range')
    fetched = await fetch_manifests(repo, env.get('GITHUB_TOKEN'))
    service_map = analyze_files(fetched['files'])
    return {
        'repo': f"{repo['owner']}/{repo['repo']}",
        'filesRead': [file['path'] for file in fetched['files']],
        'warnings': fetched['warnings'],
        'map': service_map,
        'portability': portability_report(service_map),
        'deployIssues': deploy_issues(service_map),
        **rank_plans(service_map, requests_per_day=requests_per_day, credits=credits),
    }


@app.get('/api/home')
def home(session=Session):
    """One call for the home dashboard: this user's projects with latest uptime and spend, plus recent activity."""
    user_id = session['user']['id']
    mine = 'SELECT id FROM projects WHERE user_id = ?'
    projects = db.all('SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC', user_id)
    events = db.all("SELECT r.at, r.event, r.data, p.id AS project_id, p.repo FROM reports r JOIN projects p ON p.id = r.project_id WHERE p.user_id = ? AND r.event != 'move_step' ORDER BY r.at DESC, r.id DESC LIMIT 15", user_id)
    costs = db.all(f"SELECT project_id, data FROM reports WHERE id IN (SELECT MAX(id) FROM reports WHERE event = 'report' AND project_id IN ({mine}) GROUP BY project_id)", user_id)
    uptime = db.all(f'SELECT project_id, AVG(ok) * 100 AS uptime, AVG(CASE WHEN ok = 1 THEN ms END) AS ms FROM checks WHERE at > ? AND project_id IN ({mine}) GROUP BY project_id', now_ms() - DAY, user_id)
    cost_by = {row['project_id']: json.loads(row['data']) for row in costs}
    uptime_by = {row['project_id']: row for row in uptime}

    def card(row):
        cost = cost_by.get(row['id']) or {}
        checks = uptime_by.get(row['id']) or {}
        amount = cost.get('costMonthToDate')
        return {
            **public_project(row),
            'uptimePercent': checks.get('uptime'),
            'averageMs': None if checks.get('ms') is None else js_round(checks['ms']),
            'costMonthToDate': amount if isinstance(amount, (int, float)) and not isinstance(amount, bool) else None,
            'costCloud': cost.get('cloud'),
        }
    return {
        'projects': [card(row) for row in projects],
        'events': [{**json.loads(row['data']), 'at': row['at'], 'event': row['event'], 'projectId': row['project_id'], 'repo': row['repo']} for row in events],
    }


# Accounts: code hosts and cloud accounts, saved once and reused by projects.

@app.get('/api/accounts')
def list_accounts(session=Session):
    user_id = session['user']['id']
    accounts = [public_account(row) for row in db.all('SELECT * FROM accounts WHERE user_id = ? ORDER BY created_at DESC', user_id)]
    projects = [public_project(row) for row in db.all('SELECT * FROM projects WHERE user_id = ?', user_id)]

    # Code accounts are linked by id; cloud accounts by the account or project the setup file targets.
    def count(account):
        if account['kind'] in CLOUDS:
            return len(projects_using_cloud(account, projects))
        return sum(1 for project in projects if project['accountId'] == account['id'])
    return {'accounts': [{**account, 'projects': count(account)} for account in accounts], 'methods': login_methods(env)}


@app.post('/api/accounts', status_code=201)
async def create_account(request: Request, session=Session):
    body = await read_json(request)
    kind = body.get('kind')
    account_id = str(uuid.uuid4())
    data, secret, label = {}, None, None
    if kind in ACCOUNT_SECRETS:
        plain = ACCOUNT_SECRETS[kind](body)
        if not plain:
            raise RepoError(400, 'Enter your Atlassian account email and an API token' if kind == 'bitbucket_token' else 'Enter an access token')
        if kind == 'azure_token':
            if not re.fullmatch(r'[\w.-]{1,50}', str(body.get('organization') or ''), re.ASCII):
                raise RepoError(400, 'Enter your Azure DevOps organization name')
            data = {'organization': body['organization']}
        secret = encrypt(plain, env['ENCRYPTION_KEY'], account_id)
        # Checks the credentials work before saving them.
        provider = provider_of(kind)
        try:
            label = await ADAPTERS[provider].whoami(await credentials_for(env, {'id': account_id, 'kind': kind, 'data': json.dumps(data), 'secret_enc': secret}))
        except RepoError as error:
            raise RepoError(400, f"{PROVIDERS[provider]['name']} did not accept those details. Check them and the token's permissions.") from error
        if kind == 'azure_token':
            label = f"{label} ({body['organization']})"
    elif kind in PLATFORM_KINDS:
        token = _token(body)
        if not token:
            raise RepoError(400, 'Enter an API token')
        try:
            label = await PLATFORMS[kind].whoami(token)
        except RepoError as error:
            raise RepoError(400, f'{PLATFORMS[kind].name} did not accept that token. Check it and its permissions.') from error
        secret = encrypt(token, env['ENCRYPTION_KEY'], account_id)
    elif kind in CLOUDS:
        if not validate_cloud_input(kind, body):
            raise RepoError(400, 'Check the account details and region')
        data = {'accountId': body['accountId'], 'region': body['region']} if kind == 'aws' else {'projectId': body['projectId'], 'projectNumber': body['projectNumber'], 'region': body['region']}
        label = f"AWS {body['accountId']}" if kind == 'aws' else f"GCP {body['projectId']}"
    else:
        raise RepoError(400, 'Unknown account type')
    if isinstance(body.get('label'), str) and body['label'].strip():
        label = body['label'].strip()[:60]
    db.run('INSERT INTO accounts (id, user_id, kind, label, data, secret_enc, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
           account_id, session['user']['id'], kind, label, json.dumps(data), secret, now_ms())
    return {'id': account_id, 'kind': kind, 'label': label}


@app.delete('/api/accounts/{account_id}')
def delete_account(account_id: str, session=Session):
    user_id = session['user']['id']
    get_row('accounts', account_id, user_id)
    if db.one('SELECT COUNT(*) AS n FROM projects WHERE account_id = ? AND user_id = ?', account_id, user_id)['n'] > 0:
        raise RepoError(409, 'Projects still use this account. Remove them first.')
    db.run('DELETE FROM accounts WHERE id = ? AND user_id = ?', account_id, user_id)
    return {'ok': True}


@app.get('/api/accounts/{account_id}/repos')
async def account_repos(account_id: str, session=Session):
    account = get_row('accounts', account_id, session['user']['id'])
    if account['kind'] in PLATFORM_KINDS or account['kind'] in CLOUDS:
        raise RepoError(400, 'That account is not a code host')
    return {'repos': await ADAPTERS[provider_of(account['kind'])].list_repos(await credentials_for(env, account), account)}


@app.get('/api/accounts/{account_id}/access')
async def account_access(account_id: str, session=Session):
    """What this account lets Cloudhop reach. Code hosts are asked live; clouds show what the setup file
    grants plus the latest permission check from a verify run."""
    user_id = session['user']['id']
    account = get_row('accounts', account_id, user_id)
    if account['kind'] in CLOUDS:
        projects = [public_project(row) for row in db.all('SELECT * FROM projects WHERE user_id = ?', user_id)]
        reports = db.all("SELECT r.project_id, r.at, r.data FROM reports r JOIN projects p ON p.id = r.project_id WHERE p.user_id = ? AND r.event = 'access' ORDER BY r.at DESC LIMIT 50", user_id)
        return cloud_access(public_account(account), projects, [{**json.loads(row['data']), 'projectId': row['project_id'], 'at': row['at']} for row in reports])
    if account['kind'] in PLATFORM_KINDS:
        return await PLATFORMS[account['kind']].access(decrypt_with_env(env, account['secret_enc'], account['id']))
    return await ADAPTERS[provider_of(account['kind'])].access(await credentials_for(env, account), account, env)


# Projects.

@app.get('/api/projects')
def list_projects(session=Session):
    return {'projects': [public_project(row) for row in db.all('SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC', session['user']['id'])]}


@app.post('/api/projects', status_code=201)
async def create_project(request: Request, session=Session):
    user_id = session['user']['id']
    body = await read_json(request)
    account = get_row('accounts', str(body.get('accountId') or ''), user_id)
    provider = provider_of(account['kind'])
    # Fails early if the account cannot reach the repo, and uses the host's canonical spelling.
    name = (await ADAPTERS[provider].repo_info(await credentials_for(env, account), parse_repo_for(provider, body.get('repo'))))['repo']
    if db.one('SELECT id FROM projects WHERE repo = ? AND user_id = ?', name, user_id):
        raise RepoError(409, 'That repo is already added')
    project_id = str(uuid.uuid4())
    report_token = random_token()
    kind = 'platform' if body.get('kind') == 'platform' else 'cloud'
    db.run('INSERT INTO projects (id, user_id, provider, repo, account_id, report_hash, kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
           project_id, user_id, provider, name, account['id'], sha256(report_token), kind, now_ms())
    # Shown once. Only its hash is stored.
    return {'id': project_id, 'repo': name, 'reportToken': report_token}


@app.delete('/api/projects/{project_id}')
async def delete_project(project_id: str, request: Request, session=Session):
    """Forgets a project: its settings, history and uptime checks. Nothing in the repo or the cloud is touched,
    and the report token stops working. The repo name must be typed to confirm."""
    row = get_row('projects', project_id, session['user']['id'])
    body = await read_json(request)
    if body.get('confirm') != row['repo']:
        raise RepoError(400, 'Type the repo name to confirm')
    db.batch([
        ('DELETE FROM project_secrets WHERE project_id = ?', (row['id'],)),
        ('DELETE FROM moves WHERE project_id = ?', (row['id'],)),
        ('DELETE FROM checks WHERE project_id = ?', (row['id'],)),
        ('DELETE FROM reports WHERE project_id = ?', (row['id'],)),
        ('DELETE FROM projects WHERE id = ? AND user_id = ?', (row['id'], session['user']['id'])),
    ])
    return {'ok': True}


@app.post('/api/projects/{project_id}/report-token')
def regenerate_report_token(project_id: str, session=Session):
    """Issues a new report token. The old one stops working at once; only the new one's hash is stored."""
    row = get_row('projects', project_id, session['user']['id'])
    report_token = random_token()
    db.batch([
        ('UPDATE projects SET report_hash = ? WHERE id = ? AND user_id = ?', (sha256(report_token), row['id'], session['user']['id'])),
        ('INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, ?, ?)',
         (row['id'], now_ms(), 'report_token_rotated', json.dumps({'event': 'report_token_rotated', 'by': session['user']['login']}))),
    ])
    return {'reportToken': report_token}


@app.get('/api/projects/{project_id}/status')
async def project_status(project_id: str, session=Session):
    row = get_row('projects', project_id, session['user']['id'])
    checks = db.all('SELECT at, ok, status, ms FROM checks WHERE project_id = ? AND at > ? ORDER BY at', project_id, now_ms() - DAY)
    reports = db.all("SELECT at, event, data FROM reports WHERE project_id = ? AND event != 'move_step' ORDER BY at DESC, id DESC LIMIT 20", project_id)
    events = [{**json.loads(report['data']), 'at': report['at'], 'event': report['event']} for report in reports]
    runs, runs_error = [], None
    if row['pr_url']:
        try:
            adapter, cred, _info = await project_access(row)
            runs = await adapter.runs(cred, row['repo'])
        except RepoError as error:
            runs_error = error.message
    return {
        'project': public_project(row),
        'uptime': uptime_summary(checks),
        'timeline': [{'at': check['at'], 'ok': check['ok'], 'ms': check['ms']} for check in checks],
        'events': events,
        'latestCost': next((event for event in events if event['event'] == 'report'), None),
        'runs': runs,
        'runsError': runs_error,
    }


@app.get('/api/projects/{project_id}/metrics')
def project_metrics(project_id: str, request: Request, session=Session):
    get_row('projects', project_id, session['user']['id'])
    chosen = request.query_params.get('range')
    span, bucket_count = RANGES.get(chosen, RANGES['24h'])
    now = now_ms()
    since = now - span
    checks = db.all('SELECT at, ok, status, ms FROM checks WHERE project_id = ? AND at > ? ORDER BY at', project_id, since)
    reports = [{'at': row['at'], **json.loads(row['data'])} for row in db.all("SELECT at, data FROM reports WHERE project_id = ? AND event = 'report' ORDER BY at DESC LIMIT 60", project_id)][::-1]
    return {
        'range': chosen if chosen in RANGES else '24h',
        'since': since,
        'now': now,
        **summarize_checks(checks, since, now, bucket_count),
        'costs': [{'at': report['at'], 'cloud': report.get('cloud'), 'amount': report['costMonthToDate']} for report in reports
                  if isinstance(report.get('costMonthToDate'), (int, float)) and not isinstance(report.get('costMonthToDate'), bool)],
        'resources': next((report['resources'] for report in reversed(reports) if report.get('resources')), None),
    }


@app.put('/api/projects/{project_id}/clouds/{cloud}')
async def connect_cloud(project_id: str, cloud: str, request: Request, session=Session):
    user_id = session['user']['id']
    if cloud not in CLOUDS:
        raise RepoError(400, 'Unknown cloud')
    body = await read_json(request)
    if body.get('accountId'):
        account = get_row('accounts', str(body['accountId']), user_id)
        if account['kind'] != cloud:
            raise RepoError(400, f'That is not a {cloud.upper()} account')
        body = {**json.loads(account['data']), **({'region': body['region']} if body.get('region') else {})}
    if not validate_cloud_input(cloud, body):
        raise RepoError(400, 'Check the account details and region')
    row = get_row('projects', project_id, user_id)
    # The setup file trusts this repo's CI identity, which depends on the code host.
    _adapter, _cred, info = await project_access(row)
    try:
        trust = trust_for(row['provider'], info)
    except ValueError as error:
        raise RepoError(400, str(error)) from error
    clouds = {**json.loads(row['clouds']), cloud: cloud_config(cloud, body, row['repo'])}
    db.run('UPDATE projects SET clouds = ? WHERE id = ? AND user_id = ?', json.dumps(clouds), project_id, user_id)
    return {'clouds': clouds, 'bootstrap': bootstrap_for(cloud, row['repo'], body, trust), 'bootstrapName': 'cloudhop-aws.yml' if cloud == 'aws' else 'cloudhop-gcp.sh'}


@app.post('/api/projects/{project_id}/install')
async def install(project_id: str, session=Session):
    user_id = session['user']['id']
    row = get_row('projects', project_id, user_id)
    clouds = json.loads(row['clouds'])
    if not clouds:
        raise RepoError(400, 'Connect a cloud first')
    adapter, cred, info = await project_access(row)
    manifests = await read_manifests(adapter, cred, row['repo'], info)
    service_map = analyze_files(manifests['files'])
    issues = deploy_issues(service_map)
    if issues:
        return JSONResponse({'error': 'This app cannot be deployed yet', 'issues': issues}, status_code=422)
    # The pipeline in the user's repo reports back to this address.
    cloudhop_url = env['PUBLIC_URL'].rstrip('/')
    generated = install_files(row['repo'], service_map, clouds, cloudhop_url, provider=row['provider'], existing_paths=manifests['paths'])
    pr_url = await adapter.open_change_request(cred, row['repo'], info, generated['files'], manifests['paths'], 'Add Cloudhop deployment files', install_description(row['provider'], generated['notes']))
    portability = portability_report(service_map)['worst']
    db.run('UPDATE projects SET pr_url = ?, portability = ? WHERE id = ? AND user_id = ?', pr_url, portability, project_id, user_id)
    return {'prUrl': pr_url, 'portability': portability, 'reachable': not re.search(r'localhost|127\.0\.0\.1', cloudhop_url)}


@app.post('/api/projects/{project_id}/run', status_code=202)
async def run_action(project_id: str, request: Request, session=Session):
    body = await read_json(request)
    row = get_row('projects', project_id, session['user']['id'])
    clouds = json.loads(row['clouds'])
    action, cloud = body.get('action'), body.get('cloud')
    if action not in ACTIONS or cloud not in CLOUDS or cloud not in clouds:
        raise RepoError(400, 'That action or cloud is not available')
    if body.get('approved') is not True:
        raise RepoError(400, 'Action needs approval')
    if action == 'destroy' and body.get('confirmText') != cloud:
        raise RepoError(400, 'Type the cloud name to confirm')
    needs_source = action in ('switch', 'rollback')
    source = body.get('source') if needs_source else ''
    if needs_source and (source not in CLOUDS or source == cloud or source not in clouds):
        raise RepoError(400, 'Needs a different, connected source cloud')
    adapter, cred, info = await project_access(row)
    # Guardrail: one action at a time per project, so a deploy can never race a switch or a destroy.
    if any(run['status'] != 'completed' for run in await adapter.runs(cred, row['repo'])):
        raise RepoError(409, 'Another action is still running for this project. Wait for it to finish.')
    await adapter.trigger(cred, row['repo'], info, {'action': action, 'cloud': cloud, 'source': source})
    # Audit trail: who approved what, shown in the project's history.
    db.run('INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, ?, ?)', row['id'], now_ms(), 'requested',
           json.dumps({'event': 'requested', 'action': action, 'cloud': cloud, 'source': source, 'by': session['user']['login']}))
    return {'started': True}


@app.post('/api/projects/{project_id}/command')
async def command(project_id: str, request: Request, session=Session):
    body = await read_json(request)
    return proposal_for(body.get('text'), public_project(get_row('projects', project_id, session['user']['id'])))


# Platform hosting: where the site runs now, secrets for it, moves between platforms, and pausing.

SITE_ACCOUNT = {'cloudflare': 'cloudflare_pages', 'vercel': 'vercel'}


def platform_token(account):
    return decrypt_with_env(env, account['secret_enc'], account['id'])


def platform_accounts(user_id):
    return [row for row in db.all('SELECT * FROM accounts WHERE user_id = ? ORDER BY created_at', user_id) if row['kind'] in SITE_ACCOUNT]


def set_hosting(row, update):
    current = json.loads(row.get('hosting') or '{}')
    current.update(update)
    db.run('UPDATE projects SET hosting = ? WHERE id = ?', json.dumps(current), row['id'])
    return current


def stored_secret_names(project_id):
    return {item['name'] for item in db.all('SELECT name FROM project_secrets WHERE project_id = ?', project_id)}


def report(project_id, event, data):
    db.run('INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, ?, ?)', project_id, now_ms(), event, json.dumps({'event': event, **data}))


@app.get('/api/projects/{project_id}/hosting/find')
async def find_hosting(project_id: str, session=Session):
    """Looks through the user's Cloudflare and Vercel accounts for a site built from this repo."""
    row = get_row('projects', project_id, session['user']['id'])
    sites, searched, errors = [], [], []
    for account in platform_accounts(session['user']['id']):
        platform = SITE_ACCOUNT[account['kind']]
        searched.append(account['label'])
        try:
            for site in await hosting.SITES[platform].find(platform_token(account), row['repo']):
                sites.append({**site, 'accountId': account['id'], 'accountLabel': account['label']})
        except RepoError as error:
            errors.append(f"{account['label']}: {error.message}")
    return {'sites': sites, 'searched': searched, 'errors': errors}


@app.put('/api/projects/{project_id}/hosting/current')
async def set_current_hosting(project_id: str, request: Request, session=Session):
    """"It already runs on...": records where the site runs now, from a fresh lookup (never trusted from the browser)."""
    row = get_row('projects', project_id, session['user']['id'])
    body = await read_json(request)
    if body.get('none') is True:
        set_hosting(row, {'current': None})
        return {'current': None}
    account = get_row('accounts', str(body.get('accountId') or ''), session['user']['id'])
    if account['kind'] not in SITE_ACCOUNT:
        raise RepoError(400, 'That account is not a hosting platform')
    sites = await hosting.SITES[SITE_ACCOUNT[account['kind']]].find(platform_token(account), row['repo'])
    site = next((item for item in sites if item['id'] == body.get('siteId')), None)
    if not site:
        raise RepoError(404, 'That site was not found for this repo')
    site = {**site, 'accountId': account['id']}
    set_hosting(row, {'current': site})
    db.run('UPDATE projects SET url = ?, active = ? WHERE id = ?', (site['domains'] and f"https://{site['domains'][0]}") or site['url'], site['platform'], row['id'])
    report(row['id'], 'hosting_found', {'platform': site['platform'], 'name': site['name'], 'url': site['url']})
    return {'current': site}


@app.get('/api/projects/{project_id}/secrets')
def list_secrets(project_id: str, session=Session):
    get_row('projects', project_id, session['user']['id'])
    return {'names': sorted(stored_secret_names(project_id))}


@app.put('/api/projects/{project_id}/secrets')
async def save_secrets(project_id: str, request: Request, session=Session):
    """Option 1 for secrets: Cloudhop keeps them encrypted, bound to the project and name, and adds them for you."""
    get_row('projects', project_id, session['user']['id'])
    values = (await read_json(request)).get('values')
    if not isinstance(values, dict) or not values or len(values) > 100:
        raise RepoError(400, 'Send between 1 and 100 values')
    for name, value in values.items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', str(name)) or not isinstance(value, str) or not value or len(value) > 8000:
            raise RepoError(400, f'Check {name}: names are letters, digits and _, values up to 8000 characters')
    for name, value in values.items():
        db.run('INSERT INTO project_secrets (project_id, name, value_enc, created_at) VALUES (?, ?, ?, ?) ON CONFLICT (project_id, name) DO UPDATE SET value_enc = excluded.value_enc',
               project_id, name, encrypt(value, env['ENCRYPTION_KEY'], f'{project_id}:{name}'), now_ms())
    return {'names': sorted(stored_secret_names(project_id))}


@app.delete('/api/projects/{project_id}/secrets/{name}')
def delete_secret(project_id: str, name: str, session=Session):
    get_row('projects', project_id, session['user']['id'])
    db.run('DELETE FROM project_secrets WHERE project_id = ? AND name = ?', project_id, name)
    return {'names': sorted(stored_secret_names(project_id))}


def public_move(move):
    return {'id': move['id'], 'status': move['status'], 'createdAt': move['created_at'], 'plan': json.loads(move['plan']), 'state': json.loads(move['state'])}


def get_move(project_id, move_id):
    move = db.one('SELECT * FROM moves WHERE id = ? AND project_id = ?', move_id, project_id)
    if not move:
        raise RepoError(404, 'Move not found')
    return move


@app.post('/api/projects/{project_id}/moves', status_code=201)
async def plan_move(project_id: str, request: Request, session=Session):
    """The dry run: nothing changes on any platform until the plan is approved."""
    row = get_row('projects', project_id, session['user']['id'])
    body = await read_json(request)
    target = body.get('to')
    if target not in hosting.PLATFORM_TARGETS or not hosting.PLATFORM_TARGETS[target]['move_to']:
        raise RepoError(400, 'Moving there is not available yet')
    account = get_row('accounts', str(body.get('accountId') or ''), session['user']['id'])
    if account['kind'] != hosting.PLATFORM_TARGETS[target]['account']:
        raise RepoError(400, f"Pick a {hosting.PLATFORM_TARGETS[target]['name']} account")
    current = json.loads(row.get('hosting') or '{}').get('current')
    if current and current['platform'] == target:
        raise RepoError(400, f"It already runs on {hosting.PLATFORM_TARGETS[target]['name']}")
    plan = {**hosting.move_plan(row['repo'], current, target, stored_secret_names(project_id)), 'accountId': account['id']}
    move_id = str(uuid.uuid4())
    db.run('INSERT INTO moves (id, project_id, created_at, status, plan, state) VALUES (?, ?, ?, ?, ?, ?)', move_id, row['id'], now_ms(), 'planned', json.dumps(plan), '{}')
    return public_move(get_move(project_id, move_id))


@app.get('/api/projects/{project_id}/moves/latest')
def latest_move(project_id: str, session=Session):
    get_row('projects', project_id, session['user']['id'])
    move = db.one('SELECT * FROM moves WHERE project_id = ? ORDER BY created_at DESC LIMIT 1', project_id)
    return {'move': public_move(move) if move else None}


@app.post('/api/projects/{project_id}/moves/{move_id}/approve', status_code=202)
async def approve_move(project_id: str, move_id: str, request: Request, background: BackgroundTasks, session=Session):
    row = get_row('projects', project_id, session['user']['id'])
    move = get_move(project_id, move_id)
    body = await read_json(request)
    if move['status'] != 'planned':
        raise RepoError(409, 'This move was already started')
    if body.get('approved') is not True:
        raise RepoError(400, 'The move needs approval')
    mode = body.get('secretsMode')
    if mode not in ('cloudhop', 'manual'):
        raise RepoError(400, 'Choose who adds the secrets')
    plan = json.loads(move['plan'])
    missing = [name for name in plan['secrets'] if name not in stored_secret_names(project_id)]
    if mode == 'cloudhop' and missing:
        raise RepoError(400, f"Enter these secrets first, or choose to add them yourself: {', '.join(missing)}")
    state = {'secretsMode': mode, 'steps': {step['id']: {'status': 'pending'} for step in plan['steps']}, 'by': session['user']['login']}
    db.run("UPDATE moves SET status = 'running', state = ? WHERE id = ?", json.dumps(state), move_id)
    report(row['id'], 'move_started', {'to': plan['to'], 'by': session['user']['login']})
    background.add_task(run_move, project_id, move_id)
    return {'started': True}


@app.post('/api/projects/{project_id}/moves/{move_id}/continue', status_code=202)
def continue_move(project_id: str, move_id: str, background: BackgroundTasks, session=Session):
    """After the user did their part (added secrets, installed the platform's GitHub app), pick up where it stopped."""
    get_row('projects', project_id, session['user']['id'])
    move = get_move(project_id, move_id)
    if move['status'] not in ('waiting', 'failed'):
        raise RepoError(409, 'This move is not waiting for you')
    db.run("UPDATE moves SET status = 'running' WHERE id = ?", move_id)
    background.add_task(run_move, project_id, move_id)
    return {'started': True}


async def run_move(project_id, move_id):
    """Runs the move's steps in order, saving progress after each, and stops where the user has to act."""
    move = get_move(project_id, move_id)
    row = db.one('SELECT * FROM projects WHERE id = ?', project_id)
    plan, state = json.loads(move['plan']), json.loads(move['state'])
    steps = state['steps']

    def save(status=None):
        db.run('UPDATE moves SET state = ?, status = COALESCE(?, status) WHERE id = ?', json.dumps(state), status, move_id)

    def mark(step, status, detail=None, **extra):
        steps[step] = {'status': status, 'detail': detail, 'at': now_ms(), **extra}
        save()
        if status != 'running' or step == 'deploy':
            report(project_id, 'move_step', {'step': step, 'status': status, 'detail': detail, 'to': plan['to']})

    try:
        account = db.one('SELECT * FROM accounts WHERE id = ? AND user_id = ?', plan['accountId'], row['user_id'])
        if not account:
            raise RepoError(400, 'The account for this move was removed')
        token = platform_token(account)
        adapter = hosting.SITES[plan['to']]
        current = json.loads(row.get('hosting') or '{}').get('current')
        plain = {item['name']: item['value'] for item in (current or {}).get('variables', []) if not item['secret'] and item.get('value') is not None}
        secrets = {}
        if state['secretsMode'] == 'cloudhop':
            for item in db.all('SELECT name, value_enc FROM project_secrets WHERE project_id = ?', project_id):
                if item['name'] in plan['secrets']:
                    secrets[item['name']] = decrypt(item['value_enc'], env['ENCRYPTION_KEY'], f"{project_id}:{item['name']}")

        if steps['create']['status'] != 'done':
            mark('create', 'running')
            name = re.sub(r'[^a-z0-9-]+', '-', row['repo'].split('/')[-1].lower()).strip('-')[:90] or 'site'

            async def create(project_name):
                # Recorded before the call: if the answer is lost (timeout, restart), a retry finds this project
                # instead of creating a second one.
                state['createRequested'] = project_name
                save()
                return await adapter.create(token, row['repo'], project_name, plan['settings'], {**plain, **secrets})
            try:
                site = await created_earlier(adapter, token, row['repo'], state.get('createRequested'))
                if not site:
                    try:
                        site = await create(name)
                    except hosting.PlatformError as error:
                        if error.http_status != 409:
                            raise
                        site = await create(f'{name}-{random_token()[:4].lower()}')
            except hosting.NeedsGitAccess as error:
                mark('create', 'waiting', error.message, link=error.link, action='git')
                save('waiting')
                return
            state['site'] = site
            mark('create', 'done', f"Created {site['name']}.", url=site['url'])

        site = state['site']
        if steps['variables']['status'] != 'done':
            mark('variables', 'running')
            if state['secretsMode'] == 'manual' and plan['secrets']:
                have = await adapter.env_names(token, site)
                missing = [name for name in plan['secrets'] if name not in have]
                if missing:
                    mark('variables', 'waiting', f"Add these on {hosting.PLATFORM_TARGETS[plan['to']]['name']}: {', '.join(missing)}. Then continue.",
                         missing=missing, link=f"https://vercel.com/dashboard?project={quote(site['name'])}", action='secrets')
                    save('waiting')
                    return
            mark('variables', 'done', f"{len(plain)} settings copied, {len(plan['secrets'])} secrets in place.")

        mark('deploy', 'running')
        deployment = await adapter.deploy(token, site, row['repo'], (current or {}).get('branch'))
        result, message = await hosting.wait_for_deploy(adapter, token, site, deployment)
        if result != 'READY':
            mark('deploy', 'failed', message or f'The build ended as {result}.')
            save('failed')
            return
        mark('deploy', 'done', 'Built and deployed.')

        mark('verify', 'running')
        check = await hosting.check_site(site['url'], (current or {}).get('url'))
        if not check['ok']:
            mark('verify', 'failed', check['detail'])
            save('failed')
            return
        mark('verify', 'done', check['detail'])
        mark('domain', 'skipped', 'Coming soon. Move your domain on the platform for now.')
        mark('old', 'pending', 'Your old site is still live. Pause it or delete it when you are ready.')

        fresh = {**site, 'platform': plan['to'], 'accountId': account['id'], 'variables': [], 'settings': plan['settings'], 'branch': (current or {}).get('branch')}
        set_hosting(row, {'previous': current, 'current': fresh})
        db.run('UPDATE projects SET url = ?, active = ?, previous = ? WHERE id = ?', site['url'], plan['to'], (current or {}).get('platform'), project_id)
        report(project_id, 'moved', {'from': (current or {}).get('platform'), 'to': plan['to'], 'url': site['url']})
        save('done')
    except Exception as error:  # noqa: BLE001 - every failure must end as a failed move the user can continue
        # CancelledError is not an Exception, so a cancelled task still stops at once.
        if not isinstance(error, RepoError):
            log.exception('move %s failed', move_id)
        running = next((key for key, value in steps.items() if value['status'] == 'running'), 'create')
        mark(running, 'failed', error.message if isinstance(error, RepoError) else 'Something went wrong on our side. Check the platform, then continue.')
        save('failed')


async def created_earlier(adapter, token, repo, name):
    """The project an earlier, interrupted attempt asked the platform to create, if it exists there."""
    if not name:
        return None
    return next((site for site in await adapter.find(token, repo) if site['name'] == name), None)


@app.post('/api/projects/{project_id}/hosting/pause')
async def pause_site(project_id: str, request: Request, session=Session):
    """Stops a site to save cost, with approval. Never deletes: that stays with the user."""
    row = get_row('projects', project_id, session['user']['id'])
    body = await read_json(request)
    if body.get('approved') is not True:
        raise RepoError(400, 'Pausing needs approval')
    which = body.get('which') if body.get('which') in ('current', 'previous') else 'previous'
    site = json.loads(row.get('hosting') or '{}').get(which)
    if not site:
        raise RepoError(404, 'No such site')
    if not hosting.PLATFORM_TARGETS[site['platform']]['pause']:
        raise RepoError(400, f"{hosting.PLATFORM_TARGETS[site['platform']]['name']} cannot pause a site. Delete it yourself from its settings: {site.get('deleteUrl') or site.get('manageUrl')}")
    account = get_row('accounts', site['accountId'], session['user']['id'])
    await hosting.SITES[site['platform']].pause(platform_token(account), site)
    set_hosting(row, {which: {**site, 'paused': True}})
    report(row['id'], 'paused', {'platform': site['platform'], 'name': site['name'], 'by': session['user']['login']})
    return {'paused': True}


# Logs: one timeline per project (or across all of them) from Cloudhop's own records.

LOG_LEVEL = {
    'switch_failed': 'error', 'destroyed': 'warn', 'paused': 'warn', 'report_token_rotated': 'warn',
    'deployed': 'ok', 'switched': 'ok', 'rolled_back': 'ok', 'moved': 'ok', 'verified': 'ok',
}
LOG_SOURCE = {
    'requested': 'you', 'move_started': 'you', 'paused': 'you', 'report_token_rotated': 'you', 'hosting_found': 'you',
    'move_step': 'agent', 'moved': 'agent',
}


def uptime_log(checks, repo, project_id):
    """Turns raw uptime checks into "went down" and "back up" lines, one per change."""
    lines, previous = [], None
    for check in checks:
        up = bool(check['ok'])
        if previous is not None and up != previous:
            lines.append({'at': check['at'], 'level': 'ok' if up else 'error', 'source': 'uptime', 'event': 'site_up' if up else 'site_down',
                          'projectId': project_id, 'repo': repo, 'data': {'status': check['status'], 'ms': check['ms']}})
        previous = up
    return lines


@app.get('/api/logs')
def logs(request: Request, session=Session):
    """Newest first. project= narrows to one project; before= pages back; level= and source= filter."""
    user_id = session['user']['id']
    query = request.query_params
    project_id = query.get('project')
    if project_id:
        get_row('projects', project_id, user_id)
    try:
        before = int(query.get('before') or now_ms() + 1)
        limit = min(max(int(query.get('limit') or 100), 1), 500)
    except ValueError as error:
        raise RepoError(400, 'before and limit are numbers') from error
    scope = 'p.user_id = ?' + (' AND p.id = ?' if project_id else '')
    args = [user_id] + ([project_id] if project_id else [])
    rows = db.all(f'SELECT r.at, r.event, r.data, p.id AS project_id, p.repo FROM reports r JOIN projects p ON p.id = r.project_id WHERE {scope} AND r.at < ? ORDER BY r.at DESC, r.id DESC LIMIT ?', *args, before, limit)
    entries = []
    for row in rows:
        data = json.loads(row['data'])
        level = LOG_LEVEL.get(row['event'], 'info')
        if row['event'] == 'move_step':
            level = {'failed': 'error', 'waiting': 'warn', 'done': 'ok'}.get(data.get('status'), 'info')
        entries.append({'at': row['at'], 'level': level, 'source': LOG_SOURCE.get(row['event'], 'pipeline'), 'event': row['event'], 'projectId': row['project_id'], 'repo': row['repo'], 'data': data})
    oldest = entries[-1]['at'] if len(entries) == limit else 0
    for project in db.all(f'SELECT id, repo FROM projects p WHERE {scope}', *args):
        checks = db.all('SELECT at, ok, status, ms FROM checks WHERE project_id = ? AND at < ? AND at >= ? ORDER BY at', project['id'], before, oldest)
        entries += uptime_log(checks, project['repo'], project['id'])
    entries.sort(key=lambda entry: entry['at'], reverse=True)
    level, source = query.get('level'), query.get('source')
    entries = [entry for entry in entries if (not level or entry['level'] == level) and (not source or entry['source'] == source)]
    return {'entries': entries[:limit], 'next': entries[limit - 1]['at'] if len(entries) >= limit else None}


# Domains: the user's custom domain, and a read-only check of what pointing it at the site would take.
# Moving it for real (changing DNS) is coming soon.

DOMAIN = re.compile(r'(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', re.ASCII)


@app.put('/api/projects/{project_id}/domain')
async def save_domain(project_id: str, request: Request, session=Session):
    row = get_row('projects', project_id, session['user']['id'])
    domain = str((await read_json(request)).get('domain') or '').strip().lower().rstrip('.')
    if domain and not DOMAIN.fullmatch(domain):
        raise RepoError(400, 'Enter a domain like shop.example.com')
    set_hosting(row, {'domain': domain or None})
    return {'domain': domain or None}


@app.get('/api/projects/{project_id}/domain/check')
async def check_domain(project_id: str, session=Session):
    """Which DNS record the domain needs, and, when the zone is on the user's Cloudflare account, what would
    change there. Reads only: nothing on any platform or in DNS is changed."""
    row = get_row('projects', project_id, session['user']['id'])
    project = public_project(row)
    domain = project['hosting'].get('domain')
    if not domain:
        raise RepoError(400, 'Save a domain first')
    current = project['hosting'].get('current')
    if project['kind'] == 'platform':
        if not current:
            raise RepoError(400, 'Tell Cloudhop where the site runs first')
        if current['platform'] != 'vercel':
            return {'domain': domain, 'records': [], 'supported': False, 'note': f"Domain checks for {current['platform'].replace('_', ' ').title()} are coming soon."}
        records = hosting.dns_records_for('vercel', domain)
    else:
        match = re.match(r'^http://(\d+)-(\d+)-(\d+)-(\d+)\.sslip\.io/?$', project['url'] or '')
        if not match:
            raise RepoError(400, 'Deploy first: the record points at the live server address')
        records = [{'type': 'A', 'name': '@' if domain.count('.') == 1 else domain.split('.')[0], 'content': '.'.join(match.groups())}]
    result = {'domain': domain, 'records': records, 'supported': True, 'cloudflare': None}
    for account in [a for a in platform_accounts(session['user']['id']) if a['kind'] == 'cloudflare']:
        try:
            plan = await hosting.cloudflare_dns_plan(platform_token(account), domain, records)
        except RepoError as error:
            plan = {'managedHere': False, 'note': f"{account['label']}: {error.message}"}
        if plan.get('managedHere'):
            result['cloudflare'] = {**plan, 'account': account['label']}
            break
        result['cloudflare'] = plan
    return result


@app.api_route('/api/{rest:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
def api_not_found(rest: str):
    return JSONResponse({'error': 'Not found'}, status_code=404)


# The website, from the same address, so sign-in cookies stay first-party. Security headers come
# from frontend/public/_headers, the same file a static host would use.

FRONTEND_DIR = Path(env.get('FRONTEND_DIR') or BACKEND_DIR.parent / 'frontend' / 'public')


def _security_headers():
    path = FRONTEND_DIR / '_headers'
    if not path.exists():
        return {}
    pairs = (line.strip().partition(':') for line in path.read_text(encoding='utf-8').splitlines() if line.startswith((' ', '\t')) and ':' in line)
    return {name.strip(): value.strip() for name, _sep, value in pairs}


SECURITY_HEADERS = _security_headers()


@app.middleware('http')
async def security_headers(request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        if name == 'Strict-Transport-Security' and not str(env.get('PUBLIC_URL', '')).startswith('https://'):
            continue
        response.headers.setdefault(name, value)
    return response


if FRONTEND_DIR.is_dir():
    app.mount('/', StaticFiles(directory=FRONTEND_DIR, html=True), name='frontend')

