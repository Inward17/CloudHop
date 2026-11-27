"""Cloudhop API: FastAPI on one server, with SQLite and a 5-minute background job for uptime checks.

Run locally: uvicorn cloudhop.main:app --port 8787 (from backend/). Settings come from the
environment, or from backend/.env for local use. See README.
"""
import asyncio
import ipaddress
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
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import agent, db, mcp_server, net
from .analyze import analyze_files
from .auth import (INSTALL_COOKIE, SESSION_COOKIE, STATE_COOKIE, _origin as auth_origin, admin_login, allowed_login, check_origin, check_state, clear_cookie, cookie,
                   create_session, current_session, end_session, exchange_code, github_as_user, github_authorize, login_methods, read_cookie,
                   redirect, upsert_github_user, with_cookies)
from .errors import RepoError
from .generate import (CLOUD_KINDS, bootstrap_for, cloud_access, cloud_account, cloud_config, deploy_issues, install_files, projects_using_cloud, trust_for,
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
# Reports that move the active-cloud pointer, and the approved action each must belong to.
STATE_EVENTS = {'deployed': 'deploy', 'switched': 'switch', 'switch_failed': 'switch', 'rolled_back': 'rollback', 'destroyed': 'destroy'}
# A new run takes a moment to appear in the code host's run list. Within this window the reservation alone blocks a second start.
RESERVE_MS = 120_000
SSLIP_URL = re.compile(r'https?://(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})\.sslip\.io/?', re.ASCII)
CLOUDS = set(CLOUD_KINDS)
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


async def user_installation_repos(token, installation_id, pages=10):
    """Repos this GitHub user can reach through the installation, lowercased."""
    repos = []
    for page in range(1, pages + 1):
        data = await github_as_user(token, f'/user/installations/{installation_id}/repositories?per_page=100&page={page}')
        repos += [repo['full_name'].lower() for repo in data.get('repositories', [])]
        if len(data.get('repositories', [])) < 100:
            break
    return repos


RECONNECT = 'Reconnect this GitHub App in Accounts so Cloudhop can confirm which repos you can use.'


def needs_reconnect(account):
    """GitHub App accounts saved before Cloudhop recorded which repos the person may use."""
    return account['kind'] == 'github_app' and json.loads(account['data']).get('repos') is None


def app_repos(account):
    """The repos a GitHub App account may be used for. Raises when the account must be reconnected first."""
    if needs_reconnect(account):
        raise RepoError(403, RECONNECT)
    return set(json.loads(account['data'])['repos'])


def check_repo_allowed(account, repo):
    """A GitHub App account may only be used for repos its owner confirmed access to at install time."""
    if account['kind'] != 'github_app':
        return
    if repo.lower() not in app_repos(account):
        raise RepoError(404, 'That repo is not one you can reach through this GitHub App. Install it there or ask an owner.')


async def project_access(row):
    """Everything needed to talk to a project's code host: the adapter, credentials and repo details."""
    account = get_row('accounts', row['account_id'], row['user_id'])
    check_repo_allowed(account, row['repo'])
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
        lines += ['', 'Cloud access is limited to the `cloudhop` GitHub environment, which only the default branch may use. Add required reviewers to it for a second approval on every run.']
    if provider == 'gitlab':
        lines += ['', 'Cloud access is limited to pipelines on the default branch. Protect that branch.']
    if provider == 'azure':
        lines += ['', 'Create a service connection named `cloudhop` that uses workload identity federation, then add its id as pipeline variable CLOUDHOP_SERVICE_CONNECTION_ID.']
    if notes:
        lines += ['', *(f'**Action needed:** {note}' for note in notes)]
    lines += ['', 'Check generated Dockerfiles carefully: start commands are best guesses.']
    return '\n'.join(lines)


# Background job: uptime checks every 5 minutes, a daily cost report, and cleanup.

def health_url(project):
    return project['url'].rstrip('/') + (project.get('health_path') or '/')


async def check_site(project):
    started = now_ms()
    ok, code = 0, None
    try:
        response = await net.public_get(health_url(project), timeout=10)
        code = response.status_code
        # 401, 403 and 404 mean the app is not serving this page, so they count as down. Set a health path that answers 2xx.
        ok = 1 if 200 <= code < 400 else 0
    except Exception:  # Unreachable counts as down.
        pass
    db.run('INSERT INTO checks (project_id, at, ok, status, ms) VALUES (?, ?, ?, ?, ?)', project['id'], now_ms(), ok, code, now_ms() - started if ok else None)


async def scheduled(when_ms=None):
    when = datetime.fromtimestamp((when_ms or now_ms()) / 1000, tz=timezone.utc)

    async def one(row):
        await check_site(row)
        # Once a day, ask the user's pipeline for cost and server data. Platform sites have no pipeline.
        if row['active'] in CLOUDS and (row.get('kind') or 'cloud') == 'cloud' and when.hour == 6 and when.minute < 5:
            adapter, cred, info = await project_access(row)
            await adapter.trigger(cred, row['repo'], info, {'action': 'report', 'cloud': row['active'], 'source': ''})

    results = await asyncio.gather(*(one(row) for row in db.all('SELECT * FROM projects WHERE url IS NOT NULL')), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            log.warning('scheduled job: %s', result)
    db.run('DELETE FROM checks WHERE at < ?', now_ms() - 7 * DAY)
    db.run('DELETE FROM reports WHERE at < ?', now_ms() - 365 * DAY)
    db.run('DELETE FROM sessions WHERE expires_at < ?', now_ms())


async def scheduler():
    while True:
        await asyncio.sleep(300 - (now_ms() / 1000) % 300)  # On the next 5-minute mark, like a cron.
        try:
            await scheduled()
        except Exception:
            log.exception('scheduled job failed')


def interrupted_moves():
    """A restart stops a move mid-step. Mark it failed, so the user sees it and can continue."""
    for move in db.all("SELECT id, state FROM moves WHERE status = 'running'"):
        state = json.loads(move['state'])
        for step in (state.get('steps') or {}).values():
            if step.get('status') == 'running':
                step.update(status='failed', detail='Cloudhop restarted during this step. Check the platform, then continue.')
        db.run("UPDATE moves SET status = 'failed', state = ? WHERE id = ?", json.dumps(state), move['id'])


@asynccontextmanager
async def lifespan(_app):
    db.conn()
    interrupted_moves()
    task = None if env.get('CLOUDHOP_SCHEDULER') == 'off' else asyncio.create_task(scheduler())
    yield
    if task:
        task.cancel()


# API docs only for local use: on an https site they would map every route for anyone.
LOCAL = not str(env.get('PUBLIC_URL') or '').startswith('https://')
app = FastAPI(title='Cloudhop API', lifespan=lifespan, docs_url='/api/docs' if LOCAL else None, openapi_url='/api/openapi.json' if LOCAL else None, redoc_url=None)
MAX_BODY = 1_000_000
MAX_REPORT_BODY = 64_000


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
    if request.url.path.startswith('/api/'):
        if not (env.get('ENCRYPTION_KEY') and env.get('PUBLIC_URL')):
            return JSONResponse({'error': 'Server is missing ENCRYPTION_KEY or PUBLIC_URL. See README.'}, status_code=500)
        if len(env['ENCRYPTION_KEY']) < 32:
            return JSONResponse({'error': 'ENCRYPTION_KEY is too short. Use 32 random bytes, for example: openssl rand -hex 32. See README.'}, status_code=500)
        # Bodies are read whole, so their size is capped before reading. The pipeline's report is small.
        limit = MAX_REPORT_BODY if request.url.path == '/api/report' else MAX_BODY
        length = request.headers.get('content-length')
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            if length is None and request.headers.get('transfer-encoding'):
                return JSONResponse({'error': 'Send a Content-Length header'}, status_code=411)
            if length is not None and (not length.isdigit() or int(length) > limit):
                return JSONResponse({'error': 'Request too large'}, status_code=413)
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
    if body['event'] not in STATE_EVENTS:
        db.run('INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, ?, ?)', row['id'], now_ms(), body['event'], raw)
        return {'ok': True}
    # Moving the active-cloud pointer needs the approved run this report belongs to: the newest one for the project,
    # for the same action and cloud, not already used. Delayed, duplicate or unrelated reports are kept but change nothing.
    reason = report_rejection(row['id'], body)
    if reason:
        db.run('INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, ?, ?)', row['id'], now_ms(), 'report_ignored',
               json.dumps({'event': 'report_ignored', 'reported': body['event'], 'cloud': body['cloud'], 'reason': reason}))
        return {'ok': True, 'applied': False, 'reason': reason}
    following = next_state(public_project(row), {**body, 'url': deployed_url(body.get('url'))})
    db.batch([
        ('INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, ?, ?)', (row['id'], now_ms(), body['event'], raw)),
        ('UPDATE projects SET active = ?, previous = ?, url = ? WHERE id = ?', (following['active'], following['previous'], following['url'], row['id'])),
    ])
    return {'ok': True, 'applied': True}


def report_rejection(project_id, body):
    """Why a state-changing report must not be applied, or None. Claims the run, so a second copy is refused."""
    request_id = body.get('requestId')
    run = db.one('SELECT * FROM runs WHERE id = ? AND project_id = ?', request_id, project_id) if isinstance(request_id, str) and request_id else None
    if not run:
        return 'No approved run matches this report.'
    expected_cloud = run['source'] if body['event'] == 'switch_failed' else run['cloud']
    if run['action'] != STATE_EVENTS[body['event']] or body['cloud'] != expected_cloud:
        return 'The report does not match the approved action.'
    if db.one('SELECT 1 FROM runs WHERE project_id = ? AND at > ?', project_id, run['at']):
        return 'A newer action was approved since this run started.'
    if not db.run("UPDATE runs SET status = 'reported' WHERE id = ? AND status = 'requested'", run['id']):
        return 'This run already reported.'
    return None


def deployed_url(url):
    """Only the address shape the generated pipeline reports (a public IP as an sslip.io name). Anything else is ignored,
    so a report cannot point the uptime checker somewhere else."""
    match = SSLIP_URL.fullmatch(url) if isinstance(url, str) else None
    if not match:
        return None
    try:
        address = ipaddress.ip_address('.'.join(match.groups()))
    except ValueError:
        return None
    return url if address.is_global else None


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
        # The installation's token reaches every repo it is installed on, which can be more than this person may
        # use (an org install, for one). Keep only the repos GitHub says this person can reach through it.
        repos = await user_installation_repos(token, pending_install)
        data = json.dumps({'installationId': pending_install, 'owner': owner, 'repos': repos})
        existing = db.one("SELECT id FROM accounts WHERE user_id = ? AND kind = 'github_app' AND json_extract(data, '$.installationId') = ?", session['user']['id'], pending_install)
        if existing:
            db.run('UPDATE accounts SET data = ? WHERE id = ?', data, existing['id'])
        else:
            db.run('INSERT INTO accounts (id, user_id, kind, label, data, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                   str(uuid.uuid4()), session['user']['id'], 'github_app', owner, data, now_ms())
        return redirect(f"{public_url}/#/accounts?install={'updated' if existing else 'added'}", cleared)

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
        ('DELETE FROM mcp_tokens WHERE user_id = ?', (user_id,)),
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
    costs = db.all(f"SELECT project_id, at, data FROM reports WHERE id IN (SELECT MAX(id) FROM reports WHERE event = 'report' AND project_id IN ({mine}) GROUP BY project_id)", user_id)
    uptime = db.all(f'SELECT project_id, AVG(ok) * 100 AS uptime, AVG(CASE WHEN ok = 1 THEN ms END) AS ms FROM checks WHERE at > ? AND project_id IN ({mine}) GROUP BY project_id', now_ms() - DAY, user_id)
    cost_by = {row['project_id']: {**json.loads(row['data']), 'at': row['at']} for row in costs}
    uptime_by = {row['project_id']: row for row in uptime}
    latest_moves = db.all(f'SELECT * FROM moves m WHERE project_id IN ({mine}) AND created_at = (SELECT MAX(created_at) FROM moves WHERE project_id = m.project_id)', user_id)
    move_by = {row['project_id']: move_summary(row) for row in latest_moves}

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
            # What the number covers: AWS reports the whole account's spend, not only this app.
            'costScope': cost.get('costScope'),
            'costCurrency': cost.get('currency'),
            'costAt': cost.get('at'),
            # The latest platform move, so lists can say what waits on the user without opening each project.
            'move': move_by.get(row['id']),
        }
    cards = [card(row) for row in projects]
    return {
        'projects': cards,
        'spend': spend_total(cards, cost_by),
        'events': [{**json.loads(row['data']), 'at': row['at'], 'event': row['event'], 'projectId': row['project_id'], 'repo': row['repo']} for row in events],
    }


def spend_total(cards, cost_by):
    """Month-to-date spend across projects. AWS reports a whole account, so projects that share one are counted once,
    keyed by the account in the report itself (not the project's current connection, which can change after it).
    Reports without an account are counted per project."""
    latest = {}
    for card in cards:
        if card['costMonthToDate'] is None:
            continue
        account = (cost_by.get(card['id']) or {}).get('costAccount')
        key = ('aws', account) if card['costScope'] == 'aws-account' and account else ('project', card['id'])
        if key not in latest or (card['costAt'] or 0) > (latest[key]['costAt'] or 0):
            latest[key] = card
    counted = list(latest.values())
    currencies = sorted({card['costCurrency'] or 'USD' for card in counted})
    return {
        'total': sum(card['costMonthToDate'] for card in counted) if len(currencies) == 1 else None,
        'currency': currencies[0] if len(currencies) == 1 else None,
        'currencies': currencies,
        'accounts': len(counted),
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
    return {'accounts': [{**account, 'projects': count(account), 'needsReconnect': needs_reconnect({**account, 'data': json.dumps(account['data'])})} for account in accounts],
            'methods': login_methods(env)}


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
        if kind == 'coolify':
            # Coolify runs wherever the user put it, so its address is saved with the token (and checked on every call).
            address = str(body.get('url') or '').strip().rstrip('/')
            if not re.fullmatch(r'https://[A-Za-z0-9.-]+(:\d{1,5})?', address):
                raise RepoError(400, 'Enter your Coolify address, like https://coolify.example.com')
            token, data = json.dumps({'url': address, 'token': token}), {'url': address}
        try:
            label = await PLATFORMS[kind].whoami(token)
        except RepoError as error:
            raise RepoError(400, f'{PLATFORMS[kind].name} did not accept that token. Check it and its permissions.') from error
        secret = encrypt(token, env['ENCRYPTION_KEY'], account_id)
    elif kind in agent.LLM_KINDS:
        token = _token(body)
        model = str(body.get('model') or '').strip()[:200]
        if not token or (kind != 'anthropic' and not model):
            raise RepoError(400, 'Enter an API key and a model')
        try:
            await agent.check_key(kind, token, model)
        except agent.AgentError as error:
            raise RepoError(400, str(error)) from error
        data = {'model': model} if model and kind != 'anthropic' else {}
        name = 'Claude' if kind == 'anthropic' else agent.COMPATIBLE[kind]['name']
        label = f'{name} key ending {token[-4:]}'
        secret = encrypt(token, env['ENCRYPTION_KEY'], account_id)
    elif kind in CLOUDS:
        if not validate_cloud_input(kind, body):
            raise RepoError(400, 'Check the account details and region')
        data, label = cloud_account(kind, body)
    else:
        raise RepoError(400, 'Unknown account type')
    if isinstance(body.get('label'), str) and body['label'].strip():
        label = body['label'].strip()[:60]
    db.run('INSERT INTO accounts (id, user_id, kind, label, data, secret_enc, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
           account_id, session['user']['id'], kind, label, json.dumps(data), secret, now_ms())
    if kind in agent.LLM_KINDS:  # One agent key per user: a new one replaces the old, whichever provider it was for.
        for old in llm_accounts(session['user']['id']):
            if old['id'] != account_id:
                db.run('DELETE FROM accounts WHERE id = ?', old['id'])
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
    if account['kind'] in PLATFORM_KINDS or account['kind'] in CLOUDS or account['kind'] in agent.LLM_KINDS:
        raise RepoError(400, 'That account is not a code host')
    allowed = app_repos(account) if account['kind'] == 'github_app' else None  # Before any GitHub call: an old account must reconnect.
    repos = await ADAPTERS[provider_of(account['kind'])].list_repos(await credentials_for(env, account), account)
    if allowed is not None:
        repos = [repo for repo in repos if repo.lower() in allowed]
    return {'repos': repos}


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
    if account['kind'] in agent.LLM_KINDS:
        return agent.key_access(account['kind'])
    if account['kind'] in PLATFORM_KINDS:
        return await PLATFORMS[account['kind']].access(decrypt_with_env(env, account['secret_enc'], account['id']))
    access = await ADAPTERS[provider_of(account['kind'])].access(await credentials_for(env, account), account, env)
    if account['kind'] == 'github_app':
        reconnect = needs_reconnect(account)
        allowed = set() if reconnect else app_repos(account)
        access['repos'] = [repo for repo in access['repos'] if repo['name'].lower() in allowed]
        access['total'], access['more'], access['needsReconnect'] = len(access['repos']), False, reconnect
        access['note'] = RECONNECT if reconnect else 'Shows the repos you can reach through this installation. Reconnect it after you get access to more.'
    return access


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
    asked = parse_repo_for(provider, body.get('repo'))
    check_repo_allowed(account, asked)
    # Fails early if the account cannot reach the repo, and uses the host's canonical spelling.
    name = (await ADAPTERS[provider].repo_info(await credentials_for(env, account), asked))['repo']
    if db.one('SELECT id FROM projects WHERE lower(repo) = lower(?) AND user_id = ?', name, user_id):
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
        'costs': [{'at': report['at'], 'cloud': report.get('cloud'), 'amount': report['costMonthToDate'], 'currency': report.get('currency'), 'scope': report.get('costScope')} for report in reports
                  if isinstance(report.get('costMonthToDate'), (int, float)) and not isinstance(report.get('costMonthToDate'), bool)],
        'resources': next((report['resources'] for report in reversed(reports) if report.get('resources')), None),
    }


HEALTH_PATH = re.compile(r'/[A-Za-z0-9._~!$&\'()*+,;=:@%/?-]{0,199}', re.ASCII)


@app.put('/api/projects/{project_id}/health')
async def save_health_path(project_id: str, request: Request, session=Session):
    """The path the uptime check requests, such as /health. Must start with / and stay on the site's own address."""
    row = get_row('projects', project_id, session['user']['id'])
    path = str((await read_json(request)).get('path') or '/').strip()
    if not HEALTH_PATH.fullmatch(path) or path.startswith('//'):
        raise RepoError(400, 'Enter a path on your site, like /health')
    db.run('UPDATE projects SET health_path = ? WHERE id = ?', path, row['id'])
    return {'healthPath': path}


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
    if cloud == 'hostinger':
        # One app per server (run.sh refuses too): /opt/cloudhop holds a single app.
        for other in db.all('SELECT repo, clouds FROM projects WHERE user_id = ? AND id != ?', user_id, project_id):
            if (json.loads(other['clouds']).get('hostinger') or {}).get('host') == body.get('host'):
                raise RepoError(409, f"{other['repo']} already uses the Hostinger server {body.get('host')}. Use one server per project.")
    clouds = {**json.loads(row['clouds']), cloud: cloud_config(cloud, body, row['repo'])}
    db.run('UPDATE projects SET clouds = ? WHERE id = ? AND user_id = ?', json.dumps(clouds), project_id, user_id)
    name = 'cloudhop-aws.yml' if cloud == 'aws' else 'cloudhop-gcp.sh' if cloud == 'gcp' else f'cloudhop-{cloud}-setup.txt'
    return {'clouds': clouds, 'bootstrap': bootstrap_for(cloud, row['repo'], body, trust, row['provider']), 'bootstrapName': name}


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
    notes = list(generated['notes'])
    if hasattr(adapter, 'protect_environment'):
        try:
            await adapter.protect_environment(cred, row['repo'], info)
        except RepoError:
            notes.append(f"Cloudhop could not protect the `cloudhop` environment. In repo Settings > Environments > cloudhop, limit deployment branches to {info['defaultBranch']}. "
                         'Without that, anyone who can push a branch can use your cloud access.')
    pr_url = await adapter.open_change_request(cred, row['repo'], info, generated['files'], manifests['paths'], 'Add Cloudhop deployment files', install_description(row['provider'], notes))
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
    # Reserve in one statement: two approvals that both passed the check above cannot both get here. The window covers
    # a run the code host has not listed yet.
    request_id = random_token()[:32]
    started = now_ms()
    if not db.run("""INSERT INTO runs (id, project_id, action, cloud, source, requested_by, at, status)
                     SELECT ?, ?, ?, ?, ?, ?, ?, 'requested' WHERE NOT EXISTS (SELECT 1 FROM runs WHERE project_id = ? AND at > ? AND status != 'failed')""",
                  request_id, row['id'], action, cloud, source, session['user']['login'], started, row['id'], started - RESERVE_MS):
        raise RepoError(409, 'An action was just started for this project. Wait a minute, then check Deployments.')
    try:
        await adapter.trigger(cred, row['repo'], info, {'action': action, 'cloud': cloud, 'source': source, 'request': request_id})
    except Exception:
        db.run("UPDATE runs SET status = 'failed' WHERE id = ?", request_id)
        raise
    # Audit trail: who approved what, shown in the project's history.
    db.run('INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, ?, ?)', row['id'], now_ms(), 'requested',
           json.dumps({'event': 'requested', 'action': action, 'cloud': cloud, 'source': source, 'by': session['user']['login'], 'requestId': request_id}))
    return {'started': True, 'requestId': request_id}


@app.post('/api/projects/{project_id}/command')
async def command(project_id: str, request: Request, session=Session):
    body = await read_json(request)
    row = get_row('projects', project_id, session['user']['id'])
    project = public_project(row)
    text = str(body.get('text') or '').strip()[:2000]
    llm = user_llm(session['user']['id'])
    if not (llm or agent.enabled()) or not text:
        return proposal_for(text, project)
    try:
        return await agent.respond(text, project, lambda: project_access(row), lambda: recent_activity(project_id), session['user']['id'], llm=llm)
    except agent.KeyRejected as error:
        return {'reply': str(error)}
    except agent.AgentError:
        # Keyword rules still handle the common commands.
        return proposal_for(text, project)


def llm_accounts(user_id):
    return [row for row in db.all('SELECT * FROM accounts WHERE user_id = ?', user_id) if row['kind'] in agent.LLM_KINDS]


def user_llm(user_id):
    """The user's own model key as {kind, key, model}, or None to use Cloudhop's."""
    row = next(iter(llm_accounts(user_id)), None)
    return {'kind': row['kind'], 'key': decrypt_with_env(env, row['secret_enc'], row['id']), 'model': json.loads(row['data']).get('model')} if row else None


def recent_activity(project_id):
    rows = db.all('SELECT at, event, data FROM reports WHERE project_id = ? ORDER BY at DESC, id DESC LIMIT 40', project_id)
    return [{**json.loads(row['data']), 'at': datetime.fromtimestamp(row['at'] / 1000, timezone.utc).isoformat()} for row in rows]


@app.post('/api/projects/{project_id}/explain')
async def explain(project_id: str, request: Request, session=Session):
    """Plain-language reason for a failed pipeline run ({runId}) or the latest platform move ({}). Reads only."""
    body = await read_json(request)
    row = get_row('projects', project_id, session['user']['id'])
    llm = user_llm(session['user']['id'])
    if not (llm or agent.enabled()):
        return {'reply': 'Explanations need an AI model key. Add yours in Accounts.'}
    run_id = str(body.get('runId') or '')
    if run_id and not run_id.isalnum():
        raise RepoError(400, 'Unknown run')
    what = f'Pipeline run {run_id} failed. Read its log and explain why.' if run_id else "This project's latest platform move stopped or failed. Check recent activity and explain why."
    try:
        return await agent.respond(what, public_project(row), lambda: project_access(row), lambda: recent_activity(project_id), session['user']['id'], can_propose=False, llm=llm)
    except agent.AgentError as error:
        raise RepoError(503, str(error)) from error


# Platform hosting: where the site runs now, secrets for it, moves between platforms, and pausing.

SITE_ACCOUNT = {'cloudflare': 'cloudflare_pages', 'vercel': 'vercel', 'render': 'render', 'netlify': 'netlify', 'koyeb': 'koyeb', 'railway': 'railway', 'heroku': 'heroku', 'flyio': 'flyio',
                'northflank': 'northflank', 'do_apps': 'do_apps', 'coolify': 'coolify'}


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


def move_summary(move):
    """Status of a move for lists: where it goes and, when it waits or failed, the step and what it says."""
    plan, state = json.loads(move['plan']), json.loads(move['state'])
    stuck = next(((key, step) for key, step in (state.get('steps') or {}).items() if step.get('status') in ('waiting', 'failed')), (None, {}))
    return {'status': move['status'], 'to': plan.get('to'), 'step': stuck[0], 'detail': stuck[1].get('detail'), 'action': stuck[1].get('action')}


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
    return await make_move_plan(row, session['user']['id'], body.get('to'), body.get('accountId'))


async def make_move_plan(row, user_id, target, account_id):
    """Plans a move and saves it as 'planned'. Shared by the dashboard and the MCP tool; approving stays in the dashboard."""
    project_id = row['id']
    if target not in hosting.PLATFORM_TARGETS or not hosting.PLATFORM_TARGETS[target]['move_to']:
        raise RepoError(400, 'Moving there is not available yet')
    if hosting.PLATFORM_TARGETS[target]['account'] is None:
        # GitHub Pages publishes from the repo itself, with the project's own GitHub access.
        if row['provider'] != 'github':
            raise RepoError(400, f"{hosting.PLATFORM_TARGETS[target]['name']} needs a GitHub repo")
        account = get_row('accounts', row['account_id'], user_id)
    else:
        account = get_row('accounts', str(account_id or ''), user_id)
        if account['kind'] != hosting.PLATFORM_TARGETS[target]['account']:
            raise RepoError(400, f"Pick a {hosting.PLATFORM_TARGETS[target]['name']} account")
    current = json.loads(row.get('hosting') or '{}').get('current')
    if current and current['platform'] == target:
        raise RepoError(400, f"It already runs on {hosting.PLATFORM_TARGETS[target]['name']}")
    detected, default_branch = None, None
    settings = (current or {}).get('settings') or {}
    try:
        adapter, cred, info = await project_access(row)
        default_branch = info.get('defaultBranch')
        # A site that leaves its build command or output folder to its framework (common on Vercel) gives the new
        # platform nothing to use, so the repo fills whichever is missing.
        if not (settings.get('buildCommand') and settings.get('outputDirectory')):
            detected = hosting.static_site_settings(analyze_files((await read_manifests(adapter, cred, row['repo'], info))['files']))
    except Exception:  # noqa: BLE001 - best effort: without it the plan says it found no build settings
        log.warning('could not read the repo for %s', row['repo'], exc_info=True)
    plan = {**hosting.move_plan(row['repo'], current, target, stored_secret_names(project_id), detected, default_branch), 'accountId': account['id']}
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
    if mode == 'cloudhop' and plan['secrets'] and getattr(hosting.SITES[plan['to']], 'secrets_by_hand', False):
        raise RepoError(400, f"Cloudhop cannot add secrets on {hosting.PLATFORM_TARGETS[plan['to']]['name']}. Choose to add them yourself.")
    state = {'secretsMode': mode, 'steps': {step['id']: {'status': 'pending'} for step in plan['steps']}, 'by': session['user']['login']}
    # Only one approval can start it, even when two arrive at once.
    if not db.run("UPDATE moves SET status = 'running', state = ? WHERE id = ? AND status = 'planned'", json.dumps(state), move_id):
        raise RepoError(409, 'This move was already started')
    report(row['id'], 'move_started', {'to': plan['to'], 'by': session['user']['login']})
    background.add_task(run_move, project_id, move_id)
    return {'started': True}


@app.post('/api/projects/{project_id}/moves/{move_id}/continue', status_code=202)
def continue_move(project_id: str, move_id: str, background: BackgroundTasks, session=Session):
    """After the user did their part (added secrets, installed the platform's GitHub app), pick up where it stopped."""
    get_row('projects', project_id, session['user']['id'])
    get_move(project_id, move_id)
    if not db.run("UPDATE moves SET status = 'running' WHERE id = ? AND status IN ('waiting', 'failed')", move_id):
        raise RepoError(409, 'This move is not waiting for you')
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
        adapter = hosting.SITES[plan['to']]
        if getattr(adapter, 'uses_code_host', False):
            _code_adapter, code_cred, _info = await project_access(row)
            token = code_cred['auth'].removeprefix('Bearer ')
        else:
            token = platform_token(account)
        current = json.loads(row.get('hosting') or '{}').get('current')
        # The plan holds the branch: the current site's, else the repo's default (not always main).
        branch = plan.get('branch') or (current or {}).get('branch') or 'main'
        plain = {item['name']: item['value'] for item in (current or {}).get('variables', []) if not item['secret'] and item.get('value') is not None}
        secrets = {}
        if state['secretsMode'] == 'cloudhop':
            for item in db.all('SELECT name, value_enc FROM project_secrets WHERE project_id = ?', project_id):
                if item['name'] in plan['secrets']:
                    secrets[item['name']] = decrypt(item['value_enc'], env['ENCRYPTION_KEY'], f"{project_id}:{item['name']}")

        if steps['create']['status'] != 'done':
            mark('create', 'running')
            # Within the platform's name limit, so a retry's suffix is never cut off (that would resend the same name).
            limit = getattr(adapter, 'name_limit', 90)
            name = re.sub(r'[^a-z0-9-]+', '-', row['repo'].split('/')[-1].lower()).strip('-')[:limit].strip('-') or 'site'
            # A platform that cannot find its projects by repo reports what it made as it goes, so a retry carries on
            # with that (and its name) instead of making a second one.
            name = (state.get('createPartial') or {}).get('name') or name

            async def create(project_name):
                # Recorded before the call: if the answer is lost (timeout, restart), a retry finds this project
                # instead of creating a second one.
                state['createRequested'] = project_name
                save()
                extra = {}
                if getattr(adapter, 'resumable', False):
                    def progress(done):
                        state['createPartial'] = {**done, 'name': project_name}
                        save()
                    partial = state.get('createPartial') or {}
                    extra = {'resume': partial if partial.get('name') == project_name else None, 'progress': progress}
                return await adapter.create(token, row['repo'], project_name, plan['settings'], {**plain, **secrets}, branch=branch, **extra)
            try:
                if 'existingSites' not in state:
                    # Before this move creates anything: projects already on the platform for this repo. A retry
                    # never adopts one of these, so a same-name project the user already had still gets renamed.
                    state['existingSites'] = [site_identity(site) for site in await adapter.find(token, row['repo'])]
                    save()
                site = await created_earlier(adapter, token, row['repo'], state.get('createRequested'), state['existingSites'])
                if not site:
                    try:
                        site = await create(name)
                    except hosting.PlatformError as error:
                        if error.http_status != 409:
                            raise
                        site = await create(f"{name[:limit - 5].rstrip('-')}-{random_token()[:4].lower()}")
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
                # A build the platform started on create ran before these were added, so deploy afresh with them.
                site.pop('firstDeploy', None)
                have = await adapter.env_names(token, site)
                missing = [name for name in plan['secrets'] if name not in have]
                if missing:
                    mark('variables', 'waiting', f"Add these on {hosting.PLATFORM_TARGETS[plan['to']]['name']}: {', '.join(missing)}. Then continue.",
                         missing=missing, link=adapter.settings_url(site), action='secrets')
                    save('waiting')
                    return
            # Platforms that take variables in a call of their own get them here, so a retry after a failed write, or
            # after adopting a project an interrupted attempt made, writes them again. The write replaces by name.
            if getattr(adapter, 'set_variables', None) and (plain or secrets):
                await adapter.set_variables(token, site, {**plain, **secrets})
            mark('variables', 'done', f"{len(plain)} settings copied, {len(plan['secrets'])} secrets in place.")

        mark('deploy', 'running')
        if getattr(adapter, 'needs_pipeline', False):
            waiting = await ensure_pipeline(row, adapter, site, plan, state, branch, plain)
            if waiting:
                mark('deploy', 'waiting', waiting, link=state['pipelinePr'], action='pipeline')
                save('waiting')
                return
        archive = None
        if getattr(adapter, 'needs_archive', False):
            # Platforms without a GitHub link (Heroku) build from a short-lived source archive from the code host.
            code_adapter, cred, _info = await project_access(row)
            if not hasattr(code_adapter, 'archive_url'):
                raise RepoError(400, f"{hosting.PLATFORM_TARGETS[plan['to']]['name']} moves need a GitHub repo for now.")
            archive = await code_adapter.archive_url(cred, row['repo'], branch)
        deployment = await adapter.deploy(token, site, row['repo'], branch, **({'archive': archive} if archive else {}))
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

        fresh = {**site, 'platform': plan['to'], 'accountId': account['id'], 'variables': hosting.moved_variables(plan['to'], plan, current),
                 'settings': plan['settings'], 'branch': (current or {}).get('branch'),
                 'domains': site.get('domains') or [], 'manageUrl': site.get('manageUrl') or adapter.settings_url(site)}
        set_hosting(row, {'previous': current, 'current': fresh})
        db.run('UPDATE projects SET url = ?, active = ?, previous = ? WHERE id = ?', site['url'], plan['to'], (current or {}).get('platform'), project_id)
        report(project_id, 'moved', {'from': (current or {}).get('platform'), 'to': plan['to'], 'url': site['url']})
        for name in plan['secrets']:
            db.run('DELETE FROM project_secrets WHERE project_id = ? AND name = ?', project_id, name)
        save('done')
    except Exception as error:  # noqa: BLE001 - every failure must end as a failed move the user can continue
        # CancelledError is not an Exception, so a cancelled task still stops at once.
        if not isinstance(error, RepoError):
            log.exception('move %s failed', move_id)
        running = next((key for key, value in steps.items() if value['status'] == 'running'), 'create')
        mark(running, 'failed', error.message if isinstance(error, RepoError) else 'Something went wrong on our side. Check the platform, then continue.')
        save('failed')


async def ensure_pipeline(row, adapter, site, plan, state, branch, build_args=None):
    """For platforms that deploy from a workflow in the repo (Fly.io), on the planned branch: opens a pull request
    that adds the workflow and config for this app (once per app), waits until the branch has them, then starts the
    workflow there. Config left by an earlier move to another app is replaced, not reused. Returns what the user must
    do, or None."""
    name = hosting.PLATFORM_TARGETS[plan['to']]['name']
    if row['provider'] != 'github':
        raise RepoError(400, f'{name} moves need a GitHub repo for now: they deploy from a GitHub Actions workflow.')
    code_adapter, cred, info = await project_access(row)
    branch = branch or info['defaultBranch']
    on_branch = {**info, 'defaultBranch': branch}  # read, change and run on the planned branch, not the default one
    root = (plan['settings'].get('rootDirectory') or '').strip('/')
    prefix = f'{root}/' if root else ''

    async def read(path):
        try:
            return await code_adapter.read_file(cred, row['repo'], on_branch, path)
        except RepoError as error:
            if error.status != 404:
                raise
            return None
    config_path = adapter.config_path(prefix)
    workflow, config = await read(adapter.WORKFLOW), (await read(config_path) if config_path else None)
    if not adapter.pipeline_current(site, workflow, config):
        if state.get('pipelineFor') != site['id']:
            dockerfile = await read(f'{prefix}Dockerfile') if adapter.uses_dockerfile else None
            existing = {path for path, text in ((adapter.WORKFLOW, workflow), (config_path, config), (f'{prefix}Dockerfile', dockerfile)) if path and text}
            # A Dockerfile an earlier Fly move generated is generated afresh; one the user wrote is kept.
            files = adapter.repo_files(site, plan['settings'], branch, None if (dockerfile or '').startswith(adapter.GENERATED) else dockerfile, build_args, plan['secrets'])
            state['pipelinePr'] = await code_adapter.open_change_request(
                cred, row['repo'], on_branch, files, existing, f'Deploy to {name} with Cloudhop',
                f"Adds what {name} needs to deploy {site['name']} from `{branch}`: " + ', '.join(f"`{item['path']}`" for item in files) +
                f'.\n\n{adapter.pr_note(site, branch)}')
            state['pipelineFor'] = site['id']
        return adapter.waiting_note(site, branch)
    workflow_file = adapter.WORKFLOW.rsplit('/', 1)[-1]
    if hasattr(adapter, 'run_marker'):
        # The newest run before this one starts, so only a run started after it counts as this deploy.
        site['runMarker'] = await adapter.run_marker(cred['auth'].removeprefix('Bearer '), row['repo'], branch)
    try:
        await code_adapter.trigger(cred, row['repo'], on_branch, {}, workflow=workflow_file)
    except RepoError:
        if branch == info['defaultBranch']:
            raise
        # GitHub may refuse to dispatch a workflow that is not on the default branch; run its latest run again.
        await code_adapter.rerun_latest(cred, row['repo'], workflow_file, branch)
    return None


def site_identity(site):
    """A project's identity across every account a token reaches. Cloudflare uses the project name as its id, so the
    same id can exist in two accounts; the account (scope) tells them apart."""
    return [site.get('scope'), site['id']]


async def created_earlier(adapter, token, repo, name, existing):
    """The project an earlier, interrupted attempt of this move asked the platform to create, if it exists there.
    Projects in `existing` were there before the move started, so they are never this move's."""
    if not name:
        return None
    # Moves saved before identities carried the account stored bare ids; those still match on id alone.
    known = {tuple(item) if isinstance(item, list) else item for item in existing}
    return next((site for site in await adapter.find(token, repo)
                 if site['name'] == name and tuple(site_identity(site)) not in known and site['id'] not in known), None)


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
                          'projectId': project_id, 'repo': repo, 'data': {'status': check['status'], 'ms': check['ms']}, 'key': (check['at'], 'c', check['id'])})
        previous = up
    return lines


def report_entry(row):
    data = json.loads(row['data'])
    level = LOG_LEVEL.get(row['event'], 'info')
    if row['event'] == 'move_step':
        level = {'failed': 'error', 'waiting': 'warn', 'done': 'ok'}.get(data.get('status'), 'info')
    return {'at': row['at'], 'level': level, 'source': LOG_SOURCE.get(row['event'], 'pipeline'), 'event': row['event'], 'projectId': row['project_id'],
            'repo': row['repo'], 'data': data, 'key': (row['at'], 'r', row['id'])}


def parse_cursor(text):
    """`at.kind.id` from a previous page, so entries that share a timestamp are never skipped. A bare time still works."""
    if not text:
        return None
    parts = str(text).split('.')
    try:
        if len(parts) == 1:
            return (int(parts[0]), '', 0)
        at, kind, row_id = parts
        if kind not in ('c', 'r'):
            raise ValueError(kind)
        return (int(at), kind, int(row_id))
    except ValueError as error:
        raise RepoError(400, 'Unknown page cursor') from error


LOG_BATCH = 500
LOG_SCAN_LIMIT = 50_000  # ponytail: a filter that matches almost nothing stops after this many rows and returns a cursor to continue.


@app.get('/api/logs')
def logs(request: Request, session=Session):
    """Newest first. project= narrows to one project; before= pages back; level= and source= filter."""
    user_id = session['user']['id']
    query = request.query_params
    project_id = query.get('project')
    if project_id:
        get_row('projects', project_id, user_id)
    try:
        limit = min(max(int(query.get('limit') or 100), 1), 500)
    except ValueError as error:
        raise RepoError(400, 'limit is a number') from error
    cursor = parse_cursor(query.get('before'))
    level, source = query.get('level'), query.get('source')
    wanted = lambda entry: (not level or entry['level'] == level) and (not source or entry['source'] == source)  # noqa: E731
    scope = 'p.user_id = ?' + (' AND p.id = ?' if project_id else '')
    args = [user_id] + ([project_id] if project_id else [])

    # Uptime lines come from at most a week of checks (older ones are deleted), so they are worked out in full.
    entries = []
    if not source or source == 'uptime':
        for project in db.all(f'SELECT id, repo FROM projects p WHERE {scope}', *args):
            checks = db.all('SELECT rowid AS id, at, ok, status, ms FROM checks WHERE project_id = ? ORDER BY at, rowid', project['id'])
            entries += [line for line in uptime_log(checks, project['repo'], project['id']) if wanted(line) and (not cursor or line['key'] < cursor)]

    # Reports are read newest first in batches, filtered as they come, until there are enough for this page.
    # Any matching report past the first limit + 1 cannot be on this page, so reading can stop there.
    matched, scanned, more_unread = 0, 0, False
    position = cursor
    while matched <= limit:
        if position and position[1] == 'r':
            after, after_args = '(r.at < ? OR (r.at = ? AND r.id < ?))', [position[0], position[0], position[2]]
        elif position:
            after, after_args = 'r.at < ?', [position[0]]
        else:
            after, after_args = '1', []
        rows = db.all(f'SELECT r.id, r.at, r.event, r.data, p.id AS project_id, p.repo FROM reports r JOIN projects p ON p.id = r.project_id '
                      f'WHERE {scope} AND {after} ORDER BY r.at DESC, r.id DESC LIMIT ?', *args, *after_args, LOG_BATCH)
        for row in rows:
            entry = report_entry(row)
            if wanted(entry):
                entries.append(entry)
                matched += 1
        scanned += len(rows)
        if len(rows) < LOG_BATCH:
            break
        position = (rows[-1]['at'], 'r', rows[-1]['id'])
        if scanned >= LOG_SCAN_LIMIT:
            more_unread = True
            break

    if more_unread:
        # Reading stopped at `position`; reports older than it are unread. An uptime line older than that boundary could
        # be newer than one of those reports, so it waits for a later page, which continues from the boundary.
        entries = [entry for entry in entries if entry['key'] >= position]  # The boundary report itself was read, so it stays.
    entries.sort(key=lambda entry: entry['key'], reverse=True)
    page = entries[:limit]
    if len(entries) > limit or (more_unread and page):
        next_cursor = '.'.join(str(part) for part in page[-1]['key'])
    elif more_unread:
        next_cursor = '.'.join(str(part) for part in position)
    else:
        next_cursor = None
    for entry in page:
        entry.pop('key')
    return {'entries': page, 'next': next_cursor}


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
        match = re.match(r'^https?://(\d+)-(\d+)-(\d+)-(\d+)\.sslip\.io/?$', project['url'] or '')
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


# MCP: AI assistants (Claude, Cursor and others) connect with a personal token. They read and propose; approving stays in
# the dashboard, so a leaked token or a misled model cannot change a cloud.

MCP_TOKEN_PREFIX = 'chmcp_'
MAX_MCP_TOKENS = 10


@app.get('/api/profile/mcp-tokens')
def list_mcp_tokens(session=Session):
    rows = db.all('SELECT id, name, created_at, last_used_at FROM mcp_tokens WHERE user_id = ? ORDER BY created_at DESC', session['user']['id'])
    return {'tokens': [{'id': row['id'], 'name': row['name'], 'createdAt': row['created_at'], 'lastUsedAt': row['last_used_at']} for row in rows],
            'url': f"{env['PUBLIC_URL']}/api/mcp"}


@app.post('/api/profile/mcp-tokens', status_code=201)
async def create_mcp_token(request: Request, session=Session):
    name = str((await read_json(request)).get('name') or '').strip()[:60]
    if not name:
        raise RepoError(400, 'Name the token after the assistant that uses it, like Claude Desktop')
    user_id = session['user']['id']
    if db.one('SELECT COUNT(*) AS n FROM mcp_tokens WHERE user_id = ?', user_id)['n'] >= MAX_MCP_TOKENS:
        raise RepoError(400, f'You have {MAX_MCP_TOKENS} MCP tokens. Revoke one you no longer use first.')
    token = MCP_TOKEN_PREFIX + random_token()
    token_id = str(uuid.uuid4())
    db.run('INSERT INTO mcp_tokens (id, user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?, ?)', token_id, user_id, name, sha256(token), now_ms())
    # Shown once. Only its hash is stored.
    return {'id': token_id, 'name': name, 'token': token, 'url': f"{env['PUBLIC_URL']}/api/mcp"}


@app.delete('/api/profile/mcp-tokens/{token_id}')
def revoke_mcp_token(token_id: str, session=Session):
    if not db.run('DELETE FROM mcp_tokens WHERE id = ? AND user_id = ?', token_id, session['user']['id']):
        raise RepoError(404, 'Token not found')
    return {'ok': True}


def dashboard_link(path):
    return f"{env['PUBLIC_URL']}/#/{path}"


def mcp_project(user, project_id):
    return get_row('projects', project_id, user['id'])


async def tool_list_projects(user):
    rows = db.all('SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC', user['id'])
    return {'projects': [{'id': row['id'], 'repo': row['repo'], 'kind': row.get('kind') or 'cloud', 'runsOn': row['active'], 'previous': row['previous'],
                          'url': row['url'], 'installed': bool(row['pr_url']), 'link': dashboard_link(f"p/{row['id']}")} for row in rows]}


async def tool_get_project(user, project_id):
    row = mcp_project(user, project_id)
    checks = db.all('SELECT at, ok, status, ms FROM checks WHERE project_id = ? AND at > ? ORDER BY at', row['id'], now_ms() - DAY)
    move = db.one('SELECT * FROM moves WHERE project_id = ? ORDER BY created_at DESC LIMIT 1', row['id'])
    project = public_project(row)
    current = (project['hosting'] or {}).get('current') or {}
    return {
        'id': project['id'], 'repo': project['repo'], 'kind': project['kind'], 'runsOn': project['active'], 'previous': project['previous'],
        'url': project['url'], 'connectedClouds': sorted(project['clouds']), 'installPullRequest': project['prUrl'],
        # Names only: values of settings and secrets stay in Cloudhop.
        'hosting': {'platform': current.get('platform'), 'url': current.get('url'), 'variables': [item['name'] for item in current.get('variables') or []]} if current else None,
        'uptime24h': uptime_summary(checks), 'latestMove': move_summary(move) if move else None, 'link': dashboard_link(f"p/{row['id']}"),
    }


async def tool_get_activity(user, project_id='', limit='30'):
    """Newest first. The same entries as the dashboard's Logs, without uptime lines."""
    try:
        count = min(max(int(limit), 1), 100)
    except ValueError as error:
        raise RepoError(400, 'limit is a number from 1 to 100') from error
    scope, args = 'p.user_id = ?', [user['id']]
    if project_id:
        scope, args = scope + ' AND p.id = ?', [*args, mcp_project(user, project_id)['id']]
    rows = db.all(f'SELECT r.id, r.at, r.event, r.data, p.id AS project_id, p.repo FROM reports r JOIN projects p ON p.id = r.project_id '
                  f'WHERE {scope} ORDER BY r.at DESC, r.id DESC LIMIT ?', *args, count)
    return {'entries': [{key: value for key, value in report_entry(row).items() if key != 'key'} for row in rows]}


async def tool_propose_action(user, project_id, request):
    """Turns a request into the same proposal the dashboard shows. Nothing runs: the link opens it for approval."""
    row = mcp_project(user, project_id)
    proposal = proposal_for(request, public_project(row))
    if proposal.get('reply'):
        return {'proposed': False, 'reply': proposal['reply']}
    return {'proposed': True, 'summary': proposal['summary'], 'action': proposal['action'], 'cloud': proposal['cloud'], 'warnings': proposal.get('warnings') or [],
            'approve': dashboard_link(f"p/{row['id']}?ask={quote(request[:200])}"),
            'note': 'Not started. The user approves it in the Cloudhop dashboard by opening the approve link.'}


async def tool_plan_move(user, project_id, to, account_id=''):
    """A dry run saved as a plan. Approving it, and every change on a platform, happens in the dashboard."""
    row = mcp_project(user, project_id)
    if (row.get('kind') or 'cloud') != 'platform':
        raise RepoError(400, 'Moves are for hosting platform projects. For a cloud project, use propose_action, like "switch to gcp".')
    move = await make_move_plan(row, user['id'], to, account_id or None)
    plan = move['plan']
    return {'moveId': move['id'], 'status': move['status'], 'to': plan['to'], 'copy': plan['copy'], 'secrets': plan['secrets'],
            'missingSecrets': plan['missingSecrets'], 'warnings': plan['warnings'], 'steps': [step['title'] for step in plan['steps']],
            'approve': dashboard_link(f"p/{row['id']}/hosting"), 'note': 'Planned only. The user reviews and approves it in the Cloudhop dashboard.'}


async def tool_list_accounts(user):
    rows = db.all('SELECT * FROM accounts WHERE user_id = ? ORDER BY created_at', user['id'])
    return {'accounts': [{'id': row['id'], 'kind': row['kind'], 'label': row['label']} for row in rows]}


PROJECT_ID = {'project_id': {'type': 'string', 'description': 'A project id from list_projects.'}}
MCP_TOOLS = {tool.name: tool for tool in [
    mcp_server.Tool('list_projects', "The user's Cloudhop projects: repo, where each runs, its address and dashboard link.", {}, [], tool_list_projects),
    mcp_server.Tool('get_project', 'One project in detail: where it runs, connected clouds, hosting, 24-hour uptime and the latest move.', PROJECT_ID, ['project_id'], tool_get_project),
    mcp_server.Tool('get_activity', 'Recent activity, newest first: approvals, deploys, switches, move steps and errors. Leave project_id empty for every project.',
                    {**PROJECT_ID, 'limit': {'type': 'string', 'description': 'How many entries, 1 to 100. Default 30.'}}, [], tool_get_activity),
    mcp_server.Tool('list_accounts', 'Connected accounts by kind and name (code hosts, clouds, platforms). No credentials.', {}, [], tool_list_accounts),
    mcp_server.Tool('propose_action', 'Propose a deploy, switch, rollback, plan, connection check, cost refresh or removal, described in plain words '
                    '(like "deploy to aws" or "switch to gcp"). Returns what it would do and a link where the user approves it. Never runs it.',
                    {**PROJECT_ID, 'request': {'type': 'string', 'description': 'What to do, in plain words.'}}, ['project_id', 'request'], tool_propose_action),
    mcp_server.Tool('plan_move', 'Plan moving a hosting platform project to another platform (dry run). Returns the plan and a link where the user approves it.',
                    {**PROJECT_ID, 'to': {'type': 'string', 'enum': sorted(key for key, value in hosting.PLATFORM_TARGETS.items() if value['move_to'])},
                     'account_id': {'type': 'string', 'description': 'The target platform account id from list_accounts. Not needed for github_pages.'}},
                    ['project_id', 'to'], tool_plan_move, read_only=False),
]}


@app.post('/api/mcp')
async def mcp_endpoint(request: Request):
    """Streamable HTTP MCP endpoint. Bearer token only: cookies are ignored, so a web page cannot ride a signed-in session."""
    origin = request.headers.get('origin')
    if origin and origin != auth_origin(env['PUBLIC_URL']):
        # The MCP spec asks servers to refuse other origins, against DNS rebinding from a browser.
        return JSONResponse({'error': 'Origin not allowed'}, status_code=403)
    token = request.headers.get('authorization', '').removeprefix('Bearer ').strip()
    row = db.one('SELECT * FROM mcp_tokens WHERE token_hash = ?', sha256(token)) if token.startswith(MCP_TOKEN_PREFIX) else None
    user = db.one('SELECT * FROM users WHERE id = ?', row['user_id']) if row else None
    if not user:
        return JSONResponse({'error': 'Unauthorized. Create an MCP token in Cloudhop, under Profile.'}, status_code=401,
                            headers={'WWW-Authenticate': 'Bearer realm="cloudhop"'})
    db.run('UPDATE mcp_tokens SET last_used_at = ? WHERE id = ?', now_ms(), row['id'])
    try:
        message = json.loads(await request.body())
    except ValueError:
        return JSONResponse(mcp_server._error(None, -32700, 'Parse error'), status_code=400)
    answer = await mcp_server.handle(message, MCP_TOOLS, user)
    return JSONResponse(answer) if answer else Response(status_code=202)


@app.api_route('/api/mcp', methods=['GET', 'DELETE'])
def mcp_no_stream():
    # No server-sent event stream and no sessions to end (see mcp_server).
    return Response(status_code=405, headers={'Allow': 'POST'})


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

