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
import re
from urllib.parse import quote

from . import net
from .errors import RepoError

# Where a site can run. "move_to" marks targets Cloudhop can create today; the rest are coming soon.
PLATFORM_TARGETS = {
    'vercel': {'name': 'Vercel', 'account': 'vercel', 'move_to': True, 'pause': True},
    'cloudflare_pages': {'name': 'Cloudflare Pages', 'account': 'cloudflare', 'move_to': True, 'pause': False},
    'render': {'name': 'Render', 'account': 'render', 'move_to': True, 'pause': True},
    'netlify': {'name': 'Netlify', 'account': None, 'move_to': False, 'pause': False},
}
VERCEL_GIT_APP = 'https://github.com/apps/vercel/installations/new'
CLOUDFLARE_GIT_APP = 'https://github.com/apps/cloudflare-workers-and-pages/installations/new'
RENDER_GIT_APP = 'https://github.com/apps/render/installations/new'
# Words in a platform's error that mean it cannot reach the repo. Not "authoriz": that also matches "Unauthorized",
# which is a token problem, not a missing GitHub connection.
GIT_HINT = re.compile(r'git|repositor|install|integration', re.IGNORECASE)
# What to do on each platform when it cannot see the repo. The platform's own account must be connected to the GitHub
# account that owns the repo (emails may differ); installing the app from GitHub alone does not link it.
GIT_STEPS = {
    'Cloudflare Pages': 'In the Cloudflare dashboard, open Workers & Pages, Create, Pages, Connect to Git, and connect the GitHub account that owns this repo. Give it access to the repo, then cancel the setup there and continue here.',
    'Vercel': "Install Vercel's GitHub app on this repo from your Vercel account (Settings, Git), signed in to GitHub as the repo's owner. Then continue here.",
    'Render': 'In the Render dashboard, open Account Settings, Git Deployment, and connect the GitHub account that owns this repo with access to it. Then continue here.',
}


async def _call(method, url, token, body=None, ok_missing=False, form=None):
    # form: multipart fields, for endpoints that take form data instead of JSON.
    files = {key: (None, value) for key, value in form.items()} if form else None
    response = await net.client.request(method, url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'}, json=body, files=files)
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

    async def deploy(self, token, site, repo, branch):
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

    async def deploy(self, token, site, repo, branch):
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


SITES = {'cloudflare_pages': CloudflarePages(), 'vercel': Vercel(), 'render': Render()}


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
        'gitAccess': f"{target['name']} needs its GitHub app on {repo} to build from it and deploy on every push. If it is missing, Cloudhop shows the link and waits.",
        'steps': steps,
        'branch': branch,
        'warnings': warnings,
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
