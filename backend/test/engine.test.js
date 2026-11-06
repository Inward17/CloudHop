import assert from 'node:assert/strict';
import { test } from 'node:test';
import { analyzeFiles } from '../src/analyze.js';
import { parseRepo } from '../src/github.js';
import { rankPlans, simulateCredits } from '../src/plans.js';
import { portabilityReport } from '../src/portability.js';

const TODAY = Date.parse('2026-01-01');

const nodeApp = [
  { path: 'package.json', text: JSON.stringify({ scripts: { start: 'node .' }, dependencies: { express: '4', pg: '8', bullmq: '5', '@aws-sdk/client-dynamodb': '3' } }, null, 2) },
  { path: 'Dockerfile', text: 'FROM node:22\nEXPOSE 3000\n' },
  { path: '.env.example', text: 'DATABASE_URL=postgres://u:secretvalue@h/db\nAPI_KEY=abc\n' },
];

test('finds service, datastores, worker and lock-in with evidence', () => {
  const map = analyzeFiles(nodeApp);
  assert.equal(map.services[0].framework, 'Express');
  assert.equal(map.services[0].port, 3000);
  assert.deepEqual(map.datastores.map((store) => store.kind).sort(), ['postgres', 'redis']);
  assert.equal(map.workers.length, 1);
  assert.equal(map.bindings[0].service, 'DynamoDB');
  assert.ok(map.bindings[0].evidence.line > 1);
});

test('keeps env var names and drops their values', () => {
  const map = analyzeFiles(nodeApp);
  assert.deepEqual(map.envVars, ['DATABASE_URL', 'API_KEY']);
  assert.ok(!JSON.stringify(map).includes('secretvalue'));
});

test('parses python deps, ignoring comments and version pins', () => {
  const map = analyzeFiles([{ path: 'api/requirements.txt', text: '# flask\nFastAPI[all]==0.1\npsycopg2-binary>=2\nredis\n' }]);
  assert.equal(map.services[0].framework, 'FastAPI');
  assert.deepEqual(map.datastores.map((store) => store.kind).sort(), ['postgres', 'redis']);
});

test('skips workspace roots and reads compose images', () => {
  const map = analyzeFiles([
    { path: 'package.json', text: '{"workspaces":["packages/*"]}' },
    { path: 'compose.yml', text: 'services:\n  db:\n    image: "postgres:16"\n  q:\n    image: bitnami/rabbitmq:3\n' },
  ]);
  assert.equal(map.services.length, 0);
  assert.deepEqual(map.datastores.map((store) => store.kind), ['postgres', 'rabbitmq']);
  assert.equal(map.datastores[0].evidence[0].line, 3);
});

test('pyproject without a framework is not a service', () => {
  const map = analyzeFiles([{ path: 'pyproject.toml', text: '[tool.uv.workspace]\nmembers = ["backend"]\n' }]);
  assert.equal(map.services.length, 0);
});

test('prototype-named deps do not crash or match', () => {
  const map = analyzeFiles([{ path: 'requirements.txt', text: 'constructor\n__proto__\n' }]);
  assert.equal(map.datastores.length, 0);
});

test('portability verdict follows the worst item', () => {
  const report = portabilityReport(analyzeFiles(nodeApp));
  assert.equal(report.worst, 'redesign');
  assert.equal(report.items[0].name, 'DynamoDB');
  assert.equal(portabilityReport(analyzeFiles([nodeApp[1]])).worst, 'auto');
});

test('credits burn until exhausted', () => {
  const result = simulateCredits(100, [{ amount: 300, expiresOn: '2027-01-01' }], TODAY);
  assert.equal(result.coveredMonths, 3);
  assert.equal(result.wasted, 0);
  assert.equal(result.outOfPocket12, 900);
});

test('unspent credits past expiry count as waste', () => {
  const result = simulateCredits(50, [{ amount: 5000, expiresOn: '2027-01-01' }], TODAY);
  assert.equal(result.coveredMonths, 12);
  assert.equal(result.outOfPocket12, 0);
  assert.equal(result.wasted, 4400);
});

test('expired credits are ignored and no credits means full price', () => {
  const result = simulateCredits(10, [{ amount: 100, expiresOn: '2025-01-01' }], TODAY);
  assert.equal(result.coveredMonths, 0);
  assert.equal(result.outOfPocket12, 120);
  assert.equal(result.total, 0);
});

test('partial month of credit charges only the remainder', () => {
  const result = simulateCredits(100, [{ amount: 150, expiresOn: '2027-01-01' }], TODAY);
  assert.equal(result.coveredMonths, 1);
  assert.equal(result.outOfPocket12, 1050);
});

test('no credits ranks cheapest first; large credits buy reliability on that cloud', () => {
  const map = analyzeFiles(nodeApp);
  assert.equal(rankPlans(map, { today: TODAY }).plans[0].tier, 'cheapest');
  const funded = rankPlans(map, { today: TODAY, credits: [{ cloud: 'gcp', amount: 50000, expiresOn: '2028-01-01' }] }).plans[0];
  assert.equal(funded.id, 'gcp-prod');
});

test('static site gets static plans and unpriced stores are reported', () => {
  const staticMap = analyzeFiles([{ path: 'package.json', text: '{"devDependencies":{"vite":"5"}}' }]);
  assert.equal(rankPlans(staticMap).plans[0].tier, 'static');
  const mongo = analyzeFiles([{ path: 'package.json', text: '{"scripts":{"start":"x"},"dependencies":{"mongoose":"8"}}' }]);
  assert.deepEqual(rankPlans(mongo).excluded, ['mongodb']);
});

test('parseRepo accepts GitHub repos and rejects everything else', () => {
  assert.deepEqual(parseRepo('https://github.com/a/b.git'), { owner: 'a', repo: 'b' });
  assert.deepEqual(parseRepo('a/b.js'), { owner: 'a', repo: 'b.js' });
  for (const bad of ['https://evil.com/a/b', 'a', '../a/b', 'a/b/c', null]) assert.throws(() => parseRepo(bad));
});
