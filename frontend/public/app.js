const root = document.getElementById('app');
const toast = document.getElementById('toast');
const CLOUDS = ['aws', 'gcp'];
const CLOUD_NAMES = { aws: 'AWS', gcp: 'GCP' };
const MACHINES = { aws: 't3.small, Amazon Linux 2023', gcp: 'e2-small, Debian 12' };
const KIND_NAMES = { github_app: 'GitHub App', github_token: 'GitHub token', aws: 'AWS', gcp: 'GCP' };
const TIERS = {
  auto: ['Moves automatically', 'ok'], code: ['Needs code change', 'warn'],
  redesign: ['Needs redesign', 'bad'], blocked: ['Cannot move', 'bad'],
};

// Stroke icons, 24x24. Fixed path data only, built with DOM calls.
const ICONS = {
  home: 'M3 10.5 12 3l9 7.5V21h-6v-6H9v6H3z',
  folder: 'M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2h8.5A1.5 1.5 0 0 1 21 8.5v10a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z',
  search: 'M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM20 20l-4-4',
  chart: 'M4 20V11M10 20V5M16 20v-7M21 20H3',
  cloud: 'M7 18h10.5a4.5 4.5 0 0 0 .5-8.97A6 6 0 0 0 6.34 10.1 4 4 0 0 0 7 18z',
  rocket: 'M5 15c-1.5 1.5-2 5-2 5s3.5-.5 5-2M14 4c3-1 6-1 6-1s0 3-1 6l-6 6-5-5zM9 10H5l-2 3 4 1M14 15v4l-3 2-1-4',
  swap: 'M4 8h14l-4-4M20 16H6l4 4',
  undo: 'M9 14 4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3',
  refresh: 'M20 11a8 8 0 0 0-14.9-4M4 4v4h4M4 13a8 8 0 0 0 14.9 4M20 20v-4h-4',
  shield: 'M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6zM9 12l2 2 4-4',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  external: 'M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  activity: 'M3 12h4l3-8 4 16 3-8h4',
  server: 'M4 4h16v7H4zM4 13h16v7H4zM8 7.5h.01M8 16.5h.01',
  plus: 'M12 5v14M5 12h14',
  x: 'M6 6l12 12M18 6 6 18',
  menu: 'M4 6h16M4 12h16M4 18h16',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  wallet: 'M3 7h18v12H3zM3 7l3-3h12l3 3M16 13h2',
  clock: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM12 7v5l3 2',
  check: 'M5 12l4 4 10-10',
  alert: 'M12 3 2 20h20zM12 10v4M12 17h.01',
  info: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM12 11v5M12 8h.01',
  bolt: 'M13 2 4 14h7l-1 8 9-12h-7z',
  settings: 'M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1M15 4v4M9 10v4M17 16v4',
  logout: 'M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10',
  hop: 'M6 16.5a3 3 0 1 0 0 .01M18 16.5a3 3 0 1 0 0 .01M8 13c1.5-6 6.5-6 8 0',
  key: 'M14 10a4 4 0 1 0-4 4l1 1h2v2h2v2h3v-3l-4-4z',
  lock: 'M6 11h12v10H6zM8 11V8a4 4 0 0 1 8 0v3',
  users: 'M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 21v-1a6 6 0 0 1 12 0v1M16 3.5a4 4 0 0 1 0 7.5M18 15a6 6 0 0 1 4 5.5V21',
  gauge: 'M4 18a8 8 0 1 1 16 0M12 18l4-6',
  github: 'M9 19c-4.3 1.4-4.3-2.5-6-3m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.6 4.6 0 0 0-1.3-3.2 4.2 4.2 0 0 0-.1-3.2s-1.1-.3-3.5 1.3a12.3 12.3 0 0 0-6.2 0C6.5 2.8 5.4 3.1 5.4 3.1a4.2 4.2 0 0 0-.1 3.2A4.6 4.6 0 0 0 4 9.5c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21',
};
const NS = 'http://www.w3.org/2000/svg';

let user = null;
let methods = {};
let pollTimer = null;
let toastTimer = null;
let projectsCache = [];
let currentProject = null;
const reportTokens = new Map(); // Shown once after creation, so kept only in memory.

/* Helpers. Anything from a repo or API goes in as text, never HTML, because it is untrusted. */

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}
function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.append(...children);
  return node;
}
const icon = (name, size = 18) => svg('svg', { viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', class: 'icon' }, svg('path', { d: ICONS[name] }));
const text = (tag, content, className = '') => el(tag, { textContent: content, className });
const money = (value) => '$' + (value < 100 ? value.toFixed(2) : Math.round(value).toLocaleString());
const pill = (label, tone = '') => text('span', label, `pill ${tone}`);
const external = (href, label) => el('a', { href, target: '_blank', rel: 'noopener noreferrer' }, label);
const evidence = (item) => (item ? text('code', `${item.file}${item.line ? ':' + item.line : ''}`, 'muted') : null);
const notice = (message, tone = '') => el('div', { className: `notice ${tone}` }, icon(tone === 'good' ? 'check' : tone === 'info' ? 'info' : 'alert'), el('div', {}, message));
const cardHead = (title, subtitle, ...right) => el('div', { className: 'card-head' }, el('div', {}, text('h2', title), subtitle ? text('p', subtitle, 'muted small') : null), right.length ? el('div', { className: 'row' }, right) : null);

function ago(at) {
  const seconds = Math.max(0, Math.round((Date.now() - at) / 1000));
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return new Date(at).toLocaleDateString();
}
const duration = (ms) => (ms < 60_000 ? `${Math.round(ms / 1000)} s` : ms < 3_600_000 ? `${Math.round(ms / 60_000)} min` : `${(ms / 3_600_000).toFixed(1)} h`);

function brand(href = '#/') {
  return el('a', { className: 'brand', href }, el('span', { className: 'brand-mark' }, icon('hop', 18)), text('span', 'Cloudhop'));
}

function field(label, props, hint) {
  const input = el(props.tag ?? 'input', { ...props, tag: undefined });
  return [el('label', {}, label, input, hint ? text('span', hint, 'hint') : null), input];
}

function select(label, options, props = {}) {
  const input = el('select', props, options.map(([value, name]) => el('option', { value, textContent: name })));
  return [el('label', {}, label, input), input];
}

function showToast(message, kind = 'error') {
  clearTimeout(toastTimer);
  toast.textContent = message ?? '';
  toast.className = `toast ${kind}`;
  toast.hidden = !message;
  if (message) toastTimer = setTimeout(() => { toast.hidden = true; }, 7000);
}

// Same-origin calls: the HttpOnly session cookie goes along automatically and scripts never see it.
async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    method: options.method ?? 'GET',
    headers: options.body ? { 'Content-Type': 'application/json' } : {},
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && path !== '/auth/login') {
    user = null;
    renderGate();
    throw Object.assign(new Error('Signed out'), { quiet: true });
  }
  if (!response.ok) throw Object.assign(new Error(data.error ?? 'Request failed'), { issues: data.issues });
  return data;
}

// Wraps a button action: marks it busy while running and shows any failure.
function action(button, work) {
  return async (event) => {
    event?.preventDefault();
    showToast(null);
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    try {
      await work();
    } catch (error) {
      if (!error.quiet) showToast([error.message, ...(error.issues ?? [])].join(' '));
    } finally {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  };
}

function copyButton(value) {
  const button = el('button', { type: 'button', className: 'ghost small' }, icon('copy', 14), text('span', 'Copy'));
  button.onclick = async () => {
    try {
      await navigator.clipboard.writeText(value);
      button.lastChild.textContent = 'Copied';
    } catch {
      button.lastChild.textContent = 'Select and copy';
    }
    setTimeout(() => { button.lastChild.textContent = 'Copy'; }, 1800);
  };
  return button;
}

function confirmDialog(title, message, actionLabel) {
  return new Promise((resolve) => {
    const dialog = el('dialog');
    const done = (answer) => { dialog.close(); dialog.remove(); resolve(answer); };
    dialog.append(el('div', { className: 'stack' },
      el('div', { className: 'row' }, el('div', { className: 'icon-box warn' }, icon('alert', 18)), text('h2', title, 'grow')),
      text('p', message, 'muted'),
      el('div', { className: 'row' },
        el('button', { type: 'button', className: 'danger', onclick: () => done(true) }, actionLabel),
        el('button', { type: 'button', className: 'ghost', onclick: () => done(false) }, 'Cancel'))));
    dialog.addEventListener('cancel', () => done(false));
    document.body.append(dialog);
    dialog.showModal();
  });
}

function statusPill(project) {
  if (project.active) return pill(`Live on ${CLOUD_NAMES[project.active]}`, 'ok');
  if (project.prUrl) return pill('Ready to deploy', 'info');
  return pill('Setting up', 'warn');
}

function kpi(iconName, label, value, foot, dim = false) {
  return el('div', { className: 'card kpi' },
    el('div', { className: 'kpi-top' }, text('span', label), icon(iconName, 16)),
    text('b', value, dim ? 'dim' : ''),
    foot ?? null);
}

const EVENTS = {
  deployed: ['rocket', 'ok', (event) => `Deployed to ${CLOUD_NAMES[event.cloud]}`],
  switched: ['swap', 'ok', (event) => `Moved from ${CLOUD_NAMES[event.from] ?? 'other cloud'} to ${CLOUD_NAMES[event.cloud]}${event.downtimeSeconds !== undefined ? `, ${event.downtimeSeconds} s downtime` : ''}`],
  switch_failed: ['alert', 'bad', (event) => `Switch failed, still on ${CLOUD_NAMES[event.cloud]}`],
  rolled_back: ['undo', 'info', (event) => `Rolled back to ${CLOUD_NAMES[event.cloud]}`],
  destroyed: ['trash', '', (event) => `Removed everything on ${CLOUD_NAMES[event.cloud]}`],
  verified: ['shield', 'info', (event) => `Connection to ${CLOUD_NAMES[event.cloud]} works`],
  report: ['wallet', '', (event) => `Costs and server data refreshed from ${CLOUD_NAMES[event.cloud]}`],
};

function feed(events, showRepo = false) {
  if (!events.length) return text('p', 'No activity yet. Deploys, switches and cost updates show up here.', 'muted small');
  return el('ul', { className: 'feed' }, ...events.map((event) => {
    const [iconName, tone, describe] = EVENTS[event.event] ?? ['activity', '', () => event.event];
    return el('li', {},
      el('div', { className: `feed-icon ${tone}` }, icon(iconName, 14)),
      el('div', {}, text('div', describe(event)), text('div', `${showRepo ? event.repo + ' · ' : ''}${ago(event.at)}`, 'muted small')));
  }));
}

/* App frame: dark navigation rail and light workspace. */

function setPage(section, ...nodes) {
  clearInterval(pollTimer);
  document.body.classList.remove('nav-open');
  const link = (href, iconName, label, key) => el('a', { href, className: `nav-link ${section === key ? 'on' : ''}` }, icon(iconName, 17), text('span', label));
  const recent = projectsCache.slice(0, 6).map((project) => el('a', {
    href: `#/p/${project.id}`, className: `nav-link ${currentProject === project.id ? 'on' : ''}`, title: project.repo,
  }, text('i', '', `dot ${project.active ? 'live' : ''}`), text('span', project.repo.split('/')[1])));
  const name = user?.startsWith('github:') ? user.slice(7) : 'Admin';
  const signOut = el('button', { className: 'plain small', ariaLabel: 'Sign out', title: 'Sign out' }, icon('logout', 16));
  signOut.onclick = action(signOut, async () => {
    await api('/auth/logout', { method: 'POST' });
    user = null;
    renderGate();
  });

  root.replaceChildren(el('div', { className: 'shell' },
    el('nav', { className: 'nav', ariaLabel: 'Main' },
      brand(),
      link('#/', 'home', 'Home', 'home'),
      link('#/projects', 'folder', 'Projects', 'projects'),
      link('#/accounts', 'users', 'Accounts', 'accounts'),
      link('#/analyze', 'chart', 'Analyze a repo', 'analyze'),
      recent.length ? text('div', 'Projects', 'nav-label') : null,
      recent,
      el('div', { className: 'nav-foot' },
        el('div', { className: 'nav-note' }, text('strong', 'Testing phase'), 'Use sandbox cloud accounts. Real deploys are not proven yet.'),
        el('div', { className: 'user' }, text('span', name.slice(0, 1).toUpperCase(), 'avatar'), text('span', name, 'grow'), signOut))),
    el('div', { className: 'workspace' },
      el('header', { className: 'topbar' },
        el('button', { className: 'plain menu-button', ariaLabel: 'Open menu', onclick: () => document.body.classList.toggle('nav-open') }, icon('menu', 20)),
        el('button', { className: 'search-trigger', onclick: openPalette }, icon('search', 16), text('span', 'Search projects and actions'), text('kbd', 'Ctrl K'))),
      el('main', { className: 'content' }, el('div', { className: 'inner' }, ...nodes.filter(Boolean))))));
}

function pageHead({ crumbs = [], title, subtitle, actions = [] }) {
  return el('div', { className: 'stack tight' },
    crumbs.length ? el('nav', { className: 'crumbs', ariaLabel: 'Breadcrumb' },
      ...crumbs.flatMap(([label, href], index) => [index ? text('span', '/') : null, href ? el('a', { href, textContent: label }) : text('span', label)])) : null,
    el('div', { className: 'page-head' },
      el('div', {}, text('h1', title), subtitle ?? null),
      actions.length ? el('div', { className: 'row' }, ...actions) : null));
}

/* Command palette, like the console search in AWS and GCP. */

const palette = el('dialog', { className: 'palette', ariaLabel: 'Search' });

async function openPalette() {
  try {
    if (!projectsCache.length) projectsCache = (await api('/projects')).projects;
  } catch { /* Search still works for pages. */ }
  const project = projectsCache.find((item) => item.id === currentProject);
  const items = [
    { iconName: 'home', label: 'Home', hint: 'Page', go: '#/' },
    { iconName: 'folder', label: 'Projects', hint: 'Page', go: '#/projects' },
    { iconName: 'users', label: 'Accounts', hint: 'Page', go: '#/accounts' },
    { iconName: 'chart', label: 'Analyze a repo', hint: 'Page', go: '#/analyze' },
    { iconName: 'plus', label: 'New project', hint: 'Action', go: '#/projects?new' },
    { iconName: 'plus', label: 'Add an account', hint: 'Action', go: '#/accounts' },
    ...projectsCache.map((item) => ({ iconName: 'folder', label: item.repo, hint: item.active ? `Live on ${CLOUD_NAMES[item.active]}` : 'Project', go: `#/p/${item.id}` })),
    ...(project ? [
      { iconName: 'gauge', label: `Metrics: ${project.repo.split('/')[1]}`, hint: 'Page', go: `#/p/${project.id}/metrics` },
      ...quickActions(project).map((quick) => ({ iconName: quick.iconName, label: `${quick.label}: ${project.repo.split('/')[1]}`, hint: 'Action', run: () => propose(project, quick.command) })),
    ] : []),
  ];
  const input = el('input', { placeholder: 'Search projects and actions', autocomplete: 'off', ariaLabel: 'Search' });
  const list = el('ul', { className: 'palette-list', role: 'listbox' });
  let matches = items;
  let selected = 0;
  const choose = (item) => {
    palette.close();
    if (item.run) item.run();
    else location.hash = item.go;
  };
  const draw = () => {
    const query = input.value.toLowerCase();
    matches = items.filter((item) => item.label.toLowerCase().includes(query));
    selected = Math.min(selected, Math.max(matches.length - 1, 0));
    list.replaceChildren(...(matches.length
      ? matches.map((item, index) => el('li', { role: 'option', ariaSelected: String(index === selected), onclick: () => choose(item) }, icon(item.iconName, 16), text('span', item.label), text('small', item.hint)))
      : [text('li', 'Nothing found', 'muted')]));
  };
  input.oninput = () => { selected = 0; draw(); };
  input.onkeydown = (event) => {
    if (event.key === 'ArrowDown') { selected = Math.min(selected + 1, matches.length - 1); draw(); event.preventDefault(); }
    if (event.key === 'ArrowUp') { selected = Math.max(selected - 1, 0); draw(); event.preventDefault(); }
    if (event.key === 'Enter' && matches[selected]) choose(matches[selected]);
  };
  palette.replaceChildren(el('div', { className: 'palette-input' }, icon('search', 17), input, text('kbd', 'Esc')), list);
  draw();
  if (!palette.open) palette.showModal();
  input.focus();
}

document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && user) {
    event.preventDefault();
    openPalette();
  }
});
palette.addEventListener('click', (event) => { if (event.target === palette) palette.close(); });
// On phones the menu slides over the page; tapping outside it closes it.
document.addEventListener('click', (event) => {
  if (document.body.classList.contains('nav-open') && !event.target.closest('.nav, .menu-button')) document.body.classList.remove('nav-open');
});

/* Sign in */

function renderGate(denied = false) {
  clearInterval(pollTimer);
  const parts = [];
  if (denied) parts.push(notice('That GitHub account is not allowed here. Ask the owner to add it to ALLOWED_GITHUB_USERS.'));
  if (methods.github) parts.push(el('button', { type: 'button', className: 'github', onclick: () => { location.href = '/api/auth/github'; } }, icon('github', 18), 'Continue with GitHub'));
  if (methods.github && methods.adminToken) parts.push(text('div', 'or', 'divider'));
  if (methods.adminToken) {
    const [label, input] = field('Admin token', { type: 'password', required: true, autocomplete: 'current-password' },
      'For local testing. It is ADMIN_TOKEN in backend/.dev.vars.');
    const button = el('button', { style: 'justify-content:center' }, 'Sign in with token');
    const form = el('form', { className: 'stack' }, label, button);
    form.onsubmit = action(button, async () => {
      const result = await api('/auth/login', { method: 'POST', body: { token: input.value } });
      user = result.user;
      // Changing the hash already triggers routing; calling route() too would render twice at once.
      if (location.hash === '#/') route();
      else location.hash = '#/';
    });
    parts.push(form);
  }
  if (!methods.github && !methods.adminToken) parts.push(notice('No sign-in method is set up. Configure GitHub sign-in or ADMIN_TOKEN on the backend. See README.'));
  const point = (iconName, line) => el('li', {}, icon(iconName, 19), line);
  root.replaceChildren(el('div', { className: 'gate' },
    el('section', { className: 'gate-art' }, brand('#'),
      el('div', { className: 'stack', style: 'gap:24px' },
        text('h2', 'Your app, your cloud, and the freedom to leave.'),
        el('ul', {},
          point('rocket', 'Deploy from GitHub to AWS or GCP'),
          point('swap', 'Move between clouds when you ask'),
          point('gauge', 'Uptime, response time, spend and credits'),
          point('lock', 'No cloud keys or long-lived tokens stored'))),
      text('p', 'Keyless cloud access through GitHub OIDC', 'small')),
    el('section', { className: 'gate-form' }, el('div', {},
      el('div', { className: 'stack tight' }, text('h1', 'Sign in'), text('p', 'Manage deployments in your own cloud accounts.', 'muted')),
      ...parts))));
}

/* Home */

function onboarding(projects, accounts) {
  const first = projects[0];
  const steps = [
    ['Connect GitHub', accounts.some((account) => account.kind.startsWith('github')), '#/accounts'],
    ['Add a cloud account', accounts.some((account) => CLOUDS.includes(account.kind)), '#/accounts'],
    ['Create a project', Boolean(first), '#/projects?new'],
    ['Install Cloudhop in the repo', Boolean(first?.prUrl), first ? `#/p/${first.id}/setup` : null],
    ['Deploy your app', Boolean(first?.active), first ? `#/p/${first.id}/overview` : null],
  ];
  const done = steps.filter(([, isDone]) => isDone).length;
  return el('section', { className: 'card stack' },
    cardHead('Get started', `${done} of ${steps.length} done`),
    el('div', { className: 'progress' }, el('i', { style: `width:${(done / steps.length) * 100}%` })),
    el('ul', { className: 'checklist' }, ...steps.map(([label, isDone, href]) => el('li', { className: isDone ? 'done' : '' },
      el('span', { className: `check ${isDone ? 'done' : ''}` }, icon('check', 13)),
      text('span', label, 'grow'),
      !isDone && href ? el('a', { href, className: 'small' }, 'Start') : null))));
}

// Compact drops the address and spend columns so the table fits beside the activity card on Home.
function projectsTable(projects, compact = false) {
  const row = (project) => el('tr', { className: 'link', onclick: () => { location.hash = `#/p/${project.id}`; } },
    el('td', {}, el('a', { href: `#/p/${project.id}`, className: 'project-link', onclick: (event) => event.stopPropagation() }, project.repo)),
    el('td', {}, statusPill(project)),
    el('td', { className: 'num hide-sm' }, project.uptimePercent == null ? text('span', '-', 'muted') : `${project.uptimePercent.toFixed(1)}%`),
    compact ? null : el('td', { className: 'num hide-sm' }, project.averageMs == null ? text('span', '-', 'muted') : `${project.averageMs} ms`),
    compact ? null : el('td', { className: 'num hide-sm' }, project.costMonthToDate == null ? text('span', '-', 'muted') : money(project.costMonthToDate)),
    compact ? null : el('td', { className: 'hide-sm' }, project.url ? external(project.url, project.url.replace(/^https?:\/\//, '')) : text('span', '-', 'muted')));
  return el('div', { className: 'table-wrap' }, el('table', {},
    el('thead', {}, el('tr', {}, text('th', 'Project'), text('th', 'Status'), text('th', 'Uptime 24 h', 'num hide-sm'),
      compact ? null : text('th', 'Response', 'num hide-sm'), compact ? null : text('th', 'Spend this month', 'num hide-sm'), compact ? null : text('th', 'Address', 'hide-sm'))),
    el('tbody', {}, ...projects.map(row))));
}

async function renderHome() {
  const [{ projects, events }, { accounts }] = await Promise.all([api('/home'), api('/accounts')]);
  projectsCache = projects;
  currentProject = null;
  const live = projects.filter((project) => project.active);
  const withUptime = projects.filter((project) => project.uptimePercent != null);
  const costs = projects.filter((project) => project.costMonthToDate != null);
  const hour = new Date().getHours();
  setPage('home',
    pageHead({
      title: hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening',
      subtitle: text('p', 'How your deployments are doing right now.', 'muted'),
      actions: [el('button', { onclick: () => { location.hash = '#/projects?new'; } }, icon('plus', 16), 'New project')],
    }),
    el('div', { className: 'grid grid-4' },
      kpi('folder', 'Projects', String(projects.length), text('div', `${accounts.length} connected accounts`, 'kpi-foot')),
      kpi('bolt', 'Live apps', String(live.length), text('div', live.length ? live.map((project) => CLOUD_NAMES[project.active]).join(', ') : 'Nothing deployed yet', 'kpi-foot')),
      withUptime.length
        ? kpi('activity', 'Average uptime, 24 h', `${(withUptime.reduce((sum, project) => sum + project.uptimePercent, 0) / withUptime.length).toFixed(2)}%`, text('div', 'Checked every 5 minutes', 'kpi-foot'))
        : kpi('activity', 'Average uptime, 24 h', 'No checks yet', text('div', 'Starts after first deploy', 'kpi-foot'), true),
      costs.length
        ? kpi('wallet', 'Spend this month', money(costs.reduce((sum, project) => sum + project.costMonthToDate, 0)), text('div', 'AWS only for now', 'kpi-foot'))
        : kpi('wallet', 'Spend this month', 'Not reported yet', text('div', 'Refreshed daily', 'kpi-foot'), true)),
    live.length < 1 ? onboarding(projects, accounts) : null,
    el('div', { className: 'grid grid-main' },
      el('section', { className: 'card flush' },
        cardHead('Projects', null, el('a', { href: '#/projects', className: 'small' }, 'View all')),
        projects.length ? projectsTable(projects.slice(0, 6), true) : el('div', { className: 'empty' }, text('p', 'No projects yet.', 'muted'))),
      el('section', { className: 'card' }, cardHead('Recent activity'), feed(events, true))));
}

/* Accounts */

function accountDetail(account) {
  const data = account.data;
  if (account.kind === 'github_app') return `Installation ${data.installationId}, short-lived tokens`;
  if (account.kind === 'github_token') return 'Personal token, encrypted';
  if (account.kind === 'aws') return `Account ${data.accountId}, ${data.region}`;
  return `Project ${data.projectId}, ${data.region}`;
}

function accountsTable(accounts) {
  if (!accounts.length) return el('div', { className: 'empty' }, el('div', { className: 'empty-art' }, icon('users', 26)), text('h2', 'No accounts yet'), text('p', 'Connect GitHub and at least one cloud to deploy.', 'muted'));
  return el('div', { className: 'table-wrap' }, el('table', {},
    el('thead', {}, el('tr', {}, text('th', 'Account'), text('th', 'Type'), text('th', 'Details', 'hide-sm'), text('th', 'Projects', 'num'), text('th', ''))),
    el('tbody', {}, ...accounts.map((account) => {
      const remove = el('button', { className: 'plain small', ariaLabel: `Remove ${account.label}` }, icon('trash', 15));
      remove.onclick = action(remove, async () => {
        if (!(await confirmDialog(`Remove ${account.label}?`, 'Cloudhop forgets this account. Nothing in GitHub or your cloud is changed.', 'Remove'))) return;
        await api(`/accounts/${account.id}`, { method: 'DELETE' });
        showToast('Account removed.', 'info');
        await renderAccounts();
      });
      return el('tr', {},
        el('td', {}, text('strong', account.label)),
        el('td', {}, text('span', KIND_NAMES[account.kind], 'badge')),
        el('td', { className: 'muted hide-sm' }, accountDetail(account)),
        el('td', { className: 'num' }, String(account.projects)),
        el('td', { style: 'text-align:right' }, remove));
    }))));
}

function addAccountForms() {
  const githubTab = el('div', { className: 'stack' });
  const awsTab = el('div', { className: 'stack' });
  const gcpTab = el('div', { className: 'stack' });

  // GitHub: the App is the recommended path, a personal token is the fallback for local testing.
  const [tokenLabel, tokenInput] = field('Personal access token', { type: 'password', required: true, autocomplete: 'off' },
    'Fine-grained, limited to your repos, with read and write on Contents, Pull requests, Actions and Workflows.');
  const tokenSubmit = el('button', {}, 'Save token');
  const tokenForm = el('form', { className: 'stack sub-form' }, text('h3', 'Personal token, for testing'), tokenLabel, el('div', {}, tokenSubmit));
  tokenForm.onsubmit = action(tokenSubmit, async () => {
    await api('/accounts', { method: 'POST', body: { kind: 'github_token', token: tokenInput.value } });
    showToast('GitHub token saved, encrypted.', 'info');
    await renderAccounts();
  });
  githubTab.append(
    el('div', { className: 'stack sub-form' },
      el('div', { className: 'row' }, el('div', { className: 'icon-box ok' }, icon('github', 18)), el('div', { className: 'grow' }, text('h3', 'GitHub App, recommended'), text('p', 'Pick repos on GitHub. Cloudhop keeps only an installation id and gets a fresh 1-hour token each time.', 'muted small'))),
      methods.githubApp
        ? el('div', {}, el('button', { type: 'button', className: 'github', onclick: () => { location.href = '/api/github/install'; } }, icon('github', 16), 'Install on GitHub'))
        : notice('The GitHub App is not configured on the backend yet. README explains the five settings.', 'info')),
    tokenForm);

  const cloudForm = (kind, rows) => {
    const inputs = {};
    const fields = rows.map(([key, label, props, hint]) => {
      const [wrapper, input] = field(label, { required: true, spellcheck: false, autocomplete: 'off', ...props }, hint);
      inputs[key] = input;
      return wrapper;
    });
    const submit = el('button', {}, `Save ${CLOUD_NAMES[kind]} account`);
    const form = el('form', { className: 'stack sub-form' }, el('div', { className: 'fields' }, fields), el('div', {}, submit));
    form.onsubmit = action(submit, async () => {
      const body = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim()]));
      await api('/accounts', { method: 'POST', body: { kind, ...body } });
      showToast(`${CLOUD_NAMES[kind]} account saved. Connect it to a project in that project's Setup tab.`, 'info');
      await renderAccounts();
    });
    return form;
  };
  awsTab.append(
    notice('Only the account number and region are saved. Access comes from a role you create with a file Cloudhop gives you per project, trusted by GitHub OIDC.', 'info'),
    cloudForm('aws', [['label', 'Name', { placeholder: 'Sandbox', required: false }], ['accountId', 'Account ID', { placeholder: '123456789012', inputMode: 'numeric' }], ['region', 'Default region', { value: 'us-east-1' }]]));
  gcpTab.append(
    notice('Only project details are saved. Access comes from Workload Identity Federation, set up by a script Cloudhop gives you per project.', 'info'),
    cloudForm('gcp', [['label', 'Name', { placeholder: 'Sandbox', required: false }], ['projectId', 'Project ID', { placeholder: 'my-project' }], ['projectNumber', 'Project number', { inputMode: 'numeric' }], ['region', 'Default region', { value: 'us-central1' }]]));

  const panes = { github: githubTab, aws: awsTab, gcp: gcpTab };
  const holder = el('div');
  const buttons = Object.keys(panes).map((key) => el('button', {
    type: 'button', textContent: key === 'github' ? 'GitHub' : CLOUD_NAMES[key],
    onclick: (click) => {
      buttons.forEach((button) => button.setAttribute('aria-pressed', String(button === click.currentTarget)));
      holder.replaceChildren(panes[key]);
    },
  }));
  buttons[0].setAttribute('aria-pressed', 'true');
  holder.replaceChildren(githubTab);
  return el('section', { className: 'card' }, cardHead('Add an account', 'Connect once, then use it in any project.', el('div', { className: 'segmented', role: 'group', ariaLabel: 'Account type' }, buttons)), holder);
}

function securityCard() {
  const item = (title, body) => el('li', {}, icon('lock', 16), el('div', {}, text('strong', title), text('div', body, 'muted')));
  return el('section', { className: 'card' }, cardHead('How Cloudhop keeps access safe', 'Same patterns as major hosting platforms.'),
    el('ul', { className: 'security-list' },
      item('No cloud keys', 'Deploys run in GitHub Actions with short-lived OIDC credentials, trusted only for your repo\'s protected cloudhop environment.'),
      item('Short-lived GitHub tokens', 'The GitHub App gives Cloudhop a token that expires within an hour, scoped to the repos you picked.'),
      item('Encrypted at rest', 'Personal tokens are AES-GCM encrypted with a server secret and bound to their record.'),
      item('Session cookie, not stored tokens', 'Sign-in sets an HttpOnly cookie that page scripts cannot read. Only its hash is stored.'),
      item('Approval before every change', 'Nothing deploys, moves or deletes without your approval in this dashboard.')));
}

async function renderAccounts() {
  const { accounts, methods: available } = await api('/accounts');
  methods = { ...methods, ...available };
  currentProject = null;
  setPage('accounts',
    pageHead({ title: 'Accounts', subtitle: text('p', 'GitHub and cloud accounts your projects deploy with.', 'muted') }),
    el('section', { className: 'card flush' }, cardHead('Connected accounts', `${accounts.length} total`), accountsTable(accounts)),
    el('div', { className: 'grid grid-main' }, addAccountForms(), securityCard()));
}

/* Projects */

async function newProjectDialog() {
  const { accounts } = await api('/accounts');
  const github = accounts.filter((account) => account.kind.startsWith('github'));
  const dialog = el('dialog');
  const close = el('button', { type: 'button', className: 'plain', ariaLabel: 'Close', onclick: () => dialog.close() }, icon('x'));
  const head = el('div', { className: 'dialog-head' }, el('div', { className: 'stack tight' }, text('h2', 'New project'), text('p', 'Pick a repo to deploy.', 'muted small')), close);
  if (!github.length) {
    dialog.append(el('div', { className: 'stack' }, head, notice('Connect a GitHub account first.', 'info'), el('div', {}, el('button', { type: 'button', onclick: () => { dialog.close(); location.hash = '#/accounts'; } }, 'Go to Accounts'))));
    return dialog;
  }
  const [accountLabel, accountSelect] = select('GitHub account', github.map((account) => [account.id, `${account.label} (${KIND_NAMES[account.kind]})`]));
  const suggestions = el('datalist', { id: 'repo-options' });
  const [repoLabel, repo] = field('Repository', { required: true, placeholder: 'owner/name', spellcheck: false, autocomplete: 'off' }, 'Start typing to see repos this account can reach.');
  repo.setAttribute('list', 'repo-options');
  const loadRepos = async () => {
    suggestions.replaceChildren();
    try {
      const { repos } = await api(`/accounts/${accountSelect.value}/repos`);
      suggestions.replaceChildren(...repos.map((name) => el('option', { value: name })));
    } catch (error) {
      if (!error.quiet) showToast(error.message);
    }
  };
  accountSelect.onchange = loadRepos;
  loadRepos();
  const submit = el('button', {}, 'Create project');
  const form = el('form', { className: 'stack' }, head, accountLabel, repoLabel, suggestions, el('div', { className: 'row' }, submit));
  form.onsubmit = action(submit, async () => {
    const created = await api('/projects', { method: 'POST', body: { repo: repo.value, accountId: accountSelect.value } });
    reportTokens.set(created.id, created.reportToken);
    projectsCache = [];
    dialog.close();
    location.hash = `#/p/${created.id}/setup`;
  });
  dialog.append(form);
  return dialog;
}

async function renderProjects(openNew) {
  const { projects } = await api('/home');
  projectsCache = projects;
  currentProject = null;
  const dialog = await newProjectDialog();
  const filter = el('input', { placeholder: 'Filter projects', ariaLabel: 'Filter projects', style: 'max-width:280px' });
  const tableHolder = el('div', {}, projectsTable(projects));
  filter.oninput = () => {
    const query = filter.value.toLowerCase();
    tableHolder.replaceChildren(projectsTable(projects.filter((project) => project.repo.toLowerCase().includes(query))));
  };
  const add = el('button', { onclick: () => dialog.showModal() }, icon('plus', 16), 'New project');
  setPage('projects',
    pageHead({ title: 'Projects', subtitle: text('p', 'Apps you deploy and watch from here.', 'muted'), actions: [add] }),
    projects.length
      ? el('section', { className: 'card flush' }, el('div', { style: 'padding:14px 20px', className: 'row between' }, filter, text('span', `${projects.length} total`, 'muted small')), tableHolder)
      : el('section', { className: 'card empty' },
        el('div', { className: 'empty-art' }, icon('cloud', 28)),
        text('h2', 'Deploy your first app'),
        text('p', 'Pick a GitHub repo. Cloudhop works out what it needs and deploys it to your own cloud.', 'muted'),
        el('button', { onclick: () => dialog.showModal() }, icon('plus', 16), 'New project')),
    dialog);
  if (openNew) dialog.showModal();
}

/* Project actions */

function quickActions(project) {
  const connected = CLOUDS.filter((cloud) => project.clouds[cloud]);
  if (!project.active) {
    return connected.flatMap((cloud) => [
      { iconName: 'shield', label: `Check ${CLOUD_NAMES[cloud]}`, detail: 'Confirm access works', command: `check ${cloud} connection` },
      { iconName: 'rocket', label: `Deploy to ${CLOUD_NAMES[cloud]}`, detail: 'Create the server and go live', command: `deploy to ${cloud}` },
    ]);
  }
  const other = connected.find((cloud) => cloud !== project.active);
  return [
    { iconName: 'rocket', label: 'Deploy latest', detail: 'Ship the default branch', command: 'deploy latest code' },
    other ? { iconName: 'swap', label: `Move to ${CLOUD_NAMES[other]}`, detail: 'Brief downtime while data copies', command: `switch to ${other}` } : null,
    project.previous ? { iconName: 'undo', label: 'Roll back', detail: `Return to ${CLOUD_NAMES[project.previous]}`, command: 'roll back' } : null,
    { iconName: 'refresh', label: 'Refresh data', detail: 'Pull spend, credits and server load', command: 'refresh costs' },
    project.previous ? { iconName: 'trash', label: `Remove ${CLOUD_NAMES[project.previous]}`, detail: 'Stop paying for standby', command: `destroy ${project.previous}`, danger: true } : null,
  ].filter(Boolean);
}

const proposalDialog = el('dialog', { ariaLabel: 'Confirm action' });

// Asks the backend what a command would do, then shows it for approval. Nothing runs before approval.
async function propose(project, command, afterRun) {
  try {
    const proposal = await api(`/projects/${project.id}/command`, { method: 'POST', body: { text: command } });
    if (proposal.reply) {
      showToast(proposal.reply, 'info');
      return;
    }
    const destructive = proposal.action === 'destroy';
    const approve = el('button', { type: 'button', className: destructive ? 'danger' : '' }, icon(destructive ? 'trash' : 'check', 16), destructive ? 'Destroy' : 'Approve and run');
    let confirm = null;
    let confirmInput = null;
    if (proposal.confirmText) {
      [confirm, confirmInput] = field(`Type ${proposal.confirmText} to confirm`, { autocomplete: 'off' });
      approve.disabled = true;
      confirmInput.oninput = () => { approve.disabled = confirmInput.value !== proposal.confirmText; };
    }
    approve.onclick = action(approve, async () => {
      await api(`/projects/${project.id}/run`, {
        method: 'POST',
        body: { action: proposal.action, cloud: proposal.cloud, source: proposal.source, approved: true, confirmText: confirmInput?.value },
      });
      proposalDialog.close();
      showToast('Started. Follow progress in Deployments.', 'info');
      afterRun?.();
    });
    const cancel = el('button', { type: 'button', className: 'ghost', onclick: () => proposalDialog.close() }, 'Cancel');
    proposalDialog.replaceChildren(el('div', { className: 'proposal' },
      el('div', { className: 'row' }, el('div', { className: `icon-box ${destructive ? 'warn' : ''}` }, icon(destructive ? 'alert' : 'bolt', 18)), text('h2', proposal.summary, 'grow')),
      proposal.warnings.length ? el('ul', {}, ...proposal.warnings.map((warning) => text('li', warning))) : null,
      confirm,
      el('div', { className: 'row' }, approve, cancel)));
    proposalDialog.showModal();
  } catch (error) {
    if (!error.quiet) showToast(error.message);
  }
}

/* Project: shared header and tabs */

function projectHeader(project, tab) {
  const tabLink = (key, iconName, label) => el('a', { href: `#/p/${project.id}/${key}`, className: tab === key ? 'on' : '' }, icon(iconName, 15), label);
  const deploy = quickActions(project).find((quick) => quick.iconName === 'rocket');
  return [
    pageHead({
      crumbs: [['Projects', '#/projects'], [project.repo]],
      title: project.repo,
      subtitle: el('div', { className: 'row' }, statusPill(project), project.url ? external(project.url, project.url.replace(/^https?:\/\//, '')) : null),
      actions: [
        project.url ? el('button', { className: 'ghost', onclick: () => window.open(project.url, '_blank', 'noopener') }, icon('external', 15), 'Open site') : null,
        deploy ? el('button', { onclick: () => propose(project, deploy.command) }, icon('rocket', 15), deploy.label) : null,
      ].filter(Boolean),
    }),
    el('nav', { className: 'tabs', ariaLabel: 'Project' },
      tabLink('overview', 'home', 'Overview'), tabLink('metrics', 'gauge', 'Metrics'), tabLink('deployments', 'activity', 'Deployments'),
      tabLink('infrastructure', 'server', 'Infrastructure'), tabLink('setup', 'settings', 'Setup')),
  ];
}

/* Project: overview */

function routeCard(project) {
  const node = (cloud) => {
    const connected = Boolean(project.clouds[cloud]);
    const live = project.active === cloud;
    let state = pill('Not connected');
    if (live) state = pill('Live', 'ok');
    else if (project.previous === cloud) state = pill('Standby, still billing', 'warn');
    else if (connected) state = pill('Connected', 'info');
    return el('div', { className: `node ${live ? 'live' : ''} ${connected ? '' : 'off'}` },
      el('div', { className: 'node-name' }, icon('cloud', 17), CLOUD_NAMES[cloud]),
      el('div', {}, state),
      text('div', project.clouds[cloud]?.region ?? 'No account yet', 'muted small mono'));
  };
  return el('section', { className: 'card' },
    cardHead('Where it runs', 'Your app runs in your own cloud account.'),
    el('div', { className: 'route', ariaLabel: project.active ? `Live on ${CLOUD_NAMES[project.active]}` : 'Not deployed' },
      node('aws'), el('div', { className: `route-line ${project.active ? `active ${project.active === 'aws' ? 'left' : 'right'}` : ''}` }), node('gcp')));
}

function actionsCard(project, afterRun) {
  const tiles = quickActions(project).map((quick) => el('button', {
    type: 'button', className: `action-tile ${quick.danger ? 'danger-tile' : ''}`,
    onclick: () => propose(project, quick.command, afterRun),
  }, icon(quick.iconName, 19), text('span', quick.label), text('small', quick.detail)));
  return el('section', { className: 'card' },
    cardHead('Quick actions', 'Each one asks for approval first.'),
    tiles.length ? el('div', { className: 'actions-grid' }, tiles)
      : notice(el('span', {}, 'Connect a cloud first. ', el('a', { href: `#/p/${project.id}/setup` }, 'Go to Setup')), 'info'));
}

function askCard(project, afterRun) {
  const input = el('input', { required: true, placeholder: 'Try "move this to gcp" or "roll back"', autocomplete: 'off', ariaLabel: 'Ask Cloudhop' });
  const submit = el('button', {}, 'Ask');
  const form = el('form', { className: 'ask' }, icon('bolt', 17), input, submit);
  form.onsubmit = action(submit, async () => propose(project, input.value, afterRun));
  return el('section', { className: 'card stack' }, cardHead('Ask Cloudhop', 'Describe what you want in plain words.'), form);
}

// Hourly response-time averages for the overview sparkline.
function hourlyMs(timeline) {
  const hours = Array.from({ length: 24 }, () => []);
  for (const check of timeline) {
    const index = 23 - Math.floor((Date.now() - check.at) / 3_600_000);
    if (index >= 0 && index < 24 && check.ok && check.ms !== null) hours[index].push(check.ms);
  }
  return hours.map((values) => (values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null));
}

function healthCards(data) {
  const { project, uptime, timeline, latestCost, events } = data;
  const lastSwitch = events.find((event) => event.event === 'switched');
  const cost = latestCost?.costMonthToDate;
  const credits = Array.isArray(latestCost?.credits) ? latestCost.credits : [];
  const creditTotal = credits.reduce((sum, credit) => sum + Number(credit.amount), 0);
  const metricsLink = el('a', { href: `#/p/${project.id}/metrics`, className: 'small' }, 'Open metrics');
  return [
    el('div', { className: 'grid grid-4' },
      kpi('activity', 'Uptime, 24 h', uptime.uptimePercent === null ? 'No checks yet' : `${uptime.uptimePercent.toFixed(2)}%`,
        uptime.last ? el('div', { className: 'kpi-foot' }, pill(uptime.last.ok ? 'Up now' : 'Down now', uptime.last.ok ? 'ok' : 'bad')) : text('div', 'Starts after deploy', 'kpi-foot'), uptime.uptimePercent === null),
      el('div', { className: 'card kpi' },
        el('div', { className: 'kpi-top' }, text('span', 'Average response'), icon('clock', 16)),
        text('b', uptime.averageMs === null ? 'No checks yet' : `${uptime.averageMs} ms`, uptime.averageMs === null ? 'dim' : ''),
        Charts.sparkline(hourlyMs(timeline))),
      kpi('wallet', 'Spend this month', typeof cost === 'number' ? money(cost) : project.active === 'gcp' ? 'Not on GCP yet' : 'Not reported yet',
        text('div', latestCost ? `${CLOUD_NAMES[latestCost.cloud]}, updated ${ago(latestCost.at)}` : 'Refreshed daily', 'kpi-foot'), typeof cost !== 'number'),
      credits.length ? kpi('key', 'Credits remaining', money(creditTotal), text('div', `${credits.length} credit${credits.length === 1 ? '' : 's'} on file`, 'kpi-foot'))
        : lastSwitch?.downtimeSeconds !== undefined ? kpi('swap', 'Downtime, last switch', `${lastSwitch.downtimeSeconds} s`, text('div', ago(lastSwitch.at), 'kpi-foot'))
          : kpi('key', 'Credits remaining', 'None found', text('div', 'Read from AWS billing', 'kpi-foot'), true)),
    el('div', { className: 'grid grid-main' },
      el('div', { className: 'stack' }, el('div', { className: 'grid grid-2' }, routeCard(project), actionsCard(project))),
      el('section', { className: 'card' }, cardHead('Recent activity', null, el('a', { href: `#/p/${project.id}/deployments`, className: 'small' }, 'View all')), feed(events.slice(0, 6)), el('div', { style: 'margin-top:14px' }, metricsLink))),
  ];
}

async function renderOverview(data) {
  const { project } = data;
  const live = el('div', { className: 'stack' }, healthCards(data));
  const refresh = async () => {
    try {
      live.replaceChildren(...healthCards(await api(`/projects/${project.id}/status`)));
    } catch (error) {
      if (!error.quiet) showToast(error.message);
    }
  };
  setPage('projects', ...projectHeader(project, 'overview'), live, askCard(project, () => setTimeout(refresh, 4000)));
  pollTimer = setInterval(refresh, 15000);
}

/* Project: metrics */

// 24-hour charts show times, adding the weekday for yesterday so both ends never read the same.
function timeFormatter(range) {
  return (t, long) => {
    const date = new Date(t);
    const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const day = date.toLocaleDateString([], { weekday: 'short', day: 'numeric' });
    if (long) return `${day}, ${time}`;
    if (range === '7d') return day;
    return date.toDateString() === new Date().toDateString() ? time : `${date.toLocaleDateString([], { weekday: 'short' })} ${time}`;
  };
}

function uptimeTone(bar) {
  if (bar.v === null) return 'empty';
  if (bar.v >= 100) return 'ok';
  return bar.v > 0 ? 'warn' : 'bad';
}

function meter(label, value) {
  const tone = value === null ? '' : value >= 90 ? 'bad' : value >= 70 ? 'warn' : '';
  const state = value === null ? '' : value >= 90 ? 'High' : value >= 70 ? 'Elevated' : 'Normal';
  return el('div', { className: 'meter' },
    el('div', { className: 'row between small' }, text('span', label), text('strong', value === null ? 'No data' : `${value.toFixed(0)}%  ${state}`)),
    el('div', { className: 'meter-track', role: 'img', ariaLabel: `${label} ${value === null ? 'no data' : value.toFixed(0) + ' percent'}` }, el('i', { className: tone, style: `width:${Math.min(100, value ?? 0)}%` })));
}

async function renderMetrics(data, range = '24h') {
  const { project } = data;
  const metrics = await api(`/projects/${project.id}/metrics?range=${range}`);
  const formatTime = timeFormatter(range);
  const rangeButtons = ['24h', '7d'].map((key) => el('button', {
    type: 'button', textContent: key === '24h' ? 'Last 24 hours' : 'Last 7 days', ariaPressed: String(key === range),
    onclick: () => renderMetrics(data, key).catch((error) => { if (!error.quiet) showToast(error.message); }),
  }));
  const msPoints = metrics.buckets.map((bucket) => ({ t: bucket.start, v: bucket.averageMs }));
  const hasMs = msPoints.some((point) => point.v !== null);
  const bucketRows = metrics.buckets.filter((bucket) => bucket.checks);
  const totalChecks = metrics.statuses.reduce((sum, status) => sum + status.count, 0);
  const resources = metrics.resources;

  const responseCard = el('section', { className: 'card' },
    cardHead('Response time', 'Average per time slot, from Cloudflare uptime checks.'),
    hasMs
      ? Charts.line({ points: msPoints, from: metrics.since, to: metrics.now, format: (value) => `${Math.round(value)} ms`, formatTime, ariaLabel: 'Average response time' })
      : el('div', { className: 'empty' }, text('p', 'No successful checks in this period yet.', 'muted')),
    hasMs ? Charts.dataTable(['Time', 'Average', 'Slowest', 'Checks'], bucketRows.map((bucket) => [formatTime(bucket.start, true), bucket.averageMs === null ? '-' : `${bucket.averageMs} ms`, bucket.maxMs === null ? '-' : `${bucket.maxMs} ms`, String(bucket.checks)])) : null);

  const uptimeCard = el('section', { className: 'card' },
    cardHead('Availability', 'Share of checks that succeeded in each slot.', el('div', { className: 'legend' },
      el('span', {}, el('i', { style: 'background:var(--ok)' }), 'All up'),
      el('span', {}, el('i', { style: 'background:var(--warn)' }), 'Some failed'),
      el('span', {}, el('i', { style: 'background:var(--bad)' }), 'All failed'),
      el('span', {}, el('i', { style: 'background:var(--empty)' }), 'No data'))),
    Charts.columns({
      bars: metrics.buckets.map((bucket) => ({ t: bucket.start, v: bucket.uptimePercent, checks: bucket.checks })),
      formatTime, tone: uptimeTone, ariaLabel: 'Availability per time slot',
      describe: (bar) => [bar.v === null ? 'No checks' : `${bar.v.toFixed(1)}% up`, `${formatTime(bar.t, true)} · ${bar.checks} checks`],
    }),
    Charts.dataTable(['Time', 'Up', 'Checks'], bucketRows.map((bucket) => [formatTime(bucket.start, true), `${bucket.uptimePercent.toFixed(1)}%`, String(bucket.checks)])));

  const resourcesCard = el('section', { className: 'card stack' },
    cardHead('Server resources', resources ? 'From the last data refresh on the server.' : 'Collected when you refresh data from Overview.'),
    meter('CPU load', resources?.cpuLoadPercent ?? null),
    meter('Memory used', resources?.memoryPercent ?? null),
    meter('Disk used', resources?.diskPercent ?? null));

  const costCard = el('section', { className: 'card' },
    cardHead('Spend this month', 'Month-to-date cost, refreshed daily. AWS only for now.'),
    metrics.costs.length >= 2
      ? Charts.line({ points: metrics.costs.map((cost) => ({ t: cost.at, v: cost.amount })), from: metrics.costs[0].at, to: metrics.costs.at(-1).at, format: money, formatTime: timeFormatter('7d'), height: 180, ariaLabel: 'Month-to-date spend' })
      : el('div', { className: 'empty' }, text('p', metrics.costs.length ? `${money(metrics.costs[0].amount)} so far. A trend appears after the next daily refresh.` : 'No cost data yet.', 'muted')),
    metrics.costs.length ? Charts.dataTable(['Date', 'Cloud', 'Month to date'], metrics.costs.map((cost) => [new Date(cost.at).toLocaleString(), CLOUD_NAMES[cost.cloud], money(cost.amount)])) : null);

  const statusCard = el('section', { className: 'card' },
    cardHead('Responses', 'HTTP status of each check.'),
    metrics.statuses.length
      ? el('div', { className: 'stack tight' }, ...metrics.statuses.map((status) => el('div', { className: 'hbar' },
        text('span', status.status, 'mono'),
        el('div', { className: 'hbar-track' }, el('i', { style: `width:${(status.count / totalChecks) * 100}%` })),
        text('span', `${status.count}`, 'num'))))
      : text('p', 'No checks in this period.', 'muted small'));

  const incidentsCard = el('section', { className: 'card flush' },
    cardHead('Outages', 'Runs of failed checks in a row.', metrics.incidents.length ? pill(`${metrics.incidents.length} in period`, 'bad') : pill('None', 'ok')),
    metrics.incidents.length
      ? el('div', { className: 'table-wrap' }, el('table', {},
        el('thead', {}, el('tr', {}, text('th', 'Started'), text('th', 'Lasted'), text('th', 'Failed checks', 'num'))),
        el('tbody', {}, ...metrics.incidents.map((incident) => el('tr', {},
          el('td', {}, formatTime(incident.start, true)),
          el('td', {}, incident.ongoing ? pill('Ongoing', 'bad') : duration(Math.max(incident.end - incident.start, 300_000))),
          el('td', { className: 'num' }, String(incident.failedChecks)))))))
      : el('div', { style: 'padding:0 20px 18px' }, text('p', 'No outages in this period.', 'muted small')));

  setPage('projects', ...projectHeader(project, 'metrics'),
    el('div', { className: 'row between' }, text('p', `${metrics.checks} checks in this period`, 'muted small'), el('div', { className: 'segmented', role: 'group', ariaLabel: 'Time range' }, rangeButtons)),
    el('div', { className: 'grid grid-4' },
      kpi('activity', 'Uptime', metrics.uptimePercent === null ? 'No checks yet' : `${metrics.uptimePercent.toFixed(2)}%`, null, metrics.uptimePercent === null),
      kpi('clock', 'Average response', metrics.averageMs === null ? 'No data' : `${metrics.averageMs} ms`, null, metrics.averageMs === null),
      kpi('gauge', 'Slowest 5% above', metrics.p95Ms === null ? 'No data' : `${metrics.p95Ms} ms`, text('div', '95th percentile', 'kpi-foot'), metrics.p95Ms === null),
      kpi('alert', 'Outages', String(metrics.incidents.length), text('div', metrics.incidents[0] ? `Last ${ago(metrics.incidents[0].start)}` : 'None in period', 'kpi-foot'))),
    responseCard,
    el('div', { className: 'grid grid-main' }, uptimeCard, resourcesCard),
    el('div', { className: 'grid grid-2' }, costCard, statusCard),
    incidentsCard);
}

/* Project: deployments */

function runPill(run) {
  if (run.status !== 'completed') return pill('Running', 'warn run');
  if (run.conclusion === 'success') return pill('Succeeded', 'ok');
  if (run.conclusion === 'failure') return pill('Failed', 'bad');
  return pill(run.conclusion ?? run.status);
}

function renderDeployments(data) {
  const { project, runs, runsError, events } = data;
  const table = runs.length
    ? el('div', { className: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, text('th', 'Run'), text('th', 'Status'), text('th', 'Started', 'hide-sm'), text('th', ''))),
      el('tbody', {}, ...runs.map((run) => el('tr', {},
        el('td', {}, text('strong', run.title.replace(/^cloudhop /, ''))),
        el('td', {}, runPill(run)),
        el('td', { className: 'muted hide-sm' }, run.startedAt ? ago(Date.parse(run.startedAt)) : '-'),
        el('td', { style: 'text-align:right' }, external(run.url, 'Logs')))))))
    : el('div', { className: 'empty' }, text('p', runsError && project.prUrl ? 'Runs appear after you merge the install PR.' : 'Nothing has run yet.', 'muted'));
  setPage('projects', ...projectHeader(project, 'deployments'),
    el('div', { className: 'grid grid-main' },
      el('section', { className: 'card flush' }, cardHead('Workflow runs', 'Each action runs in GitHub Actions in your repo.'), table),
      el('section', { className: 'card' }, cardHead('History'), feed(events))));
}

/* Project: infrastructure */

function consoleLink(cloud, config) {
  return cloud === 'aws'
    ? `https://${config.region}.console.aws.amazon.com/ec2/home?region=${config.region}#Instances:`
    : `https://console.cloud.google.com/compute/instances?project=${encodeURIComponent(config.projectId)}`;
}

function renderInfrastructure(data) {
  const { project } = data;
  const cards = CLOUDS.map((cloud) => {
    const config = project.clouds[cloud];
    if (!config) {
      return el('section', { className: 'card empty' }, el('div', { className: 'empty-art' }, icon('cloud', 26)),
        text('h2', `${CLOUD_NAMES[cloud]} not connected`), el('a', { href: `#/p/${project.id}/setup` }, 'Connect in Setup'));
    }
    const state = project.active === cloud ? pill('Live', 'ok') : project.previous === cloud ? pill('Standby, still billing', 'warn') : pill('No app running');
    const details = cloud === 'aws'
      ? [['Account', config.accountId], ['Region', config.region], ['Deploy role', config.roleArn], ['State bucket', config.stateBucket]]
      : [['Project', config.projectId], ['Region', config.region], ['Service account', config.serviceAccount], ['State bucket', config.stateBucket]];
    const hasApp = project.active === cloud || project.previous === cloud;
    return el('section', { className: 'card stack' },
      el('div', { className: 'row between' }, el('div', { className: 'node-name' }, icon('cloud', 17), CLOUD_NAMES[cloud]), state),
      el('dl', { className: 'detail-list' }, ...details.flatMap(([key, value]) => [text('dt', key), el('dd', {}, text('code', value))])),
      hasApp ? el('div', { className: 'stack tight' }, text('h3', 'Resources'),
        el('ul', { className: 'list small' },
          el('li', { className: 'row' }, icon('server', 15), `Virtual machine, ${MACHINES[cloud]}`),
          el('li', { className: 'row' }, icon('folder', 15), 'Postgres and Redis in containers on that machine'),
          el('li', { className: 'row' }, icon('lock', 15), 'Private bucket for moving data'))) : null,
      el('div', {}, el('button', { className: 'ghost small', onclick: () => window.open(consoleLink(cloud, config), '_blank', 'noopener') }, icon('external', 14), `Open ${CLOUD_NAMES[cloud]} console`)));
  });
  setPage('projects', ...projectHeader(project, 'infrastructure'),
    notice('Each cloud runs one small virtual machine with your app, Postgres and Redis. Managed databases and autoscaling come later.', 'info'),
    el('div', { className: 'grid grid-2' }, cards));
}

/* Project: setup */

function step(number, done, title, intro, ...body) {
  return el('section', { className: `card step ${done ? 'done' : ''}` },
    el('div', { className: 'step-no' }, done ? icon('check', 15) : String(number)),
    el('div', { className: 'stack' }, el('div', { className: 'stack tight' }, text('h2', title), intro ? text('p', intro, 'muted small') : null), ...body));
}

function connectForms(project, accounts, onChange) {
  return el('div', { className: 'grid grid-2' }, ...CLOUDS.map((cloud) => {
    const saved = accounts.filter((account) => account.kind === cloud);
    const header = el('div', { className: 'row between' }, el('div', { className: 'node-name' }, icon('cloud', 17), CLOUD_NAMES[cloud]), project.clouds[cloud] ? pill('Connected', 'ok') : pill('Not connected'));
    if (!saved.length) {
      return el('div', { className: 'stack sub-form' }, header, text('p', `No ${CLOUD_NAMES[cloud]} account saved yet.`, 'muted small'), el('div', {}, el('a', { href: '#/accounts' }, `Add an ${CLOUD_NAMES[cloud]} account`)));
    }
    const [accountLabel, accountSelect] = select('Account', saved.map((account) => [account.id, account.label]));
    const current = saved.find((account) => account.data.accountId === project.clouds[cloud]?.accountId && account.data.projectId === project.clouds[cloud]?.projectId);
    if (current) accountSelect.value = current.id;
    const [regionLabel, region] = field('Region', { required: true, value: project.clouds[cloud]?.region ?? saved[0].data.region, spellcheck: false, autocomplete: 'off' });
    accountSelect.onchange = () => { region.value = saved.find((account) => account.id === accountSelect.value).data.region; };
    const submit = el('button', {}, project.clouds[cloud] ? 'Update' : 'Connect');
    const result = el('div');
    const form = el('form', { className: 'stack sub-form' }, header, accountLabel, regionLabel, el('div', {}, submit), result);
    form.onsubmit = action(submit, async () => {
      const data = await api(`/projects/${project.id}/clouds/${cloud}`, { method: 'PUT', body: { accountId: accountSelect.value, region: region.value.trim() } });
      project.clouds[cloud] = data.clouds[cloud];
      onChange();
      result.replaceChildren(notice(el('div', { className: 'stack tight' },
        text('strong', 'One step left in your cloud'),
        text('span', cloud === 'aws' ? 'Create a CloudFormation stack from this file in the AWS console.' : 'Read this script, then run it in Google Cloud Shell.'),
        el('a', { textContent: `Download ${data.bootstrapName}`, download: data.bootstrapName, href: URL.createObjectURL(new Blob([data.bootstrap], { type: 'text/plain' })) })), 'info'));
    });
    return form;
  }));
}

async function renderSetup(data) {
  const { project, runsError } = data;
  const { accounts } = await api('/accounts');
  const connected = Object.keys(project.clouds).length > 0;
  const installed = Boolean(project.prUrl) && !runsError;
  const live = Boolean(project.active);
  const reportToken = reportTokens.get(project.id);
  const doneCount = [connected, installed, live].filter(Boolean).length;

  const installButton = el('button', {}, icon('plus', 15), project.prUrl ? 'Open a new install PR' : 'Open install PR');
  installButton.disabled = !connected;
  const installResult = el('div', {}, project.prUrl
    ? el('p', { className: 'small' }, external(project.prUrl, 'View install PR'), text('span', installed ? '  Merged and working.' : '  Merge it to continue.', 'muted'))
    : null);
  installButton.onclick = action(installButton, async () => {
    const result = await api(`/projects/${project.id}/install`, { method: 'POST' });
    project.prUrl = result.prUrl;
    installResult.replaceChildren(el('div', { className: 'stack tight' },
      el('p', {}, external(result.prUrl, 'Review and merge the PR')),
      result.reachable ? null : notice('This backend runs on localhost, so GitHub cannot send results back. Deploy the backend, then open a new PR.')));
  });

  setPage('projects', ...projectHeader(project, 'setup'),
    el('section', { className: 'card stack tight' },
      el('div', { className: 'row between' }, text('h2', 'Setup progress'), text('span', `${doneCount} of 3 milestones`, 'muted small')),
      el('div', { className: 'progress', role: 'progressbar', ariaValueNow: String(doneCount), ariaValueMin: '0', ariaValueMax: '3', ariaLabel: 'Setup progress' }, el('i', { style: `width:${(doneCount / 3) * 100}%` }))),
    step(1, connected, 'Connect your clouds', 'Pick saved accounts. Each download lets only this repo\'s protected cloudhop environment deploy. Use a sandbox account while testing.',
      connectForms(project, accounts, () => { installButton.disabled = false; })),
    step(2, false, 'Add secrets to your repo', 'In GitHub, open Settings, then Secrets and variables, then Actions.',
      el('div', { className: 'stack' },
        el('div', { className: 'stack tight' }, text('code', 'CLOUDHOP_REPORT_TOKEN'),
          reportToken
            ? el('div', { className: 'stack tight' }, el('div', { className: 'secret-box' }, text('code', reportToken), copyButton(reportToken)), notice('Save this now. It is shown only once, and only its hash is stored.'))
            : text('p', 'Shown once when the project was created. Lets workflows report results back.', 'muted small')),
        el('div', { className: 'stack tight' }, text('code', 'CLOUDHOP_ENV'),
          text('p', 'Your app settings, as NAME=value lines. DATABASE_URL and REDIS_URL are filled in for you.', 'muted small')),
        notice('Optional but recommended: in Settings, then Environments, add required reviewers to the cloudhop environment. Every cloud action then needs a second approval in GitHub.', 'info'))),
    step(3, installed, 'Install in your repo', 'Opens a PR with deployment files. Check generated Dockerfiles, since start commands are best guesses. Open a new PR after connecting another cloud.',
      el('div', { className: 'stack tight' }, el('div', {}, installButton), installResult)),
    step(4, live, 'Deploy', live ? `Live on ${CLOUD_NAMES[project.active]}.` : 'Check the connection, then deploy from Overview.',
      live ? null : el('div', {}, el('a', { href: `#/p/${project.id}/overview` }, 'Go to Overview'))));
}

async function renderProject(id, tab) {
  const data = await api(`/projects/${id}/status`);
  currentProject = id;
  if (!projectsCache.some((project) => project.id === id)) projectsCache = (await api('/projects')).projects;
  const current = tab ?? (data.project.active ? 'overview' : 'setup');
  if (current === 'metrics') await renderMetrics(data);
  else if (current === 'deployments') renderDeployments(data);
  else if (current === 'infrastructure') renderInfrastructure(data);
  else if (current === 'setup') await renderSetup(data);
  else await renderOverview(data);
}

/* Analyze */

function addCreditRow(container) {
  const row = el('div', { className: 'credit-row' });
  const cloud = el('select', { ariaLabel: 'Cloud' }, CLOUDS.map((key) => el('option', { value: key, textContent: CLOUD_NAMES[key] })));
  const amount = el('input', { type: 'number', min: '1', placeholder: 'Amount in USD', ariaLabel: 'Amount in USD', required: true });
  const expires = el('input', { type: 'date', ariaLabel: 'Expiry date', required: true });
  const remove = el('button', { type: 'button', className: 'plain small', ariaLabel: 'Remove credit', onclick: () => row.remove() }, icon('x', 15));
  row.append(cloud, amount, expires, remove);
  container.append(row);
}

function detectedCard(data) {
  const { map } = data;
  const rows = [
    ...map.services.map((service) => ['server', service.path === '.' ? 'App' : service.path, `${service.framework ?? service.runtime}, ${service.kind}${service.port ? `, port ${service.port}` : ''}`, service.evidence[0]]),
    ...map.datastores.map((store) => ['folder', store.kind, 'Datastore', store.evidence[0]]),
    ...map.workers.map((worker) => ['refresh', 'Background worker', '', worker.evidence]),
  ];
  return el('section', { className: 'card' },
    cardHead('What it needs'),
    rows.length
      ? el('ul', { className: 'list' }, ...rows.map(([iconName, name, detail, source]) => el('li', { className: 'row' },
        icon(iconName, 15), el('div', { className: 'grow' }, text('strong', name), detail ? text('div', detail, 'muted small') : null), evidence(source))))
      : text('p', 'Nothing recognized.', 'muted'),
    map.envVars.length ? el('div', { className: 'stack tight', style: 'margin-top:14px' }, text('span', 'Environment variables to set', 'muted small'), text('code', map.envVars.join(', '))) : null);
}

function portabilityCard({ verdict, worst, items }) {
  return el('section', { className: 'card' },
    cardHead('Switching clouds', null, pill(verdict, TIERS[worst][1])),
    el('ul', { className: 'list' }, ...items.map((item) => el('li', { className: 'stack tight' },
      el('div', { className: 'row between' }, text('strong', item.name), pill(TIERS[item.tier][0], TIERS[item.tier][1])),
      text('p', item.why, 'muted small'), evidence(item.evidence)))));
}

function planCard(plan, best) {
  const { credit } = plan;
  return el('article', { className: `card plan ${best ? 'best' : ''}` },
    el('div', { className: 'row' }, best ? pill('Best fit', 'brand') : null, plan.deployable ? pill('Can deploy', 'ok') : pill('Estimate only')),
    el('div', { className: 'stack tight' }, text('h3', plan.name), text('p', plan.note, 'muted small')),
    el('div', {}, text('div', money(plan.monthly), 'price'), text('span', `per month, likely ${money(plan.low)} to ${money(plan.high)}`, 'muted small')),
    el('div', { className: 'plan-stats' },
      el('div', {}, text('span', 'You pay in 12 months'), text('b', money(credit.outOfPocket12))),
      credit.total ? el('div', {}, text('span', 'Covered by credits'), text('b', `${credit.coveredMonths >= 120 ? '120+' : credit.coveredMonths} months`)) : null,
      credit.total ? el('div', {}, text('span', 'Credits expire unused'), text('b', money(credit.wasted))) : null),
    el('details', {}, text('summary', 'Cost breakdown'),
      plan.lines.map((line) => el('div', { className: 'cost-line' }, text('span', line.label), text('span', money(line.amount))))));
}

function renderAnalyze() {
  currentProject = null;
  const [repoLabel, repo] = field('Public GitHub repo', { required: true, placeholder: 'owner/name', spellcheck: false, autocomplete: 'off' });
  const [trafficLabel, traffic] = field('Requests per day', { type: 'number', min: '0', value: '1000' });
  const credits = el('div');
  const addCredit = el('button', { type: 'button', className: 'ghost small', onclick: () => addCreditRow(credits) }, icon('plus', 14), 'Add credit');
  const submit = el('button', {}, icon('search', 15), 'Analyze');
  const results = el('div', { className: 'stack', ariaLive: 'polite' });
  const form = el('form', { className: 'card stack' },
    el('div', { className: 'fields' }, repoLabel, trafficLabel),
    el('div', { className: 'stack tight' }, text('h3', 'Cloud credits'), text('p', 'Plans are ranked by what you actually pay after credits.', 'hint'), credits, el('div', {}, addCredit)),
    el('div', {}, submit));
  form.onsubmit = action(submit, async () => {
    const creditList = [...credits.children].map((row) => {
      const [cloud, amount, expiresOn] = row.querySelectorAll('select, input');
      return { cloud: cloud.value, amount: Number(amount.value), expiresOn: expiresOn.value };
    });
    const data = await api('/analyze', { method: 'POST', body: { repo: repo.value, requestsPerDay: Number(traffic.value), credits: creditList } });
    const notes = [...data.warnings, ...data.map.unknowns, ...data.deployIssues];
    results.replaceChildren(...[
      text('h2', data.repo),
      ...notes.map((note) => notice(note)),
      el('div', { className: 'grid grid-2' }, detectedCard(data), portabilityCard(data.portability)),
      el('div', { className: 'row between' }, text('h2', 'Ranked plans'), text('span', 'Unverified price estimates', 'muted small')),
      data.excluded.length ? notice(`Not priced: ${data.excluded.join(', ')}`) : null,
      el('div', { className: 'plan-grid' }, ...data.plans.map((plan, index) => planCard(plan, index === 0))),
    ].filter(Boolean));
  });
  setPage('analyze', pageHead({ title: 'Analyze a repo', subtitle: text('p', 'See what an app needs, what it costs on each cloud, and how hard it is to move.', 'muted') }), form, results);
}

/* Routing */

async function route() {
  showToast(null);
  const [path, query] = location.hash.replace(/^#\/?/, '').split('?');
  const parts = path.split('/');
  if (!user) return renderGate(parts[0] === 'denied');
  try {
    if (parts[0] === 'analyze') renderAnalyze();
    else if (parts[0] === 'accounts') await renderAccounts();
    else if (parts[0] === 'projects') await renderProjects(query === 'new');
    else if (parts[0] === 'p' && parts[1]) await renderProject(parts[1], parts[2]);
    else await renderHome();
  } catch (error) {
    if (!error.quiet) showToast(error.message);
  }
}

window.addEventListener('hashchange', route);
document.body.append(palette, proposalDialog);

(async () => {
  try {
    methods = await fetch('/api/auth/methods').then((response) => response.json());
    const me = await fetch('/api/auth/me');
    if (me.ok) user = (await me.json()).user;
  } catch {
    showToast('Cannot reach the Cloudhop backend. Is it running?');
  }
  route();
})();
