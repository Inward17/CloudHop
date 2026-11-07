// Demo backend, bundled into demo.js. Runs in the browser with sample data that resets on reload.
// `modules` holds the real backend modules, so commands, metrics and setup files match production.
const { summarizeChecks, uptimeSummary, nextState, randomToken } = modules['store.js'];
const { proposalFor } = modules['intent.js'];
const { analyzeFiles } = modules['analyze.js'];
const { portabilityReport } = modules['portability.js'];
const { rankPlans } = modules['plans.js'];
const { cloudAccess, cloudConfig, projectsUsingCloud, bootstrapFor, deployIssues, trustFor, validateCloudInput } = modules['generate.js'];
const { parseRepoFor, providerOf, PROVIDERS } = modules['providers.js'];
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
  { id: 'gitlab', kind: 'gitlab_token', label: 'demo-user', data: {}, createdAt: start - 3 * DAY },
  { id: 'gcp-sandbox', kind: 'gcp', label: 'GCP sandbox', data: { projectId: 'demo-project', projectNumber: '123456789', region: 'us-central1' }, createdAt: start - 9 * DAY },
  { id: 'cf', kind: 'cloudflare', label: 'demo-org', data: {}, createdAt: start - 4 * DAY },
  { id: 'vc', kind: 'vercel', label: 'demo-org', data: {}, createdAt: start - 4 * DAY },
];
const projects = [
  {
    id: 'storefront', provider: 'github', repo: 'demo-org/storefront', accountId: 'gh',
    clouds: { aws: cloudConfig('aws', accounts[1].data, 'demo-org/storefront'), gcp: cloudConfig('gcp', accounts[3].data, 'demo-org/storefront') },
    active: 'gcp', previous: 'aws', url: DEMO_URLS.gcp, prUrl: `${REPO_URL}/pulls`, portability: 'auto', createdAt: start - 8 * DAY,
  },
  { id: 'blog', provider: 'gitlab', repo: 'demo-org/blog', accountId: 'gitlab', clouds: {}, active: null, previous: null, url: null, prUrl: null, portability: null, createdAt: start - 2 * HOUR },
  { id: 'docs', kind: 'platform', hosting: {}, provider: 'github', repo: 'demo-org/docs', accountId: 'gh', clouds: {}, active: null, previous: null, url: null, prUrl: null, portability: null, createdAt: start - HOUR },
];
for (const item of projects) { item.kind ??= 'cloud'; item.hosting ??= {}; }
const reports = [];
const checks = [];
const runs = { storefront: [], blog: [], docs: [] };

// Hosting moves: a sample docs site on Cloudflare Pages that can move to Vercel.
const DEMO_SITE = {
  platform: 'cloudflare_pages', id: 'docs', name: 'docs', accountId: 'cf', accountLabel: 'demo-org', url: 'https://docs-demo.pages.dev', domains: ['docs.example.com'], branch: 'main',
  settings: { buildCommand: 'npm run build', outputDirectory: 'dist', rootDirectory: null, framework: null },
  variables: [{ name: 'PUBLIC_API_URL', secret: false, value: 'https://api.example.com' }, { name: 'SEARCH_API_KEY', secret: true, value: null }],
  manageUrl: 'https://dash.cloudflare.com/', deleteUrl: 'https://dash.cloudflare.com/',
};
const secretsBy = {};
const moves = [];
const MOVE_STEPS = (item, plan) => [
  { id: 'create', title: `Create the Vercel project, linked to ${item.repo}`, who: 'agent' },
  { id: 'variables', title: `Copy ${plan.copy.length} setting${plan.copy.length === 1 ? '' : 's'} and add ${plan.secrets.length} secret${plan.secrets.length === 1 ? '' : 's'}`, who: 'agent' },
  { id: 'deploy', title: 'Build and deploy the main branch', who: 'agent' },
  { id: 'verify', title: 'Check the new address loads and matches the current site', who: 'agent' },
  { id: 'domain', title: 'Move the domain', who: 'you approve', comingSoon: true },
  { id: 'old', title: 'Pause or delete the old site', who: 'you choose', optional: true },
];
function runDemoMove(item, move) {
  // Each step finishes about a second after the previous one, like the real agent but faster.
  const order = ['create', 'variables', 'deploy', 'verify'];
  const details = { create: 'Created docs on Vercel.', variables: 'Settings copied, secrets in place.', deploy: 'Built and deployed.', verify: 'https://docs-demo.vercel.app answered 200. Page title matches the current site.' };
  if (move.state.secretsMode === 'manual' && !move.state.manualDone) {
    move.state.steps.create = { status: 'done', detail: details.create };
    move.state.steps.variables = { status: 'waiting', detail: `Add these on Vercel: ${move.plan.secrets.join(', ')}. Then continue.`, action: 'secrets', link: 'https://vercel.com/dashboard', missing: move.plan.secrets };
    move.status = 'waiting';
    return;
  }
  order.forEach((key, index) => setTimeout(() => {
    move.state.steps[key] = { status: 'done', detail: details[key] };
    if (key === 'verify') {
      move.state.steps.domain = { status: 'skipped', detail: 'Coming soon. Move your domain on the platform for now.' };
      move.state.steps.old = { status: 'pending', detail: 'Your old site is still live. Pause it or delete it when you are ready.' };
      move.status = 'done';
      item.hosting = { previous: item.hosting.current ?? null, current: { platform: 'vercel', id: 'prj_demo', name: 'docs', accountId: 'vc', url: 'https://docs-demo.vercel.app', domains: [], settings: move.plan.settings, variables: [], manageUrl: 'https://vercel.com/dashboard', deleteUrl: 'https://vercel.com/dashboard' } };
      Object.assign(item, { url: 'https://docs-demo.vercel.app', active: 'vercel', previous: 'cloudflare_pages' });
      addReport(item.id, Date.now(), 'moved', { from: 'cloudflare_pages', to: 'vercel', url: item.url });
    }
  }, 900 * (index + 1)));
}

// A week of history for the storefront: deploy on AWS, move to GCP, daily cost reports, uptime checks.
function addReport(projectId, at, event, data) {
  reports.push({ projectId, at, event, ...data, event });
}
addReport('storefront', start - 7 * DAY, 'verified', { cloud: 'aws' });
// What a verify run reports back: the cloud's own answer on which actions the deploy identity may do.
const ACCESS_CHECKS = {
  aws: [['ec2:RunInstances', true], ['ec2:TerminateInstances', true], ['ec2:AuthorizeSecurityGroupIngress', true], ['ssm:SendCommand', true], ['ce:GetCostAndUsage', true],
    ['iam:CreateUser', false], ['iam:CreateAccessKey', false], ['iam:AttachUserPolicy', false], ['iam:PutRolePolicy', false], ['rds:DeleteDBInstance', false], ['lambda:CreateFunction', false], ['kms:ScheduleKeyDeletion', false], ['organizations:LeaveOrganization', false]],
  gcp: [['compute.instances.create', true], ['compute.instances.delete', true], ['compute.firewalls.create', true], ['storage.buckets.create', true], ['iam.serviceAccounts.actAs', true],
    ['resourcemanager.projects.setIamPolicy', false], ['resourcemanager.projects.delete', false], ['iam.serviceAccountKeys.create', false], ['cloudsql.instances.delete', false], ['container.clusters.delete', false], ['secretmanager.versions.access', false], ['bigquery.datasets.delete', false]],
};
const accessReport = (cloud) => ({ cloud, checks: ACCESS_CHECKS[cloud].map(([action, allowed]) => ({ action, allowed })) });
addReport('storefront', start - 7 * DAY + 1000, 'access', accessReport('aws'));
addReport('storefront', start - 2 * DAY - 600_000, 'access', accessReport('gcp'));

// Sample answers from each code host about what an account can reach.
function codeAccess(item) {
  const later = (days) => new Date(start + days * DAY).toISOString().slice(0, 10);
  const base = { kind: 'code', selection: 'selected', owner: null, more: false, permissions: null, scopes: null, expires: null, note: null };
  if (item.kind === 'github_app') {
    return { ...base, owner: 'demo-org', repos: [{ name: 'demo-org/storefront', access: null }, { name: 'demo-org/docs', access: null }], total: 2,
      permissions: [['actions', 'write'], ['contents', 'write'], ['metadata', 'read'], ['pull_requests', 'write'], ['workflows', 'write']].map(([name, level]) => ({ name, level })),
      note: 'Each use gets a new token that expires within an hour. These permissions apply to every repo listed.', manageUrl: 'https://github.com/settings/installations' };
  }
  if (item.kind === 'github_token') {
    return { ...base, repos: [{ name: 'demo-user/storefront', access: 'admin' }], total: 1, expires: later(30), manageUrl: 'https://github.com/settings/tokens',
      note: 'Fine-grained token: GitHub does not report its permissions through the API. The repos listed are the ones it can reach.' };
  }
  if (item.kind === 'gitlab_token') {
    return { ...base, repos: [{ name: 'demo-org/blog', access: 'maintainer' }, { name: 'demo-org/infra', access: 'developer' }], total: 2, scopes: ['api'], expires: later(60), manageUrl: 'https://gitlab.com/-/user_settings/personal_access_tokens' };
  }
  if (item.kind === 'bitbucket_token') {
    return { ...base, repos: [{ name: 'demo-team/shop', access: 'write' }], total: 1, manageUrl: 'https://id.atlassian.com/manage-profile/security/api-tokens',
      note: 'Bitbucket did not report this token\'s scopes. You can see them where you created it.' };
  }
  return { ...base, owner: item.data.organization, repos: [{ name: `${item.data.organization}/web/web`, access: null }], total: 1, manageUrl: `https://dev.azure.com/${item.data.organization}/_usersSettings/tokens`,
    note: 'Azure DevOps does not let a token read its own scopes. You can see them in Azure DevOps under User settings, Personal access tokens.' };
}
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

const methods = { github: false, adminToken: true, githubApp: false, openSignup: false };
const me = { id: 'demo', login: 'demo-user', name: 'Demo User', email: null, avatarUrl: null, viaGithub: true, createdAt: start - 9 * DAY, lastLoginAt: start };
const sessions = [
  { id: 'current', agent: navigator.userAgent, createdAt: start, expiresAt: start + 7 * DAY, current: true },
  { id: 'laptop', agent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15', createdAt: start - 2 * DAY, expiresAt: start + 5 * DAY, current: false },
];
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
    if (action === 'verify') addReport(item.id, Date.now() + 1, 'access', accessReport(cloud));
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
  if (path === '/auth/login' && method === 'POST') { signedIn = true; return { ok: true }; }
  if (!signedIn) fail(401, 'Sign in required');
  if (path === '/auth/me') return { user: me, methods };
  if (path === '/profile' && method === 'GET') return { user: me, counts: { projects: projects.length, accounts: accounts.length }, sessions };
  if (path === '/profile' && method === 'PATCH') {
    if (!body.name?.trim()) fail(400, 'Enter a name');
    if (body.email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(body.email)) fail(400, 'Enter a valid email or leave it empty');
    Object.assign(me, { name: body.name.trim(), email: body.email?.trim() || null });
    return { ok: true };
  }
  if (path === '/profile/sessions' && method === 'DELETE') { sessions.splice(0, sessions.length, ...sessions.filter((session) => session.current)); return { ok: true }; }
  if (path === '/profile' && method === 'DELETE') {
    if (body.confirm !== me.login) fail(400, 'Type your username to confirm');
    fail(400, 'This is a demo, so the account stays. In the real app this removes everything Cloudhop stores for you.');
  }
  if (path === '/auth/logout') { signedIn = false; return { ok: true }; }

  if (path === '/home') {
    return {
      projects: projects.map(homeProject),
      events: reports.slice().sort((a, b) => b.at - a.at).slice(0, 15).map(({ projectId, ...event }) => ({ ...event, projectId, repo: project(projectId).repo })),
    };
  }
  if (path === '/accounts' && method === 'GET') {
    return { accounts: accounts.map((item) => ({ ...item, projects: (CLOUDS.includes(item.kind) ? projectsUsingCloud(item, projects) : projects.filter((p) => p.accountId === item.id)).length })), methods };
  }
  if (path === '/accounts' && method === 'POST') {
    const id = randomToken().slice(0, 12);
    if (['github_token', 'gitlab_token', 'bitbucket_token', 'azure_token'].includes(body.kind)) {
      if (typeof body.token !== 'string' || body.token.length < 20) fail(400, 'Enter an access token');
      if (body.kind === 'bitbucket_token' && !/^[^\s@]+@[^\s@]+$/.test(body.email ?? '')) fail(400, 'Enter your Atlassian account email and an API token');
      if (body.kind === 'azure_token' && !/^[\w.-]{1,50}$/.test(body.organization ?? '')) fail(400, 'Enter your Azure DevOps organization name');
      const data = body.kind === 'azure_token' ? { organization: body.organization } : {};
      accounts.unshift({ id, kind: body.kind, label: body.kind === 'azure_token' ? `demo-user (${body.organization})` : 'demo-user', data, createdAt: Date.now() });
    } else if (['render', 'vercel', 'cloudflare'].includes(body.kind)) {
      if (typeof body.token !== 'string' || body.token.length < 20) fail(400, 'Enter an API token');
      accounts.unshift({ id, kind: body.kind, label: body.label?.trim() || 'demo-team', data: {}, createdAt: Date.now() });
    } else if (CLOUDS.includes(body.kind)) {
      if (!validateCloudInput(body.kind, body)) fail(400, 'Check the account details and region');
      const data = body.kind === 'aws' ? { accountId: body.accountId, region: body.region } : { projectId: body.projectId, projectNumber: body.projectNumber, region: body.region };
      accounts.unshift({ id, kind: body.kind, label: body.label?.trim() || (body.kind === 'aws' ? `AWS ${body.accountId}` : `GCP ${body.projectId}`), data, createdAt: Date.now() });
    } else fail(400, 'Unknown account type');
    return { id };
  }
  const accountMatch = path.match(/^\/accounts\/([\w-]+)(\/repos|\/access)?$/);
  if (accountMatch && accountMatch[2] === '/access') {
    const item = account(accountMatch[1]);
    if (['render', 'vercel', 'cloudflare'].includes(item.kind)) {
      const urls = { render: 'https://dashboard.render.com/u/settings#api-keys', vercel: 'https://vercel.com/account/settings/tokens', cloudflare: 'https://dash.cloudflare.com/profile/api-tokens' };
      return { kind: 'platform', platform: item.kind, scopes: [{ name: 'demo-team', detail: item.kind === 'cloudflare' ? 'Account' : 'Workspace' }], expires: new Date(start + 30 * DAY).toISOString(), note: 'Sample data: the real check asks the platform which workspaces or accounts the token reaches.', manageUrl: urls[item.kind] };
    }
    return CLOUDS.includes(item.kind) ? cloudAccess(item, projects, reports) : codeAccess(item);
  }
  if (accountMatch && accountMatch[2]) {
    const provider = providerOf(account(accountMatch[1]).kind);
    const owner = provider === 'azure' ? `${account(accountMatch[1]).data.organization}/web` : 'demo-org';
    return { repos: ['storefront', 'blog', 'api', 'docs'].map((name) => `${owner}/${name}`) };
  }
  if (accountMatch && method === 'DELETE') {
    account(accountMatch[1]);
    if (projects.some((p) => p.accountId === accountMatch[1])) fail(409, 'Projects still use this GitHub account. Remove them first.');
    accounts.splice(accounts.findIndex((item) => item.id === accountMatch[1]), 1);
    return { ok: true };
  }
  const hostingMatch = path.match(/^\/projects\/([\w-]+)\/(hosting\/find|hosting\/current|hosting\/pause|secrets|moves|moves\/latest|moves\/([\w-]+)\/(approve|continue))$/);
  if (hostingMatch) {
    const item = projects.find((p) => p.id === hostingMatch[1]) ?? fail(404, 'Project not found');
    const section = hostingMatch[2];
    if (section === 'hosting/find') return { sites: item.id === 'docs' ? [DEMO_SITE] : [], searched: ['demo-org (Cloudflare)', 'demo-org (Vercel)'], errors: [] };
    if (section === 'hosting/current') {
      if (body.siteId !== DEMO_SITE.id) fail(404, 'That site was not found for this repo');
      item.hosting = { ...item.hosting, current: DEMO_SITE };
      Object.assign(item, { url: 'https://docs.example.com', active: 'cloudflare_pages' });
      addReport(item.id, Date.now(), 'hosting_found', { platform: 'cloudflare_pages', name: 'docs', url: DEMO_SITE.url });
      return { current: DEMO_SITE };
    }
    if (section === 'hosting/pause') {
      const site = item.hosting[body.which ?? 'previous'] ?? fail(404, 'No such site');
      if (site.platform === 'cloudflare_pages') fail(400, `Cloudflare Pages cannot pause a site. Delete it yourself from its settings: ${site.deleteUrl}`);
      site.paused = true;
      addReport(item.id, Date.now(), 'paused', { platform: site.platform, name: site.name, by: me.login });
      return { paused: true };
    }
    if (section === 'secrets') {
      secretsBy[item.id] ??= new Set();
      if (method === 'PUT') for (const name of Object.keys(body.values ?? {})) secretsBy[item.id].add(name);
      return { names: [...secretsBy[item.id]].sort() };
    }
    if (section === 'moves' && method === 'POST') {
      if (body.to !== 'vercel') fail(400, 'Moving there is not available yet');
      const current = item.hosting.current;
      const variables = current?.variables ?? [];
      const secrets = variables.filter((v) => v.secret).map((v) => v.name);
      const have = secretsBy[item.id] ?? new Set();
      const plan = {
        repo: item.repo, to: 'vercel', accountId: body.accountId, from: current ? { platform: current.platform, name: current.name, url: current.url, domains: current.domains } : null,
        settings: Object.fromEntries(Object.entries(current?.settings ?? {}).filter(([, v]) => v)), copy: variables.filter((v) => !v.secret).map((v) => v.name), secrets, missingSecrets: secrets.filter((n) => !have.has(n)),
        gitAccess: `Vercel needs its GitHub app on ${item.repo} to build from it and deploy on every push. If it is missing, Cloudhop shows the link and waits.`,
        keepsOldSite: 'The current site stays live until you pause it. Cloudhop never deletes it; you get the link to do that yourself.',
      };
      plan.steps = MOVE_STEPS(item, plan);
      const move = { id: randomToken().slice(0, 10), status: 'planned', createdAt: Date.now(), plan, state: {} };
      moves.unshift({ ...move, projectId: item.id });
      return move;
    }
    if (section === 'moves/latest') return { move: moves.find((m) => m.projectId === item.id) ?? null };
    const move = moves.find((m) => m.id === hostingMatch[3]) ?? fail(404, 'Move not found');
    if (hostingMatch[4] === 'approve') {
      if (body.secretsMode === 'cloudhop' && move.plan.secrets.some((n) => !(secretsBy[item.id] ?? new Set()).has(n))) fail(400, `Enter these secrets first, or choose to add them yourself: ${move.plan.missingSecrets.join(', ')}`);
      move.status = 'running';
      move.state = { secretsMode: body.secretsMode, steps: Object.fromEntries(move.plan.steps.map((s) => [s.id, { status: 'pending' }])), by: me.login };
      move.state.steps.create = { status: 'running' };
      addReport(item.id, Date.now(), 'move_started', { to: 'vercel', by: me.login });
      runDemoMove(item, move);
      return { started: true };
    }
    move.status = 'running';
    move.state.manualDone = true;
    move.state.steps.variables = { status: 'running' };
    runDemoMove(item, move);
    return { started: true };
  }
  const rotate = path.match(/^\/projects\/([\w-]+)\/report-token$/);
  if (rotate && method === 'POST') {
    const item = projects.find((p) => p.id === rotate[1]) ?? fail(404, 'Project not found');
    addReport(item.id, Date.now(), 'report_token_rotated', { by: me.login });
    return { reportToken: randomToken() };
  }
  const removed = path.match(/^\/projects\/([\w-]+)$/);
  if (removed && method === 'DELETE') {
    const index = projects.findIndex((p) => p.id === removed[1]);
    if (index < 0) fail(404, 'Project not found');
    if (body.confirm !== projects[index].repo) fail(400, 'Type the repo name to confirm');
    projects.splice(index, 1);
    return { ok: true };
  }
  if (path === '/projects' && method === 'GET') return { projects };
  if (path === '/projects' && method === 'POST') {
    const provider = providerOf(account(body.accountId).kind);
    const name = parseRepoFor(provider, body.repo);
    if (projects.some((p) => p.repo === name)) fail(409, 'That repo is already added');
    const id = name.replace(/[^\w-]/g, '-');
    projects.unshift({ id, provider, repo: name, accountId: body.accountId, clouds: {}, active: null, previous: null, url: null, prUrl: null, portability: null, createdAt: Date.now(), kind: body.kind === 'platform' ? 'platform' : 'cloud', hosting: {} });
    runs[id] ??= [];
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
    item.clouds = { ...item.clouds, [arg]: cloudConfig(arg, input, item.repo) };
    // Sample identifiers stand in for what the real backend reads from the code host.
    const [first, second] = item.repo.split('/');
    const info = {
      github: { repo: item.repo },
      gitlab: { repo: item.repo, defaultBranch: 'main' },
      bitbucket: { workspace: first, workspaceUuid: '{00000000-0000-4000-8000-000000000001}', repositoryUuid: '{00000000-0000-4000-8000-000000000002}' },
      azure: { organization: first, project: second ?? 'web', organizationId: '00000000-0000-4000-8000-000000000003' },
    }[item.provider];
    return { clouds: item.clouds, bootstrap: bootstrapFor(arg, item.repo, input, trustFor(item.provider, info)), bootstrapName: arg === 'aws' ? 'cloudhop-aws.yml' : 'cloudhop-gcp.sh' };
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
    if (runs[item.id].some((run) => run.status !== 'completed')) fail(409, 'Another action is still running for this project. Wait for it to finish.');
    simulateRun(item, body.action, body.cloud, body.source);
    addReport(item.id, Date.now(), 'requested', { action: body.action, cloud: body.cloud, source: body.source ?? '', by: me.login });
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
banner.append(Object.assign(document.createElement('button'), { type: 'button', textContent: '×', ariaLabel: 'Hide demo notice', onclick: () => banner.remove() }));
document.body.append(banner);
