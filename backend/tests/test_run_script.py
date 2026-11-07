"""Runs the generated run.sh and ci.sh for real, with stand-in tofu, aws, gcloud and curl commands that
record every call. Proves step order, secret handling and failure recovery without a cloud account."""
import json
import os
import shutil
import subprocess

import pytest

from cloudhop.analyze import analyze_files
from cloudhop.generate import cloud_config, install_files

BASH = shutil.which('bash')
JQ_DIR = os.environ.get('CLOUDHOP_TEST_JQ_DIR', '')
PATH = os.pathsep.join(filter(None, [JQ_DIR, os.environ.get('PATH', '')]))
HAS_JQ = bool(BASH) and subprocess.run([BASH, '-c', 'command -v jq'], env={**os.environ, 'PATH': PATH}, capture_output=True).returncode == 0
pytestmark = pytest.mark.skipif(not HAS_JQ, reason='bash and jq are needed')

LOG = 'echo "{name} $*" | tr \'\\r\\n\' \'  \' | tr -d \'\\\\\' >> "$STUB_DIR/log"; echo >> "$STUB_DIR/log"'
STUBS = {
    'tofu': '''
case "$*" in
  *"output -raw public_ip"*) case "$*" in *infra/aws*) echo 203.0.113.10 ;; *) echo 203.0.113.20 ;; esac ;;
  *"output -raw instance_id"*) echo i-0abc ;;
  *"output -raw data_bucket"*) echo cloudhop-data ;;
  *"output -raw db_password"*) echo test-db-password ;;
  *"output -raw zone"*) echo us-central1-a ;;
  *apply*) [ -z "${FAIL_APPLY:-}" ] || exit 1 ;;
esac
exit 0
''',
    'aws': '''
case "$*" in
  *"ssm send-command"*) echo "$*" > "$STUB_DIR/last-remote"; echo cmd-1 ;;
  *"get-command-invocation"*"--query Status"*)
    if [ -n "${REMOTE_FAIL:-}" ] && grep -q -- "$REMOTE_FAIL" "$STUB_DIR/last-remote"; then echo Failed; else echo Success; fi ;;
  *StandardOutputContent*) grep -q cpuLoadPercent "$STUB_DIR/last-remote" && echo '{"cpuLoadPercent":12.5,"memoryPercent":40,"diskPercent":20}' ;;
  *StandardErrorContent*) echo "remote command failed" ;;
  *"ce get-cost-and-usage"*) echo 4.20 ;;
  *"billing get-credits"*) echo '[{"amount":"100.00","ends":1}]' ;;
  *"--query Account"*) echo 123456789012 ;;
  *"simulate-principal-policy"*) echo '[{"action":"ec2:RunInstances","allowed":true},{"action":"iam:CreateUser","allowed":false}]' ;;
esac
exit 0
''',
    'gcloud': '''
case "$*" in
  *"compute ssh"*)
    if [ -n "${REMOTE_FAIL:-}" ] && printf '%s' "$*" | grep -q -- "$REMOTE_FAIL"; then exit 1; fi
    case "$*" in *cpuLoadPercent*) echo '{"cpuLoadPercent":5,"memoryPercent":30,"diskPercent":10}' ;; esac ;;
  *"auth print-access-token"*) echo gcp-access-token ;;
esac
exit 0
''',
    'curl': '''
if printf '%s ' "$@" | grep -q 'http_code'; then
  url="${!#}"
  if [ -n "${HEALTH_FAIL:-}" ] && [[ "$url" == *"$HEALTH_FAIL"* ]]; then echo 503; else echo 200; fi
  exit 0
fi
if printf '%s ' "$@" | grep -q testIamPermissions; then echo '{"permissions":["compute.instances.create","compute.instances.delete"]}'; exit 0; fi
while [ $# -gt 0 ]; do [ "$1" = -d ] && printf "%s" "$2" | tr -d "\\r\\n" >> "$STUB_DIR/reports" && echo >> "$STUB_DIR/reports"; shift; done
exit 0
''',
}


@pytest.fixture
def setup(tmp_path):
    def make(provider='github'):
        case = tmp_path / f'case-{len(list(tmp_path.iterdir()))}'
        stubs, repo = case / 'stubs', case / 'repo'
        stubs.mkdir(parents=True)
        repo.mkdir()
        for name, body in STUBS.items():
            (stubs / name).write_text('#!/usr/bin/env bash\n' + LOG.format(name=name) + body, newline='\n')
        (stubs / 'sleep').write_text('#!/usr/bin/env bash\nexit 0\n', newline='\n')
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        service_map = analyze_files([{'path': 'package.json', 'text': '{"scripts":{"start":"node ."},"dependencies":{"express":"4","pg":"8"}}'}])
        name = 'acme/shop'
        clouds = {
            'aws': cloud_config('aws', {'accountId': '123456789012', 'region': 'us-east-1'}, name),
            'gcp': cloud_config('gcp', {'projectId': 'acme-shop', 'projectNumber': '123456789', 'region': 'us-central1'}, name),
        }
        (repo / 'package.json').write_text('{}')
        for file in install_files(name, service_map, clouds, 'http://cloudhop.test', provider=provider)['files']:
            target = repo / file['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file['content'], newline='\n')
        git = ['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t']
        subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
        subprocess.run([*git, 'add', '-A'], cwd=repo, check=True)
        subprocess.run([*git, 'commit', '-q', '-m', 'init'], cwd=repo, check=True)
        return case
    return make


def run(case, script, args, **extra):
    result = subprocess.run([BASH, f'.cloudhop/{script}', *args], cwd=case / 'repo', capture_output=True, text=True, env={
        **os.environ,
        'PATH': os.pathsep.join([str(case / 'stubs'), PATH]),
        'STUB_DIR': str(case),
        'CLOUDHOP_REPORT_TOKEN': 'report-token',
        # Fixed run identity, so results match on a laptop and inside CI (which sets its own GITHUB_* values).
        'CLOUDHOP_RUN_ID': 'run-7', 'GITHUB_RUN_ID': '', 'GITHUB_SHA': '',
        'CLOUDHOP_ENV': 'API_KEY=super-secret-value',
        **extra,
    })

    def read(name):
        path = case / name
        return path.read_text() if path.exists() else ''
    log = [line for line in read('log').split('\n') if line]
    reports = [json.loads(line) for line in read('reports').split('\n') if line]
    return {'status': result.returncode, 'output': result.stdout + result.stderr, 'log': log, 'reports': reports}


def at(log, fragments, start=0):
    """Index of the first log line containing every fragment, searching from `start`."""
    for index in range(start, len(log)):
        if all(fragment in log[index] for fragment in fragments):
            return index
    raise AssertionError(f"expected a call with {' + '.join(fragments)}")


def test_deploy_provisions_ships_config_and_source_builds_starts_checks_and_reports(setup):
    result = run(setup(), 'run.sh', ['deploy', 'aws'])
    assert result['status'] == 0, result['output']
    log = result['log']
    apply = at(log, ['tofu', 'infra/aws', 'apply'])
    ready = at(log, ['send-command', 'test -f /opt/cloudhop/ready'], apply)
    env = at(log, ['aws s3 cp', 's3://cloudhop-data/deploy/.env'], ready)
    source = at(log, ['aws s3 cp', 'deploy/source.tgz'], env)
    build = at(log, ['send-command', 'docker compose build'], source)
    removed = at(log, ['aws s3 rm', 'deploy/.env'], build)
    up = at(log, ['send-command', 'docker compose up -d'], removed)
    at(log, ['curl', 'http_code', 'http://203-0-113-10.sslip.io'], up)
    assert result['reports'][-1] == {'event': 'deployed', 'cloud': 'aws', 'runId': 'run-7', 'url': 'http://203-0-113-10.sslip.io'}
    assert 'super-secret-value' not in '\n'.join(log), 'app secrets must never appear in remote commands or arguments'
    assert 'test-db-password' not in '\n'.join(log), 'database password must stay out of remote commands'


def test_switch_stops_the_old_app_copies_the_database_starts_the_new_one_and_reports_downtime(setup):
    result = run(setup(), 'run.sh', ['switch', 'gcp', 'aws'])
    assert result['status'] == 0, result['output']
    log = result['log']
    apply = at(log, ['tofu', 'infra/gcp', 'apply'])
    stop_old = at(log, ['aws ssm send-command', 'docker compose ps --services'], apply)
    dump = at(log, ['aws ssm send-command', 'pg_dump'], stop_old)
    restore = at(log, ['gcloud compute ssh', 'pg_restore'], dump)
    start_new = at(log, ['gcloud compute ssh', 'docker compose up -d'], restore)
    at(log, ['curl', 'http_code', 'http://203-0-113-20.sslip.io'], start_new)
    report = result['reports'][-1]
    assert (report['event'], report['cloud'], report['from']) == ('switched', 'gcp', 'aws')
    assert isinstance(report['downtimeSeconds'], int)


def test_switch_that_fails_its_health_check_puts_the_app_back_on_the_old_cloud(setup):
    result = run(setup(), 'run.sh', ['switch', 'gcp', 'aws'], HEALTH_FAIL='203-0-113-20')
    assert result['status'] != 0
    log = result['log']
    stop_old = at(log, ['aws ssm send-command', 'docker compose ps --services'])
    stop_new = at(log, ['gcloud compute ssh', 'docker compose stop'], stop_old)
    at(log, ['aws ssm send-command', 'docker compose up -d'], stop_new)
    assert [report['event'] for report in result['reports']] == ['switch_failed']
    assert result['reports'][0]['cloud'] == 'aws'


def test_switch_that_fails_while_copying_data_also_puts_the_app_back(setup):
    result = run(setup(), 'run.sh', ['switch', 'gcp', 'aws'], REMOTE_FAIL='pg_restore')
    assert result['status'] != 0
    assert at(result['log'], ['aws ssm send-command', 'docker compose up -d'], at(result['log'], ['pg_restore'])) > 0
    assert [report['event'] for report in result['reports']] == ['switch_failed']


def test_rollback_stops_the_current_cloud_and_starts_the_previous_one(setup):
    result = run(setup(), 'run.sh', ['rollback', 'aws', 'gcp'])
    assert result['status'] == 0, result['output']
    stop = at(result['log'], ['gcloud compute ssh', 'docker compose ps --services'])
    at(result['log'], ['aws ssm send-command', 'docker compose up -d'], stop)
    assert result['reports'][-1]['event'] == 'rolled_back'


def test_destroy_and_report_do_what_they_say(setup):
    destroyed = run(setup(), 'run.sh', ['destroy', 'gcp'])
    assert destroyed['status'] == 0, destroyed['output']
    at(destroyed['log'], ['tofu', 'infra/gcp', 'destroy'])
    assert destroyed['reports'][-1]['event'] == 'destroyed'
    reported = run(setup(), 'run.sh', ['report', 'aws'])
    assert reported['status'] == 0, reported['output']
    report = reported['reports'][-1]
    assert report['costMonthToDate'] == 4.2
    assert report['resources'] == {'cpuLoadPercent': 12.5, 'memoryPercent': 40, 'diskPercent': 20}
    assert report['credits'][0]['amount'] == '100.00'


def test_run_script_refuses_unknown_actions_clouds_and_unconnected_clouds_before_touching_anything(setup):
    case = setup()
    for args in [['delete-everything', 'aws'], ['deploy', 'azure'], ['deploy', '../../etc'], ['switch', 'gcp', 'aws;rm -rf /'], ['deploy', 'aws"; cat /etc/passwd; "']]:
        result = run(case, 'run.sh', args)
        assert result['status'] == 2, ' '.join(args)
        assert result['log'] == [], f"nothing may run for {' '.join(args)}"
    # A project connected only to AWS refuses GCP even though GCP is a known cloud.
    config_path = case / 'repo' / '.cloudhop' / 'config.json'
    config = json.loads(config_path.read_text())
    del config['clouds']['gcp']
    config_path.write_text(json.dumps(config))
    refused = run(case, 'run.sh', ['deploy', 'gcp'])
    assert refused['status'] == 2
    assert 'not connected' in refused['output']
    assert refused['log'] == []


def test_verify_reports_which_actions_the_deploy_identity_may_do_on_each_cloud(setup):
    aws = run(setup(), 'run.sh', ['verify', 'aws'])
    assert aws['status'] == 0, aws['output']
    at(aws['log'], ['aws iam simulate-principal-policy', 'role/cloudhop-', '-deploy', 'iam:CreateUser'])
    access = next(report for report in aws['reports'] if report['event'] == 'access')
    assert access['checks'] == [{'action': 'ec2:RunInstances', 'allowed': True}, {'action': 'iam:CreateUser', 'allowed': False}]

    gcp = run(setup(), 'run.sh', ['verify', 'gcp'])
    assert gcp['status'] == 0, gcp['output']
    at(gcp['log'], ['curl', 'cloudresourcemanager.googleapis.com/v3/projects/', ':testIamPermissions'])
    access = next(report for report in gcp['reports'] if report['event'] == 'access')
    assert access['cloud'] == 'gcp'
    assert [check['action'] for check in access['checks'] if check['allowed']] == ['compute.instances.create', 'compute.instances.delete']
    assert any(check['action'] == 'resourcemanager.projects.setIamPolicy' and not check['allowed'] for check in access['checks'])


def test_switch_refuses_the_same_cloud_as_source(setup):
    result = run(setup(), 'run.sh', ['switch', 'aws', 'aws'])
    assert result['status'] != 0
    assert not any('apply' in line for line in result['log']), 'nothing may be created'


def test_ci_sh_turns_a_non_github_ci_token_into_cloud_access_then_runs_the_action(setup):
    for provider in ['gitlab', 'bitbucket', 'azure']:
        result = run(setup(provider), 'ci.sh', [], CLOUDHOP_OIDC_TOKEN='oidc-token-from-ci', CLOUDHOP_ACTION='verify', CLOUDHOP_CLOUD='aws')
        assert result['status'] == 0, f"{provider}: {result['output']}"
        at(result['log'], ['gcloud iam workload-identity-pools create-cred-config', 'workloadIdentityPools/ch-', '--credential-source-file'])
        at(result['log'], ['gcloud auth login --cred-file'])
        at(result['log'], ['aws sts get-caller-identity'])
        assert any(report['event'] == 'verified' for report in result['reports']), provider
    missing = run(setup('gitlab'), 'ci.sh', [], CLOUDHOP_OIDC_TOKEN='', CLOUDHOP_ACTION='verify', CLOUDHOP_CLOUD='aws')
    assert missing['status'] != 0
    assert 'No OIDC token' in missing['output']

