"""Hosting platforms (Render, Vercel, Cloudflare, Netlify, Koyeb, Railway, Heroku): saved with an API token, checked
against the platform, and shown on the Access view. Finding and moving sites lives in hosting.py.

Each adapter: whoami(token) -> label, access(token) -> {kind: 'platform', ...}.
"""
import asyncio

from . import net
from .errors import RepoError


async def _get(url, token, missing_ok=False, headers=None, body=None):
    # body: a JSON POST instead of a GET (Railway's GraphQL API).
    request_headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json', **(headers or {})}
    response = await (net.client.post(url, headers=request_headers, json=body) if body is not None else net.client.get(url, headers=request_headers))
    if response.is_success:
        return response.json()
    if missing_ok:
        return None
    if response.status_code in (400, 401, 403):  # Cloudflare answers a malformed token with 400.
        raise RepoError(401, 'The platform rejected the token. Check it and its permissions.')
    raise RepoError(502, f'The platform returned {response.status_code}')


def _iso_from_ms(value):
    from datetime import UTC, datetime
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat() if value else None


class Render:
    API = 'https://api.render.com/v1'
    name = 'Render'

    async def owners(self, token):
        return [item['owner'] for item in await _get(f'{self.API}/owners?limit=100', token)]

    async def whoami(self, token):
        owners = await self.owners(token)
        if not owners:
            raise RepoError(401, 'The token reaches no Render workspace.')
        return owners[0].get('name') or owners[0].get('email') or 'Render'

    async def access(self, token):
        owners = await self.owners(token)
        return {
            'kind': 'platform', 'platform': 'render',
            'scopes': [{'name': owner.get('name') or owner.get('id'), 'detail': 'Workspace' if owner.get('type') == 'team' else 'Personal workspace'} for owner in owners],
            'expires': None,
            'note': 'Render API keys act as you in every workspace listed, with full access. Render does not offer narrower keys, so use a separate account or workspace for testing.',
            'manageUrl': 'https://dashboard.render.com/u/settings#api-keys',
        }


class Vercel:
    API = 'https://api.vercel.com'
    name = 'Vercel'

    async def whoami(self, token):
        user = (await _get(f'{self.API}/v2/user', token)).get('user') or {}
        return user.get('username') or user.get('email') or 'Vercel'

    async def access(self, token):
        meta, teams = await asyncio.gather(
            _get(f'{self.API}/v5/user/tokens/current', token, missing_ok=True),
            _get(f'{self.API}/v2/teams?limit=100', token, missing_ok=True),
        )
        names = {team['id']: team.get('name') or team.get('slug') for team in (teams or {}).get('teams', [])}
        token_info = (meta or {}).get('token') or {}
        scopes = [{'name': 'Your personal account' if scope.get('type') == 'user' else names.get(scope.get('teamId'), scope.get('teamId') or 'A team'),
                   'detail': 'Personal account' if scope.get('type') == 'user' else 'Team'} for scope in token_info.get('scopes', [])]
        if not scopes:
            scopes = [{'name': name, 'detail': 'Team'} for name in names.values()]
        return {
            'kind': 'platform', 'platform': 'vercel',
            'scopes': scopes,
            'expires': _iso_from_ms(token_info.get('expiresAt')),
            'note': 'Vercel tokens act as you in the scopes listed. Create one limited to a single team and with an expiry date.',
            'manageUrl': 'https://vercel.com/account/settings/tokens',
        }


class Cloudflare:
    API = 'https://api.cloudflare.com/client/v4'
    name = 'Cloudflare'

    async def accounts(self, token):
        data = await _get(f'{self.API}/accounts?per_page=50', token)
        return data.get('result') or []

    async def whoami(self, token):
        accounts = await self.accounts(token)
        if not accounts:
            raise RepoError(401, 'The token reaches no Cloudflare account.')
        return accounts[0].get('name') or 'Cloudflare'

    async def access(self, token):
        # /user/tokens/verify works for tokens you own; account-owned tokens answer 4xx there, so it is optional.
        accounts, verify = await asyncio.gather(self.accounts(token), _get(f'{self.API}/user/tokens/verify', token, missing_ok=True))
        result = (verify or {}).get('result') or {}
        return {
            'kind': 'platform', 'platform': 'cloudflare',
            'scopes': [{'name': account.get('name') or account.get('id'), 'detail': 'Account'} for account in accounts],
            'expires': result.get('expires_on'),
            'note': 'Cloudflare does not let a token list its own permissions. You can see them where you created it. Give it only what Cloudhop will need and an expiry date.',
            'manageUrl': 'https://dash.cloudflare.com/profile/api-tokens',
        }


def _access(platform, scopes, note, manage, expires=None):
    return {'kind': 'platform', 'platform': platform, 'scopes': scopes, 'expires': expires, 'note': note, 'manageUrl': manage}


class Netlify:
    API = 'https://api.netlify.com/api/v1'
    name = 'Netlify'

    async def whoami(self, token):
        user = await _get(f'{self.API}/user', token)
        return user.get('full_name') or user.get('email') or 'Netlify'

    async def access(self, token):
        teams = await _get(f'{self.API}/accounts', token) or []
        return _access('netlify', [{'name': team.get('name') or team.get('slug'), 'detail': 'Team'} for team in teams],
                       'Netlify personal access tokens act as you in every team listed. Set an expiry date when you create one. Cloudhop uses the first team.',
                       'https://app.netlify.com/user/applications#personal-access-tokens')


class Koyeb:
    API = 'https://app.koyeb.com/v1'
    name = 'Koyeb'

    async def whoami(self, token):
        user = (await _get(f'{self.API}/account/profile', token)).get('user') or {}
        return user.get('name') or user.get('email') or 'Koyeb'

    async def access(self, token):
        user = (await _get(f'{self.API}/account/profile', token)).get('user') or {}
        return _access('koyeb', [{'name': user.get('email') or user.get('name') or 'Your organization', 'detail': 'Organization'}],
                       "Koyeb API tokens act for the whole organization they belong to. Create one in a separate organization while testing.",
                       'https://app.koyeb.com/user/settings/api')


class Railway:
    API = 'https://backboard.railway.com/graphql/v2'
    name = 'Railway'

    async def me(self, token):
        data = await _get(self.API, token, body={'query': '{ me { name email } }'})
        if (data or {}).get('errors'):
            raise RepoError(401, 'Railway did not accept the token. Use an account token (a workspace token cannot identify you).')
        return ((data or {}).get('data') or {}).get('me') or {}

    async def whoami(self, token):
        me = await self.me(token)
        return me.get('name') or me.get('email') or 'Railway'

    async def access(self, token):
        me = await self.me(token)
        return _access('railway', [{'name': me.get('email') or me.get('name') or 'Your account', 'detail': 'Account'}],
                       'Railway account tokens act as you in every workspace. Cloudhop creates a new project for each move.',
                       'https://railway.com/account/tokens')


HEROKU = {'Accept': 'application/vnd.heroku+json; version=3'}


class Heroku:
    API = 'https://api.heroku.com'
    name = 'Heroku'

    async def whoami(self, token):
        account = await _get(f'{self.API}/account', token, headers=HEROKU)
        return account.get('name') or account.get('email') or 'Heroku'

    async def access(self, token):
        account = await _get(f'{self.API}/account', token, headers=HEROKU)
        return _access('heroku', [{'name': account.get('email') or 'Your account', 'detail': 'Account'}],
                       'Heroku API keys act as you on every app you can reach. Prefer an authorization limited to one app, with an expiry date. Heroku has no free dynos.',
                       'https://dashboard.heroku.com/account/applications')


PLATFORMS = {'render': Render(), 'vercel': Vercel(), 'cloudflare': Cloudflare(), 'netlify': Netlify(), 'koyeb': Koyeb(), 'railway': Railway(), 'heroku': Heroku()}

