"""Hosting platforms (Render, Vercel, Cloudflare): saved with an API token, checked against the platform,
and shown on the Access view. Deploying to them is not built yet; AWS and GCP are the deploy targets.

Each adapter: whoami(token) -> label, access(token) -> {kind: 'platform', ...}.
"""
import asyncio

from . import net
from .errors import RepoError


async def _get(url, token, missing_ok=False):
    response = await net.client.get(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
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


PLATFORMS = {'render': Render(), 'vercel': Vercel(), 'cloudflare': Cloudflare()}

