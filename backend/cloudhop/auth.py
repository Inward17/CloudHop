"""Sign-up and sign-in.

Follows the usual hosted-platform pattern: Continue with GitHub creates the account on first visit,
then a random session id lives in an HttpOnly cookie. Only its hash is stored, and the browser never
holds a secret that scripts can read. No passwords are stored.
"""
import hmac
import uuid
from urllib.parse import urlencode, urlsplit

from fastapi.responses import JSONResponse, Response

from . import db, net
from .errors import AuthError
from .store import random_token, sha256
from .timeutil import now_ms

SESSION_COOKIE = 'cloudhop_session'
STATE_COOKIE = 'cloudhop_state'
INSTALL_COOKIE = 'cloudhop_install'
SESSION_SECONDS = 7 * 24 * 3600
LOCAL_ADMIN = 'local-admin'


def read_cookie(request, name):
    for part in request.headers.get('cookie', '').split(';'):
        key, _, value = part.strip().partition('=')
        if key == name:
            return value
    return None


def cookie(env, name, value, max_age):
    secure = '; Secure' if str(env.get('PUBLIC_URL') or '').startswith('https://') else ''
    return f'{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}'


def clear_cookie(env, name):
    return cookie(env, name, '', 0)


def login_methods(env):
    github = bool(env.get('GITHUB_CLIENT_ID') and env.get('GITHUB_CLIENT_SECRET'))
    return {
        'github': github,
        'adminToken': bool(env.get('ADMIN_TOKEN')),
        # Installing the app is verified with a GitHub sign-in, so it needs the OAuth settings too.
        'githubApp': github and bool(env.get('GITHUB_APP_ID') and env.get('GITHUB_APP_SLUG') and env.get('GITHUB_APP_PRIVATE_KEY')),
        'openSignup': github and not env.get('ALLOWED_GITHUB_USERS'),
    }


def with_cookies(response, cookies):
    for value in cookies:
        response.headers.append('set-cookie', value)
    return response


def redirect(location, cookies=()):
    return with_cookies(Response(status_code=302, headers={'location': location}), cookies)


def create_session(env, user_id, request):
    session_id = random_token()
    agent = request.headers.get('user-agent', '')[:160]
    db.run('INSERT INTO sessions (id_hash, user_id, agent, created_at, expires_at) VALUES (?, ?, ?, ?, ?)',
           sha256(session_id), user_id, agent, now_ms(), now_ms() + SESSION_SECONDS * 1000)
    return cookie(env, SESSION_COOKIE, session_id, SESSION_SECONDS)


def current_session(request):
    """The signed-in user row plus the current session's hash, or None."""
    session_id = read_cookie(request, SESSION_COOKIE)
    if not session_id:
        return None
    digest = sha256(session_id)
    user = db.one('SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id_hash = ? AND s.expires_at > ?', digest, now_ms())
    return {'user': user, 'sessionHash': digest} if user else None


def end_session(env, request):
    session_id = read_cookie(request, SESSION_COOKIE)
    if session_id:
        db.run('DELETE FROM sessions WHERE id_hash = ?', sha256(session_id))
    return clear_cookie(env, SESSION_COOKIE)


def _origin(url):
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'


def check_origin(request, env):
    """Cookies ride along on cross-site form posts, so writes must come from our own page.

    SameSite=Lax already blocks most of these; the Origin check closes the rest.
    """
    if request.method in ('GET', 'HEAD'):
        return
    if request.headers.get('origin') != _origin(env['PUBLIC_URL']):
        raise AuthError(403, 'Request did not come from the Cloudhop site')


async def admin_login(request, env):
    """Local testing only: one built-in user, no GitHub needed."""
    if not env.get('ADMIN_TOKEN'):
        raise AuthError(404, 'Admin token sign-in is turned off')
    try:
        body = await request.json()
    except ValueError:
        body = {}
    token = body.get('token') if isinstance(body, dict) else None
    if not isinstance(token, str) or not hmac.compare_digest(sha256(token), sha256(env['ADMIN_TOKEN'])):
        raise AuthError(401, 'Wrong admin token')
    now = now_ms()
    db.run("""INSERT INTO users (id, github_id, login, name, created_at, last_login_at) VALUES (?, NULL, 'admin', 'Local admin', ?, ?)
              ON CONFLICT (id) DO UPDATE SET last_login_at = excluded.last_login_at""", LOCAL_ADMIN, now, now)
    return with_cookies(JSONResponse({'ok': True}), [create_session(env, LOCAL_ADMIN, request)])


def check_state(request):
    """Short-lived random state in a cookie defeats forged OAuth callbacks."""
    expected = read_cookie(request, STATE_COOKIE)
    if not expected or expected != request.query_params.get('state'):
        raise AuthError(400, 'Sign-in link expired. Try again.')


def github_authorize(env, extra_cookies=()):
    if not login_methods(env)['github']:
        raise AuthError(404, 'GitHub sign-in is not set up')
    state = random_token()
    query = urlencode({'client_id': env['GITHUB_CLIENT_ID'], 'redirect_uri': f"{env['PUBLIC_URL']}/api/auth/callback", 'state': state})
    return redirect(f'https://github.com/login/oauth/authorize?{query}', [cookie(env, STATE_COOKIE, state, 600), *extra_cookies])


async def exchange_code(env, code):
    """The GitHub user token is used during the callback only, then dropped."""
    response = await net.client.post('https://github.com/login/oauth/access_token', headers={'Accept': 'application/json'},
                                     json={'client_id': env['GITHUB_CLIENT_ID'], 'client_secret': env['GITHUB_CLIENT_SECRET'], 'code': code})
    try:
        token = response.json().get('access_token')
    except ValueError:
        token = None
    if not token:
        raise AuthError(401, 'GitHub sign-in failed')
    return token


async def github_as_user(token, path):
    response = await net.client.get(f'https://api.github.com{path}', headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'})
    if not response.is_success:
        raise AuthError(502, 'GitHub did not answer')
    return response.json()


def allowed_login(env, login):
    allowed = env.get('ALLOWED_GITHUB_USERS')
    if not allowed:
        return True
    return login.lower() in [name.strip().lower() for name in allowed.split(',')]


def upsert_github_user(profile):
    """First sign-in creates the user; later ones refresh the profile from GitHub."""
    now = now_ms()
    existing = db.one('SELECT id FROM users WHERE github_id = ?', profile['id'])
    user_id = existing['id'] if existing else str(uuid.uuid4())
    db.run("""INSERT INTO users (id, github_id, login, name, email, avatar_url, created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT (id) DO UPDATE SET login = excluded.login, avatar_url = excluded.avatar_url, email = COALESCE(users.email, excluded.email), last_login_at = excluded.last_login_at""",
           user_id, profile['id'], profile['login'], profile.get('name') or profile['login'], profile.get('email'), profile.get('avatar_url'), now, now)
    return user_id, existing is None
