"""One adapter per code host: read files, open a change request, start the Cloudhop pipeline, list runs.

GitHub uses GitHub Actions; the others use their own CI with the shared .cloudhop/ci.sh.
"""
import asyncio
import base64
import json
import re
from urllib.parse import quote, unquote

from . import net
from .analyze import MAX_FILES, select_manifests
from .errors import RepoError
from .github import github_api, installation_details, installation_token
from .store import decrypt_with_env
from .timeutil import now_ms

MAX_PAGES = 5

PROVIDERS = {
    'github': {'name': 'GitHub', 'kinds': ['github_app', 'github_token'], 'pattern': r'[\w.-]+/[\w.-]+', 'example': 'owner/repo'},
    'gitlab': {'name': 'GitLab', 'kinds': ['gitlab_token'], 'pattern': r'[\w.-]+(/[\w.-]+){1,9}', 'example': 'group/project'},
    'bitbucket': {'name': 'Bitbucket', 'kinds': ['bitbucket_token'], 'pattern': r'[\w.-]+/[\w.-]+', 'example': 'workspace/repo'},
    'azure': {'name': 'Azure DevOps', 'kinds': ['azure_token'], 'pattern': r'[\w.-]+/[\w. .-]+/[\w. .-]+', 'example': 'organization/project/repo'},
}


def provider_of(kind):
    for name, provider in PROVIDERS.items():
        if kind in provider['kinds']:
            return name
    raise RepoError(400, 'That account is not a code host')


def parse_repo_for(provider, text):
    """Accepts a pasted URL or a plain path, and returns the canonical repo path for that provider."""
    value = re.sub(r'/+$', '', re.sub(r'\.git$', '', str(text if text is not None else '').strip()))
    value = re.sub(r'^https?://(www\.)?github\.com/', '', value)
    value = re.sub(r'^https?://gitlab\.com/', '', value)
    value = re.sub(r'^https?://bitbucket\.org/', '', value)
    value = re.sub(r'^https?://dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/]+)$', r'\1/\2/\3', value)
    value = unquote(value)
    spec = PROVIDERS[provider]
    if not re.fullmatch(spec['pattern'], value, re.ASCII) or any(part.startswith('.') or not part.strip() for part in value.split('/')):
        raise RepoError(400, f"Enter a {spec['name']} repository, like {spec['example']}")
    return value


def enc(text):
    """encodeURIComponent."""
    return quote(str(text), safe="!~*'()")


def enc_path(path):
    return '/'.join(enc(part) for part in path.split('/'))


async def http(url, auth, method='GET', body=None, form=None, raw=False, with_headers=False):
    headers = {'Accept': '*/*' if raw else 'application/json'}
    if auth:
        headers['Authorization'] = auth
    # Bitbucket's commit endpoint takes multipart form fields, one per file.
    files = [(key, (None, value)) for key, value in form] if form else None
    response = await net.client.request(method, url, headers=headers, json=body, files=files)
    if response.is_success:
        if raw:
            return response.text
        try:
            data = None if response.status_code == 204 else response.json()
        except ValueError:
            data = None
        return (data, response.headers) if with_headers else data
    if response.status_code == 401:
        raise RepoError(401, 'The code host rejected the credentials. Reconnect that account.')
    if response.status_code == 404:
        raise RepoError(404, 'The code host could not find that. Check the repo name, account access, and that the Cloudhop change is merged.')
    if response.status_code in (403, 429):
        raise RepoError(429, 'The code host refused this: rate limit reached or missing permission.')
    try:
        detail = response.json()
    except ValueError:
        detail = {}
    detail = detail if isinstance(detail, dict) else {}
    error = detail.get('error')
    message = detail.get('message') or (error.get('message') if isinstance(error, dict) else None)
    raise RepoError(502, f'The code host returned {response.status_code}' + (f': {message}' if message else ''))


def _basic(user, secret):
    return 'Basic ' + base64.b64encode(f'{user}:{secret}'.encode()).decode()


async def credentials_for(env, account):
    """Turns a saved account row into what each adapter needs to authenticate."""
    data = json.loads(account['data'])
    kind = account['kind']
    if kind == 'github_app':
        return {'auth': f"Bearer {await installation_token(env, data['installationId'])}"}
    secret = decrypt_with_env(env, account['secret_enc'], account['id'])
    if kind in ('github_token', 'gitlab_token'):
        return {'auth': f'Bearer {secret}'}
    if kind == 'bitbucket_token':
        parsed = json.loads(secret)
        return {'auth': _basic(parsed['email'], parsed['token'])}
    if kind == 'azure_token':
        return {'auth': _basic('', secret), 'organization': data['organization']}
    raise RepoError(400, 'Unknown account type')


def _scopes_from(headers, name):
    """Scopes from a response header, or None when the host does not send one."""
    value = headers.get(name)
    return None if value is None else [scope for scope in re.split(r'[ ,]+', value) if scope]


def _status_from(running, ok, failed):
    if running:
        return {'status': 'in_progress', 'conclusion': None}
    return {'status': 'completed', 'conclusion': 'success' if ok else 'failure' if failed else 'cancelled'}


def _branch_name():
    from .generate import _base36
    return f'cloudhop/install-{_base36(now_ms())}'


def _variables(inputs):
    return [{'key': key, 'value': value} for key, value in (('CLOUDHOP_ACTION', inputs['action']), ('CLOUDHOP_CLOUD', inputs['cloud']), ('CLOUDHOP_SOURCE', inputs['source']))]


# access(): what an account lets Cloudhop reach, straight from the code host. Shape:
# {kind: 'code', selection, owner, repos: [{name, access}], total, more, permissions, scopes, expires, note, manageUrl}.


class GitHub:
    API = 'https://api.github.com'

    async def whoami(self, cred):
        return (await github_api('/user', cred['auth'][7:]))['login']

    async def repo_info(self, cred, repo):
        info = await http(f'{self.API}/repos/{repo}', cred['auth'])
        return {'repo': repo, 'defaultBranch': info['default_branch']}

    async def list_paths(self, cred, repo, info):
        tree = await http(f"{self.API}/repos/{repo}/git/trees/{enc(info['defaultBranch'])}?recursive=1", cred['auth'])
        return {'entries': [{'path': entry['path'], 'size': entry.get('size')} for entry in tree['tree'] if entry['type'] == 'blob'], 'truncated': tree.get('truncated', False)}

    async def read_file(self, cred, repo, info, path):
        return await http(f"https://raw.githubusercontent.com/{repo}/{enc(info['defaultBranch'])}/{enc_path(path)}", cred['auth'], raw=True)

    async def open_change_request(self, cred, repo, info, files, existing, title, description):
        async def api(path, **options):
            return await http(f'{self.API}/repos/{repo}{path}', cred['auth'], **options)
        head = await api(f"/git/ref/heads/{enc(info['defaultBranch'])}")
        base = await api(f"/git/commits/{head['object']['sha']}")
        tree = await api('/git/trees', method='POST', body={'base_tree': base['tree']['sha'], 'tree': [
            {'path': file['path'], 'mode': '100755' if file.get('executable') else '100644', 'type': 'blob', 'content': file['content']} for file in files]})
        commit = await api('/git/commits', method='POST', body={'message': title, 'tree': tree['sha'], 'parents': [head['object']['sha']]})
        branch = f"cloudhop/install-{commit['sha'][:8]}"
        await api('/git/refs', method='POST', body={'ref': f'refs/heads/{branch}', 'sha': commit['sha']})
        pr = await api('/pulls', method='POST', body={'title': title, 'head': branch, 'base': info['defaultBranch'], 'body': description})
        return pr['html_url']

    async def trigger(self, cred, repo, info, inputs):
        await http(f'{self.API}/repos/{repo}/actions/workflows/cloudhop.yml/dispatches', cred['auth'], method='POST', body={'ref': info['defaultBranch'], 'inputs': inputs})

    async def runs(self, cred, repo):
        data = await http(f'{self.API}/repos/{repo}/actions/workflows/cloudhop.yml/runs?per_page=10', cred['auth'])
        return [{'id': run['id'], 'title': run.get('display_title'), 'status': 'completed' if run['status'] == 'completed' else 'in_progress', 'conclusion': run.get('conclusion'), 'url': run.get('html_url'), 'startedAt': run.get('run_started_at')}
                for run in data['workflow_runs']]

    async def list_repos(self, cred, account):
        if account['kind'] == 'github_app':
            return [repo['full_name'] for repo in (await http(f'{self.API}/installation/repositories?per_page=100', cred['auth']))['repositories']]
        return [repo['full_name'] for repo in await http(f'{self.API}/user/repos?per_page=100&sort=updated', cred['auth'])]

    async def access(self, cred, account, env):
        if account['kind'] == 'github_app':
            listing, installation = await asyncio.gather(
                http(f'{self.API}/installation/repositories?per_page=100', cred['auth']),
                installation_details(env, json.loads(account['data'])['installationId']),
            )
            return {
                'kind': 'code', 'selection': installation.get('repository_selection'), 'owner': (installation.get('account') or {}).get('login'),
                'repos': [{'name': repo['full_name'], 'access': None} for repo in listing['repositories']], 'total': listing['total_count'], 'more': listing['total_count'] > len(listing['repositories']),
                'permissions': [{'name': name, 'level': level} for name, level in (installation.get('permissions') or {}).items()],
                'scopes': None, 'expires': None, 'note': 'Each use gets a new token that expires within an hour. These permissions apply to every repo listed.',
                'manageUrl': installation.get('html_url'),
            }
        data, headers = await http(f'{self.API}/user/repos?per_page=100&sort=updated', cred['auth'], with_headers=True)
        scopes = _scopes_from(headers, 'x-oauth-scopes')

        def level(repo):
            permissions = repo.get('permissions') or {}
            return 'admin' if permissions.get('admin') else 'write' if permissions.get('push') else 'read'
        return {
            'kind': 'code', 'selection': 'selected', 'owner': None,
            'repos': [{'name': repo['full_name'], 'access': level(repo)} for repo in data], 'total': len(data), 'more': len(data) == 100,
            'permissions': None, 'scopes': scopes, 'expires': headers.get('github-authentication-token-expiration'),
            'note': 'Fine-grained token: GitHub does not report its permissions through the API. The repos listed are the ones it can reach.' if scopes is None else None,
            'manageUrl': 'https://github.com/settings/tokens',
        }


class GitLab:
    """gitlab.com, with a personal or group access token that has the `api` scope."""
    API = 'https://gitlab.com/api/v4'
    LEVELS = {10: 'guest', 20: 'reporter', 30: 'developer', 40: 'maintainer', 50: 'owner'}

    def project(self, repo):
        return f'{self.API}/projects/{enc(repo)}'

    async def whoami(self, cred):
        return (await http(f'{self.API}/user', cred['auth']))['username']

    async def repo_info(self, cred, repo):
        project = await http(self.project(repo), cred['auth'])
        return {'repo': project['path_with_namespace'], 'defaultBranch': project['default_branch']}

    async def list_paths(self, cred, repo, info):
        entries = []
        for page in range(1, MAX_PAGES + 1):
            items = await http(f"{self.project(repo)}/repository/tree?recursive=true&per_page=100&page={page}&ref={enc(info['defaultBranch'])}", cred['auth'])
            entries += [{'path': item['path'], 'size': 0} for item in items if item['type'] == 'blob']
            if len(items) < 100:
                return {'entries': entries, 'truncated': False}
        return {'entries': entries, 'truncated': True}

    async def read_file(self, cred, repo, info, path):
        return await http(f"{self.project(repo)}/repository/files/{enc(path)}/raw?ref={enc(info['defaultBranch'])}", cred['auth'], raw=True)

    async def open_change_request(self, cred, repo, info, files, existing, title, description):
        branch = _branch_name()
        await http(f'{self.project(repo)}/repository/commits', cred['auth'], method='POST', body={
            'branch': branch, 'start_branch': info['defaultBranch'], 'commit_message': title,
            'actions': [{'action': 'update' if file['path'] in existing else 'create', 'file_path': file['path'], 'content': file['content'], 'execute_filemode': bool(file.get('executable'))} for file in files],
        })
        request = await http(f'{self.project(repo)}/merge_requests', cred['auth'], method='POST', body={'source_branch': branch, 'target_branch': info['defaultBranch'], 'title': title, 'description': description, 'remove_source_branch': True})
        return request['web_url']

    async def trigger(self, cred, repo, info, inputs):
        await http(f'{self.project(repo)}/pipeline', cred['auth'], method='POST', body={'ref': info['defaultBranch'], 'variables': _variables(inputs)})

    async def runs(self, cred, repo):
        pipelines = await http(f'{self.project(repo)}/pipelines?per_page=10', cred['auth'])
        return [{
            'id': pipeline['id'], 'title': pipeline.get('name') or f"Pipeline {pipeline['id']}", 'url': pipeline.get('web_url'), 'startedAt': pipeline.get('created_at'),
            **_status_from(pipeline['status'] in ('created', 'pending', 'running', 'preparing', 'waiting_for_resource'), pipeline['status'] == 'success', pipeline['status'] == 'failed'),
        } for pipeline in pipelines if not pipeline.get('name') or pipeline['name'].startswith('cloudhop')]

    async def list_repos(self, cred, account=None):
        return [project['path_with_namespace'] for project in await http(f'{self.API}/projects?membership=true&simple=true&per_page=100&order_by=last_activity_at', cred['auth'])]

    async def access(self, cred, account=None, env=None):
        async def token_info():
            try:
                return await http(f'{self.API}/personal_access_tokens/self', cred['auth'])
            except RepoError:
                return None
        token, projects = await asyncio.gather(token_info(), http(f'{self.API}/projects?membership=true&per_page=100&order_by=last_activity_at', cred['auth']))

        def level(project):
            permissions = project.get('permissions') or {}
            return self.LEVELS.get(max((permissions.get('project_access') or {}).get('access_level') or 0, (permissions.get('group_access') or {}).get('access_level') or 0))
        return {
            'kind': 'code', 'selection': 'selected', 'owner': None,
            'repos': [{'name': project['path_with_namespace'], 'access': level(project)} for project in projects], 'total': len(projects), 'more': len(projects) == 100,
            'permissions': None, 'scopes': (token or {}).get('scopes'), 'expires': (token or {}).get('expires_at'),
            'note': None if token else "GitLab did not report this token's scopes.",
            'manageUrl': 'https://gitlab.com/-/user_settings/personal_access_tokens',
        }


class Bitbucket:
    """Bitbucket Cloud, with an Atlassian API token plus the account email (app passwords stop working June 2026)."""
    API = 'https://api.bitbucket.org/2.0'

    async def whoami(self, cred):
        user = await http(f'{self.API}/user', cred['auth'])
        return user.get('username') or user.get('display_name')

    async def repo_info(self, cred, repo):
        data = await http(f'{self.API}/repositories/{repo}', cred['auth'])
        return {'repo': data['full_name'], 'defaultBranch': (data.get('mainbranch') or {}).get('name') or 'main', 'repositoryUuid': data['uuid'], 'workspace': data['workspace']['slug'], 'workspaceUuid': data['workspace']['uuid']}

    async def list_paths(self, cred, repo, info):
        entries = []
        url = f"{self.API}/repositories/{repo}/src/{enc(info['defaultBranch'])}/?max_depth=10&pagelen=100"
        page = 0
        while url and page < MAX_PAGES:
            data = await http(url, cred['auth'])
            entries += [{'path': item['path'], 'size': item.get('size') or 0} for item in data['values'] if item['type'] == 'commit_file']
            url = data.get('next')
            page += 1
        return {'entries': entries, 'truncated': bool(url)}

    async def read_file(self, cred, repo, info, path):
        return await http(f"{self.API}/repositories/{repo}/src/{enc(info['defaultBranch'])}/{enc_path(path)}", cred['auth'], raw=True)

    async def open_change_request(self, cred, repo, info, files, existing, title, description):
        head = await http(f"{self.API}/repositories/{repo}/refs/branches/{enc(info['defaultBranch'])}", cred['auth'])
        branch = _branch_name()
        form = [('message', title), ('branch', branch), ('parents', head['target']['hash']), *((file['path'], file['content']) for file in files)]
        await http(f'{self.API}/repositories/{repo}/src', cred['auth'], method='POST', form=form)
        request = await http(f'{self.API}/repositories/{repo}/pullrequests', cred['auth'], method='POST', body={
            'title': title, 'description': description, 'source': {'branch': {'name': branch}}, 'destination': {'branch': {'name': info['defaultBranch']}}, 'close_source_branch': True,
        })
        return request['links']['html']['href']

    async def trigger(self, cred, repo, info, inputs):
        await http(f'{self.API}/repositories/{repo}/pipelines/', cred['auth'], method='POST', body={
            'target': {'type': 'pipeline_ref_target', 'ref_type': 'branch', 'ref_name': info['defaultBranch'], 'selector': {'type': 'custom', 'pattern': 'cloudhop'}},
            'variables': _variables(inputs),
        })

    async def runs(self, cred, repo):
        data = await http(f'{self.API}/repositories/{repo}/pipelines/?sort=-created_on&pagelen=10', cred['auth'])
        out = []
        for run in data['values']:
            if ((run.get('target') or {}).get('selector') or {}).get('pattern') != 'cloudhop':
                continue
            state = run.get('state') or {}
            result = (state.get('result') or {}).get('name')
            out.append({'id': run['build_number'], 'title': f"Cloudhop pipeline #{run['build_number']}", 'url': f"https://bitbucket.org/{repo}/pipelines/results/{run['build_number']}", 'startedAt': run.get('created_on'),
                        **_status_from(state.get('name') != 'COMPLETED', result == 'SUCCESSFUL', result == 'FAILED')})
        return out

    async def list_repos(self, cred, account=None):
        return [item['repository']['full_name'] for item in (await http(f'{self.API}/user/permissions/repositories?pagelen=100', cred['auth']))['values']]

    async def access(self, cred, account=None, env=None):
        data, headers = await http(f'{self.API}/user/permissions/repositories?pagelen=100', cred['auth'], with_headers=True)
        scopes = _scopes_from(headers, 'x-oauth-scopes')
        return {
            'kind': 'code', 'selection': 'selected', 'owner': None,
            'repos': [{'name': item['repository']['full_name'], 'access': item.get('permission')} for item in data['values']], 'total': data.get('size', len(data['values'])), 'more': bool(data.get('next')),
            'permissions': None, 'scopes': scopes, 'expires': None,
            'note': "Bitbucket did not report this token's scopes. You can see them where you created it." if scopes is None else None,
            'manageUrl': 'https://id.atlassian.com/manage-profile/security/api-tokens',
        }


def _azure_parts(repo):
    organization, project, name = repo.split('/')
    return organization, project, name, f'https://dev.azure.com/{enc(organization)}/{enc(project)}/_apis'


class Azure:
    """Azure DevOps Services, with a personal access token that has Code (read and write) and Build (read and execute)."""

    async def pipeline_id(self, cred, repo):
        api = _azure_parts(repo)[3]
        pipelines = await http(f'{api}/pipelines?api-version=7.1', cred['auth'])
        return next((pipeline['id'] for pipeline in pipelines['value'] if pipeline['name'] == 'cloudhop'), None)

    async def whoami(self, cred):
        data = await http(f"https://dev.azure.com/{enc(cred['organization'])}/_apis/connectionData", cred['auth'])
        return (data.get('authenticatedUser') or {}).get('providerDisplayName') or cred['organization']

    async def repo_info(self, cred, repo):
        organization, project, name, api = _azure_parts(repo)
        if organization != cred['organization']:
            raise RepoError(400, f"That repo is outside the {cred['organization']} organization this account uses")
        data = await http(f'{api}/git/repositories/{enc(name)}?api-version=7.1', cred['auth'])
        # The organization id is the issuer path in Azure DevOps OIDC tokens.
        connection = await http(f'https://dev.azure.com/{enc(organization)}/_apis/connectionData', cred['auth'])
        return {'repo': repo, 'defaultBranch': (data.get('defaultBranch') or 'refs/heads/main').replace('refs/heads/', ''), 'repositoryId': data['id'], 'organization': organization, 'project': project, 'organizationId': connection.get('instanceId')}

    async def list_paths(self, cred, repo, info):
        api = _azure_parts(repo)[3]
        data = await http(f"{api}/git/repositories/{info['repositoryId']}/items?recursionLevel=Full&versionDescriptor.version={enc(info['defaultBranch'])}&api-version=7.1", cred['auth'])
        return {'entries': [{'path': item['path'].lstrip('/'), 'size': 0} for item in data['value'] if not item.get('isFolder')], 'truncated': False}

    async def read_file(self, cred, repo, info, path):
        api = _azure_parts(repo)[3]
        return await http(f"{api}/git/repositories/{info['repositoryId']}/items?path={enc('/' + path)}&versionDescriptor.version={enc(info['defaultBranch'])}&$format=octetStream&api-version=7.1", cred['auth'], raw=True)

    async def open_change_request(self, cred, repo, info, files, existing, title, description):
        organization, project, name, api = _azure_parts(repo)
        refs = await http(f"{api}/git/repositories/{info['repositoryId']}/refs?filter={enc('heads/' + info['defaultBranch'])}&api-version=7.1", cred['auth'])
        branch = _branch_name()
        await http(f"{api}/git/repositories/{info['repositoryId']}/pushes?api-version=7.1", cred['auth'], method='POST', body={
            'refUpdates': [{'name': f'refs/heads/{branch}', 'oldObjectId': refs['value'][0]['objectId']}],
            'commits': [{'comment': title, 'changes': [{'changeType': 'edit' if file['path'] in existing else 'add', 'item': {'path': f"/{file['path']}"}, 'newContent': {'content': file['content'], 'contentType': 'rawtext'}} for file in files]}],
        })
        request = await http(f"{api}/git/repositories/{info['repositoryId']}/pullrequests?api-version=7.1", cred['auth'], method='POST', body={
            'sourceRefName': f'refs/heads/{branch}', 'targetRefName': f"refs/heads/{info['defaultBranch']}", 'title': title, 'description': description,
        })
        return f"https://dev.azure.com/{enc(organization)}/{enc(project)}/_git/{enc(name)}/pullrequest/{request['pullRequestId']}"

    async def trigger(self, cred, repo, info, inputs):
        name, api = _azure_parts(repo)[2], _azure_parts(repo)[3]
        pipeline = await self.pipeline_id(cred, repo)
        if not pipeline:
            # First run: register .cloudhop/azure-pipelines.yml as a pipeline named "cloudhop".
            created = await http(f'{api}/pipelines?api-version=7.1', cred['auth'], method='POST', body={
                'name': 'cloudhop', 'folder': '\\', 'configuration': {'type': 'yaml', 'path': '/.cloudhop/azure-pipelines.yml', 'repository': {'id': info['repositoryId'], 'name': name, 'type': 'azureReposGit'}},
            })
            pipeline = created['id']
        await http(f'{api}/pipelines/{pipeline}/runs?api-version=7.1', cred['auth'], method='POST', body={
            'resources': {'repositories': {'self': {'refName': f"refs/heads/{info['defaultBranch']}"}}},
            'templateParameters': {'action': inputs['action'], 'cloud': inputs['cloud'], 'source': inputs['source'] or ' '},
        })

    async def runs(self, cred, repo):
        pipeline = await self.pipeline_id(cred, repo)
        if not pipeline:
            return []
        api = _azure_parts(repo)[3]
        data = await http(f'{api}/pipelines/{pipeline}/runs?api-version=7.1', cred['auth'])
        return [{'id': run['id'], 'title': f"Cloudhop run {run.get('name')}", 'url': ((run.get('_links') or {}).get('web') or {}).get('href'), 'startedAt': run.get('createdDate'),
                 **_status_from(run.get('state') != 'completed', run.get('result') == 'succeeded', run.get('result') == 'failed')} for run in data['value'][:10]]

    async def list_repos(self, cred, account=None):
        data = await http(f"https://dev.azure.com/{enc(cred['organization'])}/_apis/git/repositories?api-version=7.1", cred['auth'])
        return [f"{cred['organization']}/{repo['project']['name']}/{repo['name']}" for repo in data['value']]

    async def access(self, cred, account=None, env=None):
        repos = await self.list_repos(cred)
        return {
            'kind': 'code', 'selection': 'selected', 'owner': cred['organization'],
            'repos': [{'name': name, 'access': None} for name in repos[:100]], 'total': len(repos), 'more': len(repos) > 100,
            'permissions': None, 'scopes': None, 'expires': None,
            'note': 'Azure DevOps does not let a token read its own scopes. You can see them in Azure DevOps under User settings, Personal access tokens.',
            'manageUrl': f"https://dev.azure.com/{enc(cred['organization'])}/_usersSettings/tokens",
        }


ADAPTERS = {'github': GitHub(), 'gitlab': GitLab(), 'bitbucket': Bitbucket(), 'azure': Azure()}


async def read_manifests(adapter, cred, repo, info):
    listing = await adapter.list_paths(cred, repo, info)
    chosen = select_manifests(listing['entries'])

    async def read(path):
        try:
            return {'path': path, 'text': await adapter.read_file(cred, repo, info, path)}
        except Exception:
            return None

    files = await asyncio.gather(*(read(entry['path']) for entry in chosen['selected']))
    warnings = []
    if listing['truncated']:
        warnings.append('Repo is very large, so only part of its file list was read.')
    if chosen['total'] > MAX_FILES:
        warnings.append(f"Read the {MAX_FILES} shallowest of {chosen['total']} manifest files.")
    if None in files:
        warnings.append('Some manifest files could not be downloaded.')
    return {'files': [file for file in files if file], 'warnings': warnings, 'paths': {entry['path'] for entry in listing['entries']}}
