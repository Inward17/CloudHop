// Answers "how hard is it to move this app to another cloud?" from the service map.

const DATASTORE_TIERS = {
  postgres: ['auto', 'Managed on every major cloud; logical replication works'],
  mysql: ['auto', 'Managed on every major cloud; replication works'],
  redis: ['auto', 'Managed on every major cloud; treat contents as cache unless persistence is confirmed'],
  objects: ['auto', 'Files sync between S3-compatible stores'],
  mongodb: ['code', 'No same-API managed service on every cloud; use Atlas or run as container'],
  rabbitmq: ['code', 'Not managed on every cloud; run as container or use hosted broker'],
  kafka: ['code', 'Managed offerings differ per cloud; needs review'],
  elasticsearch: ['code', 'Managed offerings differ per cloud; needs reindex'],
  sqlite: ['redesign', 'File on local disk does not survive container restarts; move to Postgres'],
};

const VERDICTS = {
  auto: 'Portable by design',
  code: 'Portable after code changes',
  redesign: 'Needs redesign before switching',
  blocked: 'Has parts that cannot move as is',
};
const SEVERITY = ['auto', 'code', 'redesign', 'blocked'];

export function portabilityReport(map) {
  const items = [];
  for (const service of map.services) {
    const buildable = service.runtime === 'node' || service.runtime === 'python' || service.hasDockerfile;
    items.push(buildable
      ? { name: `${service.path} (${service.runtime})`, tier: 'auto', why: 'Runs as a container, so it runs on any cloud', evidence: service.evidence[0] ?? null }
      : { name: `${service.path} (${service.runtime})`, tier: 'code', why: 'Add a Dockerfile so it can run anywhere', evidence: null });
  }
  for (const store of map.datastores) {
    const [tier, why] = DATASTORE_TIERS[store.kind];
    items.push({ name: store.kind, tier, why, evidence: store.evidence[0] });
  }
  for (const binding of map.bindings) {
    items.push({ name: binding.service, tier: binding.tier, why: binding.why, evidence: binding.evidence });
  }
  items.sort((a, b) => SEVERITY.indexOf(b.tier) - SEVERITY.indexOf(a.tier));
  const worst = items[0]?.tier ?? 'auto';
  return { verdict: VERDICTS[worst], worst, items };
}
