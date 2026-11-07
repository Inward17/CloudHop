import { MAX_FILES, selectManifests } from './analyze.js';


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

export async function installationToken(env, installationId) {
  const cached = installationTokens.get(installationId);
  if (cached && cached.expires > Date.now() + 60_000) return cached.token;
  const result = await githubApi(`/app/installations/${installationId}/access_tokens`, await appJwt(env.GITHUB_APP_ID, env.GITHUB_APP_PRIVATE_KEY), { method: 'POST' });
  installationTokens.set(installationId, { token: result.token, expires: Date.parse(result.expires_at) });
  return result.token;
}

// Owner, repo selection ("all" or "selected") and the permissions the person granted.
export async function installationDetails(env, installationId) {
  return githubApi(`/app/installations/${installationId}`, await appJwt(env.GITHUB_APP_ID, env.GITHUB_APP_PRIVATE_KEY));
}

export async function installationOwner(env, installationId) {
  return (await installationDetails(env, installationId)).account.login;
}

export async function fetchManifests({ owner, repo }, { token } = {}) {
  const tree = await githubApi(`/repos/${owner}/${repo}/git/trees/HEAD?recursive=1`, token);
  const { selected, total } = selectManifests(tree.tree.filter((entry) => entry.type === 'blob'));

  const files = await Promise.all(selected.map(async ({ path }) => {
    const encoded = path.split('/').map(encodeURIComponent).join('/');
    const headers = { 'User-Agent': 'cloudhop', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
    const response = await fetch(`https://raw.githubusercontent.com/${owner}/${repo}/HEAD/${encoded}`, { headers });
    return response.ok ? { path, text: await response.text() } : null;
  }));

  const warnings = [];
  if (tree.truncated) warnings.push('Repo is very large, so GitHub returned only part of its file list.');
  if (total > MAX_FILES) warnings.push(`Read the ${MAX_FILES} shallowest of ${total} manifest files.`);
  if (files.includes(null)) warnings.push('Some manifest files could not be downloaded.');
  return { files: files.filter(Boolean), warnings };
}
