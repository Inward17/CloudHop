// One adapter per code host: read files, open a change request, start the Cloudhop pipeline, list runs.
// GitHub uses GitHub Actions; the others use their own CI with the shared .cloudhop/ci.sh.
import { MAX_FILES, selectManifests } from './analyze.js';
import { githubApi, installationToken, RepoError } from './github.js';
import { decrypt } from './store.js';

const MAX_PAGES = 5;

export const PROVIDERS = {
  github: { name: 'GitHub', kinds: ['github_app', 'github_token'], pattern: /^[\w.-]+\/[\w.-]+$/, example: 'owner/repo' },
  gitlab: { name: 'GitLab', kinds: ['gitlab_token'], pattern: /^[\w.-]+(\/[\w.-]+){1,9}$/, example: 'group/project' },
  bitbucket: { name: 'Bitbucket', kinds: ['bitbucket_token'], pattern: /^[\w.-]+\/[\w.-]+$/, example: 'workspace/repo' },
  azure: { name: 'Azure DevOps', kinds: ['azure_token'], pattern: /^[\w.-]+\/[\w. .-]+\/[\w. .-]+$/, example: 'organization/project/repo' },
};

export function providerOf(kind) {
  const entry = Object.entries(PROVIDERS).find(([, provider]) => provider.kinds.includes(kind));
  if (!entry) throw new RepoError(400, 'That account is not a code host');
  return entry[0];
}

// Accepts a pasted URL or a plain path, and returns the canonical repo path for that provider.
export function parseRepoFor(provider, input) {
  let value = String(input ?? '').trim().replace(/\.git$/, '').replace(/\/+$/, '');
  value = value
    .replace(/^https?:\/\/(www\.)?github\.com\//, '')
    .replace(/^https?:\/\/gitlab\.com\//, '')
    .replace(/^https?:\/\/bitbucket\.org\//, '')
    .replace(/^https?:\/\/dev\.azure\.com\/([^/]+)\/([^/]+)\/_git\/([^/]+)$/, '$1/$2/$3');
  try { value = decodeURIComponent(value); } catch { /* Leave as typed; the pattern check rejects it. */ }
  const { pattern, example, name } = PROVIDERS[provider];
  if (!pattern.test(value) || value.split('/').some((part) => part.startsWith('.') || !part.trim())) {
    throw new RepoError(400, `Enter a ${name} repository, like ${example}`);
  }
  return value;
}

async function http(url, auth, { method = 'GET', body, form, raw = false } = {}) {
  const headers = { 'User-Agent': 'cloudhop', Accept: raw ? '*/*' : 'application/json' };
  if (auth) headers.Authorization = auth;
  if (body) headers['Content-Type'] = 'application/json';
  const response = await fetch(url, { method, headers, body: form ?? (body ? JSON.stringify(body) : undefined) });
  if (response.ok) {
    if (raw) return response.text();
    return response.status === 204 ? null : response.json().catch(() => null);
  }
  if (response.status === 401) throw new RepoError(401, 'The code host rejected the credentials. Reconnect that account.');
  if (response.status === 404) throw new RepoError(404, 'The code host could not find that. Check the repo name, account access, and that the Cloudhop change is merged.');
  if (response.status === 403 || response.status === 429) throw new RepoError(429, 'The code host refused this: rate limit reached or missing permission.');
  const detail = await response.json().catch(() => ({}));
  throw new RepoError(502, `The code host returned ${response.status}${detail.message || detail.error?.message ? `: ${detail.message || detail.error.message}` : ''}`);
}

const basic = (user, secret) => `Basic ${btoa(`${user}:${secret}`)}`;

// Turns a saved account into what each adapter needs to authenticate.
export async function credentialsFor(env, account) {
  const data = JSON.parse(account.data);
  if (account.kind === 'github_app') return { auth: `Bearer ${await installationToken(env, data.installationId)}` };
  const secret = await decrypt(account.secret_enc, env.ENCRYPTION_KEY, account.id);
  if (account.kind === 'github_token' || account.kind === 'gitlab_token') return { auth: `Bearer ${secret}` };
  if (account.kind === 'bitbucket_token') {
    const { email, token } = JSON.parse(secret);
    return { auth: basic(email, token) };
  }
  if (account.kind === 'azure_token') return { auth: basic('', secret), organization: data.organization };
  throw new RepoError(400, 'Unknown account type');
}

const statusFrom = (running, ok, failed) => (running ? { status: 'in_progress', conclusion: null } : { status: 'completed', conclusion: ok ? 'success' : failed ? 'failure' : 'cancelled' });

/* GitHub */

const github = {
  async whoami(cred) { return (await githubApi('/user', cred.auth.slice(7))).login; },
  async repoInfo(cred, repo) {
    const info = await http(`https://api.github.com/repos/${repo}`, cred.auth);
    return { repo, defaultBranch: info.default_branch };
  },
  async listPaths(cred, repo, info) {
    const tree = await http(`https://api.github.com/repos/${repo}/git/trees/${encodeURIComponent(info.defaultBranch)}?recursive=1`, cred.auth);
    return { entries: tree.tree.filter((entry) => entry.type === 'blob').map((entry) => ({ path: entry.path, size: entry.size })), truncated: tree.truncated };
  },
  readFile(cred, repo, info, path) {
    return http(`https://raw.githubusercontent.com/${repo}/${encodeURIComponent(info.defaultBranch)}/${path.split('/').map(encodeURIComponent).join('/')}`, cred.auth, { raw: true });
  },
  async openChangeRequest(cred, repo, info, files, existing, title, description) {
    const api = (path, options) => http(`https://api.github.com/repos/${repo}${path}`, cred.auth, options);
    const head = await api(`/git/ref/heads/${encodeURIComponent(info.defaultBranch)}`);
    const base = await api(`/git/commits/${head.object.sha}`);
    const tree = await api('/git/trees', { method: 'POST', body: { base_tree: base.tree.sha, tree: files.map((file) => ({ path: file.path, mode: file.executable ? '100755' : '100644', type: 'blob', content: file.content })) } });
    const commit = await api('/git/commits', { method: 'POST', body: { message: title, tree: tree.sha, parents: [head.object.sha] } });
    const branch = `cloudhop/install-${commit.sha.slice(0, 8)}`;
    await api('/git/refs', { method: 'POST', body: { ref: `refs/heads/${branch}`, sha: commit.sha } });
    const pr = await api('/pulls', { method: 'POST', body: { title, head: branch, base: info.defaultBranch, body: description } });
    return pr.html_url;
  },
  async trigger(cred, repo, info, inputs) {
    await http(`https://api.github.com/repos/${repo}/actions/workflows/cloudhop.yml/dispatches`, cred.auth, { method: 'POST', body: { ref: info.defaultBranch, inputs } });
  },
  async runs(cred, repo) {
    const data = await http(`https://api.github.com/repos/${repo}/actions/workflows/cloudhop.yml/runs?per_page=10`, cred.auth);
    return data.workflow_runs.map((run) => ({ id: run.id, title: run.display_title, status: run.status === 'completed' ? 'completed' : 'in_progress', conclusion: run.conclusion, url: run.html_url, startedAt: run.run_started_at }));
  },
  async listRepos(cred, account) {
    if (account.kind === 'github_app') return (await http('https://api.github.com/installation/repositories?per_page=100', cred.auth)).repositories.map((repo) => repo.full_name);
    return (await http('https://api.github.com/user/repos?per_page=100&sort=updated', cred.auth)).map((repo) => repo.full_name);
  },
};

/* GitLab (gitlab.com). Personal access token with the `api` scope. */

const GITLAB = 'https://gitlab.com/api/v4';
const gitlab = {
  async whoami(cred) { return (await http(`${GITLAB}/user`, cred.auth)).username; },
  async repoInfo(cred, repo) {
    const project = await http(`${GITLAB}/projects/${encodeURIComponent(repo)}`, cred.auth);
    return { repo: project.path_with_namespace, defaultBranch: project.default_branch };
  },
  async listPaths(cred, repo, info) {
    const entries = [];
    for (let page = 1; page <= MAX_PAGES; page++) {
      const items = await http(`${GITLAB}/projects/${encodeURIComponent(repo)}/repository/tree?recursive=true&per_page=100&page=${page}&ref=${encodeURIComponent(info.defaultBranch)}`, cred.auth);
      entries.push(...items.filter((item) => item.type === 'blob').map((item) => ({ path: item.path, size: 0 })));
      if (items.length < 100) return { entries, truncated: false };
    }
    return { entries, truncated: true };
  },
  readFile(cred, repo, info, path) {
    return http(`${GITLAB}/projects/${encodeURIComponent(repo)}/repository/files/${encodeURIComponent(path)}/raw?ref=${encodeURIComponent(info.defaultBranch)}`, cred.auth, { raw: true });
  },
  async openChangeRequest(cred, repo, info, files, existing, title, description) {
    const project = `${GITLAB}/projects/${encodeURIComponent(repo)}`;
    const branch = `cloudhop/install-${Date.now().toString(36)}`;
    await http(`${project}/repository/commits`, cred.auth, {
      method: 'POST',
      body: {
        branch, start_branch: info.defaultBranch, commit_message: title,
        actions: files.map((file) => ({ action: existing.has(file.path) ? 'update' : 'create', file_path: file.path, content: file.content, execute_filemode: Boolean(file.executable) })),
      },
    });
    const request = await http(`${project}/merge_requests`, cred.auth, { method: 'POST', body: { source_branch: branch, target_branch: info.defaultBranch, title, description, remove_source_branch: true } });
    return request.web_url;
  },
  async trigger(cred, repo, info, inputs) {
    await http(`${GITLAB}/projects/${encodeURIComponent(repo)}/pipeline`, cred.auth, {
      method: 'POST',
      body: { ref: info.defaultBranch, variables: [['CLOUDHOP_ACTION', inputs.action], ['CLOUDHOP_CLOUD', inputs.cloud], ['CLOUDHOP_SOURCE', inputs.source]].map(([key, value]) => ({ key, value })) },
    });
  },
  async runs(cred, repo) {
    const pipelines = await http(`${GITLAB}/projects/${encodeURIComponent(repo)}/pipelines?per_page=10`, cred.auth);
    return pipelines.filter((pipeline) => !pipeline.name || pipeline.name.startsWith('cloudhop')).map((pipeline) => ({
      id: pipeline.id, title: pipeline.name ?? `Pipeline ${pipeline.id}`, url: pipeline.web_url, startedAt: pipeline.created_at,
      ...statusFrom(['created', 'pending', 'running', 'preparing', 'waiting_for_resource'].includes(pipeline.status), pipeline.status === 'success', pipeline.status === 'failed'),
    }));
  },
  async listRepos(cred) {
    return (await http(`${GITLAB}/projects?membership=true&simple=true&per_page=100&order_by=last_activity_at`, cred.auth)).map((project) => project.path_with_namespace);
  },
};

/* Bitbucket Cloud. Atlassian API token plus the account email (app passwords stop working June 2026). */

const BITBUCKET = 'https://api.bitbucket.org/2.0';
const bitbucket = {
  async whoami(cred) { const user = await http(`${BITBUCKET}/user`, cred.auth); return user.username ?? user.display_name; },
  async repoInfo(cred, repo) {
    const data = await http(`${BITBUCKET}/repositories/${repo}`, cred.auth);
    return { repo: data.full_name, defaultBranch: data.mainbranch?.name ?? 'main', repositoryUuid: data.uuid, workspace: data.workspace.slug, workspaceUuid: data.workspace.uuid };
  },
  async listPaths(cred, repo, info) {
    const entries = [];
    let url = `${BITBUCKET}/repositories/${repo}/src/${encodeURIComponent(info.defaultBranch)}/?max_depth=10&pagelen=100`;
    for (let page = 0; url && page < MAX_PAGES; page++) {
      const data = await http(url, cred.auth);
      entries.push(...data.values.filter((item) => item.type === 'commit_file').map((item) => ({ path: item.path, size: item.size ?? 0 })));
      url = data.next;
    }
    return { entries, truncated: Boolean(url) };
  },
  readFile(cred, repo, info, path) {
    return http(`${BITBUCKET}/repositories/${repo}/src/${encodeURIComponent(info.defaultBranch)}/${path.split('/').map(encodeURIComponent).join('/')}`, cred.auth, { raw: true });
  },
  async openChangeRequest(cred, repo, info, files, existing, title, description) {
    const head = await http(`${BITBUCKET}/repositories/${repo}/refs/branches/${encodeURIComponent(info.defaultBranch)}`, cred.auth);
    const branch = `cloudhop/install-${Date.now().toString(36)}`;
    const form = new FormData();
    form.append('message', title);
    form.append('branch', branch);
    form.append('parents', head.target.hash);
    for (const file of files) form.append(file.path, file.content);
    await http(`${BITBUCKET}/repositories/${repo}/src`, cred.auth, { method: 'POST', form });
    const request = await http(`${BITBUCKET}/repositories/${repo}/pullrequests`, cred.auth, {
      method: 'POST',
      body: { title, description, source: { branch: { name: branch } }, destination: { branch: { name: info.defaultBranch } }, close_source_branch: true },
    });
    return request.links.html.href;
  },
  async trigger(cred, repo, info, inputs) {
    await http(`${BITBUCKET}/repositories/${repo}/pipelines/`, cred.auth, {
      method: 'POST',
      body: {
        target: { type: 'pipeline_ref_target', ref_type: 'branch', ref_name: info.defaultBranch, selector: { type: 'custom', pattern: 'cloudhop' } },
        variables: [['CLOUDHOP_ACTION', inputs.action], ['CLOUDHOP_CLOUD', inputs.cloud], ['CLOUDHOP_SOURCE', inputs.source]].map(([key, value]) => ({ key, value })),
      },
    });
  },
  async runs(cred, repo) {
    const data = await http(`${BITBUCKET}/repositories/${repo}/pipelines/?sort=-created_on&pagelen=10`, cred.auth);
    return data.values.filter((run) => run.target?.selector?.pattern === 'cloudhop').map((run) => ({
      id: run.build_number, title: `Cloudhop pipeline #${run.build_number}`, url: `https://bitbucket.org/${repo}/pipelines/results/${run.build_number}`, startedAt: run.created_on,
      ...statusFrom(run.state?.name !== 'COMPLETED', run.state?.result?.name === 'SUCCESSFUL', run.state?.result?.name === 'FAILED'),
    }));
  },
  async listRepos(cred) {
    return (await http(`${BITBUCKET}/user/permissions/repositories?pagelen=100`, cred.auth)).values.map((item) => item.repository.full_name);
  },
};

/* Azure DevOps Services. Personal access token with Code (read and write) and Build (read and execute). */

const azureParts = (repo) => {
  const [organization, project, name] = repo.split('/');
  return { organization, project, name, api: `https://dev.azure.com/${encodeURIComponent(organization)}/${encodeURIComponent(project)}/_apis` };
};
async function azurePipelineId(cred, repo) {
  const { api } = azureParts(repo);
  const pipelines = await http(`${api}/pipelines?api-version=7.1`, cred.auth);
  return pipelines.value.find((pipeline) => pipeline.name === 'cloudhop')?.id ?? null;
}
const azure = {
  async whoami(cred) {
    const data = await http(`https://dev.azure.com/${encodeURIComponent(cred.organization)}/_apis/connectionData`, cred.auth);
    return data.authenticatedUser?.providerDisplayName ?? cred.organization;
  },
  async repoInfo(cred, repo) {
    const { organization, project, name, api } = azureParts(repo);
    if (organization !== cred.organization) throw new RepoError(400, `That repo is outside the ${cred.organization} organization this account uses`);
    const data = await http(`${api}/git/repositories/${encodeURIComponent(name)}?api-version=7.1`, cred.auth);
    // The organization id is the issuer path in Azure DevOps OIDC tokens.
    const connection = await http(`https://dev.azure.com/${encodeURIComponent(organization)}/_apis/connectionData`, cred.auth);
    return { repo, defaultBranch: (data.defaultBranch ?? 'refs/heads/main').replace('refs/heads/', ''), repositoryId: data.id, organization, project, organizationId: connection.instanceId };
  },
  async listPaths(cred, repo, info) {
    const { api } = azureParts(repo);
    const data = await http(`${api}/git/repositories/${info.repositoryId}/items?recursionLevel=Full&versionDescriptor.version=${encodeURIComponent(info.defaultBranch)}&api-version=7.1`, cred.auth);
    return { entries: data.value.filter((item) => !item.isFolder).map((item) => ({ path: item.path.replace(/^\//, ''), size: 0 })), truncated: false };
  },
  readFile(cred, repo, info, path) {
    const { api } = azureParts(repo);
    return http(`${api}/git/repositories/${info.repositoryId}/items?path=${encodeURIComponent('/' + path)}&versionDescriptor.version=${encodeURIComponent(info.defaultBranch)}&$format=octetStream&api-version=7.1`, cred.auth, { raw: true });
  },
  async openChangeRequest(cred, repo, info, files, existing, title, description) {
    const { organization, project, name, api } = azureParts(repo);
    const refs = await http(`${api}/git/repositories/${info.repositoryId}/refs?filter=${encodeURIComponent('heads/' + info.defaultBranch)}&api-version=7.1`, cred.auth);
    const branch = `cloudhop/install-${Date.now().toString(36)}`;
    await http(`${api}/git/repositories/${info.repositoryId}/pushes?api-version=7.1`, cred.auth, {
      method: 'POST',
      body: {
        refUpdates: [{ name: `refs/heads/${branch}`, oldObjectId: refs.value[0].objectId }],
        commits: [{ comment: title, changes: files.map((file) => ({ changeType: existing.has(file.path) ? 'edit' : 'add', item: { path: `/${file.path}` }, newContent: { content: file.content, contentType: 'rawtext' } })) }],
      },
    });
    const request = await http(`${api}/git/repositories/${info.repositoryId}/pullrequests?api-version=7.1`, cred.auth, {
      method: 'POST',
      body: { sourceRefName: `refs/heads/${branch}`, targetRefName: `refs/heads/${info.defaultBranch}`, title, description },
    });
    return `https://dev.azure.com/${encodeURIComponent(organization)}/${encodeURIComponent(project)}/_git/${encodeURIComponent(name)}/pullrequest/${request.pullRequestId}`;
  },
  async trigger(cred, repo, info, inputs) {
    const { name, api } = azureParts(repo);
    let id = await azurePipelineId(cred, repo);
    if (!id) {
      // First run: register .cloudhop/azure-pipelines.yml as a pipeline named "cloudhop".
      const created = await http(`${api}/pipelines?api-version=7.1`, cred.auth, {
        method: 'POST',
        body: { name: 'cloudhop', folder: '\\', configuration: { type: 'yaml', path: '/.cloudhop/azure-pipelines.yml', repository: { id: info.repositoryId, name, type: 'azureReposGit' } } },
      });
      id = created.id;
    }
    await http(`${api}/pipelines/${id}/runs?api-version=7.1`, cred.auth, {
      method: 'POST',
      body: { resources: { repositories: { self: { refName: `refs/heads/${info.defaultBranch}` } } }, templateParameters: { action: inputs.action, cloud: inputs.cloud, source: inputs.source || ' ' } },
    });
  },
  async runs(cred, repo) {
    const id = await azurePipelineId(cred, repo);
    if (!id) return [];
    const { api } = azureParts(repo);
    const data = await http(`${api}/pipelines/${id}/runs?api-version=7.1`, cred.auth);
    return data.value.slice(0, 10).map((run) => ({
      id: run.id, title: `Cloudhop run ${run.name}`, url: run._links?.web?.href, startedAt: run.createdDate,
      ...statusFrom(run.state !== 'completed', run.result === 'succeeded', run.result === 'failed'),
    }));
  },
  async listRepos(cred) {
    const data = await http(`https://dev.azure.com/${encodeURIComponent(cred.organization)}/_apis/git/repositories?api-version=7.1`, cred.auth);
    return data.value.map((repo) => `${cred.organization}/${repo.project.name}/${repo.name}`);
  },
};

export const ADAPTERS = { github, gitlab, bitbucket, azure };

export async function readManifests(adapter, cred, repo, info) {
  const { entries, truncated } = await adapter.listPaths(cred, repo, info);
  const { selected, total } = selectManifests(entries);
  const files = await Promise.all(selected.map(async ({ path }) => {
    try {
      return { path, text: await adapter.readFile(cred, repo, info, path) };
    } catch {
      return null;
    }
  }));
  const warnings = [];
  if (truncated) warnings.push('Repo is very large, so only part of its file list was read.');
  if (total > MAX_FILES) warnings.push(`Read the ${MAX_FILES} shallowest of ${total} manifest files.`);
  if (files.includes(null)) warnings.push('Some manifest files could not be downloaded.');
  return { files: files.filter(Boolean), warnings, paths: new Set(entries.map((entry) => entry.path)) };
}
