import base64
import json
import re
import shutil
import subprocess

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from starlette.requests import Request

from cloudhop.analyze import analyze_files, select_manifests
from cloudhop.auth import allowed_login, check_origin, cookie, login_methods, read_cookie
from cloudhop.errors import AuthError, RepoError
from cloudhop.generate import (app_services, bootstrap_for, cloud_access, cloud_config, compose_for, deploy_issues, dockerfile_for, install_files, project_slug,
                               resource_name, trust_for, validate_cloud_input)
from cloudhop.github import app_jwt
from cloudhop.intent import proposal_for
from cloudhop.providers import parse_repo_for, provider_of
from cloudhop.store import decrypt, decrypt_with_env, encrypt, next_state, public_account, public_project, summarize_checks, uptime_summary
from cloudhop.templates import TEMPLATE_DIR

MAP = analyze_files([
    {'path': 'api/requirements.txt', 'text': 'fastapi\npsycopg2\nredis\n'},
    {'path': 'web/package.json', 'text': '{"devDependencies":{"vite":"5"}}'},
])
AWS = cloud_config('aws', {'accountId': '123456789012', 'region': 'us-east-1'}, 'o/r')
GCP = cloud_config('gcp', {'projectId': 'my-project', 'projectNumber': '123456', 'region': 'us-central1'}, 'o/r')
BOTH = {'clouds': {'aws': AWS, 'gcp': GCP}, 'active': 'aws', 'previous': None, 'portability': 'auto'}


def test_backend_gets_the_main_port_and_generated_compose_wires_data():
    assert [(service['name'], service['publicPort']) for service in app_services(MAP)] == [('app-api', 80), ('app-web', 8081)]
    compose = compose_for(MAP)
    for expected in ['DATABASE_URL: postgres://app:${DB_PASSWORD}@db:5432/app', 'REDIS_URL', 'pg_isready', '"80:8000"', '"8081:80"', 'build: ./src/api', 'build: ./src/web']:
        assert expected in compose
    assert 'IMAGE_PREFIX' not in compose, 'images are built on the VM, not pulled from a registry'


def test_install_writes_ci_infra_and_only_missing_dockerfiles():
    docker = analyze_files([{'path': 'Dockerfile', 'text': 'FROM x\nEXPOSE 9000\n'}])
    files = install_files('o/Some_Repo', docker, {'aws': AWS}, 'https://x.dev')['files']
    assert not any(file['path'] == 'Dockerfile' for file in files)
    assert any(file['path'] == '.github/workflows/cloudhop.yml' for file in files)
    config = json.loads(next(file['content'] for file in files if file['path'] == '.cloudhop/config.json'))
    assert re.fullmatch(r'cloudhop-some-repo-[a-z0-9]{1,4}', config['name'])
    with_build = [file['path'] for file in install_files('o/r', MAP, {'gcp': GCP}, 'https://x.dev')['files']]
    assert 'api/Dockerfile' in with_build and 'web/Dockerfile' in with_build
    assert len(resource_name('o/' + 'a' * 40)) <= 30


def test_same_repo_name_under_different_owners_gets_different_cloud_names():
    assert project_slug('alice/app') != project_slug('bob/app')
    assert project_slug('alice/app') == project_slug('alice/app')
    assert AWS['roleArn'].endswith(f"role/cloudhop-{project_slug('o/r')}-deploy")
    assert GCP['workloadIdentityProvider'].endswith(f"workloadIdentityPools/ch-{project_slug('o/r')}/providers/ci")


def test_cloud_names_match_the_original_javascript():
    # Changing these would orphan resources created by earlier setup files.
    assert project_slug('demo-org/shop') == 'shop-1x6k'  # Seen in the earlier LocalStack run.
    assert project_slug('o/Some_Repo').startswith('some-repo-')


def test_each_code_host_gets_its_own_ci_file_and_never_overwrites_an_existing_one():
    def ci(provider, existing=()):
        return install_files('g/p', MAP, {'aws': AWS}, 'https://x.dev', provider=provider, existing_paths=set(existing))
    assert any(file['path'] == '.gitlab-ci.yml' for file in ci('gitlab')['files'])
    assert any(file['path'] == '.cloudhop/ci.sh' for file in ci('gitlab')['files'])
    taken = ci('gitlab', ['.gitlab-ci.yml'])
    assert not any(file['path'] == '.gitlab-ci.yml' for file in taken['files']) and any(file['path'] == '.cloudhop/gitlab-ci.yml' for file in taken['files'])
    assert 'include' in taken['notes'][0]
    assert any(file['path'] == 'bitbucket-pipelines.yml' for file in ci('bitbucket')['files'])
    assert len(ci('bitbucket', ['bitbucket-pipelines.yml'])['notes']) == 1
    assert any(file['path'] == '.cloudhop/azure-pipelines.yml' for file in ci('azure')['files'])
    assert not any(file['path'] == '.cloudhop/ci.sh' for file in ci('github')['files'])


def test_clouds_trust_only_the_right_ci_identity_for_each_code_host():
    assert trust_for('github', {'repo': 'o/r'})['subject'] == 'repo:o/r:environment:cloudhop'
    gitlab = trust_for('gitlab', {'repo': 'g/sub/p', 'defaultBranch': 'main'})
    assert gitlab['subject'] == 'project_path:g/sub/p:ref_type:branch:ref:main'
    assert gitlab['awsAudience'] == 'https://gitlab.com'
    uuid = '{1de489be-ce6a-42a0-a8c8-eadbf1174ac7}'
    bitbucket = trust_for('bitbucket', {'workspace': 'team', 'workspaceUuid': '{922e118c-f34f-4226-a897-0e0afbec553d}', 'repositoryUuid': uuid})
    assert bitbucket['subject'] == f'{uuid}*'
    assert bitbucket['awsAudience'] == 'ari:cloud:bitbucket::workspace/922e118c-f34f-4226-a897-0e0afbec553d'
    assert bitbucket['issuer'].startswith('api.bitbucket.org/2.0/workspaces/team/')
    azure = trust_for('azure', {'organization': 'contoso', 'project': 'Web Shop', 'organizationId': '00000000-0000-0000-0000-000000000001'})
    assert azure['subject'] == 'sc://contoso/Web Shop/cloudhop'
    for provider, info in [('gitlab', {'repo': "g/p'; rm -rf /", 'defaultBranch': 'main'}), ('azure', {'organization': 'a', 'project': '$(evil)', 'organizationId': 'x'}),
                           ('gitlab', {'repo': 'g/p', 'defaultBranch': 'main\n'})]:
        with pytest.raises(ValueError):
            trust_for(provider, info)


def test_repo_input_is_parsed_per_code_host_including_pasted_urls():
    assert parse_repo_for('github', 'https://github.com/o/r.git') == 'o/r'
    assert parse_repo_for('gitlab', 'https://gitlab.com/group/sub/project') == 'group/sub/project'
    assert parse_repo_for('bitbucket', 'https://bitbucket.org/team/repo/') == 'team/repo'
    assert parse_repo_for('azure', 'https://dev.azure.com/contoso/Web%20Shop/_git/api') == 'contoso/Web Shop/api'
    for provider, bad in [('github', 'a/b/c'), ('bitbucket', 'a'), ('azure', 'org/repo'), ('gitlab', '../x/y'), ('gitlab', "g/p';x")]:
        with pytest.raises(RepoError):
            parse_repo_for(provider, bad)
    assert provider_of('gitlab_token') == 'gitlab'
    assert provider_of('github_app') == 'github'
    with pytest.raises(RepoError):
        provider_of('aws')


def test_manifest_selection_skips_vendored_folders_and_keeps_the_shallowest_files():
    chosen = select_manifests([
        {'path': 'node_modules/x/package.json', 'size': 10},
        {'path': 'apps/web/package.json', 'size': 10},
        {'path': 'package.json', 'size': 10},
        {'path': 'README.md', 'size': 10},
        {'path': 'huge/package.json', 'size': 10_000_000},
    ])
    assert [entry['path'] for entry in chosen['selected']] == ['package.json', 'apps/web/package.json']
    assert chosen['total'] == 2


def test_generated_dockerfiles_match_the_framework():
    assert 'uvicorn' in dockerfile_for(MAP['services'][0])
    assert '/app/dist' in dockerfile_for(MAP['services'][1])


def test_deploy_refuses_what_it_cannot_run():
    assert deploy_issues(MAP) == []
    assert 'mongodb' in deploy_issues(analyze_files([{'path': 'package.json', 'text': '{"scripts":{"start":"x"},"dependencies":{"mongoose":"8"}}'}]))[0]
    assert len(deploy_issues(analyze_files([]))) == 1


def test_cloud_input_rejects_anything_that_could_break_generated_files():
    assert validate_cloud_input('aws', {'accountId': '123456789012', 'region': 'us-east-1'})
    assert validate_cloud_input('gcp', {'projectId': 'my-project', 'projectNumber': '123456', 'region': 'us-central1'})
    assert not validate_cloud_input('aws', {'accountId': '123', 'region': 'us-east-1'})
    assert not validate_cloud_input('aws', {'accountId': '123456789012', 'region': 'x; rm -rf /'})
    assert not validate_cloud_input('aws', {'accountId': '123456789012\n', 'region': 'us-east-1'})
    assert not validate_cloud_input('aws', {'accountId': 123456789012, 'region': 'us-east-1'})
    assert not validate_cloud_input('gcp', {'projectId': '$(evil)', 'projectNumber': '1', 'region': 'us-central1'})
    assert not validate_cloud_input('azure', {})


def test_setup_files_are_filled_in_for_every_code_host_with_no_placeholders_left():
    infos = {
        'github': {'repo': 'o/r'},
        'gitlab': {'repo': 'g/p', 'defaultBranch': 'main'},
        'bitbucket': {'workspace': 'team', 'workspaceUuid': '{922e118c-f34f-4226-a897-0e0afbec553d}', 'repositoryUuid': '{1de489be-ce6a-42a0-a8c8-eadbf1174ac7}'},
        'azure': {'organization': 'contoso', 'project': 'shop', 'organizationId': '00000000-0000-0000-0000-000000000001'},
    }
    for provider, info in infos.items():
        trust = trust_for(provider, info)
        shell = bootstrap_for('gcp', 'o/r', {'projectId': 'my-project', 'region': 'us-central1'}, trust)
        stack = bootstrap_for('aws', 'o/r', {}, trust)
        assert '__' not in shell and '__' not in stack, provider
        assert f"'{trust['issuer']}:sub': '{trust['subject']}'" in stack, provider
        assert trust['gcpCondition'] in shell, provider


def test_switch_needs_approval_data_and_warns_about_downtime():
    proposal = proposal_for('Please switch this app to Google', BOTH)
    assert (proposal['action'], proposal['cloud'], proposal['source']) == ('switch', 'gcp', 'aws')
    assert len(proposal['warnings']) >= 2


def test_switch_is_refused_when_it_cannot_work():
    assert 'already' in proposal_for('move to aws', BOTH)['reply']
    assert 'Deploy first' in proposal_for('switch to gcp', {**BOTH, 'active': None})['reply']
    assert 'not connected' in proposal_for('switch to gcp', {**BOTH, 'clouds': {'aws': AWS}})['reply']
    assert 'refused' in proposal_for('switch to gcp', {**BOTH, 'portability': 'redesign'})['reply']
    assert 'Which cloud' in proposal_for('switch', BOTH)['reply']


def test_destroy_demands_typed_confirmation_and_unknown_text_gets_help():
    assert proposal_for('destroy aws', BOTH)['confirmText'] == 'aws'
    assert 'which cloud' in proposal_for('destroy', BOTH)['reply']
    assert 'Try' in proposal_for('hello there', BOTH)['reply']
    assert 'no previous' in proposal_for('roll back', BOTH)['reply']
    assert proposal_for('roll back', {**BOTH, 'previous': 'gcp'})['cloud'] == 'gcp'
    assert 'switch to gcp' in proposal_for('deploy to gcp', BOTH)['reply']


def test_reports_move_active_cloud_only_when_traffic_moved():
    project = {'active': 'aws', 'previous': None, 'url': 'http://a'}
    assert next_state(project, {'event': 'switched', 'cloud': 'gcp', 'from': 'aws', 'url': 'http://b'}) == {'active': 'gcp', 'previous': 'aws', 'url': 'http://b'}
    assert next_state(project, {'event': 'switch_failed', 'cloud': 'aws'}) == {'active': 'aws', 'previous': None, 'url': 'http://a'}
    assert next_state({'active': 'gcp', 'previous': 'aws', 'url': 'http://b'}, {'event': 'destroyed', 'cloud': 'aws'}) == {'active': 'gcp', 'previous': None, 'url': 'http://b'}
    assert next_state({'active': 'gcp', 'previous': 'aws', 'url': 'http://b'}, {'event': 'destroyed', 'cloud': 'gcp'}) == {'active': None, 'previous': 'aws', 'url': None}


def test_secrets_round_trip_encrypted_bound_to_their_row_and_stay_out_of_browser_shapes():
    stored = encrypt('ghp_example', 'key-one', 'row-1')
    assert 'ghp_example' not in stored
    assert decrypt(stored, 'key-one', 'row-1') == 'ghp_example'
    for key, row in [('key-two', 'row-1'), ('key-one', 'row-2')]:
        with pytest.raises(InvalidTag):
            decrypt(stored, key, row)
    account = {'id': 'row-1', 'kind': 'github_token', 'label': 'me', 'data': '{}', 'secret_enc': stored, 'created_at': 1}
    assert stored not in json.dumps(public_account(account))
    project = {'id': '1', 'user_id': 'u', 'repo': 'o/r', 'account_id': 'row-1', 'report_hash': 'h', 'clouds': '{}', 'created_at': 1,
               'provider': 'github', 'active': None, 'previous': None, 'url': None, 'pr_url': None, 'portability': None}
    assert '"h"' not in json.dumps(public_project(project))


def test_secret_format_matches_the_original_backend():
    # Same format as WebCrypto AES-GCM: base64(iv).base64(ciphertext + tag), key = SHA-256 of the secret.
    stored = encrypt('token', 'k', 'row')
    iv, data = (base64.b64decode(part) for part in stored.split('.'))
    assert len(iv) == 12 and len(data) == len('token') + 16


def test_secrets_survive_an_encryption_key_rotation():
    old = encrypt('token-a', 'old-key', 'row-1')
    fresh = encrypt('token-b', 'new-key', 'row-2')
    env = {'ENCRYPTION_KEY': 'new-key', 'ENCRYPTION_KEY_PREVIOUS': 'old-key'}
    assert decrypt_with_env(env, old, 'row-1') == 'token-a'
    assert decrypt_with_env(env, fresh, 'row-2') == 'token-b'
    with pytest.raises(InvalidTag):
        decrypt_with_env({'ENCRYPTION_KEY': 'new-key'}, old, 'row-1')
    with pytest.raises(InvalidTag):
        decrypt_with_env(env, old, 'row-2')


def test_metrics_buckets_checks_and_finds_outages():
    def at(minutes):
        return minutes * 60_000
    checks = [
        {'at': at(5), 'ok': 1, 'status': 200, 'ms': 100},
        {'at': at(10), 'ok': 0, 'status': 503, 'ms': None},
        {'at': at(15), 'ok': 0, 'status': None, 'ms': None},
        {'at': at(20), 'ok': 1, 'status': 200, 'ms': 300},
        {'at': at(50), 'ok': 0, 'status': 500, 'ms': None},
    ]
    summary = summarize_checks(checks, 0, at(60), 2)
    assert summary['buckets'][0]['checks'] == 4
    assert summary['buckets'][0]['uptimePercent'] == 50
    assert summary['buckets'][0]['averageMs'] == 200
    assert summary['buckets'][1]['uptimePercent'] == 0
    assert summary['p95Ms'] == 300
    assert len(summary['incidents']) == 2
    assert summary['incidents'][0]['ongoing']
    assert summary['incidents'][1] == {'start': at(10), 'end': at(20), 'failedChecks': 2}
    assert summary['statuses'][0]['status'] == '200'
    assert summarize_checks([], 0, at(60), 4)['buckets'][0]['averageMs'] is None


def _request(method, origin=None):
    headers = [(b'origin', origin.encode())] if origin else []
    return Request({'type': 'http', 'method': method, 'headers': headers, 'path': '/api/projects', 'query_string': b''})


def test_writes_from_other_sites_are_refused():
    env = {'PUBLIC_URL': 'http://localhost:5173'}
    check_origin(_request('GET'), env)
    check_origin(_request('POST', 'http://localhost:5173'), env)
    for origin in ['https://evil.example', None]:
        with pytest.raises(AuthError):
            check_origin(_request('POST', origin), env)


def test_sign_up_is_open_unless_an_allowlist_is_set_and_app_install_needs_github_sign_in():
    github = {'GITHUB_CLIENT_ID': 'id', 'GITHUB_CLIENT_SECRET': 'secret'}
    assert allowed_login({}, 'anyone')
    assert allowed_login({'ALLOWED_GITHUB_USERS': 'Alice, bob'}, 'alice')
    assert not allowed_login({'ALLOWED_GITHUB_USERS': 'Alice, bob'}, 'mallory')
    assert login_methods(github) == {'github': True, 'adminToken': False, 'githubApp': False, 'openSignup': True}
    assert login_methods({**github, 'ALLOWED_GITHUB_USERS': 'a'})['openSignup'] is False
    assert login_methods({'GITHUB_APP_ID': '1', 'GITHUB_APP_SLUG': 's', 'GITHUB_APP_PRIVATE_KEY': 'k'})['githubApp'] is False
    assert login_methods({**github, 'GITHUB_APP_ID': '1', 'GITHUB_APP_SLUG': 's', 'GITHUB_APP_PRIVATE_KEY': 'k'})['githubApp'] is True


def test_session_cookie_is_http_only_and_secure_on_https():
    assert re.search(r'HttpOnly; SameSite=Lax; Max-Age=60; Secure$', cookie({'PUBLIC_URL': 'https://app.example'}, 'a', 'b', 60))
    assert 'Secure' not in cookie({'PUBLIC_URL': 'http://localhost:5173'}, 'a', 'b', 60)
    request = Request({'type': 'http', 'method': 'GET', 'headers': [(b'cookie', b'x=1; cloudhop_session=abc=; y=2')], 'path': '/', 'query_string': b''})
    assert read_cookie(request, 'cloudhop_session') == 'abc='


def test_github_app_jwt_is_signed_rs256_and_verifies_with_the_public_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    token = app_jwt('12345', pem, 1_000_000_000_000)
    header, payload, signature = token.split('.')

    def decode(part):
        return base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))
    assert json.loads(decode(header)) == {'alg': 'RS256', 'typ': 'JWT'}
    assert json.loads(decode(payload)) == {'iat': 999_999_940, 'exp': 1_000_000_540, 'iss': '12345'}
    key.public_key().verify(decode(signature), f'{header}.{payload}'.encode(), padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(RepoError):
        app_jwt('1', '-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----')
    # GitHub downloads PKCS#1, and a one-line .env entry holds it with backslash-n escapes: both work as is.
    pkcs1 = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()
    one_line = app_jwt('12345', pkcs1.strip().replace('\n', '\\n'), 1_000_000_000_000)
    assert one_line.split('.')[:2] == token.split('.')[:2]
    key.public_key().verify(decode(one_line.split('.')[2]), '.'.join(one_line.split('.')[:2]).encode(), padding.PKCS1v15(), hashes.SHA256())


def test_uptime_summary_handles_empty_and_mixed_checks():
    assert uptime_summary([])['uptimePercent'] is None
    summary = uptime_summary([{'ok': 1, 'ms': 100}, {'ok': 0, 'ms': None}, {'ok': 1, 'ms': 300}, {'ok': 1, 'ms': 200}])
    assert (summary['uptimePercent'], summary['averageMs']) == (75, 200)


def test_generated_shell_scripts_are_valid_bash():
    bash = shutil.which('bash')
    if not bash:
        pytest.skip('bash is not installed')
    for name in ['run.sh', 'ci.sh', 'bootstrap-gcp.sh']:
        subprocess.run([bash, '-n', (TEMPLATE_DIR / name).as_posix()], check=True)


def test_cloud_access_grants_repos_and_only_that_accounts_latest_check():
    account = {'kind': 'aws', 'data': {'accountId': '111122223333'}}
    projects = [
        {'id': 'p1', 'repo': 'me/app', 'clouds': {'aws': {'accountId': '111122223333'}}},
        {'id': 'p2', 'repo': 'me/other', 'clouds': {'aws': {'accountId': '999999999999'}}},
    ]
    reports = [
        {'projectId': 'p1', 'at': 1, 'event': 'access', 'cloud': 'aws', 'checks': [{'action': 'ec2:RunInstances', 'allowed': True}]},
        {'projectId': 'p1', 'at': 2, 'event': 'access', 'cloud': 'aws', 'checks': [{'action': 'iam:CreateUser', 'allowed': False}]},
        {'projectId': 'p2', 'at': 3, 'event': 'access', 'cloud': 'aws', 'checks': [{'action': 'iam:CreateUser', 'allowed': True}]},
        {'projectId': 'p1', 'at': 4, 'event': 'access', 'cloud': 'gcp', 'checks': []},
    ]
    result = cloud_access(account, projects, reports)
    assert result['repos'] == ['me/app']
    assert result['grants']
    assert result['live'] == {'at': 2, 'repo': 'me/app', 'checks': [{'action': 'iam:CreateUser', 'allowed': False}]}
    assert cloud_access(account, [], reports)['live'] is None


def test_the_aws_role_may_simulate_only_its_own_policy():
    text = bootstrap_for('aws', 'me/app', {'accountId': '111122223333', 'region': 'us-east-1'}, trust_for('github', {'repo': 'me/app'}))
    assert re.search(r'Action: iam:SimulatePrincipalPolicy\n\s+Resource: !Sub arn:aws:iam::\$\{AWS::AccountId\}:role/cloudhop-[\w-]+-deploy\n', text)
