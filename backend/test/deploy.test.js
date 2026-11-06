import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';
import { analyzeFiles } from '../src/analyze.js';
import { appServices, bootstrapFor, cloudConfig, composeFor, deployIssues, dockerfileFor, installFiles, projectSlug, resourceName, trustFor, validateCloudInput } from '../src/generate.js';
import { parseRepoFor, providerOf } from '../src/providers.js';
import { selectManifests } from '../src/analyze.js';
import { proposalFor } from '../src/intent.js';
import { allowedLogin, checkOrigin, cookie, loginMethods, readCookie } from '../src/auth.js';
import { appJwt } from '../src/github.js';
import { decrypt, encrypt, nextState, publicAccount, publicProject, summarizeChecks, uptimeSummary } from '../src/store.js';

const map = analyzeFiles([
  { path: 'api/requirements.txt', text: 'fastapi\npsycopg2\nredis\n' },
  { path: 'web/package.json', text: '{"devDependencies":{"vite":"5"}}' },
]);
const aws = cloudConfig('aws', { accountId: '123456789012', region: 'us-east-1' }, 'o/r');
const gcp = cloudConfig('gcp', { projectId: 'my-project', projectNumber: '123456', region: 'us-central1' }, 'o/r');
const both = { clouds: { aws, gcp }, active: 'aws', previous: null, portability: 'auto' };

test('backend gets the main port and generated compose wires data', () => {
  assert.deepEqual(appServices(map).map((service) => [service.name, service.publicPort]), [['app-api', 80], ['app-web', 8081]]);
  const compose = composeFor(map);
  for (const expected of ['DATABASE_URL: postgres://app:${DB_PASSWORD}@db:5432/app', 'REDIS_URL', 'pg_isready', '"80:8000"', '"8081:80"', 'build: ./src/api', 'build: ./src/web']) assert.ok(compose.includes(expected), expected);
  assert.ok(!compose.includes('IMAGE_PREFIX'), 'images are built on the VM, not pulled from a registry');
});

test('install writes CI, infra and only missing Dockerfiles', () => {
  const docker = analyzeFiles([{ path: 'Dockerfile', text: 'FROM x\nEXPOSE 9000\n' }]);
  const { files } = installFiles({ repo: 'o/Some_Repo', map: docker, clouds: { aws }, cloudhopUrl: 'https://x.dev' });
  assert.ok(!files.some((file) => file.path === 'Dockerfile'));
  assert.ok(files.some((file) => file.path === '.github/workflows/cloudhop.yml'));
  const config = JSON.parse(files.find((file) => file.path === '.cloudhop/config.json').content);
  assert.match(config.name, /^cloudhop-some-repo-[a-z0-9]{1,4}$/);
  const withBuild = installFiles({ repo: 'o/r', map, clouds: { gcp }, cloudhopUrl: 'https://x.dev' }).files.map((file) => file.path);
  assert.ok(withBuild.includes('api/Dockerfile') && withBuild.includes('web/Dockerfile'));
  assert.ok(resourceName('o/' + 'a'.repeat(40)).length <= 30);
});

test('same repo name under different owners gets different cloud names', () => {
  assert.notEqual(projectSlug('alice/app'), projectSlug('bob/app'));
  assert.equal(projectSlug('alice/app'), projectSlug('alice/app'));
  assert.match(aws.roleArn, new RegExp(`role/cloudhop-${projectSlug('o/r')}-deploy$`));
  assert.ok(gcp.workloadIdentityProvider.endsWith(`workloadIdentityPools/ch-${projectSlug('o/r')}/providers/ci`));
});

test('each code host gets its own CI file and never overwrites an existing one', () => {
  const ci = (provider, existing = []) => installFiles({ provider, repo: 'g/p', map, clouds: { aws }, cloudhopUrl: 'https://x.dev', existingPaths: new Set(existing) });
  assert.ok(ci('gitlab').files.some((file) => file.path === '.gitlab-ci.yml'));
  assert.ok(ci('gitlab').files.some((file) => file.path === '.cloudhop/ci.sh'));
  const taken = ci('gitlab', ['.gitlab-ci.yml']);
  assert.ok(!taken.files.some((file) => file.path === '.gitlab-ci.yml') && taken.files.some((file) => file.path === '.cloudhop/gitlab-ci.yml'));
  assert.match(taken.notes[0], /include/);
  assert.ok(ci('bitbucket').files.some((file) => file.path === 'bitbucket-pipelines.yml'));
  assert.equal(ci('bitbucket', ['bitbucket-pipelines.yml']).notes.length, 1);
  assert.ok(ci('azure').files.some((file) => file.path === '.cloudhop/azure-pipelines.yml'));
  assert.ok(!ci('github').files.some((file) => file.path === '.cloudhop/ci.sh'));
});

test('clouds trust only the right CI identity for each code host', () => {
  assert.equal(trustFor('github', { repo: 'o/r' }).subject, 'repo:o/r:environment:cloudhop');
  const gitlab = trustFor('gitlab', { repo: 'g/sub/p', defaultBranch: 'main' });
  assert.equal(gitlab.subject, 'project_path:g/sub/p:ref_type:branch:ref:main');
  assert.equal(gitlab.awsAudience, 'https://gitlab.com');
  const uuid = '{1de489be-ce6a-42a0-a8c8-eadbf1174ac7}';
  const bitbucket = trustFor('bitbucket', { workspace: 'team', workspaceUuid: '{922e118c-f34f-4226-a897-0e0afbec553d}', repositoryUuid: uuid });
  assert.equal(bitbucket.subject, `${uuid}*`);
  assert.equal(bitbucket.awsAudience, 'ari:cloud:bitbucket::workspace/922e118c-f34f-4226-a897-0e0afbec553d');
  assert.match(bitbucket.issuer, /^api\.bitbucket\.org\/2\.0\/workspaces\/team\//);
  const azure = trustFor('azure', { organization: 'contoso', project: 'Web Shop', organizationId: '00000000-0000-0000-0000-000000000001' });
  assert.equal(azure.subject, 'sc://contoso/Web Shop/cloudhop');
  assert.throws(() => trustFor('gitlab', { repo: "g/p'; rm -rf /", defaultBranch: 'main' }));
  assert.throws(() => trustFor('azure', { organization: 'a', project: '$(evil)', organizationId: 'x' }));
});

test('repo input is parsed per code host, including pasted URLs', () => {
  assert.equal(parseRepoFor('github', 'https://github.com/o/r.git'), 'o/r');
  assert.equal(parseRepoFor('gitlab', 'https://gitlab.com/group/sub/project'), 'group/sub/project');
  assert.equal(parseRepoFor('bitbucket', 'https://bitbucket.org/team/repo/'), 'team/repo');
  assert.equal(parseRepoFor('azure', 'https://dev.azure.com/contoso/Web%20Shop/_git/api'), 'contoso/Web Shop/api');
  for (const [provider, bad] of [['github', 'a/b/c'], ['bitbucket', 'a'], ['azure', 'org/repo'], ['gitlab', '../x/y'], ['gitlab', "g/p';x"]]) {
    assert.throws(() => parseRepoFor(provider, bad), undefined, `${provider} ${bad}`);
  }
  assert.equal(providerOf('gitlab_token'), 'gitlab');
  assert.equal(providerOf('github_app'), 'github');
  assert.throws(() => providerOf('aws'));
});

test('manifest selection skips vendored folders and keeps the shallowest files', () => {
  const { selected, total } = selectManifests([
    { path: 'node_modules/x/package.json', size: 10 },
    { path: 'apps/web/package.json', size: 10 },
    { path: 'package.json', size: 10 },
    { path: 'README.md', size: 10 },
    { path: 'huge/package.json', size: 10_000_000 },
  ]);
  assert.deepEqual(selected.map((entry) => entry.path), ['package.json', 'apps/web/package.json']);
  assert.equal(total, 2);
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

test('setup files are filled in for every code host with no placeholders left', () => {
  const infos = {
    github: { repo: 'o/r' },
    gitlab: { repo: 'g/p', defaultBranch: 'main' },
    bitbucket: { workspace: 'team', workspaceUuid: '{922e118c-f34f-4226-a897-0e0afbec553d}', repositoryUuid: '{1de489be-ce6a-42a0-a8c8-eadbf1174ac7}' },
    azure: { organization: 'contoso', project: 'shop', organizationId: '00000000-0000-0000-0000-000000000001' },
  };
  for (const [provider, info] of Object.entries(infos)) {
    const trust = trustFor(provider, info);
    const shell = bootstrapFor('gcp', 'o/r', { projectId: 'my-project', region: 'us-central1' }, trust);
    const stack = bootstrapFor('aws', 'o/r', {}, trust);
    assert.ok(!shell.includes('__') && !stack.includes('__'), provider);
    assert.ok(stack.includes(`'${trust.issuer}:sub': '${trust.subject}'`), provider);
    assert.ok(shell.includes(trust.gcpCondition), provider);
  }
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
  const project = { id: '1', user_id: 'u', repo: 'o/r', account_id: 'row-1', report_hash: 'h', clouds: '{}', created_at: 1 };
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

test('sign-up is open unless an allowlist is set, and app install needs GitHub sign-in', () => {
  const github = { GITHUB_CLIENT_ID: 'id', GITHUB_CLIENT_SECRET: 'secret' };
  assert.ok(allowedLogin({}, 'anyone'));
  assert.ok(allowedLogin({ ALLOWED_GITHUB_USERS: 'Alice, bob' }, 'alice'));
  assert.ok(!allowedLogin({ ALLOWED_GITHUB_USERS: 'Alice, bob' }, 'mallory'));
  assert.deepEqual(loginMethods(github), { github: true, adminToken: false, githubApp: false, openSignup: true });
  assert.equal(loginMethods({ ...github, ALLOWED_GITHUB_USERS: 'a' }).openSignup, false);
  assert.equal(loginMethods({ GITHUB_APP_ID: '1', GITHUB_APP_SLUG: 's', GITHUB_APP_PRIVATE_KEY: 'k' }).githubApp, false);
  assert.equal(loginMethods({ ...github, GITHUB_APP_ID: '1', GITHUB_APP_SLUG: 's', GITHUB_APP_PRIVATE_KEY: 'k' }).githubApp, true);
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

test('generated shell scripts are valid bash', () => {
  for (const name of ['run.sh', 'ci.sh', 'bootstrap-gcp.sh']) {
    execFileSync('bash', ['-n', new URL(`../templates/${name}`, import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')]);
  }
});
