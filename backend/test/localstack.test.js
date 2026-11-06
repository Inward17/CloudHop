// Runs Cloudhop's real AWS path against LocalStack: the CloudFormation setup file, keyless sign-in
// through OIDC web identity (ci.sh), OpenTofu with the S3 state backend, and run.sh verify, plan,
// deploy, report and destroy. Every AWS call goes to LocalStack's AWS APIs.
//
// LocalStack has no real servers: EC2 instances are records, and SSM commands are accepted but
// never run. So the app itself is not built or started, and the health check is answered by a
// stand-in for requests to the app's address. Everything else is real tooling.
//
// Needs LOCALSTACK_URL (for example http://localhost:4566) plus tofu, aws, jq, git and bash on PATH.
// Run: LOCALSTACK_URL=http://localhost:4566 node --test test/localstack.test.js
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { after, before, test } from 'node:test';
import { analyzeFiles } from '../src/analyze.js';
import { bootstrapFor, cloudConfig, deployIssues, installFiles, trustFor } from '../src/generate.js';

const ENDPOINT = process.env.LOCALSTACK_URL;
const skip = ENDPOINT ? false : 'set LOCALSTACK_URL to run against LocalStack';
const REPO = 'demo-org/shop';
const ACCOUNT = '000000000000'; // LocalStack's default account.
const REGION = 'us-east-1';

const root = mkdtempSync(join(tmpdir(), 'cloudhop-localstack-'));
const work = join(root, 'repo');
const stubs = join(root, 'stubs');
const reports = [];
let server;
let reportUrl;

// Child processes run async so this process can receive run.sh's reports meanwhile.
function sh(command, { env = {}, cwd = work, timeout = 900_000 } = {}) {
  return new Promise((resolve) => {
    const child = spawn('bash', ['-c', command], { cwd, env: { ...process.env, ...env } });
    let output = '';
    child.stdout.on('data', (chunk) => { output += chunk; });
    child.stderr.on('data', (chunk) => { output += chunk; });
    const timer = setTimeout(() => child.kill('SIGKILL'), timeout);
    child.on('close', (status) => { clearTimeout(timer); resolve({ status, output }); });
  });
}
const ok = async (command, options) => {
  const result = await sh(command, options);
  assert.equal(result.status, 0, `${command}\n${result.output.slice(-4000)}`);
  return result.output.trim();
};

// Static test keys: what a GitHub Actions job has after configure-aws-credentials.
const awsEnv = (extra = {}) => ({
  AWS_ENDPOINT_URL: ENDPOINT,
  // OpenTofu's S3 backend exchanges web identity tokens at the STS endpoint, not the general one.
  AWS_ENDPOINT_URL_STS: ENDPOINT,
  // Virtual-hosted S3 names resolve to 127.0.0.1 under localhost.localstack.cloud.
  AWS_ENDPOINT_URL_S3: ENDPOINT.replace('://localhost', '://s3.localhost.localstack.cloud').replace('://127.0.0.1', '://s3.localhost.localstack.cloud'),
  AWS_ACCESS_KEY_ID: 'test',
  AWS_SECRET_ACCESS_KEY: 'test',
  AWS_REGION: REGION,
  AWS_DEFAULT_REGION: REGION,
  PATH: `${stubs}:${process.env.PATH}`,
  CLOUDHOP_RUN_ID: 'localstack-1',
  CLOUDHOP_REPORT_TOKEN: 'report-token',
  CLOUDHOP_ENV: 'API_KEY=not-a-real-secret',
  GITHUB_RUN_ID: '',
  GITHUB_SHA: '',
  ...extra,
});
const reportsSince = (count) => reports.slice(count);

before(async () => {
  if (skip) return;
  server = createServer((request, response) => {
    let body = '';
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', () => {
      if (request.url === '/api/report' && request.headers.authorization === 'Bearer report-token') reports.push(JSON.parse(body));
      response.writeHead(200, { 'Content-Type': 'application/json' }).end('{"ok":true}');
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  reportUrl = `http://127.0.0.1:${server.address().port}`;

  // Stand-in for the app's health check only: LocalStack servers do not run anything.
  mkdirSync(stubs);
  const realCurl = (await ok('command -v curl', { cwd: root })).trim();
  writeFileSync(join(stubs, 'curl'), `#!/usr/bin/env bash
# Only a final URL argument (the health check), not reports, whose last argument is a JSON body.
case "\${!#}" in http*sslip.io*) echo 200; exit 0 ;; esac
exec ${realCurl} "$@"
`);
  chmodSync(join(stubs, 'curl'), 0o755);

  // A small Node app with Postgres, with Cloudhop's files generated exactly as the install PR would.
  const app = [
    { path: 'package.json', content: JSON.stringify({ name: 'shop', scripts: { start: 'node server.js' }, dependencies: { express: '^4.19.0', pg: '^8.11.0' } }) },
    { path: 'server.js', content: "const app = require('express')(); app.get('/', (q, s) => s.send('ok')); app.listen(process.env.PORT || 3000);\n" },
  ];
  const map = analyzeFiles(app.map((file) => ({ path: file.path, text: file.content })));
  assert.deepEqual(deployIssues(map), []);
  const clouds = { aws: cloudConfig('aws', { accountId: ACCOUNT, region: REGION }, REPO) };
  const { files } = installFiles({ provider: 'gitlab', repo: REPO, map, clouds, cloudhopUrl: reportUrl });
  for (const file of [...app, ...files]) {
    mkdirSync(dirname(join(work, file.path)), { recursive: true });
    writeFileSync(join(work, file.path), file.content);
    if (file.executable) chmodSync(join(work, file.path), 0o755);
  }
  await ok('git init -q && git add -A && git -c user.email=t@example.com -c user.name=test commit -qm app');
});

after(async () => {
  if (skip) return;
  await sh('aws cloudformation delete-stack --stack-name cloudhop-localstack-test', { env: awsEnv(), cwd: root });
  server.close();
  rmSync(root, { recursive: true, force: true });
});

test('setup file: CloudFormation creates the trust, the deploy role and the state bucket', { skip }, async () => {
  const template = bootstrapFor('aws', REPO, { accountId: ACCOUNT, region: REGION }, trustFor('github', { repo: REPO }));
  writeFileSync(join(root, 'cloudhop-aws.yml'), template);
  await ok('aws cloudformation deploy --stack-name cloudhop-localstack-test --template-file cloudhop-aws.yml --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset', { env: awsEnv(), cwd: root });

  const roleArn = cloudConfig('aws', { accountId: ACCOUNT, region: REGION }, REPO).roleArn;
  const output = await ok("aws cloudformation describe-stacks --stack-name cloudhop-localstack-test --query 'Stacks[0].Outputs[?OutputKey==`RoleArn`].OutputValue' --output text", { env: awsEnv(), cwd: root });
  assert.equal(output, roleArn, 'the setup file creates the role run.sh expects');

  const roleName = roleArn.split('/').pop();
  const trust = JSON.parse(await ok(`aws iam get-role --role-name ${roleName} --query Role.AssumeRolePolicyDocument --output json`, { env: awsEnv(), cwd: root }));
  assert.equal(trust.Statement[0].Action, 'sts:AssumeRoleWithWebIdentity');
  assert.match(JSON.stringify(trust.Statement[0].Condition), /repo:demo-org\/shop:environment:cloudhop/);

  const policy = await ok(`aws iam get-role-policy --role-name ${roleName} --policy-name cloudhop --output json`, { env: awsEnv(), cwd: root });
  assert.match(policy, /iam:SimulatePrincipalPolicy/);
  assert.match(policy, /cloudhop-\*-vm/);
  await ok(`aws s3api head-bucket --bucket cloudhop-state-${ACCOUNT}`, { env: awsEnv(), cwd: root });
  // Not checked: LocalStack Community skips AWS::IAM::OIDCProvider in CloudFormation (a Pro resource type).
});

test('verify: signs in, opens the state, reports, and checks permissions', { skip }, async () => {
  const seen = reports.length;
  const { status, output } = await sh('bash .cloudhop/run.sh verify aws', { env: awsEnv() });
  assert.equal(status, 0, output.slice(-4000));
  const events = reportsSince(seen).map((report) => report.event);
  assert.ok(events.includes('verified'), output.slice(-2000));
  // Recorded rather than required: LocalStack Community may not implement the IAM policy simulator.
  const access = reportsSince(seen).find((report) => report.event === 'access');
  console.log(access ? `access check: ${JSON.stringify(access.checks)}` : 'access check: not reported (policy simulator unavailable here)');
});

test('plan: OpenTofu plans the server, bucket, role and firewall', { skip }, async () => {
  const output = await ok('bash .cloudhop/run.sh plan aws', { env: awsEnv() });
  for (const resource of ['aws_instance.vm', 'aws_s3_bucket.data', 'aws_iam_role.vm', 'aws_security_group.vm', 'aws_eip.vm']) assert.match(output, new RegExp(resource.replace('.', '\\.')));
});

test('deploy: creates everything, ships config and source through the bucket, and reports live', { skip }, async () => {
  const seen = reports.length;
  const output = await ok('bash .cloudhop/run.sh deploy aws', { env: awsEnv() });
  const deployed = reportsSince(seen).find((report) => report.event === 'deployed');
  assert.ok(deployed, output.slice(-2000));
  assert.match(deployed.url, /^http:\/\/[\d-]+\.sslip\.io$/);

  const name = JSON.parse(await ok('cat .cloudhop/config.json')).name;
  const state = await ok(`aws ec2 describe-instances --filters Name=tag:Name,Values=${name}-vm --query 'Reservations[].Instances[].State.Name' --output text`, { env: awsEnv() });
  assert.match(state, /running|pending/);
  // Secrets and source are deleted from the bucket after the server copies them.
  const left = await ok(`aws s3 ls s3://${name}-${ACCOUNT}-data/deploy/ || true`, { env: awsEnv() });
  assert.match(left, /compose\.yml/);
  assert.doesNotMatch(left, /\.env|source\.tgz/);
  const commands = await ok("aws ssm list-commands --query 'Commands[].Parameters.commands[0]' --output json", { env: awsEnv() });
  assert.match(commands, /docker compose build/);
  assert.match(commands, /docker compose up -d/);
  assert.doesNotMatch(commands, /not-a-real-secret/, 'app settings never appear in remote commands');
});

test('report: runs without the paid billing APIs and still reports', { skip }, async () => {
  const seen = reports.length;
  await ok('bash .cloudhop/run.sh report aws', { env: awsEnv() });
  assert.ok(reportsSince(seen).some((report) => report.event === 'report'));
});

test('ci.sh: a CI OIDC token becomes AWS access through the deploy role, with no keys', { skip }, async () => {
  const seen = reports.length;
  // Shaped like GitLab's ID token. LocalStack does not check the signature; real AWS checks it against the trusted provider.
  const part = (value) => Buffer.from(JSON.stringify(value)).toString('base64url');
  const token = `${part({ alg: 'RS256', typ: 'JWT' })}.${part({ iss: 'https://gitlab.com', aud: 'sts.amazonaws.com', sub: 'project_path:demo-org/shop:ref_type:branch:ref:main', iat: Math.floor(Date.now() / 1000), nbf: Math.floor(Date.now() / 1000), exp: Math.floor(Date.now() / 1000) + 3600 })}.${Buffer.from('signature').toString('base64url')}`;
  const env = awsEnv({ CLOUDHOP_OIDC_TOKEN: token, CLOUDHOP_ACTION: 'verify', CLOUDHOP_CLOUD: 'aws' });
  delete env.AWS_ACCESS_KEY_ID;
  delete env.AWS_SECRET_ACCESS_KEY;
  const { status, output } = await sh('env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY bash .cloudhop/ci.sh', { env });
  assert.equal(status, 0, output.slice(-4000));
  assert.ok(reportsSince(seen).some((report) => report.event === 'verified'));
  assert.match(output, /assumed-role\/cloudhop-[\w-]+-deploy\/cloudhop/, 'calls run as the deploy role');
});

test('destroy: removes the server and reports', { skip }, async () => {
  const seen = reports.length;
  await ok('bash .cloudhop/run.sh destroy aws', { env: awsEnv() });
  assert.ok(reportsSince(seen).some((report) => report.event === 'destroyed'));
  const name = JSON.parse(await ok('cat .cloudhop/config.json')).name;
  const state = await ok(`aws ec2 describe-instances --filters Name=tag:Name,Values=${name}-vm Name=instance-state-name,Values=running,pending --query 'Reservations[].Instances[].InstanceId' --output text`, { env: awsEnv() });
  assert.equal(state, '');
});
