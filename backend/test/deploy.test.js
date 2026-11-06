import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';
import { analyzeFiles } from '../src/analyze.js';
import { appServices, bootstrapFor, cloudConfig, composeFor, deployIssues, dockerfileFor, installFiles, resourceName, validateCloudInput } from '../src/generate.js';
import { proposalFor } from '../src/intent.js';
import { checkOrigin, cookie, readCookie } from '../src/auth.js';
import { appJwt } from '../src/github.js';
import { decrypt, encrypt, nextState, publicAccount, publicProject, summarizeChecks, uptimeSummary } from '../src/store.js';

const map = analyzeFiles([
  { path: 'api/requirements.txt', text: 'fastapi\npsycopg2\nredis\n' },
  { path: 'web/package.json', text: '{"devDependencies":{"vite":"5"}}' },
]);
const aws = cloudConfig('aws', { accountId: '123456789012', region: 'us-east-1' });
const gcp = cloudConfig('gcp', { projectId: 'my-project', projectNumber: '123456', region: 'us-central1' });
const both = { clouds: { aws, gcp }, active: 'aws', previous: null, portability: 'auto' };

test('backend gets the main port and generated compose wires data', () => {
  assert.deepEqual(appServices(map).map((service) => [service.name, service.publicPort]), [['app-api', 80], ['app-web', 8081]]);
  const compose = composeFor(map);
  for (const expected of ['DATABASE_URL: postgres://app:${DB_PASSWORD}@db:5432/app', 'REDIS_URL', 'pg_isready', '"80:8000"', '"8081:80"']) assert.ok(compose.includes(expected), expected);
});

test('install writes workflow, infra and only missing Dockerfiles', () => {
  const docker = analyzeFiles([{ path: 'Dockerfile', text: 'FROM x\nEXPOSE 9000\n' }]);
  const files = installFiles({ repo: 'o/Some_Repo', map: docker, clouds: { aws }, cloudhopUrl: 'https://x.dev' });
  assert.ok(!files.some((file) => file.path === 'Dockerfile'));
  const config = JSON.parse(files.find((file) => file.path === '.cloudhop/config.json').content);
  assert.equal(config.name, 'cloudhop-some-repo');
  assert.equal(config.clouds.aws.roleArn, 'arn:aws:iam::123456789012:role/cloudhop-deploy');
  const withBuild = installFiles({ repo: 'o/r', map, clouds: { gcp }, cloudhopUrl: 'https://x.dev' }).map((file) => file.path);
  assert.ok(withBuild.includes('api/Dockerfile') && withBuild.includes('web/Dockerfile'));
  assert.equal(resourceName('o/' + 'a'.repeat(40)).length, 29);
});

test('generated Dockerfiles match the framework', () => {
  assert.ok(dockerfileFor(map.services[0]).includes('uvicorn'));
  assert.ok(dockerfileFor(map.services[1]).includes('/app/dist'));
});

test('deploy refuses what it cannot run', () => {
  assert.deepEqual(deployIssues(map), []);
  assert.match(deployIssues(analyzeFiles([{ path: 'package.json', text: '{"scripts":{"start":"x"},"dependencies":{"mongoose":"8"}}' }]))[0], /mongodb/);
  assert.equal(deployIssues(analyzeFiles([])).length, 1);
});

test('cloud input rejects anything that could break generated files', () => {
  assert.ok(validateCloudInput('aws', { accountId: '123456789012', region: 'us-east-1' }));
  assert.ok(validateCloudInput('gcp', { projectId: 'my-project', projectNumber: '123456', region: 'us-central1' }));
  assert.ok(!validateCloudInput('aws', { accountId: '123', region: 'us-east-1' }));
  assert.ok(!validateCloudInput('aws', { accountId: '123456789012', region: 'x; rm -rf /' }));
  assert.ok(!validateCloudInput('gcp', { projectId: '$(evil)', projectNumber: '1', region: 'us-central1' }));
  assert.ok(!validateCloudInput('azure', {}));
});

test('bootstrap templates contain the repo and no placeholders', () => {
  const shell = bootstrapFor('gcp', 'o/r', { projectId: 'my-project', region: 'us-central1' });
  assert.ok(shell.includes('REPO=o/r') && !shell.includes('__'));
  assert.ok(!bootstrapFor('aws', 'o/r').includes('__'));
});

test('switch needs approval data and warns about downtime', () => {
  const proposal = proposalFor('Please switch this app to Google', both);
  assert.equal(proposal.action, 'switch');
  assert.equal(proposal.cloud, 'gcp');
  assert.equal(proposal.source, 'aws');
  assert.ok(proposal.warnings.length >= 2);
});

test('switch is refused when it cannot work', () => {
  assert.match(proposalFor('move to aws', both).reply, /already/);
  assert.match(proposalFor('switch to gcp', { ...both, active: null }).reply, /Deploy first/);
  assert.match(proposalFor('switch to gcp', { ...both, clouds: { aws } }).reply, /not connected/);
  assert.match(proposalFor('switch to gcp', { ...both, portability: 'redesign' }).reply, /refused/);
  assert.match(proposalFor('switch', both).reply, /Which cloud/);
});

test('destroy demands typed confirmation; unknown text gets help', () => {
  const proposal = proposalFor('destroy aws', both);
  assert.equal(proposal.confirmText, 'aws');
  assert.match(proposalFor('destroy', both).reply, /which cloud/);
  assert.match(proposalFor('hello there', both).reply, /Try/);
  assert.match(proposalFor('roll back', both).reply, /no previous/);
  assert.equal(proposalFor('roll back', { ...both, previous: 'gcp' }).cloud, 'gcp');
  assert.match(proposalFor('deploy to gcp', both).reply, /switch to gcp/);
});

test('reports move active cloud only when traffic moved', () => {
  const project = { active: 'aws', previous: null, url: 'http://a' };
  assert.deepEqual(nextState(project, { event: 'switched', cloud: 'gcp', from: 'aws', url: 'http://b' }), { active: 'gcp', previous: 'aws', url: 'http://b' });
  assert.deepEqual(nextState(project, { event: 'switch_failed', cloud: 'aws' }), { active: 'aws', previous: null, url: 'http://a' });
  assert.deepEqual(nextState({ active: 'gcp', previous: 'aws', url: 'http://b' }, { event: 'destroyed', cloud: 'aws' }), { active: 'gcp', previous: null, url: 'http://b' });
  assert.deepEqual(nextState({ active: 'gcp', previous: 'aws', url: 'http://b' }, { event: 'destroyed', cloud: 'gcp' }), { active: null, previous: 'aws', url: null });
});

test('secrets round-trip encrypted, bound to their row, and stay out of browser shapes', async () => {
  const stored = await encrypt('ghp_example', 'key-one', 'row-1');
  assert.ok(!stored.includes('ghp_example'));
  assert.equal(await decrypt(stored, 'key-one', 'row-1'), 'ghp_example');
  await assert.rejects(decrypt(stored, 'key-two', 'row-1'));
  await assert.rejects(decrypt(stored, 'key-one', 'row-2'));
  const account = { id: 'row-1', kind: 'github_token', label: 'me', data: '{}', secret_enc: stored, created_at: 1 };
  assert.ok(!JSON.stringify(publicAccount(account)).includes(stored));
  const project = { id: '1', repo: 'o/r', account_id: 'row-1', report_hash: 'h', clouds: '{}', created_at: 1 };
  assert.ok(!JSON.stringify(publicProject(project)).includes('"h"'));
});

test('metrics buckets checks and finds outages', () => {
  const at = (minutes) => minutes * 60_000;
  const checks = [
    { at: at(5), ok: 1, status: 200, ms: 100 },
    { at: at(10), ok: 0, status: 503, ms: null },
    { at: at(15), ok: 0, status: null, ms: null },
    { at: at(20), ok: 1, status: 200, ms: 300 },
    { at: at(50), ok: 0, status: 500, ms: null },
  ];
  const summary = summarizeChecks(checks, 0, at(60), 2);
  assert.equal(summary.buckets[0].checks, 4);
  assert.equal(summary.buckets[0].uptimePercent, 50);
  assert.equal(summary.buckets[0].averageMs, 200);
  assert.equal(summary.buckets[1].uptimePercent, 0);
  assert.equal(summary.p95Ms, 300);
  assert.equal(summary.incidents.length, 2);
  assert.ok(summary.incidents[0].ongoing);
  assert.deepEqual(summary.incidents[1], { start: at(10), end: at(20), failedChecks: 2 });
  assert.equal(summary.statuses[0].status, '200');
  assert.equal(summarizeChecks([], 0, at(60), 4).buckets[0].averageMs, null);
});

test('writes from other sites are refused', () => {
  const env = { PUBLIC_URL: 'http://localhost:5173' };
  const request = (method, origin) => new Request('http://api/api/projects', { method, headers: origin ? { Origin: origin } : {} });
  assert.doesNotThrow(() => checkOrigin(request('GET'), env));
  assert.doesNotThrow(() => checkOrigin(request('POST', 'http://localhost:5173'), env));
  assert.throws(() => checkOrigin(request('POST', 'https://evil.example'), env));
  assert.throws(() => checkOrigin(request('POST'), env));
});

test('session cookie is HttpOnly, and Secure on https', () => {
  assert.match(cookie({ PUBLIC_URL: 'https://app.example' }, 'a', 'b', 60), /HttpOnly; SameSite=Lax; Max-Age=60; Secure$/);
  assert.ok(!cookie({ PUBLIC_URL: 'http://localhost:5173' }, 'a', 'b', 60).includes('Secure'));
  assert.equal(readCookie(new Request('http://x', { headers: { Cookie: 'x=1; cloudhop_session=abc=; y=2' } }), 'cloudhop_session'), 'abc=');
});

test('GitHub App JWT is signed RS256 and verifies with the public key', async () => {
  const { privateKey, publicKey } = await crypto.subtle.generateKey({ name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['sign', 'verify']);
  const der = Buffer.from(await crypto.subtle.exportKey('pkcs8', privateKey)).toString('base64');
  const pem = `-----BEGIN PRIVATE KEY-----\n${der.match(/.{1,64}/g).join('\n')}\n-----END PRIVATE KEY-----`;
  const jwt = await appJwt('12345', pem, 1_000_000_000_000);
  const [header, payload, signature] = jwt.split('.');
  const decode = (part) => Buffer.from(part.replace(/-/g, '+').replace(/_/g, '/'), 'base64');
  assert.deepEqual(JSON.parse(decode(header)), { alg: 'RS256', typ: 'JWT' });
  assert.deepEqual(JSON.parse(decode(payload)), { iat: 999_999_940, exp: 1_000_000_540, iss: '12345' });
  assert.ok(await crypto.subtle.verify('RSASSA-PKCS1-v1_5', publicKey, decode(signature), new TextEncoder().encode(`${header}.${payload}`)));
  await assert.rejects(appJwt('1', '-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----'));
});

test('uptime summary handles empty and mixed checks', () => {
  assert.equal(uptimeSummary([]).uptimePercent, null);
  const summary = uptimeSummary([{ ok: 1, ms: 100 }, { ok: 0, ms: null }, { ok: 1, ms: 300 }, { ok: 1, ms: 200 }]);
  assert.equal(summary.uptimePercent, 75);
  assert.equal(summary.averageMs, 200);
});

test('run.sh is valid bash', () => {
  execFileSync('bash', ['-n', new URL('../templates/run.sh', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')]);
  execFileSync('bash', ['-n', new URL('../templates/bootstrap-gcp.sh', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')]);
});
