// Turns manifest files into a provider-neutral service map. Pure: no network.
// Repo content is untrusted, so it is only ever matched against fixed tables.

const code = (service, why) => ({ binding: { service, tier: 'code', why } });
const redesign = (service, why) => ({ binding: { service, tier: 'redesign', why } });
const blocked = (service, why) => ({ binding: { service, tier: 'blocked', why } });
const web = (framework) => ({ framework, kind: 'web' });
const stat = (framework) => ({ framework, kind: 'static' });

const NODE_DEPS = {
  next: web('Next.js'), nuxt: web('Nuxt'), express: web('Express'), fastify: web('Fastify'),
  '@nestjs/core': web('NestJS'), hono: web('Hono'), koa: web('Koa'), '@remix-run/node': web('Remix'),
  vite: stat('Vite'), astro: stat('Astro'), 'react-scripts': stat('Create React App'),
  pg: { datastore: 'postgres' }, postgres: { datastore: 'postgres' }, mysql: { datastore: 'mysql' },
  mysql2: { datastore: 'mysql' }, mongoose: { datastore: 'mongodb' }, mongodb: { datastore: 'mongodb' },
  redis: { datastore: 'redis' }, ioredis: { datastore: 'redis' },
  'better-sqlite3': { datastore: 'sqlite' }, sqlite3: { datastore: 'sqlite' },
  bullmq: { datastore: 'redis', worker: true }, bull: { datastore: 'redis', worker: true },
  'aws-sdk': code('AWS SDK v2', 'Calls need review for AWS-only services'),
  '@aws-sdk/client-s3': code('S3 SDK', 'Swap client or use S3-compatible endpoint'),
  '@aws-sdk/client-sqs': code('SQS', 'Replace with target queue service'),
  '@aws-sdk/client-sns': code('SNS', 'Replace with target pub/sub service'),
  '@aws-sdk/client-ses': code('SES', 'Change email provider; sender reputation does not move'),
  '@aws-sdk/client-bedrock-runtime': code('Bedrock', 'Swap model client'),
  '@aws-sdk/client-secrets-manager': code('Secrets Manager', 'Read secrets from environment instead'),
  '@aws-sdk/client-dynamodb': redesign('DynamoDB', 'No equal API elsewhere; data model must change'),
  '@aws-sdk/client-cognito-identity-provider': redesign('Cognito', 'User pools have no direct equal elsewhere'),
  '@aws-sdk/client-sfn': redesign('Step Functions', 'Workflows must be rebuilt'),
  '@aws-sdk/client-kms': blocked('KMS', 'Keys cannot be exported; re-encrypt data'),
  '@google-cloud/storage': code('Cloud Storage SDK', 'Swap client'),
  '@google-cloud/pubsub': code('Pub/Sub', 'Replace with target queue service'),
  '@google-cloud/vertexai': code('Vertex AI', 'Swap model client'),
  '@google-cloud/firestore': redesign('Firestore', 'No equal API elsewhere; data model must change'),
  'firebase-admin': redesign('Firebase', 'Auth and Firestore have no direct equal elsewhere'),
  '@google-cloud/bigquery': redesign('BigQuery', 'Queries and pipelines must be rebuilt'),
  '@azure/storage-blob': code('Blob Storage SDK', 'Swap client'),
  '@azure/service-bus': code('Service Bus', 'Replace with target queue service'),
  '@azure/cosmos': redesign('Cosmos DB', 'No equal API elsewhere; data model must change'),
};

const PYTHON_DEPS = {
  django: web('Django'), flask: web('Flask'), fastapi: web('FastAPI'),
  psycopg: { datastore: 'postgres' }, psycopg2: { datastore: 'postgres' },
  'psycopg2-binary': { datastore: 'postgres' }, asyncpg: { datastore: 'postgres' },
  pymysql: { datastore: 'mysql' }, mysqlclient: { datastore: 'mysql' }, aiomysql: { datastore: 'mysql' },
  pymongo: { datastore: 'mongodb' }, motor: { datastore: 'mongodb' },
  redis: { datastore: 'redis' }, rq: { datastore: 'redis', worker: true }, celery: { worker: true },
  boto3: code('AWS SDK', 'Calls need review for AWS-only services'),
  'google-cloud-storage': code('Cloud Storage SDK', 'Swap client'),
  'google-cloud-pubsub': code('Pub/Sub', 'Replace with target queue service'),
  'google-cloud-firestore': redesign('Firestore', 'No equal API elsewhere; data model must change'),
  'google-cloud-bigquery': redesign('BigQuery', 'Queries and pipelines must be rebuilt'),
  'firebase-admin': redesign('Firebase', 'Auth and Firestore have no direct equal elsewhere'),
  'azure-storage-blob': code('Blob Storage SDK', 'Swap client'),
  'azure-cosmos': redesign('Cosmos DB', 'No equal API elsewhere; data model must change'),
};

const COMPOSE_IMAGES = {
  postgres: 'postgres', postgis: 'postgres', mysql: 'mysql', mariadb: 'mysql', redis: 'redis', valkey: 'redis',
  mongo: 'mongodb', minio: 'objects', rabbitmq: 'rabbitmq', elasticsearch: 'elasticsearch', kafka: 'kafka',
};

const OTHER_RUNTIMES = {
  'pom.xml': 'java', 'build.gradle': 'java', 'build.gradle.kts': 'java', 'go.mod': 'go',
  Gemfile: 'ruby', 'Cargo.toml': 'rust', 'composer.json': 'php',
};
const ENV_FILES = new Set(['.env.example', '.env.sample', '.env.template']); // Never a real .env: it holds secrets.
const COMPOSE_FILES = new Set(['docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml']);

export const MANIFESTS = new Set([
  'package.json', 'requirements.txt', 'pyproject.toml', 'Dockerfile', 'Procfile',
  ...ENV_FILES, ...COMPOSE_FILES, ...Object.keys(OTHER_RUNTIMES),
]);

const SKIP = /(^|\/)(node_modules|vendor|dist|build|examples?|samples?|tests?|__tests__|fixtures|docs|\.github|\.cloudhop)\//;
export const MAX_FILES = 40; // Workers free plan allows 50 subrequests per request.
const MAX_BYTES = 200_000;

// Picks the manifest files worth reading, shallowest first, within the subrequest budget.
export function selectManifests(entries) {
  const wanted = entries
    .filter((entry) => (entry.size ?? 0) <= MAX_BYTES && !SKIP.test(entry.path) && MANIFESTS.has(entry.path.slice(entry.path.lastIndexOf('/') + 1)))
    .sort((a, b) => a.path.split('/').length - b.path.split('/').length);
  return { selected: wanted.slice(0, MAX_FILES), total: wanted.length };
}

const baseName = (path) => path.slice(path.lastIndexOf('/') + 1);
const dirName = (path) => (path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '.');

function lineOf(text, needle) {
  const index = text.split('\n').findIndex((line) => line.includes(needle));
  return index === -1 ? null : index + 1;
}

// Every finding keeps file and line so the user can check why it was detected.
export function analyzeFiles(files) {
  const services = new Map();
  const datastores = new Map();
  const bindings = new Map();
  const workers = [];
  const envVars = new Set();
  const unknowns = [];

  const serviceAt = (dir) => {
    if (!services.has(dir)) {
      services.set(dir, { path: dir, runtime: null, framework: null, kind: null, port: null, hasDockerfile: false, hasStart: false, evidence: [] });
    }
    return services.get(dir);
  };
  const addDatastore = (kind, evidence) => {
    if (!datastores.has(kind)) datastores.set(kind, { kind, evidence: [] });
    datastores.get(kind).evidence.push(evidence);
  };

  const applyDep = (signal, dep, file, text, service, productionDep) => {
    const evidence = { file, line: lineOf(text, dep), detail: dep };
    if (signal.framework && (!service.kind || signal.kind === 'web')) {
      service.framework = signal.framework;
      service.kind = signal.kind;
      service.evidence.push(evidence);
    }
    if (!productionDep) return;
    if (signal.datastore) addDatastore(signal.datastore, evidence);
    if (signal.worker) workers.push({ path: service.path, evidence });
    if (signal.binding && !bindings.has(signal.binding.service)) {
      bindings.set(signal.binding.service, { ...signal.binding, evidence });
    }
  };

  for (const { path, text } of files) {
    const name = baseName(path);
    const dir = dirName(path);

    if (name === 'package.json') {
      let pkg;
      try {
        pkg = JSON.parse(text);
      } catch {
        unknowns.push(`Could not parse ${path}`);
        continue;
      }
      const prod = Object.keys(pkg.dependencies ?? {});
      const dev = Object.keys(pkg.devDependencies ?? {});
      const hasStart = Boolean(pkg.scripts?.start);
      const isApp = hasStart || [...prod, ...dev].some((dep) => NODE_DEPS[dep]?.framework);
      if (!isApp) continue; // Workspace root or shared library, not something that runs.
      const service = serviceAt(dir);
      service.runtime = 'node';
      service.hasStart = hasStart;
      for (const dep of prod) if (NODE_DEPS[dep]) applyDep(NODE_DEPS[dep], dep, path, text, service, true);
      for (const dep of dev) if (NODE_DEPS[dep]) applyDep(NODE_DEPS[dep], dep, path, text, service, false);
    } else if (name === 'requirements.txt' || name === 'pyproject.toml') {
      const deps = text.split('\n')
        .map((line) => line.match(/^\s*["']?([A-Za-z0-9][A-Za-z0-9_.-]*)/)?.[1]?.toLowerCase())
        .filter((dep) => dep && Object.hasOwn(PYTHON_DEPS, dep));
      // A pyproject without a framework is usually a workspace root or library, not something that runs.
      const isApp = name === 'requirements.txt' || services.has(dir) || deps.some((dep) => PYTHON_DEPS[dep].framework);
      if (!isApp) continue;
      const service = serviceAt(dir);
      service.runtime = 'python';
      for (const dep of deps) applyDep(PYTHON_DEPS[dep], dep, path, text, service, true);
    } else if (name === 'Dockerfile') {
      const service = serviceAt(dir);
      service.hasDockerfile = true;
      service.runtime ??= 'container';
      const port = text.match(/^\s*EXPOSE\s+(\d+)/im)?.[1];
      if (port) service.port = Number(port);
    } else if (name === 'Procfile') {
      if (/^worker:/m.test(text)) workers.push({ path: dir, evidence: { file: path, line: lineOf(text, 'worker:'), detail: 'worker' } });
    } else if (COMPOSE_FILES.has(name)) {
      text.split('\n').forEach((line, index) => {
        const image = line.match(/^\s*image:\s*["']?(?:[\w.-]+\/)*([\w.-]+)/)?.[1]?.toLowerCase();
        if (image && COMPOSE_IMAGES[image]) addDatastore(COMPOSE_IMAGES[image], { file: path, line: index + 1, detail: `image ${image}` });
      });
    } else if (ENV_FILES.has(name)) {
      text.split('\n').forEach((line, index) => {
        const match = line.match(/^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*["']?(\w+:\/\/)?/);
        if (!match) return;
        envVars.add(match[1]); // Name only: values stay out of the map.
        const scheme = { 'postgres://': 'postgres', 'postgresql://': 'postgres', 'mysql://': 'mysql', 'mongodb://': 'mongodb', 'redis://': 'redis' }[match[2]];
        if (scheme) addDatastore(scheme, { file: path, line: index + 1, detail: match[1] });
      });
    } else if (OTHER_RUNTIMES[name]) {
      const service = serviceAt(dir);
      if (!service.runtime || service.runtime === 'container') service.runtime = OTHER_RUNTIMES[name];
    }
  }

  const list = [...services.values()];
  for (const service of list) service.kind ??= 'web';
  if (list.length === 0) unknowns.push('No runnable service found. Is the app in a language other than Node or Python, or in an excluded folder?');
  for (const service of list) {
    if (service.runtime !== 'node' && service.runtime !== 'python' && !service.hasDockerfile) {
      unknowns.push(`${service.path}: ${service.runtime} has no built-in build support yet and no Dockerfile`);
    }
    if (service.kind === 'web' && !service.framework && !service.hasDockerfile) {
      unknowns.push(`${service.path}: no known framework found, so start command needs confirming`);
    }
  }
  if (bindings.has('AWS SDK') || bindings.has('AWS SDK v2')) unknowns.push('Generic AWS SDK found. Source is not scanned yet, so which services it calls is unknown.');
  if (workers.length > 0 && !datastores.has('redis') && !datastores.has('rabbitmq')) unknowns.push('Background worker found, but its message broker was not detected.');

  return { services: list, datastores: [...datastores.values()], workers, bindings: [...bindings.values()], envVars: [...envVars], unknowns };
}
