# Cloudhop

[![CI](https://github.com/Inward17/CloudHop/actions/workflows/ci.yml/badge.svg)](https://github.com/Inward17/CloudHop/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Deploy an app to your own cloud, monitor it, and move it between AWS and GCP.

## Layout

- `backend/`: API on Cloudflare Workers with a D1 database. Port 8787 locally.
- `frontend/`: static site on Cloudflare Pages, port 5173 locally. Its `functions/api` proxy forwards `/api/*` to the backend, so the browser only talks to one site.

## Run locally

Create `backend/.dev.vars` (gitignored) with long random values:

```
ENCRYPTION_KEY=...
ADMIN_TOKEN=...
```

`ADMIN_TOKEN` turns on token sign-in, for local testing only. Start each in its own terminal:

```bash
cd backend && npm install && npm run dev
```

```bash
cd frontend && npm install && npm run dev
```

Open http://localhost:5173. Run tests from `backend/` with `npm test`.

## Where secrets live

| Secret | Where | Protection |
|---|---|---|
| Cloud credentials | Nowhere | GitHub Actions gets short-lived OIDC credentials. AWS and GCP trust only the repo's `cloudhop` GitHub environment. |
| GitHub access, recommended | Installation id in D1 | GitHub App: each use mints a token that expires within an hour, scoped to repos you picked. |
| GitHub access, testing | D1, `accounts.secret_enc` | AES-GCM with `ENCRYPTION_KEY`, bound to the row id. |
| GitHub App private key, OAuth secret, `ENCRYPTION_KEY`, `ADMIN_TOKEN` | Worker secrets | Set with `wrangler secret put`. Never in code or D1. |
| Dashboard session | HttpOnly cookie, hash in D1 | Scripts cannot read it. 7-day expiry, deleted on sign-out. Writes must come from `PUBLIC_URL`. |
| Report token | Repo secret `CLOUDHOP_REPORT_TOKEN`, hash in D1 | Shown once, only its SHA-256 is stored. |
| App settings | Repo secret `CLOUDHOP_ENV` | GitHub encrypted secrets. Reach the VM through a private bucket, deleted after copying. |

The frontend sends a Content Security Policy and other security headers from `frontend/public/_headers`.

In each repo, open Settings, then Environments, then `cloudhop`, and add required reviewers. Every cloud action then also needs approval in GitHub.

## GitHub sign-in and GitHub App

Create one GitHub App (Settings, Developer settings, GitHub Apps) with:

- Callback URL: `<PUBLIC_URL>/api/auth/callback`
- Setup URL: `<PUBLIC_URL>/api/github/setup`, with "Redirect on update" checked
- Repository permissions: Contents, Pull requests, Actions and Workflows set to read and write. Metadata read.
- Webhook: off

GitHub downloads the private key as PKCS#1. Convert it to PKCS#8:

```bash
openssl pkcs8 -topk8 -nocrypt -in github-app.pem -out github-app-pkcs8.pem
```

Then set these in `backend/` (or put them in `.dev.vars` locally):

```bash
npx wrangler secret put GITHUB_APP_ID
```

```bash
npx wrangler secret put GITHUB_APP_SLUG
```

```bash
npx wrangler secret put GITHUB_APP_PRIVATE_KEY
```

```bash
npx wrangler secret put GITHUB_CLIENT_ID
```

```bash
npx wrangler secret put GITHUB_CLIENT_SECRET
```

```bash
npx wrangler secret put ALLOWED_GITHUB_USERS
```

`ALLOWED_GITHUB_USERS` is a comma-separated list of GitHub logins allowed to sign in. Once GitHub sign-in works, delete `ADMIN_TOKEN`.

## Deploy Cloudhop

Reports from GitHub Actions need a public backend, so real testing needs this. In `backend/`:

```bash
npx wrangler d1 create cloudhop
```

Put the printed `database_id` in `backend/wrangler.jsonc`, then:

```bash
npx wrangler secret put ENCRYPTION_KEY
```

```bash
npm run deploy
```

Put the printed Worker URL in `env.production.vars.API_ORIGIN` in `frontend/wrangler.jsonc`, then in `frontend/`:

```bash
npm run deploy
```

Put the printed Pages address in `PUBLIC_URL` in `backend/wrangler.jsonc`, and run `npm run deploy` in `backend/` again.

Losing `ENCRYPTION_KEY` makes saved personal tokens unreadable. GitHub App accounts keep working.

## Use it

1. Sign in, then open Accounts. Connect GitHub and save an AWS or GCP account. Use a sandbox account while testing.
2. Create a project from a repo. Save the report token shown once as repo secret `CLOUDHOP_REPORT_TOKEN`. Put app settings in `CLOUDHOP_ENV`.
3. In the project's Setup tab, connect a saved cloud account and run the downloaded file in that cloud.
4. Open the install PR, review it, merge it.
5. Use Overview to check the connection, deploy, then switch. Metrics shows uptime, response time, outages, spend and server load.
6. Remove each cloud when finished, or it keeps billing.

## How it works

Actual work runs in GitHub Actions in the user's repo through `.cloudhop/run.sh`. Each cloud gets one VM running Docker Compose with Postgres and Redis. Images live in GHCR, so the same image runs on both clouds. A switch stops the app, copies the database with `pg_dump` and `pg_restore`, starts it on the target and checks health. If that fails, the app restarts on the source.

The backend checks each live app every 5 minutes for uptime and response time, and once a day starts a report run that collects spend, credits and server load.

## Current limits

- Deploy, switch and destroy have not been proven on real clouds yet.
- HTTP only for deployed apps. Add TLS before real user data.
- Postgres and Redis only, and Redis contents are not moved.
- A switch changes the public address. DNS is not updated for you.
- Commands use keyword rules, not an LLM.
- GCP spend is not reported yet.
- Sign-in has no rate limit yet. Add a Cloudflare rate limiting rule on `/api/auth/*` when deploying.
- Prices are unverified estimates.
