// Deterministic cost and credit math. No LLM may change these numbers.
// ponytail: hardcoded on-demand list prices (us-east-1 / us-central1) written from memory,
// not checked against a price API. Replace with cached AWS Price List and GCP Catalog data before relying on them.
export const PRICES_VERIFIED = false;

const HOURS = 730;
const SECONDS = HOURS * 3600;
const REQUEST_KB = 50;
const REQUEST_SECONDS = 0.2;

const hourly = (rate, count = 1) => rate * HOURS * count;
const fargate = (vcpu, gb) => hourly(0.04048 * vcpu + 0.004445 * gb);
const EGRESS = { aws: 0.09, gcp: 0.12 };

const has = (map, ...kinds) => map.datastores.some((store) => kinds.includes(store.kind));

function line(label, amount) {
  return { label, amount };
}

// Each builder returns cost lines for one app on one setup.
const BLUEPRINTS = [
  {
    id: 'aws-vm', cloud: 'aws', tier: 'cheapest', reliability: 1, name: 'AWS: single VM with Compose',
    note: 'All services and data on one machine. You own backups. Downtime if it fails.',
    build: () => [line('EC2 t3.small', hourly(0.0208)), line('Disk 20 GB', 1.6), line('Public IP', hourly(0.005))],
  },
  {
    id: 'aws-balanced', cloud: 'aws', tier: 'balanced', reliability: 2, name: 'AWS: ECS Express with managed data',
    note: 'Managed containers and database in one zone, with automatic backups.',
    build: (map, web) => [
      line(`Fargate x${web}`, fargate(0.25, 0.5) * web),
      line('Public IP per task', hourly(0.005, web)),
      line('Load balancer', hourly(0.0225)),
      ...(has(map, 'postgres', 'mysql') ? [line('RDS t4g.micro + 20 GB', hourly(0.016) + 2.3)] : []),
      ...(has(map, 'redis') ? [line('ElastiCache t4g.micro', hourly(0.016))] : []),
      ...(has(map, 'objects') ? [line('S3 10 GB', 0.23)] : []),
    ],
  },
  {
    id: 'aws-prod', cloud: 'aws', tier: 'production', reliability: 3, name: 'AWS: multi-zone production',
    note: 'Two copies of each service, database failover, private networking.',
    build: (map, web) => [
      line(`Fargate x${web * 2}`, fargate(0.5, 1) * web * 2),
      line('Load balancer', hourly(0.0225)),
      line('NAT gateway', hourly(0.045)),
      ...(has(map, 'postgres', 'mysql') ? [line('RDS t4g.small Multi-AZ + 20 GB', hourly(0.065) + 4.6)] : []),
      ...(has(map, 'redis') ? [line('ElastiCache t4g.small x2', hourly(0.032, 2))] : []),
      ...(has(map, 'objects') ? [line('S3 10 GB', 0.23)] : []),
    ],
  },
  {
    id: 'gcp-vm', cloud: 'gcp', tier: 'cheapest', reliability: 1, name: 'GCP: single VM with Compose',
    note: 'All services and data on one machine. You own backups. Downtime if it fails.',
    build: () => [line('e2-small', hourly(0.0168)), line('Disk 20 GB', 2), line('Public IP', hourly(0.005))],
  },
  {
    id: 'gcp-balanced', cloud: 'gcp', tier: 'balanced', reliability: 2, name: 'GCP: Cloud Run with managed data',
    note: 'Scales to zero when idle, so first request can be slow. Database has automatic backups.',
    build: (map, web, requestsPerMonth) => [
      line(`Cloud Run x${web}`, requestsPerMonth * REQUEST_SECONDS * (0.000024 + 0.0000025 * 0.5)),
      ...(has(map, 'postgres', 'mysql') ? [line('Cloud SQL db-f1-micro + 20 GB', hourly(0.0105) + 3.4)] : []),
      ...(has(map, 'redis') ? [line('Memorystore basic 1 GB', hourly(0.049))] : []),
      ...(has(map, 'objects') ? [line('Cloud Storage 10 GB', 0.2)] : []),
    ],
  },
  {
    id: 'gcp-prod', cloud: 'gcp', tier: 'production', reliability: 3, name: 'GCP: always-on with database failover',
    note: 'Always-on instances, high-availability database and Redis.',
    build: (map, web) => [
      line(`Cloud Run always-on x${web}`, SECONDS * (0.000018 + 0.000002 * 0.5) * web),
      ...(has(map, 'postgres', 'mysql') ? [line('Cloud SQL 1 vCPU HA + 20 GB', hourly(0.138) + 6.8)] : []),
      ...(has(map, 'redis') ? [line('Memorystore standard 1 GB', hourly(0.098))] : []),
      ...(has(map, 'objects') ? [line('Cloud Storage 10 GB', 0.2)] : []),
    ],
  },
];

const STATIC_BLUEPRINTS = [
  { id: 'aws-static', cloud: 'aws', tier: 'static', reliability: 3, name: 'AWS: S3 and CloudFront', note: 'Static files on a CDN.', build: () => [line('Storage and CDN base', 1)] },
  { id: 'gcp-static', cloud: 'gcp', tier: 'static', reliability: 3, name: 'GCP: Cloud Storage and CDN', note: 'Static files on a CDN.', build: () => [line('Storage and CDN base', 1)] },
];

const MONTH_MS = (365.25 / 12) * 24 * 3600 * 1000;

// Burns credits month by month, earliest expiry first. Unspent balance past expiry is waste.
export function simulateCredits(monthly, credits, today, horizon = 120) {
  const pool = credits
    .map((credit) => ({ left: credit.amount, expires: (Date.parse(credit.expiresOn) - today) / MONTH_MS }))
    .filter((credit) => credit.expires > 0)
    .sort((a, b) => a.expires - b.expires);
  const total = pool.reduce((sum, credit) => sum + credit.left, 0);
  let coveredMonths = 0;
  let outOfPocket12 = 0;
  for (let month = 0; month < horizon; month++) {
    let due = monthly;
    for (const credit of pool) {
      if (credit.expires <= month || due === 0) continue;
      const used = Math.min(credit.left, due);
      credit.left -= used;
      due -= used;
    }
    if (month < 12) outOfPocket12 += due;
    if (due === 0 && coveredMonths === month) coveredMonths++;
  }
  const wasted = pool.reduce((sum, credit) => sum + credit.left, 0);
  return { total, coveredMonths, outOfPocket12, wasted };
}

export function rankPlans(map, { requestsPerDay = 1000, credits = [], today = Date.now() } = {}) {
  const webCount = map.services.filter((service) => service.kind === 'web').length;
  const staticOnly = map.services.length > 0 && webCount === 0 && map.datastores.length === 0;
  const blueprints = staticOnly ? STATIC_BLUEPRINTS : BLUEPRINTS;
  const requestsPerMonth = requestsPerDay * 30;
  const egressGb = (requestsPerMonth * REQUEST_KB) / 1e6;
  const priced = new Set(['postgres', 'mysql', 'redis', 'objects']);
  const excluded = map.datastores.map((store) => store.kind).filter((kind) => !priced.has(kind));

  const plans = blueprints.map((blueprint) => {
    const lines = blueprint.build(map, Math.max(webCount, 1), requestsPerMonth);
    lines.push(line(`Egress ${egressGb.toFixed(1)} GB`, egressGb * EGRESS[blueprint.cloud]));
    const monthly = lines.reduce((sum, item) => sum + item.amount, 0);
    const credit = simulateCredits(monthly, credits.filter((item) => item.cloud === blueprint.cloud), today);
    return {
      deployable: blueprint.id === 'aws-vm' || blueprint.id === 'gcp-vm',
      id: blueprint.id, cloud: blueprint.cloud, tier: blueprint.tier, name: blueprint.name, note: blueprint.note, reliability: blueprint.reliability,
      lines, monthly, low: monthly * 0.8, high: monthly * 1.5, credit,
    };
  });

  // Lowest real money over 12 months wins. On a tie, take the more reliable plan,
  // because credits that would expire unused should buy better infrastructure.
  plans.sort((a, b) => a.credit.outOfPocket12 - b.credit.outOfPocket12 || b.reliability - a.reliability || a.monthly - b.monthly);
  return { plans, excluded, pricesVerified: PRICES_VERIFIED };
}
