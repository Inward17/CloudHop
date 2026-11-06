// Sign-up and sign-in. Follows the usual hosted-platform pattern: Continue with GitHub creates the
// account on first visit, then a random session id lives in an HttpOnly cookie. Only its hash is
// stored, and the browser never holds a secret that scripts can read. No passwords are stored.
import { randomToken, sha256 } from './store.js';

const SESSION_COOKIE = 'cloudhop_session';
const STATE_COOKIE = 'cloudhop_state';
export const INSTALL_COOKIE = 'cloudhop_install';
const SESSION_SECONDS = 7 * 24 * 3600;
const LOCAL_ADMIN = 'local-admin';

export class AuthError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export function readCookie(request, name) {
  const header = request.headers.get('Cookie') ?? '';
  for (const part of header.split(';')) {
    const [key, ...rest] = part.trim().split('=');
    if (key === name) return rest.join('=');
  }
  return null;
}

export function cookie(env, name, value, maxAge) {
  const secure = String(env.PUBLIC_URL ?? '').startsWith('https://') ? '; Secure' : '';
  return `${name}=${value}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure}`;
}

export function loginMethods(env) {
  const github = Boolean(env.GITHUB_CLIENT_ID && env.GITHUB_CLIENT_SECRET);
  return {
    github,
    adminToken: Boolean(env.ADMIN_TOKEN),
    // Installing the app is verified with a GitHub sign-in, so it needs the OAuth settings too.
    githubApp: github && Boolean(env.GITHUB_APP_ID && env.GITHUB_APP_SLUG && env.GITHUB_APP_PRIVATE_KEY),
    openSignup: github && !env.ALLOWED_GITHUB_USERS,
  };
}

export async function createSession(env, userId, request) {
  const id = randomToken();
  const agent = (request.headers.get('User-Agent') ?? '').slice(0, 160);
  await env.DB.prepare('INSERT INTO sessions (id_hash, user_id, agent, created_at, expires_at) VALUES (?, ?, ?, ?, ?)')
    .bind(await sha256(id), userId, agent, Date.now(), Date.now() + SESSION_SECONDS * 1000).run();
  return cookie(env, SESSION_COOKIE, id, SESSION_SECONDS);
}

// Returns the signed-in user row plus the current session's hash, or null.
export async function currentSession(request, env) {
  const id = readCookie(request, SESSION_COOKIE);
  if (!id) return null;
  const hash = await sha256(id);
  const user = await env.DB.prepare('SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id_hash = ? AND s.expires_at > ?').bind(hash, Date.now()).first();
  return user ? { user, sessionHash: hash } : null;
}

export async function endSession(request, env) {
  const id = readCookie(request, SESSION_COOKIE);
  if (id) await env.DB.prepare('DELETE FROM sessions WHERE id_hash = ?').bind(await sha256(id)).run();
  return cookie(env, SESSION_COOKIE, '', 0);
}

// Cookies ride along on cross-site form posts, so writes must come from our own page.
// SameSite=Lax already blocks most of these; the Origin check closes the rest.
export function checkOrigin(request, env) {
  if (request.method === 'GET' || request.method === 'HEAD') return;
  if (request.headers.get('Origin') !== new URL(env.PUBLIC_URL).origin) throw new AuthError(403, 'Request did not come from the Cloudhop site');
}

// Local testing only: one built-in user, no GitHub needed.
export async function adminLogin(request, env) {
  if (!env.ADMIN_TOKEN) throw new AuthError(404, 'Admin token sign-in is turned off');
  const body = await request.json().catch(() => ({}));
  if (typeof body.token !== 'string' || (await sha256(body.token)) !== (await sha256(env.ADMIN_TOKEN))) throw new AuthError(401, 'Wrong admin token');
  const now = Date.now();
  await env.DB.prepare(`INSERT INTO users (id, github_id, login, name, created_at, last_login_at) VALUES (?, NULL, 'admin', 'Local admin', ?, ?)
    ON CONFLICT (id) DO UPDATE SET last_login_at = excluded.last_login_at`).bind(LOCAL_ADMIN, now, now).run();
  return new Response(JSON.stringify({ ok: true }), { headers: { 'Content-Type': 'application/json', 'Set-Cookie': await createSession(env, LOCAL_ADMIN, request) } });
}

// Short-lived random state in a cookie defeats forged OAuth callbacks.
export function startState(env) {
  const state = randomToken();
  return { state, setCookie: cookie(env, STATE_COOKIE, state, 600) };
}

export function checkState(request, url) {
  const expected = readCookie(request, STATE_COOKIE);
  if (!expected || expected !== url.searchParams.get('state')) throw new AuthError(400, 'Sign-in link expired. Try again.');
}

export function redirect(location, cookies = []) {
  const headers = new Headers({ Location: location });
  for (const value of cookies) headers.append('Set-Cookie', value);
  return new Response(null, { status: 302, headers });
}

export function githubAuthorize(env, extraCookies = []) {
  if (!loginMethods(env).github) throw new AuthError(404, 'GitHub sign-in is not set up');
  const { state, setCookie } = startState(env);
  const url = new URL('https://github.com/login/oauth/authorize');
  url.searchParams.set('client_id', env.GITHUB_CLIENT_ID);
  url.searchParams.set('redirect_uri', `${env.PUBLIC_URL}/api/auth/callback`);
  url.searchParams.set('state', state);
  return redirect(url.toString(), [setCookie, ...extraCookies]);
}

// The GitHub user token is used during the callback only, then dropped.
export async function exchangeCode(env, code) {
  const response = await fetch('https://github.com/login/oauth/access_token', {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_id: env.GITHUB_CLIENT_ID, client_secret: env.GITHUB_CLIENT_SECRET, code }),
  });
  const { access_token: token } = await response.json();
  if (!token) throw new AuthError(401, 'GitHub sign-in failed');
  return token;
}

export function githubAsUser(token, path) {
  return fetch(`https://api.github.com${path}`, { headers: { Authorization: `Bearer ${token}`, 'User-Agent': 'cloudhop', Accept: 'application/vnd.github+json' } })
    .then((response) => (response.ok ? response.json() : Promise.reject(new AuthError(502, 'GitHub did not answer'))));
}

export function allowedLogin(env, login) {
  if (!env.ALLOWED_GITHUB_USERS) return true;
  return env.ALLOWED_GITHUB_USERS.split(',').map((name) => name.trim().toLowerCase()).includes(login.toLowerCase());
}

// First sign-in creates the user; later ones refresh the profile from GitHub.
export async function upsertGithubUser(env, profile) {
  const now = Date.now();
  const existing = await env.DB.prepare('SELECT id FROM users WHERE github_id = ?').bind(profile.id).first();
  const id = existing?.id ?? crypto.randomUUID();
  await env.DB.prepare(`INSERT INTO users (id, github_id, login, name, email, avatar_url, created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (id) DO UPDATE SET login = excluded.login, avatar_url = excluded.avatar_url, email = COALESCE(users.email, excluded.email), last_login_at = excluded.last_login_at`)
    .bind(id, profile.id, profile.login, profile.name ?? profile.login, profile.email ?? null, profile.avatar_url ?? null, now, now).run();
  return { id, isNew: !existing };
}

export function clearCookie(env, name) {
  return cookie(env, name, '', 0);
}
export const STATE = STATE_COOKIE;
