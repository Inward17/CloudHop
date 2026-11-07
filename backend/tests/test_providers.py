"""Checks each code host adapter sends the requests its API documents, against a recording fake."""
import httpx
import pytest

from cloudhop.errors import RepoError
from cloudhop.providers import ADAPTERS

FILES = [{'path': '.gitlab-ci.yml', 'content': 'a'}, {'path': '.cloudhop/ci.sh', 'content': 'b', 'executable': True}]


async def test_gitlab_project_info_tree_merge_request_pipeline_and_runs(fake_hosts):
    hosts = fake_hosts({
        'GET /repository/tree': [{'path': 'package.json', 'type': 'blob'}, {'path': 'src', 'type': 'tree'}],
        'POST /repository/commits': {'id': 'abc'},
        'POST /merge_requests': {'web_url': 'https://gitlab.com/group/sub/app/-/merge_requests/1'},
        'POST /pipeline': {'id': 9},
        'GET /pipelines': [{'id': 9, 'name': 'cloudhop deploy aws', 'status': 'running', 'web_url': 'u', 'created_at': 't'}, {'id': 8, 'name': 'other', 'status': 'success'},
                           {'id': 7, 'name': 'cloudhop verify aws', 'status': 'failed', 'web_url': 'u2', 'created_at': 't2'}],
        'GET /projects/group%2Fsub%2Fapp': {'path_with_namespace': 'group/sub/app', 'default_branch': 'trunk'},
    })
    cred = {'auth': 'Bearer glpat'}
    adapter = ADAPTERS['gitlab']
    info = await adapter.repo_info(cred, 'group/sub/app')
    assert info == {'repo': 'group/sub/app', 'defaultBranch': 'trunk'}
    listing = await adapter.list_paths(cred, 'group/sub/app', info)
    assert [entry['path'] for entry in listing['entries']] == ['package.json']
    assert 'recursive=true' in hosts.calls[1]['url'] and 'ref=trunk' in hosts.calls[1]['url']

    url = await adapter.open_change_request(cred, 'group/sub/app', info, FILES, {'.gitlab-ci.yml'}, 'Add Cloudhop', 'desc')
    assert url == 'https://gitlab.com/group/sub/app/-/merge_requests/1'
    commit = hosts.json(lambda call: '/repository/commits' in call['url'])
    assert commit['start_branch'] == 'trunk'
    assert [(item['action'], item['file_path'], item['execute_filemode']) for item in commit['actions']] == [('update', '.gitlab-ci.yml', False), ('create', '.cloudhop/ci.sh', True)]
    request = hosts.json(lambda call: '/merge_requests' in call['url'])
    assert request['source_branch'] == commit['branch'] and request['target_branch'] == 'trunk'

    await adapter.trigger(cred, 'group/sub/app', info, {'action': 'switch', 'cloud': 'gcp', 'source': 'aws'})
    pipeline = hosts.json(lambda call: call['url'].endswith('/pipeline'))
    assert pipeline['ref'] == 'trunk'
    assert pipeline['variables'] == [{'key': 'CLOUDHOP_ACTION', 'value': 'switch'}, {'key': 'CLOUDHOP_CLOUD', 'value': 'gcp'}, {'key': 'CLOUDHOP_SOURCE', 'value': 'aws'}]

    runs = await adapter.runs(cred, 'group/sub/app')
    assert [(run['title'], run['status'], run['conclusion']) for run in runs] == [('cloudhop deploy aws', 'in_progress', None), ('cloudhop verify aws', 'completed', 'failure')]
    assert all(call['auth'] == 'Bearer glpat' for call in hosts.calls)


async def test_bitbucket_uuids_listing_form_commit_pull_request_and_custom_pipeline(fake_hosts):
    hosts = fake_hosts({
        'GET /repositories/team/shop/src/': {'values': [{'path': 'package.json', 'type': 'commit_file', 'size': 10}, {'path': 'src', 'type': 'commit_directory'}], 'next': None},
        'GET /repositories/team/shop/refs/branches/main': {'target': {'hash': 'h1'}},
        'GET /repositories/team/shop/pipelines/': {'values': [{'build_number': 4, 'state': {'name': 'COMPLETED', 'result': {'name': 'SUCCESSFUL'}}, 'created_on': 't', 'target': {'selector': {'pattern': 'cloudhop'}}},
                                                              {'build_number': 3, 'state': {'name': 'COMPLETED'}, 'target': {'selector': {'type': 'branches'}}}]},
        'GET /repositories/team/shop': {'full_name': 'team/shop', 'mainbranch': {'name': 'main'}, 'uuid': '{r-uuid}', 'workspace': {'slug': 'team', 'uuid': '{w-uuid}'}},
        'POST /repositories/team/shop/src': '',
        'POST /repositories/team/shop/pullrequests': {'links': {'html': {'href': 'https://bitbucket.org/team/shop/pull-requests/1'}}},
        'POST /repositories/team/shop/pipelines/': {'uuid': 'p'},
    })
    cred = {'auth': 'Basic x'}
    adapter = ADAPTERS['bitbucket']
    info = await adapter.repo_info(cred, 'team/shop')
    assert info == {'repo': 'team/shop', 'defaultBranch': 'main', 'repositoryUuid': '{r-uuid}', 'workspace': 'team', 'workspaceUuid': '{w-uuid}'}
    assert (await adapter.list_paths(cred, 'team/shop', info))['entries'] == [{'path': 'package.json', 'size': 10}]

    url = await adapter.open_change_request(cred, 'team/shop', info, FILES, set(), 'Add Cloudhop', 'desc')
    assert url == 'https://bitbucket.org/team/shop/pull-requests/1'
    form_call = next(call for call in hosts.calls if call['method'] == 'POST' and call['url'].endswith('/src'))
    assert form_call['headers']['content-type'].startswith('multipart/form-data')
    assert 'name="parents"\r\n\r\nh1' in form_call['body']
    assert 'name=".cloudhop/ci.sh"\r\n\r\nb' in form_call['body']
    pr = hosts.json(lambda call: call['url'].endswith('/pullrequests'))
    assert pr['source']['branch']['name'].startswith('cloudhop/install-') and pr['source']['branch']['name'] in form_call['body']
    assert pr['destination']['branch']['name'] == 'main'

    await adapter.trigger(cred, 'team/shop', info, {'action': 'deploy', 'cloud': 'aws', 'source': ''})
    pipeline = hosts.json(lambda call: call['method'] == 'POST' and call['url'].endswith('/pipelines/'))
    assert pipeline['target'] == {'type': 'pipeline_ref_target', 'ref_type': 'branch', 'ref_name': 'main', 'selector': {'type': 'custom', 'pattern': 'cloudhop'}}
    assert pipeline['variables'][0]['value'] == 'deploy'
    runs = await adapter.runs(cred, 'team/shop')
    assert [(run['id'], run['conclusion'], run['url']) for run in runs] == [(4, 'success', 'https://bitbucket.org/team/shop/pipelines/results/4')]


async def test_azure_org_check_push_pull_request_and_pipeline_created_on_first_run(fake_hosts):
    pipelines = []

    def create(_request):
        pipelines.append({'id': 5, 'name': 'cloudhop'})
        return {'id': 5}
    hosts = fake_hosts({
        'GET /contoso/_apis/connectionData': {'instanceId': 'org-guid', 'authenticatedUser': {'providerDisplayName': 'Dev'}},
        'GET /git/repositories/api?': {'id': 'repo-id', 'defaultBranch': 'refs/heads/main'},
        'GET /git/repositories/repo-id/items?recursionLevel': {'value': [{'path': '/package.json', 'isFolder': False}, {'path': '/src', 'isFolder': True}]},
        'GET /git/repositories/repo-id/refs': {'value': [{'objectId': 'base-sha'}]},
        'POST /git/repositories/repo-id/pushes': {},
        'POST /git/repositories/repo-id/pullrequests': {'pullRequestId': 12},
        'GET /_apis/pipelines?': lambda _request: {'value': pipelines},
        'POST /_apis/pipelines?': create,
        'POST /_apis/pipelines/5/runs': {'id': 1},
        'GET /_apis/pipelines/5/runs': {'value': [{'id': 1, 'name': '20261106.1', 'state': 'completed', 'result': 'succeeded', 'createdDate': 't', '_links': {'web': {'href': 'w'}}}]},
    })
    cred = {'auth': 'Basic x', 'organization': 'contoso'}
    adapter = ADAPTERS['azure']
    with pytest.raises(RepoError, match='outside the contoso organization'):
        await adapter.repo_info(cred, 'other/web/api')
    info = await adapter.repo_info(cred, 'contoso/Web Shop/api')
    assert info == {'repo': 'contoso/Web Shop/api', 'defaultBranch': 'main', 'repositoryId': 'repo-id', 'organization': 'contoso', 'project': 'Web Shop', 'organizationId': 'org-guid'}
    assert any('/contoso/Web%20Shop/_apis/git/repositories/api' in call['url'] for call in hosts.calls)
    assert [entry['path'] for entry in (await adapter.list_paths(cred, 'contoso/Web Shop/api', info))['entries']] == ['package.json']

    url = await adapter.open_change_request(cred, 'contoso/Web Shop/api', info, FILES, {'.gitlab-ci.yml'}, 'Add Cloudhop', 'desc')
    assert url == 'https://dev.azure.com/contoso/Web%20Shop/_git/api/pullrequest/12'
    push = hosts.json(lambda call: '/pushes' in call['url'])
    assert push['refUpdates'][0]['oldObjectId'] == 'base-sha'
    assert [(change['changeType'], change['item']['path']) for change in push['commits'][0]['changes']] == [('edit', '/.gitlab-ci.yml'), ('add', '/.cloudhop/ci.sh')]

    assert await adapter.runs(cred, 'contoso/Web Shop/api') == []
    await adapter.trigger(cred, 'contoso/Web Shop/api', info, {'action': 'verify', 'cloud': 'aws', 'source': ''})
    created = hosts.json(lambda call: call['method'] == 'POST' and '_apis/pipelines?' in call['url'])
    assert created['configuration']['path'] == '/.cloudhop/azure-pipelines.yml'
    run = hosts.json(lambda call: call['method'] == 'POST' and '/pipelines/5/runs' in call['url'])
    assert run['templateParameters'] == {'action': 'verify', 'cloud': 'aws', 'source': ' '}
    assert [item['conclusion'] for item in await adapter.runs(cred, 'contoso/Web Shop/api')] == ['success']


async def test_github_dispatch_on_the_default_branch_and_map_workflow_runs(fake_hosts):
    hosts = fake_hosts({
        'GET /repos/o/r/actions/workflows/cloudhop.yml/runs': {'workflow_runs': [{'id': 1, 'display_title': 'cloudhop deploy aws', 'status': 'queued', 'conclusion': None, 'html_url': 'u', 'run_started_at': 't'}]},
        'GET /repos/o/r': {'default_branch': 'main'},
        'POST /dispatches': '',
    })
    cred = {'auth': 'Bearer t'}
    info = await ADAPTERS['github'].repo_info(cred, 'o/r')
    await ADAPTERS['github'].trigger(cred, 'o/r', info, {'action': 'deploy', 'cloud': 'aws', 'source': ''})
    assert hosts.json(lambda call: call['url'].endswith('/dispatches')) == {'ref': 'main', 'inputs': {'action': 'deploy', 'cloud': 'aws', 'source': ''}}
    assert [run['status'] for run in await ADAPTERS['github'].runs(cred, 'o/r')] == ['in_progress']


async def test_errors_from_any_host_become_clear_messages(fake_hosts):
    fake_hosts({})
    with pytest.raises(RepoError, match='could not find'):
        await ADAPTERS['gitlab'].repo_info({'auth': 'x'}, 'g/p')
    fake_hosts({'GET /repositories/t/r': httpx.Response(401, json={})})
    with pytest.raises(RepoError, match='rejected the credentials'):
        await ADAPTERS['bitbucket'].repo_info({'auth': 'x'}, 't/r')


async def test_access_reports_reachable_repos_their_level_and_scopes_where_exposed(fake_hosts):
    fake_hosts({
        'GET api.github.com/user/repos': httpx.Response(200, json=[
            {'full_name': 'me/app', 'permissions': {'admin': False, 'push': True, 'pull': True}},
            {'full_name': 'me/docs', 'permissions': {'admin': False, 'push': False, 'pull': True}},
        ], headers={'x-oauth-scopes': 'repo, workflow', 'github-authentication-token-expiration': '2026-12-01 00:00:00 UTC'}),
        'GET gitlab.com/api/v4/personal_access_tokens/self': {'scopes': ['api'], 'expires_at': '2026-12-31'},
        'GET gitlab.com/api/v4/projects': [{'path_with_namespace': 'group/app', 'permissions': {'project_access': {'access_level': 30}, 'group_access': {'access_level': 40}}}],
        'GET api.bitbucket.org/2.0/user/permissions/repositories': {'values': [{'permission': 'write', 'repository': {'full_name': 'team/shop'}}], 'size': 1},
        'GET dev.azure.com/contoso/_apis/git/repositories': {'value': [{'name': 'web', 'project': {'name': 'Shop'}}]},
    })
    github = await ADAPTERS['github'].access({'auth': 'Bearer t'}, {'kind': 'github_token', 'data': '{}'}, {})
    assert github['repos'] == [{'name': 'me/app', 'access': 'write'}, {'name': 'me/docs', 'access': 'read'}]
    assert github['scopes'] == ['repo', 'workflow']
    assert github['expires'] == '2026-12-01 00:00:00 UTC'
    gitlab = await ADAPTERS['gitlab'].access({'auth': 'Bearer g'})
    assert gitlab['repos'] == [{'name': 'group/app', 'access': 'maintainer'}]
    assert (gitlab['scopes'], gitlab['expires']) == (['api'], '2026-12-31')
    bitbucket = await ADAPTERS['bitbucket'].access({'auth': 'Basic b'})
    assert bitbucket['repos'] == [{'name': 'team/shop', 'access': 'write'}]
    assert bitbucket['scopes'] is None and 'did not report' in bitbucket['note']
    azure = await ADAPTERS['azure'].access({'auth': 'Basic a', 'organization': 'contoso'})
    assert azure['repos'] == [{'name': 'contoso/Shop/web', 'access': None}]
    assert 'does not let a token read its own scopes' in azure['note']


async def test_access_a_fine_grained_github_token_has_no_scope_header_and_that_is_said_plainly(fake_hosts):
    fake_hosts({'GET api.github.com/user/repos': [{'full_name': 'me/app', 'permissions': {'admin': True, 'push': True, 'pull': True}}]})
    result = await ADAPTERS['github'].access({'auth': 'Bearer t'}, {'kind': 'github_token', 'data': '{}'}, {})
    assert result['scopes'] is None
    assert result['repos'] == [{'name': 'me/app', 'access': 'admin'}]
    assert 'Fine-grained' in result['note']
