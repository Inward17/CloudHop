// Runs the generated run.sh and ci.sh for real, with stand-in tofu, aws, gcloud and curl commands
// that record every call. Proves step order, secret handling and failure recovery without a cloud account.
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { delimiter, dirname, join } from 'node:path';
import { after, test } from 'node:test';
import { analyzeFiles } from '../src/analyze.js';
import { cloudConfig, installFiles } from '../src/generate.js';

const jqDir = process.env.CLOUDHOP_TEST_JQ_DIR;
const hasJq = spawnSync('bash', ['-c', 'command -v jq'], { env: { ...process.env, PATH: [jqDir, process.env.PATH].filter(Boolean).join(delimiter) } }).status === 0;
const skip = hasJq ? false : 'jq is not installed';

const STUBS = {
  tofu: `#!/usr/bin/env bash
echo "tofu $*" | tr '\r\n' '  ' | tr -d '\\' >> "$STUB_DIR/log"; echo >> "$STUB_DIR/log"
case "$*" in
  *"output -raw public_ip"*) case "$*" in *infra/aws*) echo 203.0.113.10 ;; *) echo 203.0.113.20 ;; esac ;;
  *"output -raw instance_id"*) echo i-0abc ;;
  *"output -raw data_bucket"*) echo cloudhop-data ;;
  *"output -raw db_password"*) echo test-db-password ;;
  *"output -raw zone"*) echo us-central1-a ;;
  *apply*) [ -z "\${FAIL_APPLY:-}" ] || exit 1 ;;
esac
exit 0
`,
  aws: `#!/usr/bin/env bash
echo "aws $*" | tr '\r\n' '  ' | tr -d '\\' >> "$STUB_DIR/log"; echo >> "$STUB_DIR/log"
case "$*" in
  *"ssm send-command"*) echo "$*" > "$STUB_DIR/last-remote"; echo cmd-1 ;;
  *"get-command-invocation"*"--query Status"*)
    if [ -n "\${REMOTE_FAIL:-}" ] && grep -q -- "$REMOTE_FAIL" "$STUB_DIR/last-remote"; then echo Failed; else echo Success; fi ;;
  *StandardOutputContent*) grep -q cpuLoadPercent "$STUB_DIR/last-remote" && echo '{"cpuLoadPercent":12.5,"memoryPercent":40,"diskPercent":20}' ;;
  *StandardErrorContent*) echo "remote command failed" ;;
  *"ce get-cost-and-usage"*) echo 4.20 ;;
  *"billing get-credits"*) echo '[{"amount":"100.00","ends":1}]' ;;
  *"--query Account"*) echo 123456789012 ;;
  *"simulate-principal-policy"*) echo '[{"action":"ec2:RunInstances","allowed":true},{"action":"iam:CreateUser","allowed":false}]' ;;
esac
exit 0
`,
  gcloud: `#!/usr/bin/env bash
echo "gcloud $*" | tr '\r\n' '  ' | tr -d '\\' >> "$STUB_DIR/log"; echo >> "$STUB_DIR/log"
case "$*" in
  *"compute ssh"*)
    if [ -n "\${REMOTE_FAIL:-}" ] && printf '%s' "$*" | grep -q -- "$REMOTE_FAIL"; then exit 1; fi
    case "$*" in *cpuLoadPercent*) echo '{"cpuLoadPercent":5,"memoryPercent":30,"diskPercent":10}' ;; esac ;;
  *"auth print-access-token"*) echo gcp-access-token ;;
esac
exit 0
`,
  curl: `#!/usr/bin/env bash
echo "curl $*" | tr '\r\n' '  ' | tr -d '\\' >> "$STUB_DIR/log"; echo >> "$STUB_DIR/log"
if printf '%s ' "$@" | grep -q 'http_code'; then
  url="\${!#}"
  if [ -n "\${HEALTH_FAIL:-}" ] && [[ "$url" == *"$HEALTH_FAIL"* ]]; then echo 503; else echo 200; fi
  exit 0
fi
if printf '%s ' "$@" | grep -q testIamPermissions; then echo '{"permissions":["compute.instances.create","compute.instances.delete"]}'; exit 0; fi
while [ $# -gt 0 ]; do [ "$1" = -d ] && printf "%s" "$2" | tr -d "\r\n" >> "$STUB_DIR/reports" && echo >> "$STUB_DIR/reports"; shift; done
exit 0
`,
  sleep: '#!/usr/bin/env bash\nexit 0\n',
};

const root = mkdtempSync(join(tmpdir(), 'cloudhop-run-'));
after(() => rmSync(root, { recursive: true, force: true }));

function setup(provider = 'github') {
  const dir = mkdtempSync(join(root, 'case-'));
  const stubs = join(dir, 'stubs');
  const repo = join(dir, 'repo');
  mkdirSync(stubs);
  mkdirSync(repo);
  for (const [name, body] of Object.entries(STUBS)) {
    writeFileSync(join(stubs, name), body);
    chmodSync(join(stubs, name), 0o755);
  }
  const map = analyzeFiles([
    { path: 'package.json', text: '{"scripts":{"start":"node ."},"dependencies":{"express":"4","pg":"8"}}' },
  ]);
  const repoName = 'acme/shop';
  const clouds = {
    aws: cloudConfig('aws', { accountId: '123456789012', region: 'us-east-1' }, repoName),
    gcp: cloudConfig('gcp', { projectId: 'acme-shop', projectNumber: '123456789', region: 'us-central1' }, repoName),
  };
  const { files } = installFiles({ provider, repo: repoName, map, clouds, cloudhopUrl: 'http://cloudhop.test' });
  writeFileSync(join(repo, 'package.json'), '{}');
  for (const file of files) {
    mkdirSync(dirname(join(repo, file.path)), { recursive: true });
    writeFileSync(join(repo, file.path), file.content);
  }
  const git = (...args) => execFileSync('git', args, { cwd: repo, stdio: 'ignore' });
  git('init', '-q');
  git('-c', 'user.email=t@example.com', '-c', 'user.name=t', 'add', '-A');
  git('-c', 'user.email=t@example.com', '-c', 'user.name=t', 'commit', '-q', '-m', 'init');
  return { dir, stubs, repo };
}

function run(ctx, script, args, env = {}) {
  const result = spawnSync('bash', [`.cloudhop/${script}`, ...args], {
    cwd: ctx.repo,
    encoding: 'utf8',
    env: {
      ...process.env,
      PATH: [ctx.stubs, jqDir, process.env.PATH].filter(Boolean).join(delimiter),
      STUB_DIR: ctx.dir,
      CLOUDHOP_REPORT_TOKEN: 'report-token',
      // Fixed run identity, so results match on a laptop and inside CI (which sets its own GITHUB_* values).
      CLOUDHOP_RUN_ID: 'run-7',
      GITHUB_RUN_ID: '',
      GITHUB_SHA: '',
      CLOUDHOP_ENV: 'API_KEY=super-secret-value',
      ...env,
    },
  });
  const read = (name) => (existsSync(join(ctx.dir, name)) ? readFileSync(join(ctx.dir, name), 'utf8') : '');
  const log = read('log').split('\n').filter(Boolean);
  const reports = read('reports').split('\n').filter(Boolean).map((line) => JSON.parse(line));
  return { status: result.status, output: result.stdout + result.stderr, log, reports };
}

// Index of the first log line containing every given fragment, searching from `from`.
function at(log, fragments, from = 0) {
  const index = log.findIndex((line, i) => i >= from && fragments.every((fragment) => line.includes(fragment)));
  assert.notEqual(index, -1, `expected a call with ${fragments.join(' + ')}`);
  return index;
}

test('deploy provisions, ships config and source through the bucket, builds, starts, checks and reports', { skip }, () => {
  const ctx = setup();
  const result = run(ctx, 'run.sh', ['deploy', 'aws']);
  assert.equal(result.status, 0, result.output);
  const { log } = result;
  const apply = at(log, ['tofu', 'infra/aws', 'apply']);
  const ready = at(log, ['send-command', 'test -f /opt/cloudhop/ready'], apply);
  const env = at(log, ['aws s3 cp', 's3://cloudhop-data/deploy/.env'], ready);
  const source = at(log, ['aws s3 cp', 'deploy/source.tgz'], env);
  const build = at(log, ['send-command', 'docker compose build'], source);
  const removed = at(log, ['aws s3 rm', 'deploy/.env'], build);
  const up = at(log, ['send-command', 'docker compose up -d'], removed);
  at(log, ['curl', 'http_code', 'http://203-0-113-10.sslip.io'], up);
  assert.deepEqual(result.reports.at(-1), { event: 'deployed', cloud: 'aws', runId: 'run-7', url: 'http://203-0-113-10.sslip.io' });
  assert.ok(!log.join('\n').includes('super-secret-value'), 'app secrets must never appear in remote commands or arguments');
  assert.ok(!log.join('\n').includes('test-db-password'), 'database password must stay out of remote commands');
});

test('switch stops the old app, copies the database, starts the new one and reports downtime', { skip }, () => {
  const ctx = setup();
  const result = run(ctx, 'run.sh', ['switch', 'gcp', 'aws']);
  assert.equal(result.status, 0, result.output);
  const { log } = result;
  const apply = at(log, ['tofu', 'infra/gcp', 'apply']);
  const stopOld = at(log, ['aws ssm send-command', 'docker compose ps --services'], apply);
  const dump = at(log, ['aws ssm send-command', 'pg_dump'], stopOld);
  const restore = at(log, ['gcloud compute ssh', 'pg_restore'], dump);
  const startNew = at(log, ['gcloud compute ssh', 'docker compose up -d'], restore);
  at(log, ['curl', 'http_code', 'http://203-0-113-20.sslip.io'], startNew);
  const report = result.reports.at(-1);
  assert.equal(report.event, 'switched');
  assert.equal(report.cloud, 'gcp');
  assert.equal(report.from, 'aws');
  assert.equal(typeof report.downtimeSeconds, 'number');
});

test('switch that fails its health check puts the app back on the old cloud', { skip }, () => {
  const ctx = setup();
  const result = run(ctx, 'run.sh', ['switch', 'gcp', 'aws'], { HEALTH_FAIL: '203-0-113-20' });
  assert.notEqual(result.status, 0);
  const { log } = result;
  const stopOld = at(log, ['aws ssm send-command', 'docker compose ps --services']);
  const stopNew = at(log, ['gcloud compute ssh', 'docker compose stop'], stopOld);
  at(log, ['aws ssm send-command', 'docker compose up -d'], stopNew);
  assert.deepEqual(result.reports.map((report) => report.event), ['switch_failed']);
  assert.equal(result.reports[0].cloud, 'aws');
});

test('switch that fails while copying data also puts the app back', { skip }, () => {
  const ctx = setup();
  const result = run(ctx, 'run.sh', ['switch', 'gcp', 'aws'], { REMOTE_FAIL: 'pg_restore' });
  assert.notEqual(result.status, 0);
  const restart = at(result.log, ['aws ssm send-command', 'docker compose up -d'], at(result.log, ['pg_restore']));
  assert.ok(restart > 0);
  assert.deepEqual(result.reports.map((report) => report.event), ['switch_failed']);
});

test('rollback stops the current cloud and starts the previous one', { skip }, () => {
  const ctx = setup();
  const result = run(ctx, 'run.sh', ['rollback', 'aws', 'gcp']);
  assert.equal(result.status, 0, result.output);
  const stop = at(result.log, ['gcloud compute ssh', 'docker compose ps --services']);
  at(result.log, ['aws ssm send-command', 'docker compose up -d'], stop);
  assert.equal(result.reports.at(-1).event, 'rolled_back');
});

test('destroy and report do what they say', { skip }, () => {
  const ctx = setup();
  const destroyed = run(ctx, 'run.sh', ['destroy', 'gcp']);
  assert.equal(destroyed.status, 0, destroyed.output);
  at(destroyed.log, ['tofu', 'infra/gcp', 'destroy']);
  assert.equal(destroyed.reports.at(-1).event, 'destroyed');
  const reported = run(setup(), 'run.sh', ['report', 'aws']);
  assert.equal(reported.status, 0, reported.output);
  const report = reported.reports.at(-1);
  assert.equal(report.costMonthToDate, 4.2);
  assert.deepEqual(report.resources, { cpuLoadPercent: 12.5, memoryPercent: 40, diskPercent: 20 });
  assert.equal(report.credits[0].amount, '100.00');
});

test('deploy script refuses unknown actions, clouds and unconnected clouds before touching anything', { skip }, () => {
  const ctx = setup();
  for (const args of [['delete-everything', 'aws'], ['deploy', 'azure'], ['deploy', '../../etc'], ['switch', 'gcp', 'aws;rm -rf /'], ['deploy', 'aws"; cat /etc/passwd; "']]) {
    const result = run(ctx, 'run.sh', args);
    assert.equal(result.status, 2, args.join(' '));
    assert.equal(result.log.length, 0, `nothing may run for ${args.join(' ')}`);
  }
  // A project connected only to AWS refuses GCP even though GCP is a known cloud.
  const config = JSON.parse(readFileSync(join(ctx.repo, '.cloudhop/config.json'), 'utf8'));
  delete config.clouds.gcp;
  writeFileSync(join(ctx.repo, '.cloudhop/config.json'), JSON.stringify(config));
  const refused = run(ctx, 'run.sh', ['deploy', 'gcp']);
  assert.equal(refused.status, 2);
  assert.match(refused.output, /not connected/);
  assert.equal(refused.log.length, 0);
});

test('verify reports which actions the deploy identity may do, on each cloud', { skip }, () => {
  const aws = run(setup(), 'run.sh', ['verify', 'aws']);
  assert.equal(aws.status, 0, aws.output);
  at(aws.log, ['aws iam simulate-principal-policy', 'role/cloudhop-', '-deploy', 'iam:CreateUser']);
  const awsAccess = aws.reports.find((report) => report.event === 'access');
  assert.deepEqual(awsAccess.checks, [{ action: 'ec2:RunInstances', allowed: true }, { action: 'iam:CreateUser', allowed: false }]);

  const gcp = run(setup(), 'run.sh', ['verify', 'gcp']);
  assert.equal(gcp.status, 0, gcp.output);
  at(gcp.log, ['curl', 'cloudresourcemanager.googleapis.com/v3/projects/', ':testIamPermissions']);
  const gcpAccess = gcp.reports.find((report) => report.event === 'access');
  assert.equal(gcpAccess.cloud, 'gcp');
  assert.deepEqual(gcpAccess.checks.filter((check) => check.allowed).map((check) => check.action), ['compute.instances.create', 'compute.instances.delete']);
  assert.ok(gcpAccess.checks.some((check) => check.action === 'resourcemanager.projects.setIamPolicy' && !check.allowed));
});

test('switch refuses the same cloud as source', { skip }, () => {
  const result = run(setup(), 'run.sh', ['switch', 'aws', 'aws']);
  assert.notEqual(result.status, 0);
  assert.ok(!result.log.some((line) => line.includes('apply')), 'nothing may be created');
});

test('ci.sh turns a non-GitHub CI token into cloud access, then runs the action', { skip }, () => {
  for (const provider of ['gitlab', 'bitbucket', 'azure']) {
    const ctx = setup(provider);
    const result = run(ctx, 'ci.sh', [], { CLOUDHOP_OIDC_TOKEN: 'oidc-token-from-ci', CLOUDHOP_ACTION: 'verify', CLOUDHOP_CLOUD: 'aws' });
    assert.equal(result.status, 0, `${provider}: ${result.output}`);
    at(result.log, ['gcloud iam workload-identity-pools create-cred-config', 'workloadIdentityPools/ch-', '--credential-source-file']);
    at(result.log, ['gcloud auth login --cred-file']);
    at(result.log, ['aws sts get-caller-identity']);
    assert.ok(result.reports.some((report) => report.event === 'verified'), provider);
  }
  const missing = run(setup('gitlab'), 'ci.sh', [], { CLOUDHOP_OIDC_TOKEN: '', CLOUDHOP_ACTION: 'verify', CLOUDHOP_CLOUD: 'aws' });
  assert.notEqual(missing.status, 0);
  assert.match(missing.output, /No OIDC token/);
});
