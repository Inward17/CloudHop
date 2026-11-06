// Demo backend, bundled into demo.js. Runs in the browser with sample data that resets on reload.
// `modules` holds the real backend modules, so commands, metrics and setup files match production.
const { summarizeChecks, uptimeSummary, nextState, randomToken } = modules['store.js'];
const { proposalFor } = modules['intent.js'];
const { analyzeFiles } = modules['analyze.js'];
const { portabilityReport } = modules['portability.js'];
const { rankPlans } = modules['plans.js'];
const { cloudConfig, bootstrapFor, deployIssues, validateCloudInput } = modules['generate.js'];
const { parseRepo, fetchManifests, RepoError } = modules['github.js'];

const REPO_URL = 'https://github.com/Inward17/CloudHop';
const DAY = 24 * 3600 * 1000;
const HOUR = 3600 * 1000;
const start = Date.now();
const CLOUDS = ['aws', 'gcp'];
const DEMO_URLS = { aws: 'http://203-0-113-10.sslip.io', gcp: 'http://203-0-113-20.sslip.io' }; // Documentation-only addresses.

let signedIn = true;
let seed = 7;
const random = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
const fail = (status, message) => { throw Object.assign(new Error(message), { status }); };

const accounts = [
  { id: 'gh', kind: 'github_app', label: 'demo-org', data: { installationId: '1000001', owner: 'demo-org' }, createdAt: start - 9 * DAY },
  { id: 'aws-sandbox', kind: 'aws', label: 'AWS sandbox', data: { accountId: '123456789012', region: 'us-east-1' }, createdAt: start - 9 * DAY },
  { id: 'gcp-sandbox', kind: 'gcp', label: 'GCP sandbox', data: { projectId: 'demo-project', projectNumber: '123456789', region: 'us-central1' }, createdAt: start - 9 * DAY },
];
const projects = [
  {
    id: 'storefront', repo: 'demo-org/storefront', accountId: 'gh',
    clouds: { aws: cloudConfig('aws', accounts[1].data), gcp: cloudConfig('gcp', accounts[2].data) },
    active: 'gcp', previous: 'aws', url: DEMO_URLS.gcp, prUrl: `${REPO_URL}/pulls`, portability: 'auto', createdAt: start - 8 * DAY,
  },
  { id: 'blog', repo: 'demo-org/blog', accountId: 'gh', clouds: {}, active: null, previous: null, url: null, prUrl: null, portability: null, createdAt: start - 2 * HOUR },
];
const reports = [];
const checks = [];
const runs = { storefront: [], blog: [] };

// A week of history for the storefront: deploy on AWS, move to GCP, daily cost reports, uptime checks.
function addReport(projectId, at, event, data) {
  reports.push({ projectId, at, event, ...data, event });
}
addReport('storefront', start - 7 * DAY, 'verified', { cloud: 'aws' });
addReport('storefront', start - 7 * DAY + 600_000, 'deployed', { cloud: 'aws', url: DEMO_URLS.aws });
addReport('storefront', start - 2 * DAY, 'switched', { cloud: 'gcp', from: 'aws', url: DEMO_URLS.gcp, downtimeSeconds: 42 });
for (let day = 6; day >= 0; day--) {
  addReport('storefront', start - day * DAY - 3 * HOUR, 'report', {
    cloud: 'aws',
    costMonthToDate: Number((1.2 + (6 - day) * 0.61).toFixed(2)),
    credits: [{ amount: String(950 - (6 - day) * 0.6), ends: 1 }],
    resources: { cpuLoadPercent: 18 + ((6 - day) * 5) % 30, memoryPercent: 52 + (6 - day) * 2, diskPercent: 31 },
  });
}
for (let at = start - 7 * DAY + 300_000; at < start; at += 300_000) {
  const hoursAgo = (start - at) / HOUR;
  const down = (hoursAgo > 30 && hoursAgo < 30.4) || (hoursAgo > 100 && hoursAgo < 101.1) || random() < 0.003;
  const daily = 1 + 0.35 * Math.sin((at / HOUR / 24) * 2 * Math.PI);
  checks.push({ projectId: 'storefront', at, ok: down ? 0 : 1, status: down ? (random() < 0.5 ? 503 : null) : 200, ms: down ? null : Math.round(70 * daily + random() * 40 + (random() < 0.03 ? 250 : 0)) });
}
runs.storefront.push(
  { id: 3, title: 'cloudhop report aws', status: 'completed', conclusion: 'success', url: `${REPO_URL}/actions`, startedAt: new Date(start - 3 * HOUR).toISOString() },
  { id: 2, title: 'cloudhop switch gcp', status: 'completed', conclusion: 'success', url: `${REPO_URL}/actions`, startedAt: new Date(start - 2 * DAY).toISOString() },
  { id: 1, title: 'cloudhop deploy aws', status: 'completed', conclusion: 'success', url: `${REPO_URL}/actions`, startedAt: new Date(start - 7 * DAY).toISOString() },
);

const methods = { github: false, adminToken: true, githubApp: false };
const project = (id) => projects.find((item) => item.id === id) ?? fail(404, 'Project not found');
const account = (id) => accounts.find((item) => item.id === id) ?? fail(404, 'Account not found');
const eventsFor = (id) => reports.filter((report) => report.projectId === id).sort((a, b) => b.at - a.at).map(({ projectId, ...event }) => event);
const checksSince = (id, since) => checks.filter((check) => check.projectId === id && check.at > since);

// Approving an action starts a pretend workflow run that finishes a few seconds later.
function simulateRun(item, action, cloud, source) {
  const run = { id: Date.now(), title: `cloudhop ${action} ${cloud}`, status: 'in_progress', conclusion: null, url: `${REPO_URL}/actions`, startedAt: new Date().toISOString() };
  runs[item.id].unshift(run);
  setTimeout(() => {
    run.status = 'completed';
    run.conclusion = 'success';
    const events = {
      verify: { event: 'verified', cloud },
      deploy: { event: 'deployed', cloud, url: DEMO_URLS[cloud] },
      switch: { event: 'switched', cloud, from: source, url: DEMO_URLS[cloud], downtimeSeconds: 30 + Math.round(Math.random() * 40) },
      rollback: { event: 'rolled_back', cloud, from: source, url: DEMO_URLS[cloud] },
      destroy: { event: 'destroyed', cloud },
      report: { event: 'report', cloud, ...(cloud === 'aws' ? { costMonthToDate: 5.9, credits: [{ amount: '946.10', ends: 1 }] } : {}), resources: { cpuLoadPercent: 22, memoryPercent: 61, diskPercent: 32 } },
    };
    const report = events[action];
    if (!report) return;
    addReport(item.id, Date.now(), report.event, report);
    Object.assign(item, nextState(item, report));
  }, 5000);
}

function homeProject(item) {
  const recent = checksSince(item.id, Date.now() - DAY);
  const summary = uptimeSummary(recent);
  const cost = eventsFor(item.id).find((event) => event.event === 'report' && typeof event.costMonthToDate === 'number');
  return { ...item, uptimePercent: summary.uptimePercent, averageMs: summary.averageMs, costMonthToDate: cost?.costMonthToDate ?? null, costCloud: cost?.cloud ?? null };
}

async function handle(method, path, query, body) {
  if (path === '/auth/methods') return methods;
  if (path === '/auth/login' && method === 'POST') { signedIn = true; return { user: 'demo' }; }
  if (!signedIn) fail(401, 'Sign in required');
  if (path === '/auth/me') return { user: 'demo', methods };
  if (path === '/auth/logout') { signedIn = false; return { ok: true }; }

  if (path === '/home') {
    return {
      projects: projects.map(homeProject),
      events: reports.slice().sort((a, b) => b.at - a.at).slice(0, 15).map(({ projectId, ...event }) => ({ ...event, projectId, repo: project(projectId).repo })),
    };
  }
  if (path === '/accounts' && method === 'GET') {
    return { accounts: accounts.map((item) => ({ ...item, projects: projects.filter((p) => p.accountId === item.id).length })), methods };
  }
  if (path === '/accounts' && method === 'POST') {
    const id = randomToken().slice(0, 12);
    if (body.kind === 'github_token') {
      if (typeof body.token !== 'string' || body.token.length < 20) fail(400, 'Enter a GitHub token');
      accounts.unshift({ id, kind: 'github_token', label: 'demo-user', data: {}, createdAt: Date.now() });
    } else if (CLOUDS.includes(body.kind)) {
      if (!validateCloudInput(body.kind, body)) fail(400, 'Check the account details and region');
      const data = body.kind === 'aws' ? { accountId: body.accountId, region: body.region } : { projectId: body.projectId, projectNumber: body.projectNumber, region: body.region };
      accounts.unshift({ id, kind: body.kind, label: body.label?.trim() || (body.kind === 'aws' ? `AWS ${body.accountId}` : `GCP ${body.projectId}`), data, createdAt: Date.now() });
    } else fail(400, 'Unknown account type');
    return { id };
  }
  const accountMatch = path.match(/^\/accounts\/([\w-]+)(\/repos)?$/);
  if (accountMatch && accountMatch[2]) { account(accountMatch[1]); return { repos: ['demo-org/storefront', 'demo-org/blog', 'demo-org/api', 'demo-org/docs'] }; }
  if (accountMatch && method === 'DELETE') {
    account(accountMatch[1]);
    if (projects.some((p) => p.accountId === accountMatch[1])) fail(409, 'Projects still use this GitHub account. Remove them first.');
    accounts.splice(accounts.findIndex((item) => item.id === accountMatch[1]), 1);
    return { ok: true };
  }
  if (path === '/projects' && method === 'GET') return { projects };
  if (path === '/projects' && method === 'POST') {
    const { owner, repo } = parseRepo(body.repo);
    account(body.accountId);
    const name = `${owner}/${repo}`;
    if (projects.some((p) => p.repo === name)) fail(409, 'That repo is already added');
    const id = name.replace(/[^\w-]/g, '-');
    projects.unshift({ id, repo: name, accountId: body.accountId, clouds: {}, active: null, previous: null, url: null, prUrl: null, portability: null, createdAt: Date.now() });
    runs[id] = [];
    return { id, repo: name, reportToken: `demo-${randomToken().slice(0, 24)}` };
  }
  if (path === '/analyze' && method === 'POST') {
    const repo = parseRepo(body.repo);
    const { files, warnings } = await fetchManifests(repo);
    const map = analyzeFiles(files);
    return { repo: `${repo.owner}/${repo.repo}`, filesRead: files.map((file) => file.path), warnings, map, portability: portabilityReport(map), deployIssues: deployIssues(map), ...rankPlans(map, { requestsPerDay: body.requestsPerDay ?? 1000, credits: body.credits ?? [] }) };
  }

  const match = path.match(/^\/projects\/([\w-]+)\/(\w+)(?:\/(\w+))?$/);
  if (!match) fail(404, 'Not found');
  const [, id, section, arg] = match;
  const item = project(id);
  if (section === 'status') {
    const recent = checksSince(id, Date.now() - DAY);
    const events = eventsFor(id).slice(0, 20);
    return {
      project: item, uptime: uptimeSummary(recent), timeline: recent.map(({ at, ok, ms }) => ({ at, ok, ms })), events,
      latestCost: events.find((event) => event.event === 'report') ?? null, runs: runs[id], runsError: item.prUrl ? null : 'Not installed',
    };
  }
  if (section === 'metrics') {
    const range = query.get('range') === '7d' ? '7d' : '24h';
    const [span, buckets] = range === '7d' ? [7 * DAY, 56] : [DAY, 48];
    const now = Date.now();
    const costReports = eventsFor(id).filter((event) => event.event === 'report').reverse();
    return {
      range, since: now - span, now, ...summarizeChecks(checksSince(id, now - span), now - span, now, buckets),
      costs: costReports.filter((event) => typeof event.costMonthToDate === 'number').map((event) => ({ at: event.at, cloud: event.cloud, amount: event.costMonthToDate })),
      resources: costReports.at(-1)?.resources ?? null,
    };
  }
  if (section === 'clouds' && method === 'PUT') {
    const saved = account(body.accountId);
    if (saved.kind !== arg) fail(400, `That is not a ${arg.toUpperCase()} account`);
    const input = { ...saved.data, ...(body.region ? { region: body.region } : {}) };
    if (!validateCloudInput(arg, input)) fail(400, 'Check the account details and region');
    item.clouds = { ...item.clouds, [arg]: cloudConfig(arg, input) };
    return { clouds: item.clouds, bootstrap: bootstrapFor(arg, item.repo, input), bootstrapName: arg === 'aws' ? 'cloudhop-aws.yml' : 'cloudhop-gcp.sh' };
  }
  if (section === 'install') {
    if (!Object.keys(item.clouds).length) fail(400, 'Connect a cloud first');
    item.prUrl = `${REPO_URL}/pulls`;
    item.portability = 'auto';
    return { prUrl: item.prUrl, portability: 'auto', reachable: true };
  }
  if (section === 'command') return proposalFor(body.text, item);
  if (section === 'run') {
    if (!body.approved) fail(400, 'Action needs approval');
    if (!item.clouds[body.cloud]) fail(400, 'That action or cloud is not available');
    if (body.action === 'destroy' && body.confirmText !== body.cloud) fail(400, 'Type the cloud name to confirm');
    simulateRun(item, body.action, body.cloud, body.source);
    return { started: true };
  }
  fail(404, 'Not found');
}

const pageFetch = window.fetch.bind(window);
window.fetch = async (input, init = {}) => {
  const url = typeof input === 'string' ? input : input.url;
  if (!url.startsWith('/api/')) return pageFetch(input, init);
  const parsed = new URL(url, 'http://demo');
  await new Promise((resolve) => setTimeout(resolve, 120)); // Feels like a network call.
  try {
    const data = await handle(init.method ?? 'GET', parsed.pathname.slice(4), parsed.searchParams, init.body ? JSON.parse(init.body) : null);
    return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
  } catch (error) {
    const status = error.status ?? (error instanceof RepoError ? error.status : 500);
    return new Response(JSON.stringify({ error: error.message }), { status, headers: { 'Content-Type': 'application/json' } });
  }
};

const banner = document.createElement('div');
banner.className = 'demo-banner';
banner.append('Demo with sample data. Nothing touches real clouds, and changes reset on reload. ');
banner.append(Object.assign(document.createElement('a'), { href: REPO_URL, textContent: 'Source on GitHub' }));
document.body.append(banner);
