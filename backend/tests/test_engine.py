import json

import pytest

from cloudhop.analyze import analyze_files
from cloudhop.errors import RepoError
from cloudhop.github import parse_repo
from cloudhop.plans import rank_plans, simulate_credits
from cloudhop.portability import portability_report
from cloudhop.timeutil import parse_date_ms

TODAY = parse_date_ms('2026-01-01')

NODE_APP = [
    {'path': 'package.json', 'text': json.dumps({'scripts': {'start': 'node .'}, 'dependencies': {'express': '4', 'pg': '8', 'bullmq': '5', '@aws-sdk/client-dynamodb': '3'}}, indent=2)},
    {'path': 'Dockerfile', 'text': 'FROM node:22\nEXPOSE 3000\n'},
    {'path': '.env.example', 'text': 'DATABASE_URL=postgres://u:secretvalue@h/db\nAPI_KEY=abc\n'},
]


def test_finds_service_datastores_worker_and_lock_in_with_evidence():
    service_map = analyze_files(NODE_APP)
    assert service_map['services'][0]['framework'] == 'Express'
    assert service_map['services'][0]['port'] == 3000
    assert sorted(store['kind'] for store in service_map['datastores']) == ['postgres', 'redis']
    assert len(service_map['workers']) == 1
    assert service_map['bindings'][0]['service'] == 'DynamoDB'
    assert service_map['bindings'][0]['evidence']['line'] > 1


def test_keeps_env_var_names_and_drops_their_values():
    service_map = analyze_files(NODE_APP)
    assert service_map['envVars'] == ['DATABASE_URL', 'API_KEY']
    assert 'secretvalue' not in json.dumps(service_map)


def test_parses_python_deps_ignoring_comments_and_version_pins():
    service_map = analyze_files([{'path': 'api/requirements.txt', 'text': '# flask\nFastAPI[all]==0.1\npsycopg2-binary>=2\nredis\n'}])
    assert service_map['services'][0]['framework'] == 'FastAPI'
    assert sorted(store['kind'] for store in service_map['datastores']) == ['postgres', 'redis']


def test_skips_workspace_roots_and_reads_compose_images():
    service_map = analyze_files([
        {'path': 'package.json', 'text': '{"workspaces":["packages/*"]}'},
        {'path': 'compose.yml', 'text': 'services:\n  db:\n    image: "postgres:16"\n  q:\n    image: bitnami/rabbitmq:3\n'},
    ])
    assert service_map['services'] == []
    assert [store['kind'] for store in service_map['datastores']] == ['postgres', 'rabbitmq']
    assert service_map['datastores'][0]['evidence'][0]['line'] == 3


def test_pyproject_without_a_framework_is_not_a_service():
    assert analyze_files([{'path': 'pyproject.toml', 'text': '[tool.uv.workspace]\nmembers = ["backend"]\n'}])['services'] == []


def test_odd_dependency_names_and_broken_manifests_do_not_crash():
    assert analyze_files([{'path': 'requirements.txt', 'text': 'constructor\n__proto__\n'}])['datastores'] == []
    weird = analyze_files([{'path': 'package.json', 'text': '{"scripts":"x","dependencies":["pg"]}'}, {'path': 'a/package.json', 'text': 'not json'}])
    assert weird['services'] == []
    assert any('Could not parse' in item for item in weird['unknowns'])


def test_portability_verdict_follows_the_worst_item():
    report = portability_report(analyze_files(NODE_APP))
    assert report['worst'] == 'redesign'
    assert report['items'][0]['name'] == 'DynamoDB'
    assert portability_report(analyze_files([NODE_APP[1]]))['worst'] == 'auto'


def test_credits_burn_until_exhausted():
    result = simulate_credits(100, [{'amount': 300, 'expiresOn': '2027-01-01'}], TODAY)
    assert (result['coveredMonths'], result['wasted'], result['outOfPocket12']) == (3, 0, 900)


def test_unspent_credits_past_expiry_count_as_waste():
    result = simulate_credits(50, [{'amount': 5000, 'expiresOn': '2027-01-01'}], TODAY)
    assert (result['coveredMonths'], result['outOfPocket12'], result['wasted']) == (12, 0, 4400)


def test_expired_credits_are_ignored_and_no_credits_means_full_price():
    result = simulate_credits(10, [{'amount': 100, 'expiresOn': '2025-01-01'}], TODAY)
    assert (result['coveredMonths'], result['outOfPocket12'], result['total']) == (0, 120, 0)


def test_partial_month_of_credit_charges_only_the_remainder():
    result = simulate_credits(100, [{'amount': 150, 'expiresOn': '2027-01-01'}], TODAY)
    assert (result['coveredMonths'], result['outOfPocket12']) == (1, 1050)


def test_no_credits_ranks_cheapest_first_and_large_credits_buy_reliability():
    service_map = analyze_files(NODE_APP)
    assert rank_plans(service_map, today=TODAY)['plans'][0]['tier'] == 'cheapest'
    funded = rank_plans(service_map, today=TODAY, credits=[{'cloud': 'gcp', 'amount': 50000, 'expiresOn': '2028-01-01'}])['plans'][0]
    assert funded['id'] == 'gcp-prod'


def test_static_site_gets_static_plans_and_unpriced_stores_are_reported():
    assert rank_plans(analyze_files([{'path': 'package.json', 'text': '{"devDependencies":{"vite":"5"}}'}]))['plans'][0]['tier'] == 'static'
    mongo = analyze_files([{'path': 'package.json', 'text': '{"scripts":{"start":"x"},"dependencies":{"mongoose":"8"}}'}])
    assert rank_plans(mongo)['excluded'] == ['mongodb']


def test_parse_repo_accepts_github_repos_and_rejects_everything_else():
    assert parse_repo('https://github.com/a/b.git') == {'owner': 'a', 'repo': 'b'}
    assert parse_repo('a/b.js') == {'owner': 'a', 'repo': 'b.js'}
    for bad in ['https://evil.com/a/b', 'a', '../a/b', 'a/b/c', None, 'a/b c', 'a/b?x=1']:
        with pytest.raises(RepoError):
            parse_repo(bad)
