"""DigitalOcean, Hetzner and Hostinger: run.sh for real against stand-in ssh, ssh-keygen and curl commands that answer
like the cloud APIs, plus the setup text and account checks."""
import json
import shutil
import subprocess

import pytest
from test_run_script import BASH, HAS_JQ, LOG, STUBS, at, run

from cloudhop.analyze import analyze_files
from cloudhop.generate import bootstrap_for, cloud_account, cloud_config, install_files, validate_cloud_input

needs_jq = pytest.mark.skipif(not HAS_JQ, reason='bash and jq are needed')
TOKEN = 'dop_v1_secret-token-value'

SSH_STUBS = {
    'ssh-keygen': '''
case "$*" in
  *-y*) echo "ssh-ed25519 AAAAC3Nza-test cloudhop" ;;
  *md5*) echo "256 MD5:aa:bb:cc:dd test (ED25519)" ;;
esac
exit 0
''',
    'ssh': '''
command="${!#}"
printf '%s\\0' "$command" >> "$STUB_DIR/remote"
case "$command" in *"cat > "*) cat > /dev/null ;; esac
if [ -n "${REMOTE_FAIL:-}" ] && printf '%s' "$command" | grep -q -- "$REMOTE_FAIL"; then exit 1; fi
case "$command" in
  *db_password*) echo ssh-db-password ;;
  *"sudo -n cat"*) echo dump-bytes ;;
  *cpuLoadPercent*) echo '{"cpuLoadPercent":7,"memoryPercent":20,"diskPercent":15}' ;;
esac
exit 0
''',
    # Cloud APIs answer from small state files; anything else behaves like the shared curl stand-in.
    'curl': '''
method=GET; url="${!#}"; body=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in -X) method="${args[$((i + 1))]}" ;; -d) body="${args[$((i + 1))]}" ;; esac
done
case "$url" in
  https://api.digitalocean.com/*|https://api.hetzner.cloud/*)
    cat > /dev/null
    printf '%s %s %s\\n' "$method" "$url" "$body" >> "$STUB_DIR/api"
    case "$method $url" in
      "GET "*/account/keys/*) exit 22 ;;
      "POST "*/droplets) touch "$STUB_DIR/server" ;;
      "POST "*/servers) touch "$STUB_DIR/server" ;;
      "GET "*/droplets?tag_name=*)
        lookups=$(( $(cat "$STUB_DIR/lookups" 2>/dev/null || echo 0) + 1 )); echo "$lookups" > "$STUB_DIR/lookups"
        if [ -n "${LOOKUP_FAIL_FROM:-}" ] && [ "$lookups" -ge "$LOOKUP_FAIL_FROM" ]; then exit 22; fi
        if [ -f "$STUB_DIR/server" ]; then echo '{"droplets":[{"id":1,"status":"active","networks":{"v4":[{"type":"private","ip_address":"10.0.0.2"},{"type":"public","ip_address":"198.51.100.7"}]}}]}'; else echo '{"droplets":[]}'; fi ;;
      "GET "*/servers?label_selector=*)
        if [ -f "$STUB_DIR/server" ]; then echo '{"servers":[{"id":77,"public_net":{"ipv4":{"ip":"198.51.100.8"}}}]}'; else echo '{"servers":[]}'; fi ;;
      "GET "*/firewalls*) echo '{"firewalls":[]}' ;;
      "GET "*/ssh_keys*) echo '{"ssh_keys":[]}' ;;
      "POST "*/ssh_keys) echo '{"ssh_key":{"id":42}}' ;;
      "GET "*/server_types*) echo '{"server_types":[
        {"name":"cx-old","architecture":"x86","deprecation":{"announced":"2025-01-01"},"prices":[{"location":"fsn1","price_monthly":{"gross":"1.00"}}]},
        {"name":"cax-arm","architecture":"arm","deprecation":null,"prices":[{"location":"fsn1","price_monthly":{"gross":"2.00"}}]},
        {"name":"cx-big","architecture":"x86","deprecation":null,"prices":[{"location":"fsn1","price_monthly":{"gross":"9.00"}}]},
        {"name":"cx-cheap","architecture":"x86","deprecation":null,"prices":[{"location":"fsn1","price_monthly":{"gross":"4.15"}}]}]}' ;;
      *) echo '{}' ;;
    esac
    exit 0 ;;
esac
if printf '%s ' "$@" | grep -q 'http_code'; then echo 200; exit 0; fi
[ -z "$body" ] || { printf '%s' "$body" | tr -d '\\r\\n' >> "$STUB_DIR/reports"; echo >> "$STUB_DIR/reports"; }
exit 0
''',
}


@pytest.fixture
def ssh_setup(tmp_path):
    def make(clouds):
        case = tmp_path / f'case-{len(list(tmp_path.iterdir()))}'
        stubs, repo = case / 'stubs', case / 'repo'
        stubs.mkdir(parents=True)
        repo.mkdir()
        for name, body in {**STUBS, **SSH_STUBS}.items():
            (stubs / name).write_text('#!/usr/bin/env bash\n' + LOG.format(name=name) + body, newline='\n')
        (stubs / 'sleep').write_text('#!/usr/bin/env bash\nexit 0\n', newline='\n')
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        service_map = analyze_files([{'path': 'package.json', 'text': '{"scripts":{"start":"node ."},"dependencies":{"express":"4","pg":"8"}}'}])
        config = {cloud: cloud_config(cloud, data, 'acme/shop') for cloud, data in clouds.items()}
        (repo / 'package.json').write_text('{}')
        for file in install_files('acme/shop', service_map, config, 'http://cloudhop.test')['files']:
            target = repo / file['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file['content'], newline='\n')
        git = ['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t']
        subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
        subprocess.run([*git, 'add', '-A'], cwd=repo, check=True)
        subprocess.run([*git, 'commit', '-q', '-m', 'init'], cwd=repo, check=True)
        return case
    return make


def api_calls(case):
    path = case / 'api'
    return path.read_text().splitlines() if path.exists() else []


KEY = '-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----'


@needs_jq
def test_digitalocean_deploy_creates_a_droplet_and_firewall_then_ships_over_ssh(ssh_setup):
    case = ssh_setup({'digitalocean': {'region': 'nyc3'}})
    result = run(case, 'run.sh', ['deploy', 'digitalocean', '', 'req1'], DIGITALOCEAN_TOKEN=TOKEN, CLOUDHOP_SSH_KEY=KEY)
    assert result['status'] == 0, result['output']
    calls = api_calls(case)
    created = next(json.loads(line.split(' ', 2)[2]) for line in calls if line.startswith('POST') and line.split(' ')[1].endswith('/droplets'))
    assert created == {'name': created['name'], 'region': 'nyc3', 'size': 's-1vcpu-1gb', 'image': 'ubuntu-24-04-x64', 'ssh_keys': ['aa:bb:cc:dd'], 'tags': [created['tags'][0]]}
    assert any(line.startswith('POST https://api.digitalocean.com/v2/account/keys') for line in calls), 'adds the key when DigitalOcean lacks it'
    firewall = next(json.loads(line.split(' ', 2)[2]) for line in calls if line.startswith('POST') and '/firewalls' in line)
    assert {rule['ports'] for rule in firewall['inbound_rules']} == {'22', '80', '443', '8081-8090'} and firewall['tags'] == created['tags']
    log = result['log']
    setup = at(log, ['ssh', 'root@198.51.100.7', 'docker-compose-linux-x86_64', '/opt/cloudhop/owner', 'CLOUDHOP_OWNER=cloudhop-'])
    env = at(log, ['ssh', 'cat > /opt/cloudhop/.env'], setup)
    build = at(log, ['ssh', 'docker compose build'], env)
    up = at(log, ['ssh', 'docker compose up -d'], build)
    at(log, ['curl', 'http_code', 'https://198-51-100-7.sslip.io'], up)
    assert result['reports'][-1] == {'event': 'deployed', 'cloud': 'digitalocean', 'runId': 'run-7', 'requestId': 'req1', 'url': 'https://198-51-100-7.sslip.io'}
    everything = '\n'.join(log + calls)
    assert TOKEN not in everything, 'the API token goes to curl on stdin, never in arguments'
    assert 'super-secret-value' not in everything and 'ssh-db-password' not in everything


@needs_jq
def test_hetzner_deploy_picks_the_cheapest_current_x86_type_and_accepts_a_base64_key(ssh_setup):
    import base64
    case = ssh_setup({'hetzner': {'region': 'fsn1'}})
    result = run(case, 'run.sh', ['deploy', 'hetzner', '', ''], HCLOUD_TOKEN='hcloud-token', CLOUDHOP_SSH_KEY=base64.b64encode(KEY.encode()).decode())
    assert result['status'] == 0, result['output']
    server = next(json.loads(line.split(' ', 2)[2]) for line in api_calls(case) if line.startswith('POST') and line.split(' ')[1].endswith('/servers'))
    assert (server['server_type'], server['location'], server['image'], server['ssh_keys']) == ('cx-cheap', 'fsn1', 'ubuntu-24.04', [42])
    firewall = next(json.loads(line.split(' ', 2)[2]) for line in api_calls(case) if line.startswith('POST') and '/firewalls' in line)
    assert firewall['apply_to'][0]['label_selector']['selector'] == f"cloudhop={server['labels']['cloudhop']}"
    assert result['reports'][-1]['url'] == 'https://198-51-100-8.sslip.io'


@needs_jq
def test_switch_from_aws_to_a_hostinger_server_copies_the_database_through_ssh(ssh_setup):
    case = ssh_setup({'aws': {'accountId': '123456789012', 'region': 'us-east-1'}, 'hostinger': {'host': '203.0.113.50', 'user': 'deploy'}})
    result = run(case, 'run.sh', ['switch', 'hostinger', 'aws', ''], CLOUDHOP_SSH_KEY=KEY)
    assert result['status'] == 0, result['output']
    log = result['log']
    assert not api_calls(case), 'Hostinger uses no API'
    stop = at(log, ['send-command', 'docker compose ps --services'])
    dump = at(log, ['send-command', 'pg_dump'], stop)
    fetched = at(log, ['aws s3 cp', 's3://cloudhop-data/deploy/transfer'], dump)
    sent = at(log, ['ssh', 'deploy@203.0.113.50', 'sudo -n bash -c', 'cat > /tmp/db.dump'], fetched)
    restore = at(log, ['ssh', 'pg_restore'], sent)
    at(log, ['ssh', 'docker compose up -d'], restore)
    assert result['reports'][-1]['event'] == 'switched' and result['reports'][-1]['url'] == 'https://203-0-113-50.sslip.io'


@needs_jq
def test_destroy_removes_cloud_servers_but_only_stops_the_app_on_hostinger(ssh_setup):
    case = ssh_setup({'hetzner': {'region': 'fsn1'}, 'hostinger': {'host': '203.0.113.50'}})
    (case / 'server').touch()
    result = run(case, 'run.sh', ['destroy', 'hetzner', '', ''], HCLOUD_TOKEN='hcloud-token', CLOUDHOP_SSH_KEY=KEY)
    assert result['status'] == 0, result['output']
    assert any(line.startswith('DELETE https://api.hetzner.cloud/v1/servers/77') for line in api_calls(case))
    result = run(case, 'run.sh', ['destroy', 'hostinger', '', ''], CLOUDHOP_SSH_KEY=KEY)
    assert result['status'] == 0, result['output']
    down = result['log'][at(result['log'], ['ssh', 'root@203.0.113.50', 'docker compose down'])]
    assert 'sudo' not in down, 'root runs commands directly: a Debian server may have no sudo'
    assert '/opt/cloudhop/owner' in down, 'only the project that owns the server may stop its app'
    assert not any(line.startswith('DELETE') and 'hostinger' in line for line in api_calls(case))
    assert result['reports'][-1]['event'] == 'destroyed'


@needs_jq
def test_missing_secrets_stop_the_run_with_what_to_add(ssh_setup):
    case = ssh_setup({'digitalocean': {'region': 'nyc3'}})
    result = run(case, 'run.sh', ['verify', 'digitalocean', '', ''], DIGITALOCEAN_TOKEN=TOKEN)
    assert result['status'] != 0 and 'CLOUDHOP_SSH_KEY secret is missing' in result['output']
    result = run(case, 'run.sh', ['verify', 'digitalocean', '', ''], CLOUDHOP_SSH_KEY=KEY, DIGITALOCEAN_TOKEN='$(DIGITALOCEAN_TOKEN)')
    assert result['status'] != 0 and 'DIGITALOCEAN_TOKEN secret is missing' in result['output'], 'an undefined Azure secret counts as missing'


def test_setup_text_names_the_secrets_for_each_code_host_and_never_a_key():
    text = bootstrap_for('digitalocean', 'acme/shop', {'region': 'nyc3'}, {}, 'gitlab')
    assert 'DIGITALOCEAN_TOKEN' in text and 'CLOUDHOP_SSH_KEY' in text and 'Settings, CI/CD, Variables' in text
    assert 'droplet create, read, delete' in text
    hostinger = bootstrap_for('hostinger', 'acme/shop', {'host': '203.0.113.50', 'user': 'deploy'}, {}, 'github')
    assert '203.0.113.50' in hostinger and 'never buys, cancels or deletes' in hostinger and 'HCLOUD_TOKEN' not in hostinger


def test_cloud_inputs_are_checked_before_they_reach_scripts():
    assert validate_cloud_input('digitalocean', {'region': 'nyc3'})
    assert not validate_cloud_input('digitalocean', {'region': 'nyc3; rm -rf /'})
    assert validate_cloud_input('hetzner', {'region': 'ash'}) and validate_cloud_input('hetzner', {'region': 'fsn1', 'serverType': 'cx22'})
    assert not validate_cloud_input('hetzner', {'region': 'fsn1', 'serverType': 'cx22 && curl'})
    assert validate_cloud_input('hostinger', {'host': '203.0.113.50'}) and validate_cloud_input('hostinger', {'host': '203.0.113.50', 'user': 'deploy'})
    assert not validate_cloud_input('hostinger', {'host': 'vps.example.com'}), 'the https address is built from the IPv4 address'
    assert not validate_cloud_input('hostinger', {'host': '203.0.113.256'})
    assert not validate_cloud_input('hostinger', {'host': '-oProxyCommand=x'}) and not validate_cloud_input('hostinger', {'host': '1.2.3.4', 'user': 'root;id'})
    assert cloud_account('hostinger', {'host': '203.0.113.50'}) == ({'host': '203.0.113.50', 'user': 'root'}, 'Hostinger 203.0.113.50')



def remote_scripts_parse(case):
    """Every command sent over SSH is a valid shell script, after unwrapping its `bash -c '...'`. Returns the scripts."""
    commands = [item for item in (case / 'remote').read_text().split('\0') if item]
    unwrap = 'eval "set -- $1"; bash -n -c "${!#}"'
    broken = [command for command in commands if subprocess.run([BASH, '-c', unwrap, '_', command], capture_output=True).returncode != 0]
    assert not broken, broken[:2]
    return [subprocess.run([BASH, '-c', 'eval "set -- $1"; printf %s "${!#}"', '_', command], capture_output=True, text=True).stdout for command in commands]


@needs_jq
def test_every_remote_script_parses_including_the_owner_check(ssh_setup):
    case = ssh_setup({'hetzner': {'region': 'fsn1'}, 'hostinger': {'host': '203.0.113.50'}})
    assert run(case, 'run.sh', ['deploy', 'hetzner', '', ''], HCLOUD_TOKEN='hcloud-token', CLOUDHOP_SSH_KEY=KEY)['status'] == 0
    assert run(case, 'run.sh', ['destroy', 'hostinger', '', ''], CLOUDHOP_SSH_KEY=KEY)['status'] == 0
    # The login shell reads the command first and may be dash: no bash-only $'...' quoting, and plain sh parses it.
    sent = [item for item in (case / 'remote').read_text().split('\0') if item]
    assert sent and not [command for command in sent if "$'" in command]
    if shutil.which('sh'):
        assert not [command for command in sent if subprocess.run(['sh', '-n', '-c', command], capture_output=True).returncode != 0]
    commands = remote_scripts_parse(case)
    assert any('/opt/cloudhop/owner' in command and 'CLOUDHOP_OWNER=' in command for command in commands)
    assert any('/opt/cloudhop/owner' in command and 'docker compose down' in command for command in commands)


@needs_jq
def test_a_failed_server_lookup_never_creates_a_second_server(ssh_setup):
    # The first lookup (finding the address) works, the one just before creating fails: nothing may be created.
    case = ssh_setup({'digitalocean': {'region': 'nyc3'}})
    result = run(case, 'run.sh', ['deploy', 'digitalocean', '', ''], DIGITALOCEAN_TOKEN=TOKEN, CLOUDHOP_SSH_KEY=KEY, LOOKUP_FAIL_FROM='2')
    assert result['status'] != 0 and 'Could not look up the DigitalOcean server' in result['output'], result['output'][-1500:]
    assert not [call for call in api_calls(case) if call.startswith('POST') and call.split()[1].endswith('/droplets')]
