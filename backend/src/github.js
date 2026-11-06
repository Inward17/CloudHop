import { MANIFESTS } from './analyze.js';
import { decrypt } from './store.js';

const SKIP = /(^|\/)(node_modules|vendor|dist|build|examples?|samples?|tests?|__tests__|fixtures|docs|\.github|\.cloudhop)\//;
const MAX_FILES = 40; // Workers free plan allows 50 subrequests per request.
const MAX_BYTES = 200_000;

export class RepoError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// Accepting only owner/repo on github.com keeps this from fetching arbitrary URLs.
export function parseRepo(input) {
  const match = String(input ?? '').trim().match(/^(?:https?:\/\/github\.com\/)?([\w.-]+)\/([\w.-]+?)(?:\.git)?\/?$/);
  if (!match || match[1].startsWith('.') || match[2].startsWith('.')) {
    throw new RepoError(400, 'Enter a GitHub repo, like owner/name or https://github.com/owner/name');
  }
  return { owner: match[1], repo: match[2] };
}

export async function githubApi(path, token, options = {}) {
  const response = await fetch(`https://api.github.com${path}`, {
    method: options.method ?? 'GET',
    headers: {
      'User-Agent': 'cloudhop',
      Accept: 'application/vnd.github+json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (response.ok) return response.status === 204 ? null : response.json();
  if (response.status === 401) throw new RepoError(401, 'GitHub rejected the credentials. Reconnect the GitHub account.');
  if (response.status === 404) throw new RepoError(404, 'GitHub could not find that. Check the repo name, account access, and that the Cloudhop PR is merged.');
  if (response.status === 403 || response.status === 429) throw new RepoError(429, 'GitHub refused this: rate limit reached or missing permission.');
  const detail = await response.json().catch(() => ({}));
  throw new RepoError(502, `GitHub returned ${response.status}${detail.message ? `: ${detail.message}` : ''}`);
}

/* GitHub App tokens, the way Vercel and Render connect repos: nothing long-lived is stored,
   only the installation id. Each use gets a token that expires within an hour. */

const b64url = (input) => {
  const bytes = typeof input === 'string' ? new TextEncoder().encode(input) : new Uint8Array(input);
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
};

// The key must be PKCS#8 ("BEGIN PRIVATE KEY"). GitHub downloads PKCS#1, so the README shows the one-line conversion.
export async function appJwt(appId, privateKeyPem, now = Date.now()) {
  if (!privateKeyPem.includes('BEGIN PRIVATE KEY')) throw new RepoError(500, 'GITHUB_APP_PRIVATE_KEY must be PKCS#8. See README.');
  const der = Uint8Array.from(atob(privateKeyPem.replace(/-----[^-]+-----|\s/g, '')), (char) => char.charCodeAt(0));
  const key = await crypto.subtle.importKey('pkcs8', der, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['sign']);
  const seconds = Math.floor(now / 1000);
  const unsigned = `${b64url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }))}.${b64url(JSON.stringify({ iat: seconds - 60, exp: seconds + 540, iss: String(appId) }))}`;
  const signature = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, new TextEncoder().encode(unsigned));
  return `${unsigned}.${b64url(signature)}`;
}

const installationTokens = new Map(); // installation id -> { token, expires }, per Worker instance only

async function installationToken(env, installationId) {
  const cached = installationTokens.get(installationId);
  if (cached && cached.expires > Date.now() + 60_000) return cached.token;
  const result = await githubApi(`/app/installations/${installationId}/access_tokens`, await appJwt(env.GITHUB_APP_ID, env.GITHUB_APP_PRIVATE_KEY), { method: 'POST' });
  installationTokens.set(installationId, { token: result.token, expires: Date.parse(result.expires_at) });
  return result.token;
}

export async function installationOwner(env, installationId) {
  const installation = await githubApi(`/app/installations/${installationId}`, await appJwt(env.GITHUB_APP_ID, env.GITHUB_APP_PRIVATE_KEY));
  return installation.account.login;
}

export async function tokenForAccount(env, account) {
  if (account.kind === 'github_app') return installationToken(env, JSON.parse(account.data).installationId);
  if (account.kind === 'github_token') return decrypt(account.secret_enc, env.ENCRYPTION_KEY, account.id);
  throw new RepoError(400, 'That account is not a GitHub account');
}

export async function listRepos(account, token) {
  if (account.kind === 'github_app') {
    const data = await githubApi('/installation/repositories?per_page=100', token);
    return data.repositories.map((repo) => repo.full_name);
  }
  const repos = await githubApi('/user/repos?per_page=100&sort=updated', token);
  return repos.map((repo) => repo.full_name);
}

export async function fetchManifests({ owner, repo }, { token } = {}) {
  const tree = await githubApi(`/repos/${owner}/${repo}/git/trees/HEAD?recursive=1`, token);
  const wanted = tree.tree
    .filter((entry) => entry.type === 'blob' && entry.size <= MAX_BYTES && !SKIP.test(entry.path))
    .filter((entry) => MANIFESTS.has(entry.path.slice(entry.path.lastIndexOf('/') + 1)))
    .sort((a, b) => a.path.split('/').length - b.path.split('/').length);
  const selected = wanted.slice(0, MAX_FILES);

  const files = await Promise.all(selected.map(async ({ path }) => {
    const encoded = path.split('/').map(encodeURIComponent).join('/');
    const headers = { 'User-Agent': 'cloudhop', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
    const response = await fetch(`https://raw.githubusercontent.com/${owner}/${repo}/HEAD/${encoded}`, { headers });
    return response.ok ? { path, text: await response.text() } : null;
  }));

  const warnings = [];
  if (tree.truncated) warnings.push('Repo is very large, so GitHub returned only part of its file list.');
  if (wanted.length > MAX_FILES) warnings.push(`Read the ${MAX_FILES} shallowest of ${wanted.length} manifest files.`);
  if (files.includes(null)) warnings.push('Some manifest files could not be downloaded.');
  return { files: files.filter(Boolean), warnings };
}

// One commit on a new branch plus a PR. Merging the PR is the user's approval to install.
export async function openInstallPr(repoName, token, files) {
  const repoInfo = await githubApi(`/repos/${repoName}`, token);
  const base = repoInfo.default_branch;
  const head = await githubApi(`/repos/${repoName}/git/ref/heads/${encodeURIComponent(base)}`, token);
  const baseCommit = await githubApi(`/repos/${repoName}/git/commits/${head.object.sha}`, token);
  const tree = await githubApi(`/repos/${repoName}/git/trees`, token, {
    method: 'POST',
    body: {
      base_tree: baseCommit.tree.sha,
      tree: files.map((file) => ({ path: file.path, mode: file.executable ? '100755' : '100644', type: 'blob', content: file.content })),
    },
  });
  const commit = await githubApi(`/repos/${repoName}/git/commits`, token, {
    method: 'POST',
    body: { message: 'Add Cloudhop deployment files', tree: tree.sha, parents: [head.object.sha] },
  });
  const branch = `cloudhop/install-${commit.sha.slice(0, 8)}`;
  await githubApi(`/repos/${repoName}/git/refs`, token, { method: 'POST', body: { ref: `refs/heads/${branch}`, sha: commit.sha } });
  const pr = await githubApi(`/repos/${repoName}/pulls`, token, {
    method: 'POST',
    body: {
      title: 'Add Cloudhop deployment files',
      head: branch,
      base,
      body: 'Adds the workflow, infrastructure and Compose files Cloudhop uses. Nothing runs until you merge this and start an action from Cloudhop.\n\nCloud access is limited to the `cloudhop` GitHub environment. Add required reviewers to that environment in repo settings for a second approval on every run.\n\nCheck generated Dockerfiles carefully: start commands are best guesses.',
    },
  });
  return pr.html_url;
}

export async function dispatchWorkflow(repoName, token, inputs) {
  const repoInfo = await githubApi(`/repos/${repoName}`, token);
  await githubApi(`/repos/${repoName}/actions/workflows/cloudhop.yml/dispatches`, token, {
    method: 'POST',
    body: { ref: repoInfo.default_branch, inputs },
  });
}

export async function listRuns(repoName, token) {
  const data = await githubApi(`/repos/${repoName}/actions/workflows/cloudhop.yml/runs?per_page=10`, token);
  return data.workflow_runs.map((run) => ({
    id: run.id, title: run.display_title, status: run.status, conclusion: run.conclusion, url: run.html_url, startedAt: run.run_started_at,
  }));
}
