"""Runs Cloudhop's real AWS path against LocalStack: the CloudFormation setup file, keyless sign-in through
OIDC web identity (ci.sh), OpenTofu with the S3 state backend, and run.sh verify, plan, deploy, report and
destroy. Every AWS call goes to LocalStack's AWS APIs.

LocalStack has no real servers: EC2 instances are records, and SSM commands are accepted but never run.
So the app itself is not built or started, and the health check is answered by a stand-in for requests
to the app's address. Everything else is real tooling.

Needs LOCALSTACK_URL (for example http://localhost:4566) plus tofu, aws, jq, git and bash on PATH.
Run: LOCALSTACK_URL=http://localhost:4566 python -m pytest tests/test_localstack.py
"""
import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cloudhop.analyze import analyze_files
from cloudhop.generate import bootstrap_for, cloud_config, deploy_issues, install_files, trust_for

ENDPOINT = os.environ.get('LOCALSTACK_URL')
pytestmark = pytest.mark.skipif(not ENDPOINT, reason='set LOCALSTACK_URL to run against LocalStack')
REPO = 'demo-org/shop'
ACCOUNT = '000000000000'  # LocalStack's default account.
REGION = 'us-east-1'
STACK = 'cloudhop-localstack-test'
reports = []


class Capture(BaseHTTPRequestHandler):
    """Stands in for Cloudhop's /api/report, recording what run.sh sends."""

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get('content-length', 0)))
        if self.path == '/api/report' and self.headers.get('authorization') == 'Bearer report-token':
            reports.append(json.loads(body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *_):
        pass


@pytest.fixture(scope='module')
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp('localstack')
    work, stubs = root / 'repo', root / 'stubs'
    server = ThreadingHTTPServer(('127.0.0.1', 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report_url = f'http://127.0.0.1:{server.server_address[1]}'

    # Stand-in for the app's health check only: LocalStack servers do not run anything. Only a final URL
    # argument is answered here, not reports, whose last argument is a JSON body.
    stubs.mkdir()
    (stubs / 'curl').write_text(f'#!/usr/bin/env bash\ncase "${{!#}}" in http*sslip.io*) echo 200; exit 0 ;; esac\nexec {shutil.which("curl")} "$@"\n', newline='\n')
    (stubs / 'curl').chmod(0o755)

    # A small Node app with Postgres, with Cloudhop's files generated exactly as the install PR would.
    app = [
        {'path': 'package.json', 'content': json.dumps({'name': 'shop', 'scripts': {'start': 'node server.js'}, 'dependencies': {'express': '^4.19.0', 'pg': '^8.11.0'}})},
        {'path': 'server.js', 'content': "const app = require('express')(); app.get('/', (q, s) => s.send('ok')); app.listen(process.env.PORT || 3000);\n"},
    ]
    service_map = analyze_files([{'path': file['path'], 'text': file['content']} for file in app])
    assert deploy_issues(service_map) == []
    clouds = {'aws': cloud_config('aws', {'accountId': ACCOUNT, 'region': REGION}, REPO)}
    for file in app + install_files(REPO, service_map, clouds, report_url, provider='gitlab')['files']:
        target = work / file['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file['content'], newline='\n')
        if file.get('executable'):
            target.chmod(0o755)
    git = ['git', '-c', 'user.email=t@example.com', '-c', 'user.name=test']
    subprocess.run(['git', 'init', '-q'], cwd=work, check=True)
    subprocess.run([*git, 'add', '-A'], cwd=work, check=True)
    subprocess.run([*git, 'commit', '-qm', 'app'], cwd=work, check=True)
    yield {'root': root, 'work': work, 'stubs': stubs, 'name': json.loads((work / '.cloudhop/config.json').read_text())['name']}
    sh(world_env(stubs), 'aws cloudformation delete-stack --stack-name ' + STACK, root)
    server.shutdown()


def world_env(stubs, **extra):
    """Static test keys: what a GitHub Actions job has after configure-aws-credentials."""
    return {
        **os.environ,
        'AWS_ENDPOINT_URL': ENDPOINT,
        # OpenTofu's S3 backend exchanges web identity tokens at the STS endpoint, not the general one.
        'AWS_ENDPOINT_URL_STS': ENDPOINT,
        # Virtual-hosted S3 names resolve to 127.0.0.1 under localhost.localstack.cloud.
        'AWS_ENDPOINT_URL_S3': re.sub(r'://(localhost|127\.0\.0\.1)', '://s3.localhost.localstack.cloud', ENDPOINT),
        'AWS_ACCESS_KEY_ID': 'test', 'AWS_SECRET_ACCESS_KEY': 'test', 'AWS_REGION': REGION, 'AWS_DEFAULT_REGION': REGION,
        'PATH': f"{stubs}{os.pathsep}{os.environ['PATH']}",
        'CLOUDHOP_RUN_ID': 'localstack-1', 'CLOUDHOP_REPORT_TOKEN': 'report-token', 'CLOUDHOP_ENV': 'API_KEY=not-a-real-secret',
        'GITHUB_RUN_ID': '', 'GITHUB_SHA': '',
        **extra,
    }


def sh(env, command, cwd):
    result = subprocess.run(['bash', '-c', command], cwd=cwd, env=env, capture_output=True, text=True, timeout=900)
    return result.returncode, result.stdout + result.stderr


def ok(env, command, cwd):
    status, output = sh(env, command, cwd)
    assert status == 0, f'{command}\n{output[-4000:]}'
    return output.strip()


def since(count):
    return reports[count:]


def test_setup_file_creates_the_trust_the_deploy_role_and_the_state_bucket(world):
    env, root = world_env(world['stubs']), world['root']
    (root / 'cloudhop-aws.yml').write_text(bootstrap_for('aws', REPO, {'accountId': ACCOUNT, 'region': REGION}, trust_for('github', {'repo': REPO})), newline='\n')
    ok(env, f'aws cloudformation deploy --stack-name {STACK} --template-file cloudhop-aws.yml --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset', root)
    role_arn = cloud_config('aws', {'accountId': ACCOUNT, 'region': REGION}, REPO)['roleArn']
    output = ok(env, f"aws cloudformation describe-stacks --stack-name {STACK} --query 'Stacks[0].Outputs[?OutputKey==`RoleArn`].OutputValue' --output text", root)
    assert output == role_arn, 'the setup file creates the role run.sh expects'
    role = role_arn.split('/')[-1]
    trust = json.loads(ok(env, f'aws iam get-role --role-name {role} --query Role.AssumeRolePolicyDocument --output json', root))
    assert trust['Statement'][0]['Action'] == 'sts:AssumeRoleWithWebIdentity'
    assert 'repo:demo-org/shop:environment:cloudhop' in json.dumps(trust['Statement'][0]['Condition'])
    policy = ok(env, f'aws iam get-role-policy --role-name {role} --policy-name cloudhop --output json', root)
    assert 'iam:SimulatePrincipalPolicy' in policy and 'cloudhop-*-vm' in policy
    ok(env, f'aws s3api head-bucket --bucket cloudhop-state-{ACCOUNT}', root)
    # Not checked: LocalStack Community skips AWS::IAM::OIDCProvider in CloudFormation (a Pro resource type).


def test_verify_signs_in_opens_the_state_reports_and_checks_permissions(world):
    count = len(reports)
    status, output = sh(world_env(world['stubs']), 'bash .cloudhop/run.sh verify aws', world['work'])
    assert status == 0, output[-4000:]
    assert 'verified' in [report['event'] for report in since(count)], output[-2000:]
    # Recorded rather than required: LocalStack Community does not implement the IAM policy simulator.
    access = next((report for report in since(count) if report['event'] == 'access'), None)
    print('access check:', json.dumps(access['checks']) if access else 'not reported (policy simulator unavailable here)')


def test_plan_lists_the_server_bucket_role_firewall_and_address(world):
    output = ok(world_env(world['stubs']), 'bash .cloudhop/run.sh plan aws', world['work'])
    for resource in ['aws_instance.vm', 'aws_s3_bucket.data', 'aws_iam_role.vm', 'aws_security_group.vm', 'aws_eip.vm']:
        assert resource in output


def test_deploy_creates_everything_ships_config_and_source_and_reports_live(world):
    env, work, name = world_env(world['stubs']), world['work'], world['name']
    count = len(reports)
    output = ok(env, 'bash .cloudhop/run.sh deploy aws', work)
    deployed = next((report for report in since(count) if report['event'] == 'deployed'), None)
    assert deployed, output[-2000:]
    assert re.fullmatch(r'http://[\d-]+\.sslip\.io', deployed['url'])
    state = ok(env, f"aws ec2 describe-instances --filters Name=tag:Name,Values={name}-vm --query 'Reservations[].Instances[].State.Name' --output text", work)
    assert re.search(r'running|pending', state)
    # Secrets and source are deleted from the bucket after the server copies them.
    left = ok(env, f'aws s3 ls s3://{name}-{ACCOUNT}-data/deploy/ || true', work)
    assert 'compose.yml' in left and '.env' not in left and 'source.tgz' not in left
    commands = ok(env, "aws ssm list-commands --query 'Commands[].Parameters.commands[0]' --output json", work)
    assert 'docker compose build' in commands and 'docker compose up -d' in commands
    assert 'not-a-real-secret' not in commands, 'app settings never appear in remote commands'


def test_report_runs_without_the_paid_billing_apis_and_still_reports(world):
    count = len(reports)
    ok(world_env(world['stubs']), 'bash .cloudhop/run.sh report aws', world['work'])
    assert any(report['event'] == 'report' for report in since(count))


def test_ci_sh_turns_a_ci_oidc_token_into_aws_access_through_the_deploy_role_with_no_keys(world):
    # Shaped like GitLab's ID token. LocalStack does not check the signature; real AWS checks it against the trusted provider.
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    now = int(time.time())
    token = '.'.join([part({'alg': 'RS256', 'typ': 'JWT'}), part({'iss': 'https://gitlab.com', 'aud': 'sts.amazonaws.com', 'sub': 'project_path:demo-org/shop:ref_type:branch:ref:main',
                                                                  'iat': now, 'nbf': now, 'exp': now + 3600}), base64.urlsafe_b64encode(b'signature').rstrip(b'=').decode()])
    env = world_env(world['stubs'], CLOUDHOP_OIDC_TOKEN=token, CLOUDHOP_ACTION='verify', CLOUDHOP_CLOUD='aws')
    del env['AWS_ACCESS_KEY_ID'], env['AWS_SECRET_ACCESS_KEY']
    count = len(reports)
    status, output = sh(env, 'bash .cloudhop/ci.sh', world['work'])
    assert status == 0, output[-4000:]
    assert any(report['event'] == 'verified' for report in since(count))
    assert re.search(r'assumed-role/cloudhop-[\w-]+-deploy/cloudhop', output), 'calls run as the deploy role'


def test_destroy_removes_the_server_and_reports(world):
    env, work, name = world_env(world['stubs']), world['work'], world['name']
    count = len(reports)
    ok(env, 'bash .cloudhop/run.sh destroy aws', work)
    assert any(report['event'] == 'destroyed' for report in since(count))
    running = ok(env, f"aws ec2 describe-instances --filters Name=tag:Name,Values={name}-vm Name=instance-state-name,Values=running,pending --query 'Reservations[].Instances[].InstanceId' --output text", work)
    assert running == ''
