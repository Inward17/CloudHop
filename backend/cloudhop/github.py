"""GitHub API, GitHub App tokens and the public-repo analyzer's file fetch."""
import asyncio
import base64
import json
import re
import time
from urllib.parse import quote

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import net
from .analyze import MAX_FILES, select_manifests
from .errors import RepoError
from .timeutil import now_ms, parse_date_ms


def parse_repo(text):
    """Accepting only owner/repo on github.com keeps this from fetching arbitrary URLs."""
    match = re.fullmatch(r'(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?', str(text if text is not None else '').strip(), re.ASCII)
    if not match or match.group(1).startswith('.') or match.group(2).startswith('.'):
        raise RepoError(400, 'Enter a GitHub repo, like owner/name or https://github.com/owner/name')
    return {'owner': match.group(1), 'repo': match.group(2)}


async def github_api(path, token=None, method='GET', body=None):
    headers = {'Accept': 'application/vnd.github+json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    response = await net.client.request(method, f'https://api.github.com{path}', headers=headers, json=body)
    if response.is_success:
        return None if response.status_code == 204 else response.json()
    if response.status_code == 401:
        raise RepoError(401, 'GitHub rejected the credentials. Reconnect the GitHub account.')
    if response.status_code == 404:
        raise RepoError(404, 'GitHub could not find that. Check the repo name, account access, and that the Cloudhop PR is merged.')
    if response.status_code in (403, 429):
        raise RepoError(429, 'GitHub refused this: rate limit reached or missing permission.')
    try:
        message = response.json().get('message')
    except ValueError:
        message = None
    raise RepoError(502, f'GitHub returned {response.status_code}' + (f': {message}' if message else ''))


# GitHub App tokens, the way Vercel and Render connect repos: nothing long-lived is stored,
# only the installation id. Each use gets a token that expires within an hour.

def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def app_jwt(app_id, private_key_pem, now=None):
    """RS256 JWT for the GitHub App. Takes the key as GitHub downloads it (PKCS#1) or as PKCS#8, with real
    line breaks or with backslash-n escapes (how a one-line .env entry holds it)."""
    pem = (private_key_pem or '').replace('\\n', '\n').strip()
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError) as error:
        raise RepoError(500, 'GITHUB_APP_PRIVATE_KEY is not a valid private key. See README.') from error
    seconds = int((now if now is not None else now_ms()) / 1000)
    header = _b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    payload = _b64url(json.dumps({'iat': seconds - 60, 'exp': seconds + 540, 'iss': str(app_id)}, separators=(',', ':')).encode())
    unsigned = f'{header}.{payload}'
    signature = key.sign(unsigned.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f'{unsigned}.{_b64url(signature)}'


_installation_tokens = {}  # installation id -> (token, expires ms)


async def installation_token(env, installation_id):
    cached = _installation_tokens.get(installation_id)
    if cached and cached[1] > now_ms() + 60_000:
        return cached[0]
    result = await github_api(f'/app/installations/{installation_id}/access_tokens', app_jwt(env['GITHUB_APP_ID'], env['GITHUB_APP_PRIVATE_KEY']), method='POST')
    _installation_tokens[installation_id] = (result['token'], parse_date_ms(result['expires_at']) or int(time.time() * 1000))
    return result['token']


async def installation_details(env, installation_id):
    """Owner, repo selection ("all" or "selected") and the permissions the person granted."""
    return await github_api(f'/app/installations/{installation_id}', app_jwt(env['GITHUB_APP_ID'], env['GITHUB_APP_PRIVATE_KEY']))


async def installation_owner(env, installation_id):
    return (await installation_details(env, installation_id))['account']['login']


async def fetch_manifests(repo, token=None):
    owner, name = repo['owner'], repo['repo']
    tree = await github_api(f'/repos/{owner}/{name}/git/trees/HEAD?recursive=1', token)
    chosen = select_manifests([entry for entry in tree['tree'] if entry['type'] == 'blob'])

    async def read(path):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        encoded = '/'.join(quote(part, safe="!~*'()") for part in path.split('/'))
        response = await net.client.get(f'https://raw.githubusercontent.com/{owner}/{name}/HEAD/{encoded}', headers=headers)
        return {'path': path, 'text': response.text} if response.is_success else None

    files = await asyncio.gather(*(read(entry['path']) for entry in chosen['selected']))
    warnings = []
    if tree.get('truncated'):
        warnings.append('Repo is very large, so GitHub returned only part of its file list.')
    if chosen['total'] > MAX_FILES:
        warnings.append(f"Read the {MAX_FILES} shallowest of {chosen['total']} manifest files.")
    if None in files:
        warnings.append('Some manifest files could not be downloaded.')
    return {'files': [file for file in files if file], 'warnings': warnings}
