// D1 tables, secret handling and metric math.

const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', secret_enc TEXT, created_at INTEGER NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL UNIQUE, account_id TEXT NOT NULL, report_hash TEXT NOT NULL,
    clouds TEXT NOT NULL DEFAULT '{}', active TEXT, previous TEXT, url TEXT, pr_url TEXT, portability TEXT, created_at INTEGER NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS sessions (id_hash TEXT PRIMARY KEY, user TEXT NOT NULL, expires_at INTEGER NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, at INTEGER NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS checks (project_id TEXT NOT NULL, at INTEGER NOT NULL, ok INTEGER NOT NULL, status INTEGER, ms INTEGER)`,
  `CREATE INDEX IF NOT EXISTS checks_by_project ON checks (project_id, at)`,
  `CREATE INDEX IF NOT EXISTS reports_by_project ON reports (project_id, at)`,
];

let ready = false;
export async function ensureSchema(db) {
  if (ready) return;
  await db.batch(SCHEMA.map((sql) => db.prepare(sql)));
  ready = true;
}

const toBase64 = (bytes) => btoa(String.fromCharCode(...bytes));
const fromBase64 = (text) => Uint8Array.from(atob(text), (char) => char.charCodeAt(0));

export async function sha256(text) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return toBase64(new Uint8Array(digest));
}

async function aesKey(secret) {
  const raw = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret));
  return crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt', 'decrypt']);
}

// `bound` is the row id, passed as AES-GCM additional data: a ciphertext copied into another row will not decrypt.
export async function encrypt(plain, secret, bound) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const data = await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: new TextEncoder().encode(bound) }, await aesKey(secret), new TextEncoder().encode(plain));
  return `${toBase64(iv)}.${toBase64(new Uint8Array(data))}`;
}

export async function decrypt(stored, secret, bound) {
  const [iv, data] = stored.split('.').map(fromBase64);
  const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv, additionalData: new TextEncoder().encode(bound) }, await aesKey(secret), data);
  return new TextDecoder().decode(plain);
}

export function randomToken() {
  return toBase64(crypto.getRandomValues(new Uint8Array(32))).replace(/[^A-Za-z0-9]/g, '');
}

// Never includes report_hash, so rows are safe to send to the browser.
export function publicProject(row) {
  return {
    id: row.id, repo: row.repo, accountId: row.account_id, clouds: JSON.parse(row.clouds), active: row.active, previous: row.previous,
    url: row.url, prUrl: row.pr_url, portability: row.portability, createdAt: row.created_at,
  };
}

// Never includes secret_enc.
export function publicAccount(row) {
  return { id: row.id, kind: row.kind, label: row.label, data: JSON.parse(row.data), createdAt: row.created_at };
}

export function uptimeSummary(checks) {
  const up = checks.filter((check) => check.ok).length;
  const okChecks = checks.filter((check) => check.ok && check.ms !== null);
  return {
    checks: checks.length,
    uptimePercent: checks.length ? (up / checks.length) * 100 : null,
    averageMs: okChecks.length ? Math.round(okChecks.reduce((sum, check) => sum + check.ms, 0) / okChecks.length) : null,
    last: checks.at(-1) ?? null,
  };
}

// Buckets raw uptime checks for charts, and finds outages as runs of consecutive failed checks.
export function summarizeChecks(checks, since, now, bucketCount) {
  const size = (now - since) / bucketCount;
  const buckets = Array.from({ length: bucketCount }, (_, index) => ({ start: Math.round(since + index * size), checks: 0, up: 0, msTotal: 0, msCount: 0, maxMs: null }));
  const statuses = {};
  const incidents = [];
  let open = null;
  for (const check of checks) {
    const bucket = buckets[Math.min(bucketCount - 1, Math.floor((check.at - since) / size))];
    if (bucket) {
      bucket.checks++;
      if (check.ok) bucket.up++;
      if (check.ok && check.ms !== null) {
        bucket.msTotal += check.ms;
        bucket.msCount++;
        bucket.maxMs = Math.max(bucket.maxMs ?? 0, check.ms);
      }
    }
    const key = check.status === null ? 'No response' : String(check.status);
    statuses[key] = (statuses[key] ?? 0) + 1;
    if (!check.ok) {
      open ??= { start: check.at, end: check.at, failedChecks: 0 };
      open.end = check.at;
      open.failedChecks++;
    } else if (open) {
      incidents.push({ ...open, end: check.at });
      open = null;
    }
  }
  if (open) incidents.push({ ...open, ongoing: true });
  const sorted = checks.filter((check) => check.ok && check.ms !== null).map((check) => check.ms).sort((a, b) => a - b);
  return {
    ...uptimeSummary(checks),
    p95Ms: sorted.length ? sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * 0.95) - 1)] : null,
    buckets: buckets.map((bucket) => ({
      start: bucket.start,
      checks: bucket.checks,
      uptimePercent: bucket.checks ? (bucket.up / bucket.checks) * 100 : null,
      averageMs: bucket.msCount ? Math.round(bucket.msTotal / bucket.msCount) : null,
      maxMs: bucket.maxMs,
    })),
    statuses: Object.entries(statuses).map(([status, count]) => ({ status, count })).sort((a, b) => b.count - a.count),
    incidents: incidents.reverse(),
  };
}

// A report moves the "active cloud" pointer only when traffic actually moved.
export function nextState(project, report) {
  if (report.event === 'deployed') return { active: report.cloud, previous: project.previous, url: report.url ?? project.url };
  if (report.event === 'switched' || report.event === 'rolled_back') return { active: report.cloud, previous: report.from ?? project.active, url: report.url ?? project.url };
  if (report.event === 'destroyed') {
    if (report.cloud === project.active) return { active: null, previous: project.previous === report.cloud ? null : project.previous, url: null };
    if (report.cloud === project.previous) return { active: project.active, previous: null, url: project.url };
  }
  return { active: project.active, previous: project.previous, url: project.url };
}
