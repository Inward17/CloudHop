import { analyzeFiles } from './analyze.js';
import {
  AuthError, INSTALL_COOKIE, STATE, adminLogin, allowedLogin, checkOrigin, checkState, clearCookie, cookie, createSession, currentSession,
  endSession, exchangeCode, githubAsUser, githubAuthorize, loginMethods, readCookie, redirect, upsertGithubUser,
} from './auth.js';
import { bootstrapFor, cloudConfig, deployIssues, installFiles, validateCloudInput } from './generate.js';
import { dispatchWorkflow, fetchManifests, githubApi, installationOwner, listRepos, listRuns, openInstallPr, parseRepo, RepoError, tokenForAccount } from './github.js';
import { proposalFor } from './intent.js';
import { rankPlans } from './plans.js';
import { portabilityReport } from './portability.js';
import { encrypt, ensureSchema, nextState, publicAccount, publicProject, publicUser, randomToken, sha256, summarizeChecks, uptimeSummary } from './store.js';

const json = (body, status = 200, headers = {}) => Response.json(body, { status, headers });
const DAY = 24 * 3600 * 1000;
const ACTIONS = new Set(['verify', 'plan', 'deploy', 'switch', 'rollback', 'destroy', 'report']);
const CLOUDS = new Set(['aws', 'gcp']);
const RANGES = { '24h': [DAY, 48], '7d': [7 * DAY, 56] };

function parseCredits(raw) {
  if (raw === undefined) return [];
  if (!Array.isArray(raw) || raw.length > 20) throw new RepoError(400, 'Credits must be a list of at most 20 items');
  return raw.map((credit) => {
    const valid = CLOUDS.has(credit?.cloud) && Number.isFinite(credit.amount) && credit.amount > 0 && !Number.isNaN(Date.parse(credit.expiresOn));
    if (!valid) throw new RepoError(400, 'Each credit needs a cloud, a positive amount and an expiry date');
    return { cloud: credit.cloud, amount: credit.amount, expiresOn: credit.expiresOn };
  });
}

async function readJson(request) {
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== 'object') throw new RepoError(400, 'Request body must be JSON');
  return body;
}

// Another user's row looks exactly like a missing one, so ids reveal nothing.
async function getRow(env, table, id, userId) {
  const row = await env.DB.prepare(`SELECT * FROM ${table} WHERE id = ? AND user_id = ?`).bind(id, userId).first();
  if (!row) throw new RepoError(404, table === 'projects' ? 'Project not found' : 'Account not found');
  return row;
}

async function projectToken(env, row) {
  return tokenForAccount(env, await getRow(env, 'accounts', row.account_id, row.user_id));
}

async function analyze(request, env) {
  const body = await readJson(request);
  const repo = parseRepo(body.repo);
  const credits = parseCredits(body.credits);
  const requestsPerDay = body.requestsPerDay ?? 1000;
  if (!Number.isFinite(requestsPerDay) || requestsPerDay < 0 || requestsPerDay > 1e9) throw new RepoError(400, 'Requests per day is out of range');
  const { files, warnings } = await fetchManifests(repo, { token: env.GITHUB_TOKEN });
  const map = analyzeFiles(files);
  return json({
    repo: `${repo.owner}/${repo.repo}`,
    filesRead: files.map((file) => file.path),
    warnings,
    map,
    portability: portabilityReport(map),
    deployIssues: deployIssues(map),
    ...rankPlans(map, { requestsPerDay, credits }),
  });
}

/* Sign-in callback: handles both sign-up/sign-in and confirming a GitHub App install. */

async function githubCallback(request, env, session) {
  const url = new URL(request.url);
  checkState(request, url);
  const token = await exchangeCode(env, url.searchParams.get('code'));
  const pendingInstall = readCookie(request, INSTALL_COOKIE);
  const cleared = [clearCookie(env, STATE), clearCookie(env, INSTALL_COOKIE)];

  if (pendingInstall) {
    // The installation id in GitHub's redirect can be forged, so only accept installations
    // this GitHub user can actually see.
    if (!session) return redirect(`${env.PUBLIC_URL}/#/`, cleared);
    const { installations } = await githubAsUser(token, '/user/installations?per_page=100');
    if (!installations.some((item) => String(item.id) === pendingInstall)) return redirect(`${env.PUBLIC_URL}/#/accounts?install=denied`, cleared);
    const owner = await installationOwner(env, pendingInstall);
    const existing = await env.DB.prepare("SELECT id FROM accounts WHERE user_id = ? AND kind = 'github_app' AND json_extract(data, '$.installationId') = ?").bind(session.user.id, pendingInstall).first();
    if (!existing) {
      await env.DB.prepare('INSERT INTO accounts (id, user_id, kind, label, data, created_at) VALUES (?, ?, ?, ?, ?, ?)')
        .bind(crypto.randomUUID(), session.user.id, 'github_app', owner, JSON.stringify({ installationId: pendingInstall, owner }), Date.now()).run();
    }
    return redirect(`${env.PUBLIC_URL}/#/accounts`, cleared);
  }

  const profile = await githubAsUser(token, '/user');
  if (!allowedLogin(env, profile.login)) return redirect(`${env.PUBLIC_URL}/#/denied`, cleared);
  const { id, isNew } = await upsertGithubUser(env, profile);
  return redirect(`${env.PUBLIC_URL}/#/${isNew ? '?welcome' : ''}`, [...cleared, await createSession(env, id, request)]);
}

/* Profile */

async function profile(env, session) {
  const { results } = await env.DB.prepare('SELECT id_hash, agent, created_at, expires_at FROM sessions WHERE user_id = ? AND expires_at > ? ORDER BY created_at DESC')
    .bind(session.user.id, Date.now()).all();
  const counts = await env.DB.prepare('SELECT (SELECT COUNT(*) FROM projects WHERE user_id = ?1) AS projects, (SELECT COUNT(*) FROM accounts WHERE user_id = ?1) AS accounts').bind(session.user.id).first();
  return json({
    user: publicUser(session.user),
    counts,
    // A short prefix of the hash identifies a session without exposing anything usable.
    sessions: results.map((row) => ({ id: row.id_hash.slice(0, 12), agent: row.agent, createdAt: row.created_at, expiresAt: row.expires_at, current: row.id_hash === session.sessionHash })),
  });
}

async function updateProfile(request, env, session) {
  const body = await readJson(request);
  const name = typeof body.name === 'string' ? body.name.trim().slice(0, 80) : '';
  const email = typeof body.email === 'string' ? body.email.trim().slice(0, 254) : '';
  if (!name) throw new RepoError(400, 'Enter a name');
  if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new RepoError(400, 'Enter a valid email or leave it empty');
  await env.DB.prepare('UPDATE users SET name = ?, email = ? WHERE id = ?').bind(name, email || null, session.user.id).run();
  return json({ ok: true });
}

async function endOtherSessions(env, session) {
  await env.DB.prepare('DELETE FROM sessions WHERE user_id = ? AND id_hash != ?').bind(session.user.id, session.sessionHash).run();
  return json({ ok: true });
}

// Removes everything Cloudhop stores for this user. Cloud resources and repo files are untouched.
async function deleteProfile(request, env, session) {
  const body = await readJson(request);
  if (body.confirm !== session.user.login) throw new RepoError(400, 'Type your username to confirm');
  const id = session.user.id;
  const mine = 'SELECT id FROM projects WHERE user_id = ?';
  await env.DB.batch([
    env.DB.prepare(`DELETE FROM checks WHERE project_id IN (${mine})`).bind(id),
    env.DB.prepare(`DELETE FROM reports WHERE project_id IN (${mine})`).bind(id),
    env.DB.prepare('DELETE FROM projects WHERE user_id = ?').bind(id),
    env.DB.prepare('DELETE FROM accounts WHERE user_id = ?').bind(id),
    env.DB.prepare('DELETE FROM sessions WHERE user_id = ?').bind(id),
    env.DB.prepare('DELETE FROM users WHERE id = ?').bind(id),
  ]);
  return json({ ok: true }, 200, { 'Set-Cookie': cookie(env, 'cloudhop_session', '', 0) });
}

/* Accounts: GitHub connections and cloud accounts, saved once and reused by projects. */

async function listAccounts(env, userId) {
  const { results } = await env.DB.prepare('SELECT * FROM accounts WHERE user_id = ? ORDER BY created_at DESC').bind(userId).all();
  const { results: usage } = await env.DB.prepare('SELECT account_id, COUNT(*) AS n FROM projects WHERE user_id = ? GROUP BY account_id').bind(userId).all();
  const used = new Map(usage.map((row) => [row.account_id, row.n]));
  return json({ accounts: results.map((row) => ({ ...publicAccount(row), projects: used.get(row.id) ?? 0 })), methods: loginMethods(env) });
}

async function createAccount(request, env, userId) {
  const body = await readJson(request);
  const id = crypto.randomUUID();
  let label;
  let data = {};
  let secret = null;
  if (body.kind === 'github_token') {
    if (typeof body.token !== 'string' || body.token.length < 20) throw new RepoError(400, 'Enter a GitHub token');
    const owner = await githubApi('/user', body.token);
    label = owner.login;
    secret = await encrypt(body.token, env.ENCRYPTION_KEY, id);
  } else if (CLOUDS.has(body.kind)) {
    if (!validateCloudInput(body.kind, body)) throw new RepoError(400, 'Check the account details and region');
    data = body.kind === 'aws'
      ? { accountId: body.accountId, region: body.region }
      : { projectId: body.projectId, projectNumber: body.projectNumber, region: body.region };
    label = body.kind === 'aws' ? `AWS ${body.accountId}` : `GCP ${body.projectId}`;
  } else {
    throw new RepoError(400, 'Unknown account type');
  }
  if (typeof body.label === 'string' && body.label.trim()) label = body.label.trim().slice(0, 60);
  await env.DB.prepare('INSERT INTO accounts (id, user_id, kind, label, data, secret_enc, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .bind(id, userId, body.kind, label, JSON.stringify(data), secret, Date.now()).run();
  return json({ id, kind: body.kind, label }, 201);
}

async function deleteAccount(env, id, userId) {
  await getRow(env, 'accounts', id, userId);
  const used = await env.DB.prepare('SELECT COUNT(*) AS n FROM projects WHERE account_id = ? AND user_id = ?').bind(id, userId).first();
  if (used.n > 0) throw new RepoError(409, 'Projects still use this GitHub account. Remove them first.');
  await env.DB.prepare('DELETE FROM accounts WHERE id = ? AND user_id = ?').bind(id, userId).run();
  return json({ ok: true });
}

// After GitHub installs the app, confirm through a GitHub sign-in that this user really owns that installation.
function githubInstallDone(request, env) {
  const installationId = new URL(request.url).searchParams.get('installation_id');
  if (!/^\d+$/.test(installationId ?? '')) throw new RepoError(400, 'Missing installation');
  return githubAuthorize(env, [cookie(env, INSTALL_COOKIE, installationId, 600)]);
}

async function accountRepos(env, id, userId) {
  const account = await getRow(env, 'accounts', id, userId);
  return json({ repos: await listRepos(account, await tokenForAccount(env, account)) });
}

/* Projects */

async function createProject(request, env, userId) {
  const body = await readJson(request);
  const { owner, repo } = parseRepo(body.repo);
  const account = await getRow(env, 'accounts', String(body.accountId ?? ''), userId);
  const name = `${owner}/${repo}`;
  await githubApi(`/repos/${name}`, await tokenForAccount(env, account)); // Fails early if the account cannot reach the repo.
  const existing = await env.DB.prepare('SELECT id FROM projects WHERE repo = ? AND user_id = ?').bind(name, userId).first();
  if (existing) throw new RepoError(409, 'That repo is already added');
  const id = crypto.randomUUID();
  const reportToken = randomToken();
  await env.DB.prepare('INSERT INTO projects (id, user_id, repo, account_id, report_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)')
    .bind(id, userId, name, account.id, await sha256(reportToken), Date.now()).run();
  // Shown once. Only its hash is stored.
  return json({ id, repo: name, reportToken }, 201);
}

async function connectCloud(request, env, id, cloud, userId) {
  if (!CLOUDS.has(cloud)) throw new RepoError(400, 'Unknown cloud');
  let body = await readJson(request);
  if (body.accountId) {
    const account = await getRow(env, 'accounts', String(body.accountId), userId);
    if (account.kind !== cloud) throw new RepoError(400, `That is not a ${cloud.toUpperCase()} account`);
    body = { ...JSON.parse(account.data), ...(body.region ? { region: body.region } : {}) };
  }
  if (!validateCloudInput(cloud, body)) throw new RepoError(400, 'Check the account details and region');
  const row = await getRow(env, 'projects', id, userId);
  const clouds = { ...JSON.parse(row.clouds), [cloud]: cloudConfig(cloud, body) };
  await env.DB.prepare('UPDATE projects SET clouds = ? WHERE id = ? AND user_id = ?').bind(JSON.stringify(clouds), id, userId).run();
  return json({ clouds, bootstrap: bootstrapFor(cloud, row.repo, body), bootstrapName: cloud === 'aws' ? 'cloudhop-aws.yml' : 'cloudhop-gcp.sh' });
}

async function install(request, env, id, userId) {
  const row = await getRow(env, 'projects', id, userId);
  const clouds = JSON.parse(row.clouds);
  if (Object.keys(clouds).length === 0) throw new RepoError(400, 'Connect a cloud first');
  const [owner, repo] = row.repo.split('/');
  const token = await projectToken(env, row);
  const { files } = await fetchManifests({ owner, repo }, { token });
  const map = analyzeFiles(files);
  const issues = deployIssues(map);
  if (issues.length) return json({ error: 'This app cannot be deployed yet', issues }, 422);
  const { origin } = new URL(request.url);
  const prUrl = await openInstallPr(row.repo, token, installFiles({ repo: row.repo, map, clouds, cloudhopUrl: origin }));
  const portability = portabilityReport(map).worst;
  await env.DB.prepare('UPDATE projects SET pr_url = ?, portability = ? WHERE id = ? AND user_id = ?').bind(prUrl, portability, id, userId).run();
  return json({ prUrl, portability, reachable: !/localhost|127\.0\.0\.1/.test(origin) });
}

async function runAction(request, env, id, userId) {
  const body = await readJson(request);
  const row = await getRow(env, 'projects', id, userId);
  const clouds = JSON.parse(row.clouds);
  if (!ACTIONS.has(body.action) || !CLOUDS.has(body.cloud) || !clouds[body.cloud]) throw new RepoError(400, 'That action or cloud is not available');
  if (body.approved !== true) throw new RepoError(400, 'Action needs approval');
  if (body.action === 'destroy' && body.confirmText !== body.cloud) throw new RepoError(400, 'Type the cloud name to confirm');
  const source = body.source ?? '';
  if ((body.action === 'switch' || body.action === 'rollback') && (!CLOUDS.has(source) || source === body.cloud)) throw new RepoError(400, 'Needs a different source cloud');
  await dispatchWorkflow(row.repo, await projectToken(env, row), { action: body.action, cloud: body.cloud, source });
  return json({ started: true }, 202);
}

async function status(env, id, userId) {
  const row = await getRow(env, 'projects', id, userId);
  const [checks, reports] = await env.DB.batch([
    env.DB.prepare('SELECT at, ok, status, ms FROM checks WHERE project_id = ? AND at > ? ORDER BY at').bind(id, Date.now() - DAY),
    env.DB.prepare('SELECT at, event, data FROM reports WHERE project_id = ? ORDER BY at DESC LIMIT 20').bind(id),
  ]);
  const events = reports.results.map((report) => ({ ...JSON.parse(report.data), at: report.at, event: report.event }));
  let runs = [];
  let runsError = null;
  if (row.pr_url) {
    try {
      runs = await listRuns(row.repo, await projectToken(env, row));
    } catch (error) {
      if (!(error instanceof RepoError)) throw error;
      runsError = error.message;
    }
  }
  return json({
    project: publicProject(row),
    uptime: uptimeSummary(checks.results),
    timeline: checks.results.map((check) => ({ at: check.at, ok: check.ok, ms: check.ms })),
    events,
    latestCost: events.find((event) => event.event === 'report') ?? null,
    runs,
    runsError,
  });
}

async function metrics(env, id, range, userId) {
  await getRow(env, 'projects', id, userId);
  const [span, bucketCount] = RANGES[range] ?? RANGES['24h'];
  const now = Date.now();
  const since = now - span;
  const [checks, costs] = await env.DB.batch([
    env.DB.prepare('SELECT at, ok, status, ms FROM checks WHERE project_id = ? AND at > ? ORDER BY at').bind(id, since),
    env.DB.prepare("SELECT at, data FROM reports WHERE project_id = ? AND event = 'report' ORDER BY at DESC LIMIT 60").bind(id),
  ]);
  const reports = costs.results.map((row) => ({ at: row.at, ...JSON.parse(row.data) })).reverse();
  return json({
    range: range in RANGES ? range : '24h',
    since,
    now,
    ...summarizeChecks(checks.results, since, now, bucketCount),
    costs: reports.filter((report) => typeof report.costMonthToDate === 'number').map((report) => ({ at: report.at, cloud: report.cloud, amount: report.costMonthToDate })),
    resources: [...reports].reverse().find((report) => report.resources)?.resources ?? null,
  });
}

// One call for the home dashboard: this user's projects with latest uptime and spend, plus recent activity.
async function home(env, userId) {
  const mine = 'SELECT id FROM projects WHERE user_id = ?';
  const [projects, events, costs, uptime] = await env.DB.batch([
    env.DB.prepare('SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC').bind(userId),
    env.DB.prepare('SELECT r.at, r.event, r.data, p.id AS project_id, p.repo FROM reports r JOIN projects p ON p.id = r.project_id WHERE p.user_id = ? ORDER BY r.at DESC LIMIT 15').bind(userId),
    env.DB.prepare(`SELECT project_id, data FROM reports WHERE id IN (SELECT MAX(id) FROM reports WHERE event = 'report' AND project_id IN (${mine}) GROUP BY project_id)`).bind(userId),
    env.DB.prepare(`SELECT project_id, AVG(ok) * 100 AS uptime, AVG(CASE WHEN ok = 1 THEN ms END) AS ms FROM checks WHERE at > ? AND project_id IN (${mine}) GROUP BY project_id`).bind(Date.now() - DAY, userId),
  ]);
  const costBy = new Map(costs.results.map((row) => [row.project_id, JSON.parse(row.data)]));
  const uptimeBy = new Map(uptime.results.map((row) => [row.project_id, row]));
  return json({
    projects: projects.results.map((row) => {
      const cost = costBy.get(row.id);
      const checks = uptimeBy.get(row.id);
      return {
        ...publicProject(row),
        uptimePercent: checks?.uptime ?? null,
        averageMs: checks?.ms == null ? null : Math.round(checks.ms),
        costMonthToDate: typeof cost?.costMonthToDate === 'number' ? cost.costMonthToDate : null,
        costCloud: cost?.cloud ?? null,
      };
    }),
    events: events.results.map((row) => ({ ...JSON.parse(row.data), at: row.at, event: row.event, projectId: row.project_id, repo: row.repo })),
  });
}

// Reached by the workflow in the user's repo, so it uses that repo's report token, not a session.
async function receiveReport(request, env) {
  const token = request.headers.get('Authorization')?.replace(/^Bearer /, '') ?? '';
  const row = token ? await env.DB.prepare('SELECT * FROM projects WHERE report_hash = ?').bind(await sha256(token)).first() : null;
  if (!row) return json({ error: 'Unauthorized' }, 401);
  const body = await readJson(request);
  if (typeof body.event !== 'string' || !CLOUDS.has(body.cloud)) throw new RepoError(400, 'Bad report');
  const raw = JSON.stringify(body);
  if (raw.length > 20_000) throw new RepoError(413, 'Report too large');
  const url = typeof body.url === 'string' && /^https?:\/\/[\w.-]+(:\d+)?\/?$/.test(body.url) ? body.url : undefined;
  const next = nextState(publicProject(row), { ...body, url });
  await env.DB.batch([
    env.DB.prepare('INSERT INTO reports (project_id, at, event, data) VALUES (?, ?, ?, ?)').bind(row.id, Date.now(), body.event, raw),
    env.DB.prepare('UPDATE projects SET active = ?, previous = ?, url = ? WHERE id = ?').bind(next.active, next.previous, next.url, row.id),
  ]);
  return json({ ok: true });
}

async function route(request, env) {
  const url = new URL(request.url);
  const { pathname } = url;
  const method = request.method;
  if (!pathname.startsWith('/api/')) return json({ error: 'Not found' }, 404);
  if (!env.ENCRYPTION_KEY || !env.PUBLIC_URL) return json({ error: 'Server is missing ENCRYPTION_KEY or PUBLIC_URL. See README.' }, 500);
  await ensureSchema(env.DB);

  // Public routes.
  if (pathname === '/api/report' && method === 'POST') return receiveReport(request, env);
  if (pathname === '/api/auth/methods') return json(loginMethods(env));
  if (pathname === '/api/auth/github') return githubAuthorize(env);
  if (pathname === '/api/auth/callback') return githubCallback(request, env, await currentSession(request, env));
  if (pathname === '/api/auth/login' && method === 'POST') {
    checkOrigin(request, env);
    return adminLogin(request, env);
  }

  // Everything below needs a signed-in session, and only ever touches that user's rows.
  const session = await currentSession(request, env);
  if (!session) return json({ error: 'Sign in required' }, 401);
  checkOrigin(request, env);
  const userId = session.user.id;

  if (pathname === '/api/auth/me') return json({ user: publicUser(session.user), methods: loginMethods(env) });
  if (pathname === '/api/auth/logout' && method === 'POST') return json({ ok: true }, 200, { 'Set-Cookie': await endSession(request, env) });
  if (pathname === '/api/profile' && method === 'GET') return profile(env, session);
  if (pathname === '/api/profile' && method === 'PATCH') return updateProfile(request, env, session);
  if (pathname === '/api/profile' && method === 'DELETE') return deleteProfile(request, env, session);
  if (pathname === '/api/profile/sessions' && method === 'DELETE') return endOtherSessions(env, session);
  if (pathname === '/api/github/install') {
    if (!loginMethods(env).githubApp) throw new RepoError(404, 'GitHub App is not set up. See README.');
    return redirect(`https://github.com/apps/${encodeURIComponent(env.GITHUB_APP_SLUG)}/installations/new`);
  }
  if (pathname === '/api/github/setup') return githubInstallDone(request, env);
  if (pathname === '/api/analyze' && method === 'POST') return analyze(request, env);
  if (pathname === '/api/home' && method === 'GET') return home(env, userId);
  if (pathname === '/api/accounts' && method === 'GET') return listAccounts(env, userId);
  if (pathname === '/api/accounts' && method === 'POST') return createAccount(request, env, userId);
  const account = pathname.match(/^\/api\/accounts\/([\w-]+)(\/repos)?$/);
  if (account && method === 'DELETE' && !account[2]) return deleteAccount(env, account[1], userId);
  if (account && method === 'GET' && account[2]) return accountRepos(env, account[1], userId);
  if (pathname === '/api/projects' && method === 'GET') {
    const { results } = await env.DB.prepare('SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC').bind(userId).all();
    return json({ projects: results.map(publicProject) });
  }
  if (pathname === '/api/projects' && method === 'POST') return createProject(request, env, userId);

  const match = pathname.match(/^\/api\/projects\/([\w-]+)(?:\/([\w-]+))?(?:\/([\w-]+))?$/);
  if (!match) return json({ error: 'Not found' }, 404);
  const [, id, section, arg] = match;
  if (section === 'status' && method === 'GET') return status(env, id, userId);
  if (section === 'metrics' && method === 'GET') return metrics(env, id, url.searchParams.get('range'), userId);
  if (section === 'clouds' && method === 'PUT') return connectCloud(request, env, id, arg, userId);
  if (section === 'install' && method === 'POST') return install(request, env, id, userId);
  if (section === 'run' && method === 'POST') return runAction(request, env, id, userId);
  if (section === 'command' && method === 'POST') {
    const body = await readJson(request);
    return json(proposalFor(body.text, publicProject(await getRow(env, 'projects', id, userId))));
  }
  return json({ error: 'Not found' }, 404);
}

async function checkSite(env, project) {
  const started = Date.now();
  let ok = 0;
  let code = null;
  try {
    const response = await fetch(project.url, { redirect: 'follow', signal: AbortSignal.timeout(10_000) });
    code = response.status;
    ok = code < 500 ? 1 : 0;
  } catch {
    // Unreachable counts as down.
  }
  await env.DB.prepare('INSERT INTO checks (project_id, at, ok, status, ms) VALUES (?, ?, ?, ?, ?)')
    .bind(project.id, Date.now(), ok, code, ok ? Date.now() - started : null).run();
}

export default {
  async fetch(request, env) {
    try {
      return await route(request, env);
    } catch (error) {
      if (error instanceof RepoError || error instanceof AuthError) return json({ error: error.message }, error.status);
      console.error(error);
      return json({ error: 'Something went wrong' }, 500);
    }
  },

  async scheduled(event, env) {
    await ensureSchema(env.DB);
    const { results } = await env.DB.prepare('SELECT * FROM projects WHERE url IS NOT NULL').all();
    const when = new Date(event.scheduledTime);
    await Promise.allSettled(results.map(async (row) => {
      await checkSite(env, row);
      // Once a day, ask the user's workflow for cost and server data.
      if (row.active && when.getUTCHours() === 6 && when.getUTCMinutes() < 5) {
        await dispatchWorkflow(row.repo, await projectToken(env, row), { action: 'report', cloud: row.active, source: '' });
      }
    }));
    await env.DB.prepare('DELETE FROM checks WHERE at < ?').bind(Date.now() - 7 * DAY).run();
    await env.DB.prepare('DELETE FROM sessions WHERE expires_at < ?').bind(Date.now()).run();
  },
};
