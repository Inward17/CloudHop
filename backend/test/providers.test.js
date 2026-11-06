// Checks each code host adapter sends the requests its API documents, using a recorded fake fetch.
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { ADAPTERS } from '../src/providers.js';

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

// Answers by matching "METHOD url-fragment"; records every call for assertions.
function fakeFetch(routes) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method ?? 'GET';
    calls.push({ method, url: String(url), body: options.body, auth: options.headers?.Authorization });
    const match = Object.entries(routes).find(([key]) => {
      const [routeMethod, fragment] = key.split(' ');
      return routeMethod === method && String(url).includes(fragment);
    });
    if (!match) return new Response(JSON.stringify({ message: `no route for ${method} ${url}` }), { status: 404 });
    const value = typeof match[1] === 'function' ? match[1](url, options) : match[1];
    return typeof value === 'string' ? new Response(value) : Response.json(value);
  };
  return calls;
}
const json = (call) => JSON.parse(call.body);
const files = [{ path: '.gitlab-ci.yml', content: 'a' }, { path: '.cloudhop/ci.sh', content: 'b', executable: true }];

test('GitLab: project info, tree, merge request, pipeline with variables, run list', async () => {
  const calls = fakeFetch({
    'GET /repository/tree': [{ path: 'package.json', type: 'blob' }, { path: 'src', type: 'tree' }],
    'POST /repository/commits': { id: 'abc' },
    'POST /merge_requests': { web_url: 'https://gitlab.com/group/sub/app/-/merge_requests/1' },
    'POST /pipeline': { id: 9 },
    'GET /pipelines': [{ id: 9, name: 'cloudhop deploy aws', status: 'running', web_url: 'u', created_at: 't' }, { id: 8, name: 'other', status: 'success' }, { id: 7, name: 'cloudhop verify aws', status: 'failed', web_url: 'u2', created_at: 't2' }],
    'GET /projects/group%2Fsub%2Fapp': { path_with_namespace: 'group/sub/app', default_branch: 'trunk' },
  });
  const cred = { auth: 'Bearer glpat' };
  const info = await ADAPTERS.gitlab.repoInfo(cred, 'group/sub/app');
  assert.deepEqual(info, { repo: 'group/sub/app', defaultBranch: 'trunk' });
  const { entries } = await ADAPTERS.gitlab.listPaths(cred, 'group/sub/app', info);
  assert.deepEqual(entries.map((entry) => entry.path), ['package.json']);
  assert.match(calls[1].url, /recursive=true.*ref=trunk/);

  const url = await ADAPTERS.gitlab.openChangeRequest(cred, 'group/sub/app', info, files, new Set(['.gitlab-ci.yml']), 'Add Cloudhop', 'desc');
  assert.equal(url, 'https://gitlab.com/group/sub/app/-/merge_requests/1');
  const commit = json(calls.find((call) => call.url.includes('/repository/commits')));
  assert.equal(commit.start_branch, 'trunk');
  assert.deepEqual(commit.actions.map((item) => [item.action, item.file_path, item.execute_filemode]), [['update', '.gitlab-ci.yml', false], ['create', '.cloudhop/ci.sh', true]]);
  const request = json(calls.find((call) => call.url.includes('/merge_requests')));
  assert.equal(request.source_branch, commit.branch);
  assert.equal(request.target_branch, 'trunk');

  await ADAPTERS.gitlab.trigger(cred, 'group/sub/app', info, { action: 'switch', cloud: 'gcp', source: 'aws' });
  const pipeline = json(calls.find((call) => call.url.endsWith('/pipeline')));
  assert.equal(pipeline.ref, 'trunk');
  assert.deepEqual(pipeline.variables, [{ key: 'CLOUDHOP_ACTION', value: 'switch' }, { key: 'CLOUDHOP_CLOUD', value: 'gcp' }, { key: 'CLOUDHOP_SOURCE', value: 'aws' }]);

  const runs = await ADAPTERS.gitlab.runs(cred, 'group/sub/app');
  assert.deepEqual(runs.map((run) => [run.title, run.status, run.conclusion]), [['cloudhop deploy aws', 'in_progress', null], ['cloudhop verify aws', 'completed', 'failure']]);
  assert.ok(calls.every((call) => call.auth === 'Bearer glpat'));
});

test('Bitbucket: repo info with UUIDs, file listing, commit as form data, pull request, custom pipeline', async () => {
  const calls = fakeFetch({
    'GET /repositories/team/shop/src/': { values: [{ path: 'package.json', type: 'commit_file', size: 10 }, { path: 'src', type: 'commit_directory' }], next: null },
    'GET /repositories/team/shop/refs/branches/main': { target: { hash: 'h1' } },
    'GET /repositories/team/shop/pipelines/': { values: [{ build_number: 4, state: { name: 'COMPLETED', result: { name: 'SUCCESSFUL' } }, created_on: 't', target: { selector: { pattern: 'cloudhop' } } }, { build_number: 3, state: { name: 'COMPLETED' }, target: { selector: { type: 'branches' } } }] },
    'GET /repositories/team/shop': { full_name: 'team/shop', mainbranch: { name: 'main' }, uuid: '{r-uuid}', workspace: { slug: 'team', uuid: '{w-uuid}' } },
    'POST /repositories/team/shop/src': '',
    'POST /repositories/team/shop/pullrequests': { links: { html: { href: 'https://bitbucket.org/team/shop/pull-requests/1' } } },
    'POST /repositories/team/shop/pipelines/': { uuid: 'p' },
  });
  const cred = { auth: 'Basic x' };
  const info = await ADAPTERS.bitbucket.repoInfo(cred, 'team/shop');
  assert.deepEqual(info, { repo: 'team/shop', defaultBranch: 'main', repositoryUuid: '{r-uuid}', workspace: 'team', workspaceUuid: '{w-uuid}' });
  assert.deepEqual((await ADAPTERS.bitbucket.listPaths(cred, 'team/shop', info)).entries, [{ path: 'package.json', size: 10 }]);

  const url = await ADAPTERS.bitbucket.openChangeRequest(cred, 'team/shop', info, files, new Set(), 'Add Cloudhop', 'desc');
  assert.equal(url, 'https://bitbucket.org/team/shop/pull-requests/1');
  const form = calls.find((call) => call.method === 'POST' && call.url.endsWith('/src')).body;
  assert.ok(form instanceof FormData);
  assert.equal(form.get('parents'), 'h1');
  assert.equal(form.get('.cloudhop/ci.sh'), 'b');
  const pr = json(calls.find((call) => call.url.endsWith('/pullrequests')));
  assert.equal(pr.source.branch.name, form.get('branch'));
  assert.equal(pr.destination.branch.name, 'main');

  await ADAPTERS.bitbucket.trigger(cred, 'team/shop', info, { action: 'deploy', cloud: 'aws', source: '' });
  const pipeline = json(calls.find((call) => call.method === 'POST' && call.url.endsWith('/pipelines/')));
  assert.deepEqual(pipeline.target, { type: 'pipeline_ref_target', ref_type: 'branch', ref_name: 'main', selector: { type: 'custom', pattern: 'cloudhop' } });
  assert.equal(pipeline.variables[0].value, 'deploy');

  const runs = await ADAPTERS.bitbucket.runs(cred, 'team/shop');
  assert.deepEqual(runs.map((run) => [run.id, run.conclusion, run.url]), [[4, 'success', 'https://bitbucket.org/team/shop/pipelines/results/4']]);
});

test('Azure DevOps: org check, push to a new branch, pull request, pipeline created on first run', async () => {
  let pipelines = [];
  const calls = fakeFetch({
    'GET /contoso/_apis/connectionData': { instanceId: 'org-guid', authenticatedUser: { providerDisplayName: 'Dev' } },
    'GET /git/repositories/api?': { id: 'repo-id', defaultBranch: 'refs/heads/main' },
    'GET /git/repositories/repo-id/items?recursionLevel': { value: [{ path: '/package.json', isFolder: false }, { path: '/src', isFolder: true }] },
    'GET /git/repositories/repo-id/refs': { value: [{ objectId: 'base-sha' }] },
    'POST /git/repositories/repo-id/pushes': {},
    'POST /git/repositories/repo-id/pullrequests': { pullRequestId: 12 },
    'GET /_apis/pipelines?': () => ({ value: pipelines }),
    'POST /_apis/pipelines?': () => { pipelines = [{ id: 5, name: 'cloudhop' }]; return { id: 5 }; },
    'POST /_apis/pipelines/5/runs': { id: 1 },
    'GET /_apis/pipelines/5/runs': { value: [{ id: 1, name: '20261106.1', state: 'completed', result: 'succeeded', createdDate: 't', _links: { web: { href: 'w' } } }] },
  });
  const cred = { auth: 'Basic x', organization: 'contoso' };
  await assert.rejects(ADAPTERS.azure.repoInfo(cred, 'other/web/api'), /outside the contoso organization/);
  const info = await ADAPTERS.azure.repoInfo(cred, 'contoso/Web Shop/api');
  assert.deepEqual(info, { repo: 'contoso/Web Shop/api', defaultBranch: 'main', repositoryId: 'repo-id', organization: 'contoso', project: 'Web Shop', organizationId: 'org-guid' });
  assert.ok(calls.some((call) => call.url.includes('/contoso/Web%20Shop/_apis/git/repositories/api')));
  assert.deepEqual((await ADAPTERS.azure.listPaths(cred, 'contoso/Web Shop/api', info)).entries.map((entry) => entry.path), ['package.json']);

  const url = await ADAPTERS.azure.openChangeRequest(cred, 'contoso/Web Shop/api', info, files, new Set(['.gitlab-ci.yml']), 'Add Cloudhop', 'desc');
  assert.equal(url, 'https://dev.azure.com/contoso/Web%20Shop/_git/api/pullrequest/12');
  const push = json(calls.find((call) => call.url.includes('/pushes')));
  assert.equal(push.refUpdates[0].oldObjectId, 'base-sha');
  assert.deepEqual(push.commits[0].changes.map((change) => [change.changeType, change.item.path]), [['edit', '/.gitlab-ci.yml'], ['add', '/.cloudhop/ci.sh']]);

  assert.deepEqual(await ADAPTERS.azure.runs(cred, 'contoso/Web Shop/api'), []);
  await ADAPTERS.azure.trigger(cred, 'contoso/Web Shop/api', info, { action: 'verify', cloud: 'aws', source: '' });
  const created = json(calls.find((call) => call.method === 'POST' && /_apis\/pipelines\?/.test(call.url)));
  assert.equal(created.configuration.path, '/.cloudhop/azure-pipelines.yml');
  const run = json(calls.find((call) => call.url.includes('/pipelines/5/runs') && call.method === 'POST'));
  assert.deepEqual(run.templateParameters, { action: 'verify', cloud: 'aws', source: ' ' });
  assert.deepEqual((await ADAPTERS.azure.runs(cred, 'contoso/Web Shop/api')).map((item) => item.conclusion), ['success']);
});

test('GitHub: dispatch on the default branch and map workflow runs', async () => {
  const calls = fakeFetch({
    'GET /repos/o/r/actions/workflows/cloudhop.yml/runs': { workflow_runs: [{ id: 1, display_title: 'cloudhop deploy aws', status: 'queued', conclusion: null, html_url: 'u', run_started_at: 't' }] },
    'GET /repos/o/r': { default_branch: 'main' },
    'POST /dispatches': '',
  });
  const cred = { auth: 'Bearer t' };
  const info = await ADAPTERS.github.repoInfo(cred, 'o/r');
  await ADAPTERS.github.trigger(cred, 'o/r', info, { action: 'deploy', cloud: 'aws', source: '' });
  assert.deepEqual(json(calls.find((call) => call.url.endsWith('/dispatches'))), { ref: 'main', inputs: { action: 'deploy', cloud: 'aws', source: '' } });
  assert.deepEqual((await ADAPTERS.github.runs(cred, 'o/r')).map((run) => run.status), ['in_progress']);
});

test('errors from any host become clear messages', async () => {
  fakeFetch({});
  await assert.rejects(ADAPTERS.gitlab.repoInfo({ auth: 'x' }, 'g/p'), /could not find/);
  globalThis.fetch = async () => new Response('{}', { status: 401 });
  await assert.rejects(ADAPTERS.bitbucket.repoInfo({ auth: 'x' }, 't/r'), /rejected the credentials/);
});
