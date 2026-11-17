"""Platform hosting: find where a repo's site runs today, and move it to another platform.

A move is planned first (a dry run the user approves), then run step by step:
create the new project, copy settings and variables, deploy, check it, and stop for the user where needed:
secrets Cloudhop cannot read, the platform's own GitHub access, the domain (coming soon), and deletion
(never automatic: the user gets a link). Pausing the old site is offered where the platform supports it.

Built against the Vercel and Cloudflare Pages REST APIs; not yet run against real accounts.
"""
import asyncio
import re
from urllib.parse import quote, urlsplit

import httpx

from . import net
from .errors import RepoError

# Where a site can run. "move_to" marks targets Cloudhop can create today; the rest are coming soon.
PLATFORM_TARGETS = {
    'vercel': {'name': 'Vercel', 'account': 'vercel', 'move_to': True, 'pause': True},
    'cloudflare_pages': {'name': 'Cloudflare Pages', 'account': 'cloudflare', 'move_to': False, 'pause': False},
    'render': {'name': 'Render', 'account': 'render', 'move_to': False, 'pause': True},
    'netlify': {'name': 'Netlify', 'account': None, 'move_to': False, 'pause': False},
}
VERCEL_GIT_APP = 'https://github.com/apps/vercel/installations/new'


async def _call(method, url, token, body=None, ok_missing=False):
    try:
        response = await net.client.request(method, url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'}, json=body)
    except (httpx.TransportError, TimeoutError) as error:
        # Network trouble (timeouts, refused or dropped connections). The message names the host only, never the token.
        raise PlatformUnavailable(urlsplit(url).hostname) from error
    if response.is_success:
        return response.json() if response.content else {}
    if ok_missing and response.status_code == 404:
        return None
    try:
        detail = response.json()
    except ValueError:
        detail = {}
    error = detail.get('error') if isinstance(detail.get('error'), dict) else {}
    errors = detail.get('errors') if isinstance(detail.get('errors'), list) else []
    message = error.get('message') or (errors[0].get('message') if errors and isinstance(errors[0], dict) else None) or detail.get('message')
    code = error.get('code') or ''
    raise PlatformError(response.status_code, code, message or f'The platform returned {response.status_code}')


class PlatformError(RepoError):
    def __init__(self, status, code, message):
        super().__init__(502 if status >= 500 else 400, message)
        self.http_status = status
        self.code = code


def _same_repo(owner, name, repo):
    return f'{owner}/{name}'.lower() == repo.lower()


# Cloudflare Pages

class CloudflarePages:
    API = 'https://api.cloudflare.com/client/v4'

    async def find(self, token, repo):
        """Pages projects built from this GitHub repo, in every account the token reaches."""
        accounts = (await _call('GET', f'{self.API}/accounts?per_page=50', token)).get('result') or []
        found = []
        for account in accounts:
            projects = (await _call('GET', f"{self.API}/accounts/{account['id']}/pages/projects", token)).get('result') or []
            for project in projects:
                config = (project.get('source') or {}).get('config') or {}
                if (project.get('source') or {}).get('type') == 'github' and _same_repo(config.get('owner', ''), config.get('repo_name', ''), repo):
                    found.append(self.describe(account, project))
        return found

    def describe(self, account, project):
        build = project.get('build_config') or {}
        env = ((project.get('deployment_configs') or {}).get('production') or {}).get('env_vars') or {}
        variables = []
        for name, spec in env.items():
            spec = spec or {}
            secret = spec.get('type') == 'secret_text'
            # Secret values never come back from the API; plain ones do.
            variables.append({'name': name, 'secret': secret, 'value': None if secret else spec.get('value')})
        root = (build.get('root_dir') or '').strip('/') or None
        return {
            'platform': 'cloudflare_pages', 'id': project['name'], 'name': project['name'], 'scope': account['id'], 'scopeName': account.get('name'),
            'url': f"https://{project['subdomain']}" if project.get('subdomain') else None,
            'domains': [domain for domain in project.get('domains') or [] if not domain.endswith('.pages.dev')],
            'branch': ((project.get('source') or {}).get('config') or {}).get('production_branch') or project.get('production_branch'),
            'settings': {'buildCommand': build.get('build_command') or None, 'outputDirectory': build.get('destination_dir') or None, 'rootDirectory': root, 'framework': None},
            'variables': variables,
            'manageUrl': f"https://dash.cloudflare.com/{account['id']}/pages/view/{quote(project['name'])}/settings/production",
            'deleteUrl': f"https://dash.cloudflare.com/{account['id']}/pages/view/{quote(project['name'])}/settings/production",
        }

    async def pause(self, token, site):
        raise RepoError(400, 'Cloudflare Pages cannot pause a project. Delete it yourself from its settings page when you no longer need it.')


# Vercel

class Vercel:
    API = 'https://api.vercel.com'

    async def team(self, token):
        """teamId query for tokens limited to one team; empty for personal tokens."""
        meta = await _call('GET', f'{self.API}/v5/user/tokens/current', token, ok_missing=True) or {}
        teams = [scope.get('teamId') for scope in (meta.get('token') or {}).get('scopes', []) if scope.get('type') == 'team' and scope.get('teamId')]
        return f'teamId={teams[0]}' if len(teams) == 1 else ''

    def _q(self, team, *parts):
        query = '&'.join(part for part in (team, *parts) if part)
        return f'?{query}' if query else ''

    async def find(self, token, repo):
        team = await self.team(token)
        data = await _call('GET', f"{self.API}/v10/projects{self._q(team, 'repoUrl=' + quote(f'https://github.com/{repo}', safe=''))}", token)
        found = []
        for project in data.get('projects') or []:
            link = project.get('link') or {}
            if link.get('type') == 'github' and _same_repo(link.get('org', ''), link.get('repo', ''), repo):
                found.append(await self.describe(token, team, project))
        return found

    async def describe(self, token, team, project):
        env = await _call('GET', f"{self.API}/v9/projects/{project['id']}/env{self._q(team)}", token, ok_missing=True) or {}
        variables = []
        for item in env.get('envs') or []:
            if 'production' not in (item.get('target') or []):
                continue
            secret = item.get('type') in ('secret', 'sensitive', 'encrypted')
            variables.append({'name': item['key'], 'secret': secret, 'value': None if secret else item.get('value')})
        return {
            'platform': 'vercel', 'id': project['id'], 'name': project['name'], 'scope': team, 'scopeName': None,
            'url': f"https://{project['name']}.vercel.app",
            'domains': [],
            'branch': (project.get('link') or {}).get('productionBranch'),
            'settings': {'buildCommand': project.get('buildCommand'), 'outputDirectory': project.get('outputDirectory'), 'rootDirectory': project.get('rootDirectory'), 'framework': project.get('framework')},
            'variables': variables,
            'paused': bool(project.get('paused')),
            'manageUrl': f"https://vercel.com/{project.get('accountId', '')}/{project['name']}/settings",
            'deleteUrl': f"https://vercel.com/{project.get('accountId', '')}/{project['name']}/settings",
        }

    async def create(self, token, repo, name, settings, variables):
        """Creates a project linked to the GitHub repo. Fails with git_access when Vercel's GitHub app cannot see the repo."""
        team = await self.team(token)
        body = {
            'name': name,
            'gitRepository': {'type': 'github', 'repo': repo},
            'environmentVariables': [{'key': key, 'value': value, 'target': ['production', 'preview'], 'type': 'encrypted'} for key, value in variables.items()],
        }
        for field in ('framework', 'buildCommand', 'outputDirectory', 'rootDirectory'):
            if settings.get(field):
                body[field] = settings[field]
        try:
            project = await _call('POST', f'{self.API}/v11/projects{self._q(team)}', token, body)
        except PlatformError as error:
            if re.search(r'git|repository|install', str(error.message), re.IGNORECASE) or error.code in ('repo_not_found', 'missing_repo_permission', 'git_repository_not_found'):
                raise NeedsGitAccess(VERCEL_GIT_APP) from error
            raise
        return {'id': project['id'], 'name': project['name'], 'scope': team, 'url': f"https://{project['name']}.vercel.app"}

    async def env_names(self, token, site):
        env = await _call('GET', f"{self.API}/v9/projects/{site['id']}/env{self._q(site.get('scope', ''))}", token) or {}
        return {item['key'] for item in env.get('envs') or [] if 'production' in (item.get('target') or [])}

    async def deploy(self, token, site, repo, branch):
        owner, name = repo.split('/', 1)
        deployment = await _call('POST', f"{self.API}/v13/deployments{self._q(site.get('scope', ''))}", token, {
            'name': site['name'], 'project': site['id'], 'target': 'production',
            'gitSource': {'type': 'github', 'org': owner, 'repo': name, 'ref': branch or 'main'},
        })
        return deployment['id']

    async def deployment_state(self, token, site, deployment_id):
        data = await _call('GET', f"{self.API}/v13/deployments/{deployment_id}{self._q(site.get('scope', ''))}", token)
        return data.get('readyState') or data.get('status'), data.get('errorMessage')

    async def pause(self, token, site):
        await _call('POST', f"{self.API}/v1/projects/{site['id']}/pause{self._q(site.get('scope', ''))}", token, {})

    async def unpause(self, token, site):
        await _call('POST', f"{self.API}/v1/projects/{site['id']}/unpause{self._q(site.get('scope', ''))}", token, {})

    # Domains (coming soon in the interface; logic kept here so it can be tested and switched on later).
    async def add_domain(self, token, site, domain):
        data = await _call('POST', f"{self.API}/v10/projects/{site['id']}/domains{self._q(site.get('scope', ''))}", token, {'name': domain})
        return {'verified': bool(data.get('verified')), 'verification': data.get('verification') or []}


class PlatformUnavailable(RepoError):
    def __init__(self, host):
        super().__init__(502, f'Could not reach {host}. Check that it is up, then continue the move.')


class NeedsGitAccess(RepoError):
    def __init__(self, link):
        super().__init__(400, 'Vercel cannot see this repo yet. Install Vercel\'s GitHub app on it, then retry.')
        self.link = link


SITES = {'cloudflare_pages': CloudflarePages(), 'vercel': Vercel()}


def dns_records_for(platform, domain):
    """The DNS record a domain needs to point at a platform. Apex domains get an A record, others a CNAME."""
    apex = domain.count('.') == 1
    if platform == 'vercel':
        return [{'type': 'A', 'name': '@', 'content': '76.76.21.21'}] if apex else [{'type': 'CNAME', 'name': domain.split('.')[0], 'content': 'cname.vercel-dns.com'}]
    raise RepoError(400, 'Moving a domain to this platform is not supported yet')


async def cloudflare_dns_plan(token, domain, records):
    """What changing DNS on Cloudflare would do: which existing records get replaced. Changes nothing."""
    api = CloudflarePages.API
    zone_name = '.'.join(domain.split('.')[-2:])
    zones = (await _call('GET', f'{api}/zones?name={quote(zone_name)}', token)).get('result') or []
    if not zones:
        return {'managedHere': False, 'records': records, 'note': f'{zone_name} is not on Cloudflare DNS. Change the record where the domain is registered.'}
    zone = zones[0]
    existing = (await _call('GET', f"{api}/zones/{zone['id']}/dns_records?name={quote(domain)}", token)).get('result') or []
    return {'managedHere': True, 'zone': zone['id'], 'replace': [{'id': item['id'], 'type': item['type'], 'content': item.get('content')} for item in existing if item['type'] in ('A', 'AAAA', 'CNAME')], 'records': records}


def move_plan(repo, source, target_platform, stored_secret_names):
    """The dry run the user approves: what is copied, what is missing, and which steps need them."""
    target = PLATFORM_TARGETS[target_platform]
    variables = (source or {}).get('variables') or []
    plain = [item['name'] for item in variables if not item['secret'] and item.get('value') is not None]
    secrets = [item['name'] for item in variables if item['secret'] or item.get('value') is None]
    missing = [name for name in secrets if name not in stored_secret_names]
    settings = (source or {}).get('settings') or {}
    steps = [
        {'id': 'create', 'title': f"Create the {target['name']} project, linked to {repo}", 'who': 'agent'},
        {'id': 'variables', 'title': f"Copy {len(plain)} setting{'s' if len(plain) != 1 else ''} and add {len(secrets)} secret{'s' if len(secrets) != 1 else ''}", 'who': 'agent' if not missing else 'you or agent'},
        {'id': 'deploy', 'title': f"Build and deploy the {source.get('branch') or 'default' if source else 'default'} branch", 'who': 'agent'},
        {'id': 'verify', 'title': 'Check the new address loads' + (' and matches the current site' if source and source.get('url') else ''), 'who': 'agent'},
        {'id': 'domain', 'title': 'Move the domain', 'who': 'you approve', 'comingSoon': True},
        {'id': 'old', 'title': 'Pause or delete the old site', 'who': 'you choose', 'optional': True},
    ]
    return {
        'repo': repo,
        'from': {key: source[key] for key in ('platform', 'name', 'url', 'domains', 'manageUrl')} if source else None,
        'to': target_platform,
        'settings': {key: value for key, value in settings.items() if value},
        'copy': plain,
        'secrets': secrets,
        'missingSecrets': missing,
        'gitAccess': f"{target['name']} needs its GitHub app on {repo} to build from it and deploy on every push. If it is missing, Cloudhop shows the link and waits.",
        'steps': steps,
        'keepsOldSite': 'The current site stays live until you pause it. Cloudhop never deletes it; you get the link to do that yourself.',
    }


async def wait_for_deploy(adapter, token, site, deployment_id, attempts=60, delay=5):
    for _ in range(attempts):
        state, message = await adapter.deployment_state(token, site, deployment_id)
        if state in ('READY', 'ERROR', 'CANCELED'):
            return state, message
        await asyncio.sleep(delay)
    return 'TIMEOUT', 'The build did not finish in time. Check it on the platform.'


async def check_site(url, compare_with=None):
    """The new address must answer without a server error; if the old site is known, titles should match."""
    try:
        response = await net.client.get(url, follow_redirects=True, timeout=15)
    except Exception as error:  # noqa: BLE001 - any network failure means the site is not up
        return {'ok': False, 'detail': f'{url} did not answer ({type(error).__name__}).'}
    if response.status_code >= 500:
        return {'ok': False, 'detail': f'{url} answered {response.status_code}.'}
    result = {'ok': True, 'detail': f'{url} answered {response.status_code}.'}
    if compare_with:
        def title(text):
            match = re.search(r'<title[^>]*>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)
            return match.group(1).strip() if match else None
        try:
            old = await net.client.get(compare_with, follow_redirects=True, timeout=15)
            same = title(old.text) == title(response.text)
            result['detail'] += ' Page title matches the current site.' if same else ' Page title differs from the current site; check it.'
            result['matches'] = same
        except Exception:  # noqa: BLE001
            result['detail'] += ' The current site could not be loaded to compare.'
    return result
