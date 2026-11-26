"""Platform hosting: find where a repo's site runs today, and move it to another platform.

A move is planned first (a dry run the user approves), then run step by step:
create the new project, copy settings and variables, deploy, check it, and stop for the user where needed:
secrets Cloudhop cannot read, the platform's own GitHub access, the domain (coming soon), and deletion
(never automatic: the user gets a link). Pausing the old site is offered where the platform supports it.

Built against the Vercel and Cloudflare Pages REST APIs; not yet run against real accounts.

Each target adapter: create(token, repo, name, settings, variables, branch) -> site, env_names(token, site),
deploy(token, site, repo, branch) -> deployment id, deployment_state(token, site, id) -> (READY|ERROR|CANCELED|other, message),
settings_url(site). Platforms that can pause also have pause/unpause.
"""
import asyncio
import json
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from . import net
from .errors import RepoError

# Where a site can run. "move_to" marks targets Cloudhop can create today; the rest are coming soon.
PLATFORM_TARGETS = {
    'vercel': {'name': 'Vercel', 'account': 'vercel', 'move_to': True, 'pause': True},
    'cloudflare_pages': {'name': 'Cloudflare Pages', 'account': 'cloudflare', 'move_to': True, 'pause': False},
    'render': {'name': 'Render', 'account': 'render', 'move_to': True, 'pause': True},
    'netlify': {'name': 'Netlify', 'account': 'netlify', 'move_to': True, 'pause': True},
    'koyeb': {'name': 'Koyeb', 'account': 'koyeb', 'move_to': True, 'pause': True},
    'railway': {'name': 'Railway', 'account': 'railway', 'move_to': True, 'pause': True},
    'heroku': {'name': 'Heroku', 'account': 'heroku', 'move_to': True, 'pause': True},
    # Fly.io machines start again on the next request (auto start), so a stop is not a pause.
    'flyio': {'name': 'Fly.io', 'account': 'flyio', 'move_to': True, 'pause': False},
    'northflank': {'name': 'Northflank', 'account': 'northflank', 'move_to': True, 'pause': True},
    'do_apps': {'name': 'DigitalOcean App Platform', 'account': 'do_apps', 'move_to': True, 'pause': True},
    # GitHub Pages uses the project's own GitHub account. Turning Pages off deletes the site, so there is no pause.
    'github_pages': {'name': 'GitHub Pages', 'account': None, 'move_to': True, 'pause': False},
    'coolify': {'name': 'Coolify', 'account': 'coolify', 'move_to': True, 'pause': True},
}
# Platforms that run the app as a web service rather than serving built files.
SERVER_TARGETS = {'koyeb', 'railway', 'heroku', 'northflank'}
# Platforms that deploy from a workflow Cloudhop adds to the repo, rather than from their own GitHub link.
PIPELINE_TARGETS = {'flyio', 'github_pages'}
VERCEL_GIT_APP = 'https://github.com/apps/vercel/installations/new'
CLOUDFLARE_GIT_APP = 'https://github.com/apps/cloudflare-workers-and-pages/installations/new'
RENDER_GIT_APP = 'https://github.com/apps/render/installations/new'
NETLIFY_GIT_APP = 'https://app.netlify.com/start'
KOYEB_GIT_APP = 'https://app.koyeb.com/settings/integrations'
RAILWAY_GIT_APP = 'https://railway.com/account/integrations'
NORTHFLANK_GIT_APP = 'https://app.northflank.com/s/account/integrations/vcs'
DO_GIT_APP = 'https://cloud.digitalocean.com/apps/github/install'
# Words in a platform's error that mean it cannot reach the repo. Not "authoriz": that also matches "Unauthorized",
# which is a token problem, not a missing GitHub connection.
GIT_HINT = re.compile(r'git|repositor|install|integration', re.IGNORECASE)
# What to do on each platform when it cannot see the repo. The platform's own account must be connected to the GitHub
# account that owns the repo (emails may differ); installing the app from GitHub alone does not link it.
GIT_STEPS = {
    'Cloudflare Pages': 'In the Cloudflare dashboard, open Workers & Pages, Create, Pages, Connect to Git, and connect the GitHub account that owns this repo. Give it access to the repo, then cancel the setup there and continue here.',
    'Vercel': "Install Vercel's GitHub app on this repo from your Vercel account (Settings, Git), signed in to GitHub as the repo's owner. Then continue here.",
    'Render': 'In the Render dashboard, open Account Settings, Git Deployment, and connect the GitHub account that owns this repo with access to it. Then continue here.',
    'Netlify': 'In Netlify, choose Add new project, Import an existing project, GitHub, and give Netlify access to this repo as its owner. Cancel the setup there, then continue here.',
    'Koyeb': "In Koyeb, open Settings, Integrations, and install Koyeb's GitHub app on this repo as its owner. Then continue here.",
    'Railway': "In Railway, open Account Settings, Integrations, and connect GitHub so Railway's app can see this repo. Then continue here.",
    'Northflank': 'In Northflank, open Account settings, Git, and link the GitHub account that owns this repo, with access to it. Then continue here.',
    'DigitalOcean App Platform': "Install DigitalOcean's GitHub app on this repo (the link opens it), signed in to GitHub as the repo's owner. Then continue here.",
    'GitHub Pages': "Cloudhop's GitHub access cannot turn on Pages for this repo. In the repo, open Settings, Pages, and set Source to GitHub Actions. Then continue here.",
    'Coolify': 'In Coolify, open Sources and add a GitHub App with access to this repo, or make the repo public. Then continue here.',
}


async def _call(method, url, token, body=None, ok_missing=False, form=None, headers=None):
    # form: multipart fields, for endpoints that take form data instead of JSON. headers: extra or replacement headers.
    files = {key: (None, value) for key, value in form.items()} if form else None
    try:
        response = await net.client.request(method, url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json', **(headers or {})}, json=body, files=files)
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

    # ponytail: uses the first Cloudflare account the token reaches. Add an account choice to the move plan when
    # users hold tokens for several accounts.
    async def account(self, token):
        accounts = (await _call('GET', f'{self.API}/accounts?per_page=50', token)).get('result') or []
        if not accounts:
            raise RepoError(400, 'The Cloudflare token reaches no account.')
        return accounts[0]['id']

    async def create(self, token, repo, name, settings, variables, branch=None):
        """A Pages project linked to the GitHub repo, building on pushes to the production branch. Values are stored
        as secrets, so none can be read back. Fails with git access when Cloudflare's GitHub app cannot see the repo."""
        account = await self.account(token)
        owner, repo_name = repo.split('/', 1)
        branch = branch or 'main'
        build = {key: value for key, value in (('build_command', settings.get('buildCommand')), ('destination_dir', settings.get('outputDirectory')),
                                                ('root_dir', settings.get('rootDirectory'))) if value}
        env_vars = {key: {'type': 'secret_text', 'value': value} for key, value in variables.items()}
        body = {
            'name': name, 'production_branch': branch, 'build_config': build,
            'source': {'type': 'github', 'config': {
                'owner': owner, 'repo_name': repo_name, 'production_branch': branch, 'deployments_enabled': True,
                'production_deployments_enabled': True, 'pr_comments_enabled': False, 'preview_deployment_setting': 'none'}},
            'deployment_configs': {'production': {'env_vars': env_vars}},
        }
        try:
            project = (await _call('POST', f'{self.API}/accounts/{account}/pages/projects', token, body)).get('result') or {}
        except PlatformError as error:
            if re.search(r'already (exists|taken|in use)', str(error.message), re.IGNORECASE):
                raise PlatformError(409, error.code, error.message) from error
            if GIT_HINT.search(str(error.message)):
                raise NeedsGitAccess(CLOUDFLARE_GIT_APP, 'Cloudflare Pages', error.message) from error
            raise
        subdomain = project.get('subdomain') or f'{name}.pages.dev'
        project_name = project.get('name') or name
        manage = f'https://dash.cloudflare.com/{account}/pages/view/{quote(project_name)}/settings/production'
        return {'id': project_name, 'name': project_name, 'scope': account, 'url': f'https://{subdomain}', 'manageUrl': manage, 'deleteUrl': manage}

    async def env_names(self, token, site):
        project = (await _call('GET', f"{self.API}/accounts/{site['scope']}/pages/projects/{quote(site['name'])}", token)).get('result') or {}
        return set((((project.get('deployment_configs') or {}).get('production') or {}).get('env_vars') or {}).keys())

    async def deploy(self, token, site, repo, branch, **_):
        result = await _call('POST', f"{self.API}/accounts/{site['scope']}/pages/projects/{quote(site['name'])}/deployments", token,
                             form={'branch': branch or 'main'})
        return (result.get('result') or {})['id']

    async def deployment_state(self, token, site, deployment_id):
        result = await _call('GET', f"{self.API}/accounts/{site['scope']}/pages/projects/{quote(site['name'])}/deployments/{quote(deployment_id)}", token)
        stage = (result.get('result') or {}).get('latest_stage') or {}
        status, name = stage.get('status'), stage.get('name')
        if status == 'failure':
            return 'ERROR', f"The {name or 'build'} stage failed. Check the deployment on Cloudflare."
        if status == 'canceled':
            return 'CANCELED', None
        if name == 'deploy' and status == 'success':
            return 'READY', None
        return 'BUILDING', None

    def settings_url(self, site):
        return f"https://dash.cloudflare.com/{site['scope']}/pages/view/{quote(site['name'])}/settings/environment-variables"


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

    async def create(self, token, repo, name, settings, variables, branch=None):
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
                raise NeedsGitAccess(VERCEL_GIT_APP, 'Vercel', error.message) from error
            raise
        return {'id': project['id'], 'name': project['name'], 'scope': team, 'url': f"https://{project['name']}.vercel.app"}

    async def env_names(self, token, site):
        env = await _call('GET', f"{self.API}/v9/projects/{site['id']}/env{self._q(site.get('scope', ''))}", token) or {}
        return {item['key'] for item in env.get('envs') or [] if 'production' in (item.get('target') or [])}

    async def deploy(self, token, site, repo, branch, **_):
        owner, name = repo.split('/', 1)
        deployment = await _call('POST', f"{self.API}/v13/deployments{self._q(site.get('scope', ''))}", token, {
            'name': site['name'], 'project': site['id'], 'target': 'production',
            'gitSource': {'type': 'github', 'org': owner, 'repo': name, 'ref': branch or 'main'},
        })
        return deployment['id']

    async def deployment_state(self, token, site, deployment_id):
        data = await _call('GET', f"{self.API}/v13/deployments/{deployment_id}{self._q(site.get('scope', ''))}", token)
        return data.get('readyState') or data.get('status'), data.get('errorMessage')

    def settings_url(self, site):
        return f"https://vercel.com/dashboard?project={quote(site['name'])}"

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


# Render

class Render:
    """Render services built from a GitHub repo. Moves to Render create a static site, which is free and fits sites
    coming from Vercel or Cloudflare Pages. Render returns variable values, so nothing has to be re-entered."""
    API = 'https://api.render.com/v1'
    # ponytail: build settings Render needs when the source site left them to the framework (Vercel often does).
    # Shown in the plan; the user can change them on Render.
    DEFAULT_BUILD = 'npm install && npm run build'
    DEFAULT_PUBLISH = 'dist'

    async def _pages(self, token, path, key):
        """Every item of a cursor-paged list endpoint, unwrapped from its {cursor, <key>} wrapper (at most 2,000)."""
        items, cursor = [], None
        for _ in range(20):
            separator = '&' if '?' in path else '?'
            page = await _call('GET', f"{self.API}{path}{separator}limit=100{f'&cursor={quote(cursor)}' if cursor else ''}", token) or []
            items += [entry[key] for entry in page if isinstance(entry, dict) and key in entry]
            if len(page) < 100:
                break
            cursor = page[-1].get('cursor')
        return items

    # ponytail: uses the first workspace the key reaches, like Cloudflare's first account. Add a workspace choice to the
    # move plan when users hold keys for several workspaces.
    async def owner(self, token):
        owners = await self._pages(token, '/owners', 'owner')
        if not owners:
            raise RepoError(400, 'The Render key reaches no workspace.')
        return owners[0]['id']

    async def find(self, token, repo):
        """Services built from this GitHub repo, in every workspace the key reaches."""
        found = []
        for service in await self._pages(token, '/services', 'service'):
            if _same_repo(*(_repo_parts(service.get('repo')) or ('', '')), repo):
                found.append(await self.describe(token, service))
        return found

    async def describe(self, token, service):
        details = service.get('serviceDetails') or {}
        native = details.get('envSpecificDetails') or {}
        env = await self._pages(token, f"/services/{service['id']}/env-vars", 'envVar')
        static = service.get('type') == 'static_site'
        return {
            'platform': 'render', 'id': service['id'], 'name': service.get('name') or service['id'], 'scope': service.get('ownerId'), 'scopeName': None,
            'url': details.get('url'), 'domains': [],
            'branch': service.get('branch'),
            'kind': 'static' if static else 'server',
            'settings': {'buildCommand': details.get('buildCommand') or native.get('buildCommand'), 'outputDirectory': details.get('publishPath') if static else None,
                         'rootDirectory': (service.get('rootDir') or '').strip('/') or None, 'framework': None},
            # Render returns values, so variables copy across without asking for them again.
            'variables': [{'name': item['key'], 'secret': False, 'value': item.get('value')} for item in env if item.get('key')],
            'paused': service.get('suspended') == 'suspended',
            'manageUrl': service.get('dashboardUrl') or 'https://dashboard.render.com',
            'deleteUrl': f"{service['dashboardUrl']}/settings" if service.get('dashboardUrl') else 'https://dashboard.render.com',
        }

    async def create(self, token, repo, name, settings, variables, branch=None):
        """A static site linked to the repo, deploying on pushes to the branch. Render starts the first deploy itself."""
        owner = await self.owner(token)
        body = {
            'type': 'static_site', 'name': name, 'ownerId': owner, 'repo': f'https://github.com/{repo}', 'branch': branch or 'main', 'autoDeploy': 'yes',
            **({'rootDir': settings['rootDirectory']} if settings.get('rootDirectory') else {}),
            'envVars': [{'key': key, 'value': value} for key, value in variables.items()],
            'serviceDetails': {'buildCommand': self.build_command(settings.get('buildCommand')), 'publishPath': settings.get('outputDirectory') or self.DEFAULT_PUBLISH},
        }
        try:
            created = await _call('POST', f'{self.API}/services', token, body) or {}
        except PlatformError as error:
            if GIT_HINT.search(str(error.message)) or 'github' in str(error.message).lower():
                raise NeedsGitAccess(RENDER_GIT_APP, 'Render', error.message) from error
            raise
        service = created.get('service') or {}
        dashboard = service.get('dashboardUrl') or 'https://dashboard.render.com'
        return {'id': service['id'], 'name': service.get('name') or name, 'scope': owner, 'url': (service.get('serviceDetails') or {}).get('url'),
                'firstDeploy': created.get('deployId'), 'manageUrl': dashboard, 'deleteUrl': f'{dashboard}/settings'}

    def build_command(self, command):
        """Render static sites do not install dependencies on their own (Vercel and Cloudflare Pages do), so an npm or
        yarn build that skips the install gets one in front."""
        command = (command or '').strip() or self.DEFAULT_BUILD
        if not re.search(r'\binstall\b|\bnpm ci\b|\bpnpm i\b', command) and command.split()[0] in ('npm', 'yarn', 'pnpm', 'npx', 'vite', 'astro', 'next'):
            installer = 'yarn install' if command.startswith('yarn') else 'pnpm install' if command.startswith('pnpm') else 'npm install'
            command = f'{installer} && {command}'
        return command

    async def env_names(self, token, site):
        return {item['key'] for item in await self._pages(token, f"/services/{site['id']}/env-vars", 'envVar') if item.get('key')}

    async def deploy(self, token, site, repo, branch, **_):
        # Creating the site already started a deploy; follow that one instead of queueing a second.
        # Taken once: a retry after a failed first deploy starts a fresh one.
        first = site.pop('firstDeploy', None)
        if first:
            return first
        deployment = await _call('POST', f"{self.API}/services/{site['id']}/deploys", token, {'clearCache': 'do_not_clear'}) or {}
        return deployment['id']

    async def deployment_state(self, token, site, deployment_id):
        deployment = await _call('GET', f"{self.API}/services/{site['id']}/deploys/{quote(deployment_id)}", token) or {}
        status = deployment.get('status')
        if status == 'live':
            return 'READY', None
        if status in ('build_failed', 'update_failed', 'pre_deploy_failed'):
            return 'ERROR', f"The Render deploy ended as {status.replace('_', ' ')}. Check its logs on Render."
        if status in ('canceled', 'deactivated'):
            return 'CANCELED', None
        return 'BUILDING', None

    def settings_url(self, site):
        return site.get('manageUrl') or 'https://dashboard.render.com'

    async def pause(self, token, site):
        await _call('POST', f"{self.API}/services/{site['id']}/suspend", token, {})

    async def unpause(self, token, site):
        await _call('POST', f"{self.API}/services/{site['id']}/resume", token, {})


def _repo_parts(url):
    """owner, name from a GitHub URL as Render stores it, or None for other hosts."""
    match = re.fullmatch(r'https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?', str(url or ''))
    return match.groups() if match else None


class NeedsGitAccess(RepoError):
    def __init__(self, link, platform, said=None):
        message = f"{platform} cannot see this repo yet. {GIT_STEPS.get(platform, 'Install its GitHub app on the repo, then continue.')}"
        if said:
            message += f' ({platform} said: {str(said)[:200]})'
        super().__init__(400, message)
        self.link = link


# Netlify

class Netlify:
    """Git-linked static sites. Netlify keeps secret values to itself, so they are asked for like Vercel's.

    Not yet checked against a real account: the build trigger's response, and whether creating a Git-linked site needs
    the id of Netlify's GitHub app installation (the API reference does not say how to find it)."""
    API = 'https://api.netlify.com/api/v1'

    async def accounts(self, token):
        return await _call('GET', f'{self.API}/accounts', token) or []

    async def find(self, token, repo):
        found = []
        for page in range(1, 11):
            sites = await _call('GET', f'{self.API}/sites?filter=all&per_page=100&page={page}', token) or []
            for site in sites:
                build = site.get('build_settings') or {}
                if str(build.get('repo_path') or '').lower() == repo.lower() and build.get('provider') in (None, 'github'):
                    found.append(await self.describe(token, site))
            if len(sites) < 100:
                break
        return found

    async def describe(self, token, site):
        build = site.get('build_settings') or {}
        env = await _call('GET', f"{self.API}/accounts/{site.get('account_id')}/env?site_id={site['id']}", token, ok_missing=True) or []
        variables = []
        for item in env:
            values = item.get('values') or []
            value = next((entry.get('value') for entry in values if entry.get('context') in ('all', 'production')), None)
            secret = bool(item.get('is_secret'))
            if not secret and value is None:
                continue  # Only set for previews, branch deploys or local dev: production never sees it.
            variables.append({'name': item['key'], 'secret': secret, 'value': None if secret else value})
        return {
            'platform': 'netlify', 'id': site['id'], 'name': site['name'], 'scope': site.get('account_slug'), 'scopeName': site.get('account_name'),
            'url': site.get('ssl_url') or site.get('url'), 'domains': [domain for domain in [site.get('custom_domain')] if domain],
            'branch': build.get('repo_branch'), 'kind': 'static',
            'settings': {'buildCommand': build.get('cmd') or None, 'outputDirectory': build.get('dir') or None, 'rootDirectory': (build.get('base') or '').strip('/') or None, 'framework': None},
            'variables': variables, 'accountId': site.get('account_id'),
            'manageUrl': site.get('admin_url') or 'https://app.netlify.com', 'deleteUrl': f"{site.get('admin_url') or 'https://app.netlify.com'}/configuration/general",
        }

    async def create(self, token, repo, name, settings, variables, branch=None):
        accounts = await self.accounts(token)
        if not accounts:
            raise RepoError(400, 'The Netlify token reaches no team.')
        team = accounts[0]  # ponytail: first team the token reaches, like Cloudflare's first account
        repo_info = {'provider': 'github', 'repo_path': repo, 'repo_branch': branch or 'main',
                     **({'cmd': settings['buildCommand']} if settings.get('buildCommand') else {}),
                     **({'dir': settings['outputDirectory']} if settings.get('outputDirectory') else {}),
                     **({'base': settings['rootDirectory']} if settings.get('rootDirectory') else {})}
        try:
            site = await _call('POST', f"{self.API}/{team['slug']}/sites", token, {'name': name, 'repo': repo_info})
        except PlatformError as error:
            if re.search(r'unique|taken|already', str(error.message), re.IGNORECASE):
                raise PlatformError(409, error.code, error.message) from error
            if GIT_HINT.search(str(error.message)) or 'github' in str(error.message).lower():
                raise NeedsGitAccess(NETLIFY_GIT_APP, 'Netlify', error.message) from error
            raise
        if variables:
            await _call('POST', f"{self.API}/accounts/{team['id']}/env?site_id={site['id']}", token,
                        [{'key': key, 'values': [{'value': value, 'context': 'all'}], 'is_secret': False} for key, value in variables.items()])
        admin = site.get('admin_url') or 'https://app.netlify.com'
        return {'id': site['id'], 'name': site.get('name') or name, 'scope': team.get('slug'), 'accountId': team['id'], 'url': site.get('ssl_url') or site.get('url'),
                'manageUrl': admin, 'deleteUrl': f'{admin}/configuration/general'}

    async def env_names(self, token, site):
        env = await _call('GET', f"{self.API}/accounts/{site['accountId']}/env?site_id={site['id']}", token) or []
        return {item['key'] for item in env if item.get('key')}

    async def deploy(self, token, site, repo, branch, **_):
        build = await _call('POST', f"{self.API}/sites/{site['id']}/builds", token, {}) or {}
        return build.get('deploy_id') or build['id']

    async def deployment_state(self, token, site, deployment_id):
        deploy = await _call('GET', f'{self.API}/deploys/{quote(deployment_id)}', token) or {}
        state = deploy.get('state')
        if state == 'ready':
            return 'READY', None
        if state == 'error':
            return 'ERROR', deploy.get('error_message') or 'The Netlify build failed. Check its log on Netlify.'
        if state == 'rejected':
            return 'CANCELED', None
        return 'BUILDING', None

    def settings_url(self, site):
        return f"{site.get('manageUrl') or 'https://app.netlify.com'}/configuration/env"

    async def pause(self, token, site):
        # Disabling stops the site serving; it can be enabled again. Netlify has no lighter pause.
        await _call('PUT', f"{self.API}/sites/{site['id']}/disable?reason={quote('Paused by Cloudhop after a move')}", token, {})

    async def unpause(self, token, site):
        await _call('PUT', f"{self.API}/sites/{site['id']}/enable", token, {})


# Koyeb

class Koyeb:
    """A web service built from the repo with buildpacks, on Koyeb's free instance. Shapes follow Koyeb's generated
    API client (koyeb-api-client-go); not yet checked against a real account."""
    API = 'https://app.koyeb.com/v1'
    PORT = 8000  # Koyeb sets PORT for buildpack apps; this is the port it routes to.

    async def find(self, token, repo):
        found = []
        services = (await _call('GET', f'{self.API}/services?limit=100', token) or {}).get('services') or []
        for service in services:
            deployment_id = service.get('active_deployment_id') or service.get('latest_deployment_id')
            if not deployment_id:
                continue
            deployment = (await _call('GET', f'{self.API}/deployments/{deployment_id}', token, ok_missing=True) or {}).get('deployment') or {}
            git = (deployment.get('definition') or {}).get('git') or {}
            if str(git.get('repository') or '').lower().removeprefix('github.com/') == repo.lower():
                found.append(await self.describe(token, service, deployment))
        return found

    async def describe(self, token, service, deployment):
        definition = deployment.get('definition') or {}
        git = definition.get('git') or {}
        app = (await _call('GET', f"{self.API}/apps/{service['app_id']}", token, ok_missing=True) or {}).get('app') or {}
        domain = next((item.get('name') for item in app.get('domains') or [] if item.get('name')), None)
        return {
            'platform': 'koyeb', 'id': service['id'], 'name': service.get('name') or service['id'], 'scope': service.get('app_id'), 'scopeName': app.get('name'),
            'url': f'https://{domain}' if domain else None, 'domains': [], 'branch': git.get('branch'), 'kind': 'server',
            'settings': {'buildCommand': git.get('build_command') or None, 'outputDirectory': None, 'rootDirectory': (git.get('workdir') or '').strip('/') or None, 'framework': None},
            'variables': [{'name': item['key'], 'secret': bool(item.get('secret')), 'value': None if item.get('secret') else item.get('value')}
                          for item in definition.get('env') or [] if item.get('key')],
            'paused': service.get('status') in ('PAUSED', 'PAUSING'),
            'manageUrl': f"https://app.koyeb.com/services/{service['id']}", 'deleteUrl': f"https://app.koyeb.com/services/{service['id']}/settings",
        }

    async def create(self, token, repo, name, settings, variables, branch=None):
        try:
            app = (await _call('POST', f'{self.API}/apps', token, {'name': name}) or {}).get('app') or {}
        except PlatformError as error:
            if re.search(r'already|exists|taken|unique', str(error.message), re.IGNORECASE):
                raise PlatformError(409, error.code, error.message) from error
            raise
        git = {'repository': f'github.com/{repo}', 'branch': branch or 'main',
               **({'workdir': settings['rootDirectory']} if settings.get('rootDirectory') else {}),
               **({'build_command': settings['buildCommand']} if settings.get('buildCommand') else {})}
        definition = {'name': name, 'type': 'WEB', 'git': git, 'instance_types': [{'type': 'free'}], 'regions': ['was'],
                      'scalings': [{'min': 1, 'max': 1}], 'ports': [{'port': self.PORT, 'protocol': 'http'}], 'routes': [{'port': self.PORT, 'path': '/'}],
                      'env': [{'key': 'PORT', 'value': str(self.PORT)}, *({'key': key, 'value': value} for key, value in variables.items())]}
        try:
            service = (await _call('POST', f'{self.API}/services', token, {'app_id': app['id'], 'name': name, 'definition': definition}) or {}).get('service') or {}
        except PlatformError as error:
            if GIT_HINT.search(str(error.message)) or 'github' in str(error.message).lower():
                raise NeedsGitAccess(KOYEB_GIT_APP, 'Koyeb', error.message) from error
            raise
        domain = next((item.get('name') for item in app.get('domains') or [] if item.get('name')), None)
        manage = f"https://app.koyeb.com/services/{service['id']}"
        return {'id': service['id'], 'name': name, 'scope': app['id'], 'url': f'https://{domain}' if domain else None,
                'firstDeploy': service.get('latest_deployment_id'), 'manageUrl': manage, 'deleteUrl': f'{manage}/settings'}

    async def env_names(self, token, site):
        service = (await _call('GET', f"{self.API}/services/{site['id']}", token) or {}).get('service') or {}
        deployment_id = service.get('latest_deployment_id')
        deployment = (await _call('GET', f'{self.API}/deployments/{deployment_id}', token) or {}).get('deployment') or {} if deployment_id else {}
        return {item['key'] for item in (deployment.get('definition') or {}).get('env') or [] if item.get('key')}

    async def deploy(self, token, site, repo, branch, **_):
        first = site.pop('firstDeploy', None)  # Creating the service started a deploy; follow it once.
        if first:
            return first
        await _call('POST', f"{self.API}/services/{site['id']}/redeploy", token, {})
        service = (await _call('GET', f"{self.API}/services/{site['id']}", token) or {}).get('service') or {}
        return service['latest_deployment_id']

    async def deployment_state(self, token, site, deployment_id):
        deployment = (await _call('GET', f'{self.API}/deployments/{quote(deployment_id)}', token) or {}).get('deployment') or {}
        status = deployment.get('status')
        if status == 'HEALTHY':
            return 'READY', None
        if status in ('ERROR', 'ERRORING', 'UNHEALTHY'):
            return 'ERROR', f"The Koyeb deployment ended as {status.lower()}. Check its logs on Koyeb."
        if status in ('CANCELED', 'CANCELING', 'STASHED', 'STOPPED'):
            return 'CANCELED', None
        return 'BUILDING', None

    def settings_url(self, site):
        return f"{site.get('manageUrl') or 'https://app.koyeb.com'}/settings"

    async def pause(self, token, site):
        await _call('POST', f"{self.API}/services/{site['id']}/pause", token, {})

    async def unpause(self, token, site):
        await _call('POST', f"{self.API}/services/{site['id']}/resume", token, {})


# Railway

class Railway:
    """A service built from the repo in a new Railway project. Railway's API does not say which repo an existing
    service builds from, so Cloudhop cannot find sites there, only move to it. Not yet checked against a real account."""
    API = 'https://backboard.railway.com/graphql/v2'

    async def gql(self, token, query, variables=None):
        data = await _call('POST', self.API, token, {'query': query, 'variables': variables or {}}) or {}
        if data.get('errors'):  # Railway reports errors with HTTP 200.
            message = str(data['errors'][0].get('message') or 'Railway refused the request')
            raise PlatformError(409 if re.search(r'already|taken|exists', message, re.IGNORECASE) else 400, '', message)
        return data.get('data') or {}

    async def find(self, token, repo):
        return []

    # Railway projects cannot be found by repo, so a retry must reuse the project an earlier attempt made.
    resumable = True

    async def create(self, token, repo, name, settings, variables, branch=None, resume=None, progress=None):
        if resume and resume.get('projectId'):
            project, environment = {'id': resume['projectId']}, {'id': resume['environmentId']}
        else:
            project = (await self.gql(token, 'mutation($input: ProjectCreateInput!) { projectCreate(input: $input) { id } }', {'input': {'name': name}}))['projectCreate']
            environments = (await self.gql(token, 'query($id: String!) { project(id: $id) { environments { edges { node { id name } } } } }', {'id': project['id']}))['project']
            edges = environments['environments']['edges']
            environment = next((edge['node'] for edge in edges if edge['node']['name'] == 'production'), edges[0]['node'])
            if progress:
                progress({'projectId': project['id'], 'environmentId': environment['id']})
        try:
            service = (await self.gql(token, 'mutation($input: ServiceCreateInput!) { serviceCreate(input: $input) { id } }', {'input': {
                'projectId': project['id'], 'name': name, 'source': {'repo': repo}, 'branch': branch or 'main', 'variables': variables or {}}}))['serviceCreate']
        except PlatformError as error:
            if GIT_HINT.search(str(error.message)) or 'github' in str(error.message).lower():
                raise NeedsGitAccess(RAILWAY_GIT_APP, 'Railway', error.message) from error
            raise
        update = {key: settings[field] for key, field in (('rootDirectory', 'rootDirectory'), ('buildCommand', 'buildCommand')) if settings.get(field)}
        if update:
            await self.gql(token, 'mutation($serviceId: String!, $environmentId: String!, $input: ServiceInstanceUpdateInput!) { serviceInstanceUpdate(serviceId: $serviceId, environmentId: $environmentId, input: $input) }',
                           {'serviceId': service['id'], 'environmentId': environment['id'], 'input': update})
        domain = (await self.gql(token, 'mutation($input: ServiceDomainCreateInput!) { serviceDomainCreate(input: $input) { domain } }',
                                 {'input': {'serviceId': service['id'], 'environmentId': environment['id']}}))['serviceDomainCreate']['domain']
        manage = f"https://railway.com/project/{project['id']}/service/{service['id']}"
        # Connecting the repo starts a build; firstDeploy marks it to be followed (cleared when secrets are added by hand).
        return {'id': service['id'], 'name': name, 'scope': project['id'], 'environmentId': environment['id'], 'url': f'https://{domain}',
                'manageUrl': manage, 'deleteUrl': f'{manage}/settings', 'firstDeploy': True}

    async def env_names(self, token, site):
        values = (await self.gql(token, 'query($p: String!, $e: String!, $s: String) { variables(projectId: $p, environmentId: $e, serviceId: $s) }',
                                 {'p': site['scope'], 'e': site['environmentId'], 's': site['id']}))['variables'] or {}
        return set(values)

    async def latest_deployment(self, token, site):
        data = await self.gql(token, 'query($input: DeploymentListInput!) { deployments(input: $input, first: 1) { edges { node { id status } } } }',
                              {'input': {'projectId': site['scope'], 'serviceId': site['id'], 'environmentId': site['environmentId']}})
        edges = data['deployments']['edges']
        return edges[0]['node'] if edges else None

    async def deploy(self, token, site, repo, branch, **_):
        # Follow the build Railway started on create, once. Later deploys are started explicitly.
        if site.pop('firstDeploy', None):
            latest = await self.latest_deployment(token, site)
            if latest:
                return latest['id']
        data = await self.gql(token, 'mutation($s: String!, $e: String!) { serviceInstanceDeployV2(serviceId: $s, environmentId: $e) }',
                              {'s': site['id'], 'e': site['environmentId']})
        return data['serviceInstanceDeployV2']

    async def deployment_state(self, token, site, deployment_id):
        status = ((await self.gql(token, 'query($id: String!) { deployment(id: $id) { status } }', {'id': deployment_id})).get('deployment') or {}).get('status')
        if status == 'SUCCESS':
            return 'READY', None
        if status in ('FAILED', 'CRASHED'):
            return 'ERROR', f"The Railway deployment ended as {status.lower()}. Check its logs on Railway."
        if status in ('REMOVED', 'SKIPPED'):
            return 'CANCELED', None
        return 'BUILDING', None

    def settings_url(self, site):
        return f"{site.get('manageUrl') or 'https://railway.com/dashboard'}/variables"

    async def pause(self, token, site):
        latest = await self.latest_deployment(token, site)
        if not latest:
            raise RepoError(400, 'Nothing is deployed on Railway to stop.')
        await self.gql(token, 'mutation($id: String!) { deploymentStop(id: $id) }', {'id': latest['id']})

    async def unpause(self, token, site):
        latest = await self.latest_deployment(token, site)
        if latest:
            await self.gql(token, 'mutation($id: String!) { deploymentRestart(id: $id) }', {'id': latest['id']})


# Heroku

HEROKU_HEADERS = {'Accept': 'application/vnd.heroku+json; version=3'}


class Heroku:
    """A Heroku app built from a source archive of the repo. Heroku's API keeps no GitHub link, so Cloudhop cannot
    find apps there and hands Heroku a short-lived archive link from the code host for each build. Paid dynos only.
    Not yet checked against a real account."""
    API = 'https://api.heroku.com'
    needs_archive = True

    async def find(self, token, repo):
        return []

    async def create(self, token, repo, name, settings, variables, branch=None):
        try:
            app = await _call('POST', f'{self.API}/apps', token, {'name': name}, headers=HEROKU_HEADERS) or {}
        except PlatformError as error:
            if re.search(r'taken|already|exists', str(error.message), re.IGNORECASE):
                raise PlatformError(409, error.code, error.message) from error
            raise
        if variables:
            await _call('PATCH', f"{self.API}/apps/{app['name']}/config-vars", token, dict(variables), headers=HEROKU_HEADERS)
        manage = f"https://dashboard.heroku.com/apps/{app['name']}"
        return {'id': app['name'], 'name': app['name'], 'scope': app.get('id'), 'url': (app.get('web_url') or '').rstrip('/') or None,
                'manageUrl': manage, 'deleteUrl': f'{manage}/settings'}

    async def env_names(self, token, site):
        return set(await _call('GET', f"{self.API}/apps/{site['id']}/config-vars", token, headers=HEROKU_HEADERS) or {})

    async def deploy(self, token, site, repo, branch, archive=None, **_):
        if not archive:
            raise RepoError(400, 'Heroku builds need a source archive from the code host, and none was available.')
        build = await _call('POST', f"{self.API}/apps/{site['id']}/builds", token, {'source_blob': {'url': archive, 'version': branch or 'main'}}, headers=HEROKU_HEADERS) or {}
        return build['id']

    async def deployment_state(self, token, site, deployment_id):
        build = await _call('GET', f"{self.API}/apps/{site['id']}/builds/{quote(deployment_id)}", token, headers=HEROKU_HEADERS) or {}
        status = build.get('status')
        if status == 'succeeded':
            return 'READY', None
        if status == 'failed':
            return 'ERROR', 'The Heroku build failed. Check its build log on Heroku.'
        return 'BUILDING', None

    def settings_url(self, site):
        return f"{site.get('manageUrl') or 'https://dashboard.heroku.com/apps'}/settings"

    async def pause(self, token, site):
        # Maintenance mode keeps dynos running and billing, so pausing scales every process type to zero. The counts
        # are kept on the site (saved with it), so resuming brings back the same formation.
        formation = await _call('GET', f"{self.API}/apps/{site['id']}/formation", token, headers=HEROKU_HEADERS) or []
        running = {item['type']: item['quantity'] for item in formation if item.get('quantity')}
        if running:
            site['formation'] = running
            await _call('PATCH', f"{self.API}/apps/{site['id']}/formation", token, {'updates': [{'type': kind, 'quantity': 0} for kind in running]}, headers=HEROKU_HEADERS)

    async def unpause(self, token, site):
        counts = site.pop('formation', None) or {'web': 1}
        await _call('PATCH', f"{self.API}/apps/{site['id']}/formation", token, {'updates': [{'type': kind, 'quantity': count} for kind, count in counts.items()]}, headers=HEROKU_HEADERS)


# Fly.io

FLY_GRAPHQL = 'https://api.fly.io/graphql'
# setup-flyctl pinned to a commit (v1.6) and flyctl to a version, so a moved tag cannot change what runs with the token.
FLY_ACTION = 'superfly/flyctl-actions/setup-flyctl@ed8efb33836e8b2096c7fd3ba1c8afe303ebbff1 # v1.6'
FLYCTL_VERSION = '0.5.16'
CHECKOUT_ACTION = 'actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0'


def fly_headers(token):
    # Tokens from `fly tokens create` start with "FlyV1 " and are sent as they are; older personal tokens use Bearer.
    return {'Authorization': token} if token.startswith('FlyV1 ') else {}


class FlyIO:
    """A Fly.io app deployed by a GitHub Actions workflow in the user's repo. Fly's API cannot build from GitHub, so
    Cloudhop creates the app, opens a pull request with fly.toml, the workflow and (if missing) a Dockerfile, and the
    user adds an app-scoped deploy token as the repo secret FLY_API_TOKEN. Cloudhop never holds that deploy token.
    Fly apps keep no repo link, so there is no find. Not yet checked against a real account."""
    API = 'https://api.machines.dev/v1'
    WORKFLOW = '.github/workflows/cloudhop-fly.yml'
    needs_pipeline = True
    deploy_attempts = 180  # Remote Docker builds take longer than platform builds: wait up to 15 minutes.
    timeout_message = ('Fly.io did not start the app in time. Open the "Cloudhop Fly deploy" run in GitHub Actions: '
                       'the repo secret FLY_API_TOKEN must hold a deploy token for this app.')

    async def gql(self, token, query, variables=None):
        data = await _call('POST', FLY_GRAPHQL, token, {'query': query, 'variables': variables or {}}, headers=fly_headers(token)) or {}
        if data.get('errors'):
            message = data['errors'][0].get('message') if isinstance(data['errors'][0], dict) else None
            raise PlatformError(400, 'GRAPHQL', message or 'Fly.io refused the request.')
        return data.get('data') or {}

    async def find(self, token, repo):
        return []

    # Fly apps cannot be found by repo, so a retry must reuse the app an earlier attempt made.
    resumable = True

    async def create(self, token, repo, name, settings, variables, branch=None, resume=None, progress=None):
        if resume and resume.get('org'):
            org = resume['org']
        else:
            orgs = ((await self.gql(token, '{ organizations { nodes { slug } } }')).get('organizations') or {}).get('nodes') or []
            org = orgs[0]['slug'] if orgs else 'personal'
            try:
                await _call('POST', f'{self.API}/apps', token, {'app_name': name, 'org_slug': org}, headers=fly_headers(token))
            except PlatformError as error:
                # Fly answers 422 for a taken name (and other validation errors); only a name clash is renamed.
                if error.http_status == 422 and re.search(r'taken|already|exists|unique', str(error.message), re.IGNORECASE):
                    raise PlatformError(409, error.code, error.message) from error
                raise
            if progress:
                progress({'org': org})
        if variables:
            await self.gql(token, 'mutation($input: SetSecretsInput!) { setSecrets(input: $input) { app { name } } }',
                           {'input': {'appId': name, 'secrets': [{'key': key, 'value': value} for key, value in variables.items()]}})
        manage = f'https://fly.io/apps/{name}'
        return {'id': name, 'name': name, 'scope': org, 'url': f'https://{name}.fly.dev', 'manageUrl': manage, 'deleteUrl': f'{manage}/settings',
                'rootDirectory': settings.get('rootDirectory')}

    async def env_names(self, token, site):
        app = (await self.gql(token, 'query($name: String!) { app(name: $name) { secrets { name } } }', {'name': site['id']})).get('app') or {}
        return {item['name'] for item in app.get('secrets') or []}

    GENERATED = '# Generated by Cloudhop for Fly.io.'

    def repo_files(self, site, settings, branch, dockerfile=None, build_args=None, secrets=()):
        """What the pull request adds: fly.toml and the workflow, plus a Dockerfile that builds the static site and
        serves it with nginx when the repo has none. An existing Dockerfile is used as it is, on its EXPOSE port.
        Fly secrets reach only the running app, so copied plain settings go to the build as build arguments
        ([build.args] in fly.toml, declared with ARG in the generated Dockerfile). They are committed, so never secrets."""
        root = (settings.get('rootDirectory') or '').strip('/')
        prefix = f'{root}/' if root else ''
        port = 80
        files = []
        args = {key: value for key, value in (build_args or {}).items() if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)}
        if dockerfile:
            exposed = re.search(r'^\s*EXPOSE\s+(\d+)', dockerfile, re.IGNORECASE | re.MULTILINE)
            port = int(exposed.group(1)) if exposed else 8080
        else:
            build = settings.get('buildCommand') or 'npm run build'
            output = (settings.get('outputDirectory') or 'dist').strip('/')
            files.append({'path': f'{prefix}Dockerfile', 'content': (
                f'{self.GENERATED} Check the build command and output folder before merging.\n'
                'FROM node:22-slim AS build\nWORKDIR /app\n' + ''.join(f'ARG {key}\n' for key in args) +
                f'COPY . .\nRUN npm ci || npm install\nRUN {build}\n\n'
                f'FROM nginx:alpine\nCOPY --from=build /app/{output} /usr/share/nginx/html\nEXPOSE 80\n')})
        files.append({'path': f'{prefix}fly.toml', 'content': (
            '# Generated by Cloudhop.\n'
            f"app = '{site['id']}'\nprimary_region = 'iad'\n\n"
            '[http_service]\n'
            f'  internal_port = {port}\n  force_https = true\n  auto_stop_machines = \'stop\'\n'
            '  auto_start_machines = true\n  min_machines_running = 0\n'
            # JSON strings are valid TOML basic strings.
            + ('\n[build.args]\n' + ''.join(f'  {key} = {json.dumps(str(value))}\n' for key, value in args.items()) if args else ''))})
        files.append({'path': self.WORKFLOW, 'content': (
            '# Generated by Cloudhop: deploys to Fly.io on every push to the branch, and when Cloudhop starts it.\n'
            'name: Cloudhop Fly deploy\n'
            f"on:\n  push:\n    branches: ['{branch}']\n  workflow_dispatch:\n"
            'permissions:\n  contents: read\n'
            'concurrency: cloudhop-fly\n'
            'jobs:\n  deploy:\n    runs-on: ubuntu-latest\n    steps:\n'
            f'      - uses: {CHECKOUT_ACTION}\n'
            f'      - uses: {FLY_ACTION}\n        with:\n          version: {FLYCTL_VERSION}\n'
            '      - run: flyctl deploy --remote-only\n'
            + (f'        working-directory: {root}\n' if root else '')
            + '        env:\n          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}\n')})
        return files

    async def deploy(self, token, site, repo, branch, **_):
        # The workflow was started by Cloudhop just before this; machines updated after now belong to that deploy.
        return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    async def deployment_state(self, token, site, deployment_id):
        machines = await _call('GET', f"{self.API}/apps/{site['id']}/machines", token, headers=fly_headers(token)) or []
        fresh = [machine for machine in machines if (machine.get('updated_at') or '') >= deployment_id]
        if any(machine.get('state') == 'started' for machine in fresh):
            return 'READY', None
        if fresh and all(machine.get('state') in ('failed', 'launch_failed') for machine in fresh):
            return 'ERROR', 'The new Fly.io machine failed to start. Check its logs on Fly.io.'
        return 'BUILDING', None

    def settings_url(self, site):
        return f"{site.get('manageUrl') or 'https://fly.io/dashboard'}/secrets"

    # What ensure_pipeline needs: where the config lives, whether the branch already deploys this app, and what to say.
    uses_dockerfile = True

    def config_path(self, prefix):
        return f'{prefix}fly.toml'

    def pipeline_current(self, site, workflow, config):
        return bool(workflow and config and f"app = '{site['id']}'" in config)

    def pr_note(self, site, branch):
        return f'Before merging, add the repo secret `FLY_API_TOKEN` with a deploy token for this app only: `fly tokens create deploy -a {site["id"]}`.'

    def waiting_note(self, site, branch):
        return (f"Merge the pull request into {branch}, and add the repo secret FLY_API_TOKEN with a deploy token for this app "
                f"(fly tokens create deploy -a {site['id']}). Then continue.")


# Northflank

class Northflank:
    """A combined service (build and run) from the repo in a new Northflank project, built with buildpacks on the
    smallest plan. Shapes follow Northflank's API reference; not yet checked against a real account. The plan id of
    the free sandbox (nf-compute-10) is from memory."""
    API = 'https://api.northflank.com/v1'
    PORT = 8080
    PLAN = 'nf-compute-10'
    REGION = 'europe-west'

    async def listing(self, token, path, key):
        items, cursor = [], None
        for _ in range(20):
            data = await _call('GET', f'{self.API}{path}?per_page=100' + (f'&cursor={quote(cursor)}' if cursor else ''), token) or {}
            items += (data.get('data') or {}).get(key) or []
            page = data.get('pagination') or {}
            if not (page.get('hasNextPage') and page.get('cursor')):
                return items
            cursor = page['cursor']
        return items

    async def find(self, token, repo):
        found = []
        for project in await self.listing(token, '/projects', 'projects'):
            for service in await self.listing(token, f"/projects/{project['id']}/services", 'services'):
                if service.get('serviceType') != 'combined':
                    continue
                detail = (await _call('GET', f"{self.API}/projects/{project['id']}/services/{service['id']}", token, ok_missing=True) or {}).get('data') or {}
                parts = _repo_parts((detail.get('vcsData') or {}).get('projectUrl'))
                if parts and _same_repo(*parts, repo):
                    found.append(await self.describe(token, project, detail))
        return found

    async def public_url(self, token, project_id, service_id):
        ports = ((await _call('GET', f'{self.API}/projects/{project_id}/services/{service_id}/ports', token, ok_missing=True) or {}).get('data') or {}).get('ports') or []
        dns = next((port.get('dns') for port in ports if port.get('public') and port.get('dns')), None)
        return f'https://{dns}' if dns else None

    async def describe(self, token, project, service):
        env = ((await _call('GET', f"{self.API}/projects/{project['id']}/services/{service['id']}/runtime-environment/details", token, ok_missing=True) or {}).get('data') or {}).get('runtimeEnvironment') or {}
        build = (service.get('buildSettings') or {}).get('buildpack') or (service.get('buildSettings') or {}).get('dockerfile') or {}
        manage = f"https://app.northflank.com/s/project/{project['id']}/services/{service['id']}"
        return {
            'platform': 'northflank', 'id': service['id'], 'name': service.get('name') or service['id'], 'scope': project['id'], 'scopeName': project.get('name'),
            'url': await self.public_url(token, project['id'], service['id']), 'domains': [], 'branch': (service.get('vcsData') or {}).get('projectBranch'), 'kind': 'server',
            'settings': {'buildCommand': None, 'outputDirectory': None, 'rootDirectory': (build.get('buildContext') or build.get('dockerWorkDir') or '').strip('/') or None, 'framework': None},
            # Northflank keeps every variable encrypted as a secret, so none is copied as plain text: they are asked for.
            'variables': [{'name': name, 'secret': True, 'value': None} for name in env],
            'paused': bool((service.get('status') or {}).get('paused')),
            'manageUrl': manage, 'deleteUrl': f'{manage}/settings',
        }

    # A new project is made first; a retry reuses it instead of making a second one.
    resumable = True

    async def create(self, token, repo, name, settings, variables, branch=None, resume=None, progress=None):
        if resume and resume.get('projectId'):
            project_id = resume['projectId']
        else:
            try:
                project_id = ((await _call('POST', f'{self.API}/projects', token, {'name': name[:39], 'region': self.REGION}) or {}).get('data') or {})['id']
            except PlatformError as error:
                if error.http_status == 409 or re.search(r'already|exists|taken', str(error.message), re.IGNORECASE):
                    raise PlatformError(409, error.code, error.message) from error
                raise
            if progress:
                progress({'projectId': project_id})
        root = (settings.get('rootDirectory') or '').strip('/')
        body = {
            'name': name[:39], 'billing': {'deploymentPlan': self.PLAN}, 'deployment': {'instances': 1},
            'vcsData': {'projectUrl': f'https://github.com/{repo}', 'projectType': 'github', 'projectBranch': branch or 'main'},
            'buildSettings': {'buildpack': {'buildContext': f'/{root}' if root else '/'}},
            'ports': [{'name': 'p01', 'internalPort': self.PORT, 'public': True, 'protocol': 'HTTP'}],
            'runtimeEnvironment': {'PORT': str(self.PORT), **variables},
        }
        try:
            service = (await _call('POST', f'{self.API}/projects/{project_id}/services/combined', token, body) or {}).get('data') or {}
        except PlatformError as error:
            if GIT_HINT.search(str(error.message)) or 'github' in str(error.message).lower():
                raise NeedsGitAccess(NORTHFLANK_GIT_APP, 'Northflank', error.message) from error
            raise
        manage = f"https://app.northflank.com/s/project/{project_id}/services/{service['id']}"
        # Creating the service starts its first build; firstDeploy marks it to be followed once.
        return {'id': service['id'], 'name': name, 'scope': project_id, 'url': await self.public_url(token, project_id, service['id']),
                'firstDeploy': True, 'manageUrl': manage, 'deleteUrl': f'{manage}/settings'}

    async def env_names(self, token, site):
        env = ((await _call('GET', f"{self.API}/projects/{site['scope']}/services/{site['id']}/runtime-environment/details", token) or {}).get('data') or {}).get('runtimeEnvironment') or {}
        return set(env)

    async def deploy(self, token, site, repo, branch, **_):
        if site.pop('firstDeploy', None):
            return 'first'
        build = (await _call('POST', f"{self.API}/projects/{site['scope']}/services/{site['id']}/build", token, {}) or {}).get('data') or {}
        return build['id']

    async def deployment_state(self, token, site, deployment_id):
        service = (await _call('GET', f"{self.API}/projects/{site['scope']}/services/{site['id']}", token) or {}).get('data') or {}
        status = service.get('status') or {}
        if deployment_id == 'first':
            build = (status.get('build') or {}).get('status')
        else:
            build = ((await _call('GET', f"{self.API}/projects/{site['scope']}/services/{site['id']}/build/{quote(deployment_id)}", token) or {}).get('data') or {}).get('status')
        if build in ('FAILURE', 'SUBMISSION_FAILURE', 'CRASHED', 'UNSCHEDULABLE'):
            return 'ERROR', f'The Northflank build ended as {build.lower()}. Check its build log on Northflank.'
        if build == 'ABORTED':
            return 'CANCELED', None
        deployment = (status.get('deployment') or {}).get('status')
        if build == 'SUCCESS' and deployment == 'FAILED':
            return 'ERROR', 'The Northflank service failed to start. Check its logs on Northflank.'
        if build == 'SUCCESS' and deployment == 'COMPLETED':
            site['url'] = site.get('url') or await self.public_url(token, site['scope'], site['id'])
            return 'READY', None
        return 'BUILDING', None

    def settings_url(self, site):
        return f"{site.get('manageUrl') or 'https://app.northflank.com'}/environment"

    async def pause(self, token, site):
        await _call('POST', f"{self.API}/projects/{site['scope']}/services/{site['id']}/pause", token, {})

    async def unpause(self, token, site):
        await _call('POST', f"{self.API}/projects/{site['scope']}/services/{site['id']}/resume", token, {})


# DigitalOcean App Platform

class DigitalOceanApps:
    """A static site on DigitalOcean App Platform (free for static sites), built from the repo by DigitalOcean's
    GitHub app. Shapes follow DigitalOcean's OpenAPI spec; not yet checked against a real account."""
    API = 'https://api.digitalocean.com/v2'
    REGION = 'nyc'

    async def find(self, token, repo):
        found = []
        for page in range(1, 11):
            data = await _call('GET', f'{self.API}/apps?per_page=100&page={page}', token) or {}
            for app in data.get('apps') or []:
                component = self.component(app.get('spec') or {}, repo)
                if component:
                    found.append(self.describe(app, *component))
            if not ((data.get('links') or {}).get('pages') or {}).get('next'):
                break
        return found

    @staticmethod
    def component(spec, repo):
        """The first static site or service in an app spec that builds from this repo, and which kind it is."""
        for key, kind in (('static_sites', 'static'), ('services', 'server')):
            for item in spec.get(key) or []:
                if str((item.get('github') or {}).get('repo') or '').lower() == repo.lower():
                    return item, kind
        return None

    def describe(self, app, item, kind):
        spec = app.get('spec') or {}
        manage = f"https://cloud.digitalocean.com/apps/{app['id']}"
        envs = [*(spec.get('envs') or []), *(item.get('envs') or [])]
        return {
            'platform': 'do_apps', 'id': app['id'], 'name': spec.get('name') or app['id'], 'scope': None,
            'url': app.get('live_url') or (f"https://{app['default_ingress'].removeprefix('https://')}" if app.get('default_ingress') else None),
            'domains': [domain['domain'] for domain in spec.get('domains') or [] if domain.get('domain')], 'branch': (item.get('github') or {}).get('branch'), 'kind': kind,
            'settings': {'buildCommand': item.get('build_command') or None, 'outputDirectory': item.get('output_dir') or None,
                         'rootDirectory': (item.get('source_dir') or '').strip('/') or None, 'framework': None},
            # Encrypted values come back encrypted (EV[...]), so those are asked for.
            'variables': [{'name': env['key'], 'secret': env.get('type') == 'SECRET', 'value': None if env.get('type') == 'SECRET' else env.get('value')} for env in envs if env.get('key')],
            'paused': bool((spec.get('maintenance') or {}).get('archive') or (spec.get('maintenance') or {}).get('enabled')),
            'manageUrl': manage, 'deleteUrl': f'{manage}/settings',
        }

    async def create(self, token, repo, name, settings, variables, branch=None):
        site = {'name': name[:32], 'github': {'repo': repo, 'branch': branch or 'main', 'deploy_on_push': True},
                **({'build_command': settings['buildCommand']} if settings.get('buildCommand') else {}),
                **({'output_dir': settings['outputDirectory']} if settings.get('outputDirectory') else {}),
                **({'source_dir': settings['rootDirectory']} if settings.get('rootDirectory') else {}),
                # A static site only sees variables while it builds. Every value is stored encrypted.
                'envs': [{'key': key, 'value': value, 'scope': 'BUILD_TIME', 'type': 'SECRET'} for key, value in variables.items()]}
        try:
            app = (await _call('POST', f'{self.API}/apps', token, {'spec': {'name': name[:32], 'region': self.REGION, 'static_sites': [site]}}) or {}).get('app') or {}
        except PlatformError as error:
            if re.search(r'already|exists|taken|unique', str(error.message), re.IGNORECASE):
                raise PlatformError(409, error.code, error.message) from error
            if GIT_HINT.search(str(error.message)) or 'github' in str(error.message).lower():
                raise NeedsGitAccess(DO_GIT_APP, 'DigitalOcean App Platform', error.message) from error
            raise
        manage = f"https://cloud.digitalocean.com/apps/{app['id']}"
        return {'id': app['id'], 'name': name[:32], 'scope': None, 'url': app.get('live_url') or None,
                'firstDeploy': (app.get('pending_deployment') or {}).get('id'), 'manageUrl': manage, 'deleteUrl': f'{manage}/settings'}

    async def app(self, token, site):
        return (await _call('GET', f"{self.API}/apps/{site['id']}", token) or {}).get('app') or {}

    async def env_names(self, token, site):
        spec = (await self.app(token, site)).get('spec') or {}
        return {env['key'] for item in [spec, *(spec.get('static_sites') or []), *(spec.get('services') or [])] for env in item.get('envs') or [] if env.get('key')}

    async def deploy(self, token, site, repo, branch, **_):
        first = site.pop('firstDeploy', None)  # Creating the app started a deployment; follow it once.
        if first:
            return first
        return ((await _call('POST', f"{self.API}/apps/{site['id']}/deployments", token, {'force_build': True}) or {}).get('deployment') or {})['id']

    async def deployment_state(self, token, site, deployment_id):
        deployment = (await _call('GET', f"{self.API}/apps/{site['id']}/deployments/{quote(deployment_id)}", token) or {}).get('deployment') or {}
        phase = deployment.get('phase')
        if phase == 'ACTIVE':
            # The address is only known once the first deployment is live.
            site['url'] = site.get('url') or (await self.app(token, site)).get('live_url')
            return 'READY', None
        if phase == 'ERROR':
            return 'ERROR', 'The DigitalOcean build failed. Check the deployment log on DigitalOcean.'
        if phase in ('CANCELED', 'SUPERSEDED'):
            return 'CANCELED', None
        return 'BUILDING', None

    def settings_url(self, site):
        return f"{site.get('manageUrl') or 'https://cloud.digitalocean.com/apps'}/settings"

    async def set_archived(self, token, site, archived):
        spec = (await self.app(token, site)).get('spec') or {}
        await _call('PUT', f"{self.API}/apps/{site['id']}", token, {'spec': {**spec, 'maintenance': {**(spec.get('maintenance') or {}), 'enabled': archived, 'archive': archived}}})

    async def pause(self, token, site):
        # Archiving stops the app serving and billing, and keeps its settings so it can be restored.
        await self.set_archived(token, site, True)

    async def unpause(self, token, site):
        await self.set_archived(token, site, False)


# GitHub Pages

PAGES_ACTIONS = {
    'setup-node': 'actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0',
    'upload': 'actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9 # v5.0.0',
    'deploy': 'actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346 # v5.0.1',
}


class GitHubPages:
    """A static site on GitHub Pages, built and published by a GitHub Actions workflow Cloudhop opens a pull request
    for. Uses the project's own GitHub access (no platform token), so `token` here is that GitHub token. Cloudhop
    cannot write Actions secrets, so secrets are added by the user in the repo. There is no find: a site already on
    Pages is recorded by hand. Not yet checked against a real repo."""
    API = 'https://api.github.com'
    WORKFLOW = '.github/workflows/cloudhop-pages.yml'
    GENERATED = '# Generated by Cloudhop for GitHub Pages.'
    needs_pipeline = True
    uses_code_host = True
    secrets_by_hand = True
    uses_dockerfile = False
    deploy_attempts = 120
    timeout_message = 'GitHub Pages did not publish in time. Open the "Cloudhop Pages deploy" run in GitHub Actions to see why.'

    async def find(self, token, repo):
        return []

    @staticmethod
    def address(repo):
        owner, name = repo.split('/', 1)
        host = f'{owner.lower()}.github.io'
        return f'https://{host}/' if name.lower() == host else f'https://{host}/{name}/'

    # Asking the user to turn Pages on is recorded, so the retry after they did it does not ask again.
    resumable = True

    async def create(self, token, repo, name, settings, variables, branch=None, resume=None, progress=None):
        manage = f'https://github.com/{repo}/settings/pages'
        site = {'id': repo, 'name': repo.split('/', 1)[1], 'scope': repo.split('/', 1)[0], 'url': self.address(repo), 'manageUrl': manage, 'deleteUrl': manage,
                'rootDirectory': settings.get('rootDirectory')}
        if resume and resume.get('askedToEnable'):
            return site  # The user turned Pages on by hand; the deploy shows whether that worked.
        try:
            await _call('POST', f'{self.API}/repos/{repo}/pages', token, {'build_type': 'workflow'})
        except PlatformError as error:
            if error.http_status == 409:  # Pages is already on, maybe from a branch: switch it to the workflow.
                await _call('PUT', f'{self.API}/repos/{repo}/pages', token, {'build_type': 'workflow'})
            elif error.http_status in (403, 404):
                if progress:
                    progress({'askedToEnable': True})
                raise NeedsGitAccess(manage, 'GitHub Pages', error.message) from error
            else:
                raise
        pages = await _call('GET', f'{self.API}/repos/{repo}/pages', token, ok_missing=True) or {}
        return {**site, 'url': pages.get('html_url') or site['url']}

    async def env_names(self, token, site):
        names = set()
        for kind in ('secrets', 'variables'):
            data = await _call('GET', f"{self.API}/repos/{site['id']}/actions/{kind}?per_page=100", token, ok_missing=True) or {}
            names |= {item['name'] for item in data.get(kind) or [] if item.get('name')}
        return names

    def config_path(self, prefix):
        return None

    def pipeline_current(self, site, workflow, config):
        return bool(workflow and workflow.startswith(self.GENERATED))

    def pr_note(self, site, branch):
        return f'Pages serves the site at {site["url"]}. Add any secrets the build needs as Actions secrets in this repo before merging.'

    def waiting_note(self, site, branch):
        return f'Merge the pull request into {branch}, and add any secrets the build needs as Actions secrets in the repo. Then continue.'

    def repo_files(self, site, settings, branch, dockerfile=None, build_args=None, secrets=()):
        """The workflow: build the site (when it has a build command) and publish the output folder to Pages. Copied
        plain settings are written into the workflow; secrets are read from Actions secrets, never written."""
        root = (settings.get('rootDirectory') or '').strip('/')
        output = (settings.get('outputDirectory') or ('.' if not settings.get('buildCommand') else 'dist')).strip('/') or '.'
        folder = f'{root}/{output}' if root and output != '.' else (root or output)
        names = [key for key in (build_args or {}) if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)]
        env = ''.join(f'          {key}: {json.dumps(str(build_args[key]))}\n' for key in names) + \
            ''.join(f'          {key}: ${{{{ secrets.{key} }}}}\n' for key in secrets if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key))
        workdir = f'        working-directory: {root}\n' if root else ''
        build = ''
        if settings.get('buildCommand'):
            build = (f"      - uses: {PAGES_ACTIONS['setup-node']}\n        with:\n          node-version: 22\n"
                     f'      - run: npm ci || npm install\n{workdir}'
                     f"      - run: {settings['buildCommand']}\n{workdir}" + (f'        env:\n{env}' if env else ''))
        return [{'path': self.WORKFLOW, 'content': (
            f'{self.GENERATED} Publishes the site on every push to the branch, and when Cloudhop starts it.\n'
            'name: Cloudhop Pages deploy\n'
            f"on:\n  push:\n    branches: ['{branch}']\n  workflow_dispatch:\n"
            'permissions:\n  contents: read\n  pages: write\n  id-token: write\n'
            'concurrency:\n  group: cloudhop-pages\n  cancel-in-progress: false\n'
            'jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n'
            f'      - uses: {CHECKOUT_ACTION}\n' + build +
            f"      - uses: {PAGES_ACTIONS['upload']}\n        with:\n          path: {folder}\n"
            '  deploy:\n    needs: build\n    runs-on: ubuntu-latest\n'
            '    environment:\n      name: github-pages\n      url: ${{ steps.deployment.outputs.page_url }}\n'
            f"    steps:\n      - id: deployment\n        uses: {PAGES_ACTIONS['deploy']}\n")}]

    async def deploy(self, token, site, repo, branch, **_):
        # The run was started just before this, so runs created in the last two minutes count as this deploy.
        # ponytail: a push-started run in that window also counts; it publishes the same branch.
        return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 120))

    async def deployment_state(self, token, site, deployment_id):
        workflow = self.WORKFLOW.rsplit('/', 1)[-1]
        runs = (await _call('GET', f"{self.API}/repos/{site['id']}/actions/workflows/{workflow}/runs?per_page=5", token) or {}).get('workflow_runs') or []
        run = next((item for item in runs if (item.get('created_at') or '') >= deployment_id), None)
        if not run or run.get('status') != 'completed':
            return 'BUILDING', None
        if run.get('conclusion') == 'success':
            return 'READY', None
        if run.get('conclusion') == 'cancelled':
            return 'CANCELED', None
        return 'ERROR', f"The GitHub Pages run ended as {run.get('conclusion')}. Open it in GitHub Actions: {run.get('html_url') or ''}".strip()

    def settings_url(self, site):
        return f"https://github.com/{site['id']}/settings/secrets/actions"


# Coolify

class Coolify:
    """An app on the user's own Coolify (cloud or self-hosted), built with Nixpacks and served as static files.
    The saved secret holds the instance address and its API token. Every call checks the address is a public
    internet host, since the user chooses it. Shapes follow Coolify's OpenAPI file; not yet checked against a real instance."""

    @staticmethod
    def parse(token):
        saved = json.loads(token)
        return saved['url'].rstrip('/'), saved['token']

    async def call(self, method, token, path, body=None, ok_missing=False):
        base, secret = self.parse(token)
        url = f'{base}/api/v1{path}'
        try:
            await net.check_public(url)
        except net.UnsafeAddress as error:
            raise RepoError(400, 'The Coolify address must be a public https address.') from error
        return await _call(method, url, secret, body, ok_missing=ok_missing)

    @staticmethod
    def same_repo(url, repo):
        text = str(url or '').lower().removesuffix('.git').removeprefix('https://').removeprefix('github.com/').removeprefix('git@github.com:')
        return text == repo.lower()

    async def find(self, token, repo):
        apps = await self.call('GET', token, '/applications') or []
        return [await self.describe(token, app) for app in apps if self.same_repo(app.get('git_repository'), repo)]

    def manage(self, token, uuid):
        return f'{self.parse(token)[0]}/project/application/{uuid}'

    async def describe(self, token, app):
        envs = await self.call('GET', token, f"/applications/{app['uuid']}/envs", ok_missing=True) or []
        fqdn = next((item.strip() for item in str(app.get('fqdn') or '').split(',') if item.strip()), None)
        return {
            'platform': 'coolify', 'id': app['uuid'], 'name': app.get('name') or app['uuid'], 'scope': None,
            'url': fqdn, 'domains': [], 'branch': app.get('git_branch'), 'kind': 'server' if app.get('build_pack') in ('dockerfile', 'dockercompose') else 'static',
            'settings': {'buildCommand': app.get('build_command') or None, 'outputDirectory': app.get('publish_directory') or None,
                         'rootDirectory': (app.get('base_directory') or '').strip('/') or None, 'framework': None},
            'variables': [{'name': env['key'], 'secret': bool(env.get('is_shown_once')), 'value': None if env.get('is_shown_once') else env.get('value')}
                          for env in envs if env.get('key') and not env.get('is_preview')],
            'paused': str(app.get('status') or '').startswith(('exited', 'stopped')),
            'manageUrl': self.manage(token, app['uuid']), 'deleteUrl': f"{self.manage(token, app['uuid'])}/danger",
        }

    async def create(self, token, repo, name, settings, variables, branch=None):
        projects = await self.call('GET', token, '/projects') or []
        project = projects[0]['uuid'] if projects else (await self.call('POST', token, '/projects', {'name': 'Cloudhop'}))['uuid']
        servers = await self.call('GET', token, '/servers') or []
        if not servers:
            raise RepoError(400, 'This Coolify has no server to deploy to. Add one in Coolify, then continue.')
        apps = await self.call('GET', token, '/github-apps', ok_missing=True) or []
        root = (settings.get('rootDirectory') or '').strip('/')
        body = {'project_uuid': project, 'server_uuid': servers[0]['uuid'], 'environment_name': 'production', 'name': name,
                'git_repository': f'https://github.com/{repo}', 'git_branch': branch or 'main', 'build_pack': 'nixpacks', 'is_static': True,
                'ports_exposes': '80', 'instant_deploy': False, 'autogenerate_domain': True,
                **({'base_directory': f'/{root}'} if root else {}),
                **({'build_command': settings['buildCommand']} if settings.get('buildCommand') else {}),
                **({'publish_directory': f"/{settings['outputDirectory'].strip('/')}"} if settings.get('outputDirectory') else {})}
        # ponytail: the first GitHub App Coolify has; a repo it cannot see fails the create and asks the user.
        if apps:
            body['github_app_uuid'] = apps[0]['uuid']
        try:
            created = await self.call('POST', token, '/applications/private-github-app' if apps else '/applications/public', body) or {}
        except PlatformError as error:
            if GIT_HINT.search(str(error.message)) or 'github' in str(error.message).lower():
                raise NeedsGitAccess(f'{self.parse(token)[0]}/sources', 'Coolify', error.message) from error
            raise
        uuid = created['uuid']
        if variables:
            await self.call('PATCH', token, f'/applications/{uuid}/envs/bulk', {'data': [{'key': key, 'value': value, 'is_preview': False} for key, value in variables.items()]})
        app = await self.call('GET', token, f'/applications/{uuid}', ok_missing=True) or {}
        fqdn = next((item.strip() for item in str(app.get('fqdn') or '').split(',') if item.strip()), None)
        return {'id': uuid, 'name': name, 'scope': None, 'url': fqdn, 'manageUrl': self.manage(token, uuid), 'deleteUrl': f'{self.manage(token, uuid)}/danger'}

    async def env_names(self, token, site):
        return {env['key'] for env in await self.call('GET', token, f"/applications/{site['id']}/envs") or [] if env.get('key')}

    async def deploy(self, token, site, repo, branch, **_):
        path = f"/deploy?uuid={quote(site['id'])}&force=false"
        try:
            data = await self.call('POST', token, path)
        except PlatformError as error:
            if error.http_status != 405:
                raise
            data = await self.call('GET', token, path)  # Coolify releases before the POST form take GET.
        deployments = (data or {}).get('deployments') or []
        if not deployments:
            raise RepoError(400, 'Coolify did not start a deployment.')
        return deployments[0]['deployment_uuid']

    async def deployment_state(self, token, site, deployment_id):
        status = (await self.call('GET', token, f'/deployments/{quote(deployment_id)}') or {}).get('status')
        if status == 'finished':
            return 'READY', None
        if status == 'failed':
            return 'ERROR', 'The Coolify deployment failed. Check its log in Coolify.'
        if str(status or '').startswith('cancelled'):
            return 'CANCELED', None
        return 'BUILDING', None

    def settings_url(self, site):
        return f"{site.get('manageUrl')}/environment-variables"

    async def pause(self, token, site):
        await self.call('POST', token, f"/applications/{site['id']}/stop")

    async def unpause(self, token, site):
        await self.call('POST', token, f"/applications/{site['id']}/start")


SITES = {'cloudflare_pages': CloudflarePages(), 'vercel': Vercel(), 'render': Render(), 'netlify': Netlify(), 'koyeb': Koyeb(), 'railway': Railway(), 'heroku': Heroku(), 'flyio': FlyIO(),
         'northflank': Northflank(), 'do_apps': DigitalOceanApps(), 'github_pages': GitHubPages(), 'coolify': Coolify()}


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


def static_site_settings(service_map):
    """Build settings for a static site found in the repo (Vite, Astro, Create React App and the like), or None."""
    site = next((service for service in service_map['services'] if service['kind'] == 'static'), None)
    if not site:
        return None
    return {'buildCommand': 'npm run build', 'outputDirectory': 'build' if site.get('framework') == 'Create React App' else 'dist',
            'rootDirectory': None if site['path'] == '.' else site['path'], 'framework': None}


def move_plan(repo, source, target_platform, stored_secret_names, detected=None, default_branch=None):
    """The dry run the user approves: what is copied, what is missing, and which steps need them.
    `detected` is the static site found in the repo, used when there is no current site to copy settings from."""
    target = PLATFORM_TARGETS[target_platform]
    variables = (source or {}).get('variables') or []
    plain = [item['name'] for item in variables if not item['secret'] and item.get('value') is not None]
    secrets = [item['name'] for item in variables if item['secret'] or item.get('value') is None]
    missing = [name for name in secrets if name not in stored_secret_names]
    settings = {key: value for key, value in ((source or {}).get('settings') or {}).items() if value}
    branch = (source or {}).get('branch') or default_branch
    warnings = []
    # Fill each missing build setting from the repo, keeping whatever the current site sets explicitly. The root folder
    # only comes from the repo when there is no current site: a site without one builds from the repo root.
    filled = [key for key in ('buildCommand', 'outputDirectory') if not settings.get(key) and (detected or {}).get(key)]
    if not source and (detected or {}).get('rootDirectory'):
        filled.append('rootDirectory')
    for key in filled:
        settings[key] = detected[key]
    if filled:
        where = f" in {settings['rootDirectory']}/" if settings.get('rootDirectory') else ''
        reason = 'No current site to copy from' if not source else 'The current site leaves these to its framework'
        warnings.append(f"{reason}, so the repo supplies them: the static site{where} builds with '{settings.get('buildCommand')}' into "
                        f"'{settings.get('outputDirectory')}'. Change them on {target['name']} if your build differs.")
    elif not settings and not source:
        warnings.append(f"No current site and no static site found in the repo, so {target['name']} gets no build settings and publishes the repo "
                        'as it is. Set the build command and output folder there, or tell Cloudhop where the site runs today.')
    if (source or {}).get('kind') == 'server':
        warnings.append(f"The current site is a running server on {PLATFORM_TARGETS[source['platform']]['name']}, not a static site. "
                        f"{target['name']} serves built files{' and functions' if target_platform != 'render' else ''}, so a server app may not work there. Test the new address before moving the domain.")
    if target_platform == 'github_pages':
        address = GitHubPages.address(repo)
        warnings.append(f"Cloudhop opens a pull request with a GitHub Actions workflow that builds the site and publishes it to {address}. "
                        + ("A project site lives under a folder, so set your framework's base path to "
                           f"/{repo.split('/', 1)[1]}/ unless you add a custom domain. " if not address.endswith('.github.io/') else '')
                        + 'Copied plain settings are written into the workflow; Cloudhop cannot write Actions secrets, so you add those in the repo.')
    if target_platform == 'flyio':
        warnings.append(f"{target['name']} cannot build from GitHub by itself. Cloudhop opens a pull request with fly.toml, a GitHub Actions "
                        "workflow and, if the repo has none, a Dockerfile that serves the built site with nginx. Merge it and add the repo secret "
                        "FLY_API_TOKEN with a deploy token for the new app (`fly tokens create deploy -a <app>`). Cloudhop never sees that token. "
                        "Secrets reach only the running app; copied plain settings are also written to fly.toml as build arguments, so the build sees them.")
    if target_platform in SERVER_TARGETS and (source or {}).get('kind') != 'server':
        warnings.append(f"{target['name']} runs your app as a web service, so the repo needs a start command that serves it (for a static "
                        "site, for example `npx serve -s dist`). Build-only static sites belong on Netlify, Vercel, Cloudflare Pages or Render.")
    if target_platform == 'heroku' and settings.get('rootDirectory'):
        # Heroku detects the app from files at the root of the source archive, and the archive is the whole repo.
        raise RepoError(400, f"The app is in {settings['rootDirectory']}/, but Heroku builds from the repo root. "
                             'Move it to a platform that takes a root folder (Render, Netlify, Vercel, Koyeb or Railway).')
    if target_platform == 'render':
        build = Render().build_command(settings.get('buildCommand'))
        warnings.append(f"Cloudhop creates a Render static site (free) that builds with '{build}' and publishes "
                        f"'{settings.get('outputDirectory') or Render.DEFAULT_PUBLISH}'. Render does not install dependencies by itself, so the install is part of the build.")
    steps = [
        {'id': 'create', 'title': f"Create the {target['name']} project, linked to {repo}", 'who': 'agent'},
        {'id': 'variables', 'title': f"Copy {len(plain)} setting{'s' if len(plain) != 1 else ''} and add {len(secrets)} secret{'s' if len(secrets) != 1 else ''}", 'who': 'agent' if not missing else 'you or agent'},
        {'id': 'deploy', 'title': f"Build and deploy the {branch or 'default'} branch", 'who': 'agent'},
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
        'gitAccess': (f"{target['name']} deploys from a workflow in {repo}, so moves to it need a GitHub repo. Every push to the branch deploys." if target_platform in PIPELINE_TARGETS
                      else f"{target['name']} needs its GitHub app on {repo} to build from it and deploy on every push. If it is missing, Cloudhop shows the link and waits."),
        'steps': steps,
        'branch': branch,
        'warnings': warnings,
        'keepsOldSite': 'The current site stays live until you pause it. Cloudhop never deletes it; you get the link to do that yourself.',
    }


async def wait_for_deploy(adapter, token, site, deployment_id, attempts=None, delay=5):
    for _ in range(attempts or getattr(adapter, 'deploy_attempts', 60)):
        state, message = await adapter.deployment_state(token, site, deployment_id)
        if state in ('READY', 'ERROR', 'CANCELED'):
            return state, message
        await asyncio.sleep(delay)
    return 'TIMEOUT', getattr(adapter, 'timeout_message', 'The build did not finish in time. Check it on the platform.')


async def check_site(url, compare_with=None):
    """The new address must answer without a server error; if the old site is known, titles should match."""
    try:
        response = await net.public_get(url, timeout=15)
    except Exception as error:  # noqa: BLE001 - any network failure means the site is not up
        return {'ok': False, 'detail': f'{url} did not answer ({type(error).__name__}).'}
    if not 200 <= response.status_code < 400:
        # A 404 or 401 means the site is not serving its page, even though the server answered.
        return {'ok': False, 'detail': f'{url} answered {response.status_code}, so the site is not serving its page. Check the build output folder on the platform.'}
    result = {'ok': True, 'detail': f'{url} answered {response.status_code}.'}
    if compare_with:
        def title(text):
            match = re.search(r'<title[^>]*>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)
            return match.group(1).strip() if match else None
        try:
            old = await net.public_get(compare_with, timeout=15)
            same = title(old.text) == title(response.text)
            result['detail'] += ' Page title matches the current site.' if same else ' Page title differs from the current site; check it.'
            result['matches'] = same
        except Exception:  # noqa: BLE001
            result['detail'] += ' The current site could not be loaded to compare.'
    return result
