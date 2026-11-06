// Dashboard sign-in. Follows the usual hosted-platform pattern: Sign in with GitHub (or an admin token
// for local testing), then a random session id in an HttpOnly cookie. Only its hash is stored, and the
// browser never holds a secret that scripts can read.
import { randomToken, sha256 } from './store.js';

const SESSION_COOKIE = 'cloudhop_session';
const STATE_COOKIE = 'cloudhop_state';
const SESSION_SECONDS = 7 * 24 * 3600;

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
  return {
    github: Boolean(env.GITHUB_CLIENT_ID && env.GITHUB_CLIENT_SECRET && env.ALLOWED_GITHUB_USERS),
    adminToken: Boolean(env.ADMIN_TOKEN),
    githubApp: Boolean(env.GITHUB_APP_ID && env.GITHUB_APP_SLUG && env.GITHUB_APP_PRIVATE_KEY),
  };
}

export async function createSession(env, user) {
  const id = randomToken();
  await env.DB.prepare('INSERT INTO sessions (id_hash, user, expires_at) VALUES (?, ?, ?)')
    .bind(await sha256(id), user, Date.now() + SESSION_SECONDS * 1000).run();
  return cookie(env, SESSION_COOKIE, id, SESSION_SECONDS);
}

export async function currentUser(request, env) {
  const id = readCookie(request, SESSION_COOKIE);
  if (!id) return null;
  const row = await env.DB.prepare('SELECT user FROM sessions WHERE id_hash = ? AND expires_at > ?').bind(await sha256(id), Date.now()).first();
  return row?.user ?? null;
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

export async function adminLogin(request, env) {
  if (!env.ADMIN_TOKEN) throw new AuthError(404, 'Admin token sign-in is turned off');
  const body = await request.json().catch(() => ({}));
  if (typeof body.token !== 'string' || (await sha256(body.token)) !== (await sha256(env.ADMIN_TOKEN))) throw new AuthError(401, 'Wrong admin token');
  return new Response(JSON.stringify({ user: 'admin' }), { headers: { 'Content-Type': 'application/json', 'Set-Cookie': await createSession(env, 'admin') } });
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

const redirect = (location, cookies = []) => {
  const headers = new Headers({ Location: location });
  for (const value of cookies) headers.append('Set-Cookie', value);
  return new Response(null, { status: 302, headers });
};
export { redirect };

export function githubLoginRedirect(env) {
  if (!loginMethods(env).github) throw new AuthError(404, 'GitHub sign-in is not set up');
  const { state, setCookie } = startState(env);
  const url = new URL('https://github.com/login/oauth/authorize');
  url.searchParams.set('client_id', env.GITHUB_CLIENT_ID);
  url.searchParams.set('redirect_uri', `${env.PUBLIC_URL}/api/auth/callback`);
  url.searchParams.set('state', state);
  return redirect(url.toString(), [setCookie]);
}

// The GitHub user token is used once to learn who signed in, then dropped.
export async function githubLoginCallback(request, env) {
  const url = new URL(request.url);
  checkState(request, url);
  const exchange = await fetch('https://github.com/login/oauth/access_token', {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_id: env.GITHUB_CLIENT_ID, client_secret: env.GITHUB_CLIENT_SECRET, code: url.searchParams.get('code') }),
  });
  const { access_token: token } = await exchange.json();
  if (!token) throw new AuthError(401, 'GitHub sign-in failed');
  const user = await fetch('https://api.github.com/user', { headers: { Authorization: `Bearer ${token}`, 'User-Agent': 'cloudhop' } }).then((response) => response.json());
  const allowed = env.ALLOWED_GITHUB_USERS.split(',').map((name) => name.trim().toLowerCase()).filter(Boolean);
  if (!user.login || !allowed.includes(user.login.toLowerCase())) return redirect(`${env.PUBLIC_URL}/#/denied`, [cookie(env, STATE_COOKIE, '', 0)]);
  return redirect(`${env.PUBLIC_URL}/#/`, [await createSession(env, `github:${user.login}`), cookie(env, STATE_COOKIE, '', 0)]);
}
