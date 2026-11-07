# Cloudhop

[![CI](https://github.com/Inward17/CloudHop/actions/workflows/ci.yml/badge.svg)](https://github.com/Inward17/CloudHop/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Deploy an app from GitHub, GitLab, Bitbucket or Azure DevOps to your own cloud, monitor it, and move it between AWS and GCP.

**[Try the demo](https://inward17.github.io/CloudHop/demo/)**: the real dashboard running on sample data in your browser. Nothing touches real clouds.

## Code hosts

| Host | Connect with | Deploys run in | Clouds trust |
|---|---|---|---|
| GitHub | GitHub App (short-lived tokens) or a fine-grained token | GitHub Actions | The repo's protected `cloudhop` environment |
| GitLab (gitlab.com) | Access token with the `api` scope | GitLab CI | The project's default branch |
| Bitbucket Cloud | Atlassian account email and API token | Bitbucket Pipelines (turn them on in repo settings) | The repository |
| Azure DevOps | Organization and personal access token (Code read/write, Build read/execute) | Azure Pipelines | The project's `cloudhop` service connection |

Every host gets the same `.cloudhop/run.sh`. GitHub uses its own workflow; GitLab, Bitbucket and Azure DevOps run `.cloudhop/ci.sh`, which installs the tools and exchanges the CI's OIDC token for short-lived cloud access. Cloudhop never overwrites an existing `.gitlab-ci.yml` or `bitbucket-pipelines.yml`: the change request says what to add instead.

Images are built on the server from the deployed commit, so no container registry or registry token is needed on any host.

Azure DevOps needs one manual step per project: create a service connection named `cloudhop` that uses workload identity federation, and add its id as pipeline variable `CLOUDHOP_SERVICE_CONNECTION_ID`. The cloud setup file assumes the token issuer is `vstoken.dev.azure.com/<organization id>`; check it against the first pipeline run.

Only GitHub, through the GitHub App or a token, has been exercised against a real account so far. GitLab, Bitbucket and Azure DevOps reach their real APIs and reject bad credentials correctly, but no pipeline has run on them yet.

## Supported devices

Phones from 320 px wide, tablets, laptops and large screens. Light and dark themes: the button in the top bar cycles light, dark and match-device, and the choice is remembered on that browser. Touch screens get 44 px tap targets, 16 px inputs (so iOS Safari does not zoom), tap-to-read chart values and safe-area spacing on notched phones. Charts all have a table view. Text uses each device's own system font, so nothing is downloaded and every engine renders the same weights.

Browsers: current Chrome, Edge and Firefox, and Safari 15.4 or later. Every page was checked with Playwright in WebKit (Safari's engine), Firefox and Chromium at phone, tablet and desktop sizes, in light and dark mode, for script errors, sideways overflow, clipped or overlapping text, and working menus, dialogs, search and chart values.

## What is verified, and what is not

| Area | How it is checked |
|---|---|
| Deploy, switch, rollback, destroy, report | `run.sh` runs for real against stand-in `tofu`, `aws`, `gcloud` and `curl` that record every call: step order, failure recovery (the app returns to the old cloud) and secrets never appearing in remote commands |
| GitLab, Bitbucket and Azure DevOps CI entry | `ci.sh` runs against the same stand-ins and is checked to turn each CI's OIDC token into AWS and GCP access |
| Code host APIs | Each adapter's requests are checked against a recording fake for all four hosts |
| Infrastructure | CI runs `tofu validate` against the real AWS and Google providers, `cfn-lint` on the AWS setup file for every host, and `shellcheck` on every script |
| AWS APIs | CI runs the AWS path against LocalStack: the setup file through CloudFormation, OIDC sign-in through `ci.sh`, OpenTofu with the S3 state backend, then verify, plan, deploy, report and destroy (`backend/tests/test_localstack.py`). LocalStack has no real servers, so the app itself is not built or started, and its health check is a stand-in |
| Backend API | `backend/tests/test_api.py` drives the FastAPI app over HTTP the way the dashboard does: sign-in, cross-site write refusal, accounts, projects, cloud setup, install PR, approvals, the one-at-a-time rule, reports, metrics, access, per-user isolation and account deletion, with fake code hosts |
| Dashboard against the real backend | Playwright signs in, adds AWS and GCP accounts, opens Access, tries a bad GitHub token, analyzes a public repo, edits the profile, switches theme and signs out, in Chromium, Firefox and WebKit |
| Real clouds and real code hosts | **Not yet.** No deploy has run in a real AWS or GCP account, and no pipeline has run on GitLab, Bitbucket or Azure DevOps |

Plug and play today means: any of four code hosts (GitHub, GitLab, Bitbucket, Azure DevOps), deployed to AWS or GCP, switchable between those two. Apps must be Node or Python, or bring a Dockerfile, and use Postgres and Redis at most. Other clouds, managed databases and other datastores are not supported yet.

## Layout

- `backend/`: the API, in Python with FastAPI. SQLite for storage, and a background job every 5 minutes for uptime checks. Port 8787 locally.
- `frontend/`: the dashboard, plain HTML, CSS and JavaScript. Port 5173 locally; its dev server passes `/api/*` to the backend, so the browser only talks to one site. In production the backend serves these files itself, from the same address.
- `demo/`: the GitHub Pages demo. `demo/engine/` is a frozen JavaScript copy of the backend logic, so the demo runs entirely in the browser with sample data. It can drift from the Python backend over time.

## Demo on GitHub Pages

The Demo workflow builds `demo/` with the dashboard from `frontend/public` and publishes it to https://inward17.github.io/CloudHop/demo/ when those folders change on `main`. Build it yourself:

```bash
node demo/build.js _site
```

## Run locally

Needs Python 3.11 or later and Node 22. Create `backend/.env` (gitignored) with long random values:

```
ENCRYPTION_KEY=...
ADMIN_TOKEN=...
PUBLIC_URL=http://localhost:5173
```

`ADMIN_TOKEN` turns on token sign-in, for local testing only. The database is `backend/data/cloudhop.db` (gitignored); set `DATABASE_PATH` to put it elsewhere. Install the backend once:

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

On Windows the venv's programs are in `.venv\Scripts` instead of `.venv/bin`. Then start each in its own terminal:

```bash
cd backend && .venv/bin/uvicorn cloudhop.main:app --host 127.0.0.1 --port 8787
```

```bash
cd frontend && npm run dev
```

Open http://localhost:5173. API docs are at http://localhost:8787/api/docs. Run the tests from `backend/`:

```bash
.venv/bin/python -m pytest
```

The `run.sh` tests need `bash` and `jq`; they are skipped without them.

### AWS on LocalStack

Runs the real AWS steps against LocalStack instead of an AWS account. Needs Docker, plus `tofu`, `aws`, `jq` and `git`:

```bash
docker run -d --name cloudhop-localstack -p 4566:4566 -e SERVICES=iam,sts,s3,ec2,ssm,cloudformation localstack/localstack:4.14.0
```

```bash
cd backend && LOCALSTACK_URL=http://localhost:4566 .venv/bin/python -m pytest tests/test_localstack.py
```

`4.14.0` is the last LocalStack Community image that runs without an account. Newer images need a free LocalStack account and `LOCALSTACK_AUTH_TOKEN`. What LocalStack cannot show: the app running on a real server, OIDC token signatures (it does not check them), the IAM policy simulator (not in Community, so the live access check is skipped there), and the GitHub OIDC provider resource (a Pro CloudFormation type).

## Where secrets live

| Secret | Where | Protection |
|---|---|---|
| Cloud credentials | Nowhere | Each CI run gets short-lived OIDC credentials. AWS and GCP trust only that one repo (on GitHub, only its `cloudhop` environment). |
| GitHub access, recommended | Installation id in the database | GitHub App: each use mints a token that expires within an hour, scoped to repos you picked. |
| GitHub token, GitLab, Bitbucket and Azure DevOps tokens | Database, `accounts.secret_enc` | AES-GCM with `ENCRYPTION_KEY`, bound to the row id, so a copied value fails on another row. Checked against the host before saving, never sent back to the browser. These are long-lived: give them the narrowest scopes and an expiry date. |
| GitHub App private key, OAuth secret, `ENCRYPTION_KEY`, `ENCRYPTION_KEY_PREVIOUS`, `ADMIN_TOKEN` | Server environment variables (`backend/.env` locally, gitignored) | Never in code or the database. |
| Passwords | Nowhere | Sign-up and sign-in use GitHub, so there is no password to store or leak. |
| Dashboard session | HttpOnly cookie, hash in the database | Scripts cannot read it. 7-day expiry. Sign out, or end other sessions from the profile page. Writes must come from `PUBLIC_URL`. |
| Report token | Repo secret `CLOUDHOP_REPORT_TOKEN`, hash in the database | Shown once, only its SHA-256 is stored. |
| App settings | Repo secret `CLOUDHOP_ENV` | GitHub encrypted secrets. Reach the VM through a private bucket, deleted after copying. |

Every account and project belongs to one user, and every query filters by that user. Another user's ids return "not found". Deleting your account from the profile page removes everything Cloudhop stores for you.

The website is sent with a Content Security Policy and other security headers from `frontend/public/_headers`, by both the backend and the local dev server.

In each repo, open Settings, then Environments, then `cloudhop`, and add required reviewers. Every cloud action then also needs approval in GitHub.

To rotate `ENCRYPTION_KEY`, move the current value to `ENCRYPTION_KEY_PREVIOUS` and set a new `ENCRYPTION_KEY`. Saved tokens stay readable through the previous key; save them again (or reconnect) to encrypt them with the new one, then remove `ENCRYPTION_KEY_PREVIOUS`.

## See what Cloudhop can reach

On the Accounts page, each account has an Access button. It asks the provider directly:

| Account | What it shows | Where it comes from |
|---|---|---|
| GitHub App | Repos (all, or the ones you selected) and the permissions you granted | `GET /installation/repositories`, `GET /app/installations/{id}` |
| GitHub token | Repos it can reach and your level on each; scopes and expiry for classic tokens | `GET /user/repos`, `X-OAuth-Scopes` and `github-authentication-token-expiration` headers. Fine-grained tokens do not expose their permissions. |
| GitLab | Projects and your role on each; token scopes and expiry | `GET /projects?membership=true`, `GET /personal_access_tokens/self` |
| Bitbucket | Repos and your permission on each; scopes if Bitbucket sends them | `GET /user/permissions/repositories` |
| Azure DevOps | Repos in the organization | `GET /_apis/git/repositories`. Azure DevOps does not let a token read its own scopes. |
| AWS, GCP | What the setup file grants, which repos' CI can use it, and a live check | Each verify run asks the cloud which of a fixed list of actions the deploy identity may do: the AWS IAM policy simulator (`iam:SimulatePrincipalPolicy`, allowed only on the project's own role) or Google Cloud `projects.testIamPermissions`. Anything allowed that Cloudhop never needs is flagged. |

Cloudhop stores no cloud keys, so it cannot check a cloud account on its own: the live check comes from the latest verify run of a project that uses that account.

## What the agent may do

Cloudhop's agent only proposes. It cannot run anything a person did not approve, and it cannot run anything outside a fixed list:

- **Fixed actions.** The only actions are verify, plan, deploy, switch, rollback, destroy and report, on AWS or GCP. The backend rejects anything else, and so does `run.sh` in the repo, so a hand-started or altered pipeline run cannot do more. Bitbucket and Azure DevOps pipelines also restrict these inputs to the same lists.
- **Approval first.** Every action needs a click; destroy also needs the repo name typed. The backend checks this again, not just the page.
- **Only connected clouds.** Actions run only on clouds connected to that project, and switch or rollback need a different, connected source cloud. `run.sh` refuses clouds missing from its config.
- **One at a time.** A new action is refused while another is still running for the same project.
- **Audit trail.** Each approved action is recorded with who approved it, and shows in the project's activity.
- **Fixed steps.** `run.sh` runs the same steps every time. It does not take commands from chat, from the repo's code or from the app's settings.
- **Limited reach.** Cloud roles trust one repo only, and the commands box uses keyword rules, not an LLM, so there is no prompt to inject.

The cloud roles can still manage VMs, firewalls and buckets across the whole account. Use a sandbox account or project for Cloudhop until roles are narrowed to Cloudhop-tagged resources.

## Sign-up, sign-in and the GitHub App

Users sign up by continuing with GitHub: the first sign-in creates their account. One GitHub App handles both sign-in and repo access.

**Quick setup (local):** from `backend/`, run the script below and open the address it prints. It sends GitHub a ready-made app description (GitHub's App Manifest flow); check it and click Create GitHub App. The script then saves the app's id, slug, client id, client secret and private key to `backend/.env`. Restart the backend.

```bash
python scripts/create_github_app.py
```

Add `--org NAME` to create it under an organization. The app is private to its owner, has webhooks off, and asks for Contents, Pull requests, Actions and Workflows (read and write) plus Metadata (read).

**By hand:** create the app in Settings, Developer settings, GitHub Apps with:

- Callback URL: `<PUBLIC_URL>/api/auth/callback`
- Setup URL: `<PUBLIC_URL>/api/github/setup`, with "Redirect on update" checked
- Repository permissions: Contents, Pull requests, Actions and Workflows set to read and write. Metadata read.
- Webhook: off

Then set `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` as environment variables on the server, or in `backend/.env` locally. The private key works as GitHub downloads it; in `.env`, put it on one line in quotes with `
` for each line break.

`ALLOWED_GITHUB_USERS` is optional. Leave it unset for open sign-up, or set a comma-separated list of GitHub logins to allow only those people. Once GitHub sign-in works, delete `ADMIN_TOKEN`.

When someone installs the app, GitHub's redirect names an installation id that anyone could forge. Cloudhop sends them through a GitHub sign-in and only saves the installation if their GitHub account can see it.

Email and password sign-up is not offered, so there are no passwords to store, reset or leak.

## Hosting Cloudhop

Not set up yet: the Python backend is tested locally only for now. Reports from the pipelines need a public address, so real end-to-end testing will need it hosted.

The plan: one FastAPI server serves the website and the API from one address, so sign-in cookies stay first-party. Run it behind HTTPS (for example Caddy or a platform's built-in TLS) with `PUBLIC_URL` set to that address, a persistent disk for the SQLite file, and one instance (the background job and SQLite assume one).

Losing `ENCRYPTION_KEY` makes saved tokens unreadable. GitHub App accounts keep working.

## Use it

1. Sign in, then open Accounts and choose Add account. Connect a code host and save an AWS or GCP account. Use a sandbox account while testing. Access shows what each one lets Cloudhop reach.
2. Create a project from a repo. Save the report token shown once as repo secret `CLOUDHOP_REPORT_TOKEN`. Put app settings in `CLOUDHOP_ENV`.
3. In the project's Setup tab, connect a saved cloud account and run the downloaded file in that cloud.
4. Open the install PR, review it, merge it.
5. Use Overview to check the connection, deploy, then switch. Metrics shows uptime, response time, outages, spend and server load.
6. Remove each cloud when finished, or it keeps billing.

## How it works

Actual work runs in the repo's own CI (GitHub Actions, GitLab CI, Bitbucket Pipelines or Azure Pipelines) through `.cloudhop/run.sh`. Each cloud gets one VM running Docker Compose with Postgres and Redis. Images are built on the VM from the deployed commit, so both clouds run the same code. A switch stops the app, copies the database with `pg_dump` and `pg_restore`, starts it on the target and checks health. If that fails, the app restarts on the source.

The backend checks each live app every 5 minutes for uptime and response time, and once a day starts a report run that collects spend, credits and server load.

## Current limits

- Deploy, switch and destroy have not been proven on real clouds yet.
- HTTP only for deployed apps. Add TLS before real user data.
- Postgres and Redis only, and Redis contents are not moved.
- A switch changes the public address. DNS is not updated for you.
- Commands use keyword rules, not an LLM.
- GCP spend is not reported yet.
- Sign-in has no rate limit yet. Add one on `/api/auth/*` in the reverse proxy when hosting.
- One server only: SQLite and the in-process 5-minute job do not spread across machines. Move to Postgres and a separate scheduler to scale out.
- Prices are unverified estimates.
