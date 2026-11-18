# Contributing to Cloudhop

Thanks for helping. This guide covers how to report problems, propose changes and get a pull request merged.
Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Security problems** never go in public issues or pull requests. Follow [SECURITY.md](SECURITY.md).
- **Bugs and ideas**: search open and closed issues first, then open one with the matching template.
- **Larger changes** (a new cloud, platform or code host, or a change to how approvals work): open an issue to agree on
  the approach before writing code.

## Project layout

| Path | What lives there |
|---|---|
| `backend/cloudhop` | FastAPI app, SQLite storage, provider adapters, the agent's fixed actions |
| `backend/templates` | Files Cloudhop writes into users' repos: pipelines, `run.sh`, OpenTofu, setup files |
| `backend/tests` | pytest suite; outside services are faked, nothing calls real clouds |
| `frontend/public` | The dashboard (plain JavaScript, no build step) |
| `demo` | The browser demo, built from the dashboard with a fake API |

## Run it locally

See **Run locally** in the [README](README.md). In short:

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
cd backend && .venv/bin/uvicorn cloudhop.main:app --host 127.0.0.1 --port 8787
```

```bash
cd frontend && npm run dev
```

Use sandbox accounts while testing. Never point tests or local runs at production data or real cloud resources.

## Checks to run before a pull request

These match the CI jobs in `.github/workflows/ci.yml`:

```bash
cd backend && ruff check cloudhop tests scripts && python -m pytest -q
```

```bash
node --check frontend/public/app.js && node --check frontend/public/charts.js && node --check demo/mock.js
```

```bash
node demo/build.js _site && node --check _site/demo/demo.js
```

If you changed `backend/templates`, also run `shellcheck --severity=warning` on the shell templates. The `infra` CI job
validates the OpenTofu files and lints the rendered setup files with `cfn-lint`.

## Writing changes

- Keep each pull request focused on one problem. Bug fixes include a regression test that fails without the fix.
- Fake outside services in tests (see the `fake_hosts` fixture in `backend/tests/conftest.py`). Tests must not need
  real accounts or credentials.
- Keep the safety rules intact: every cloud or platform change needs a person's approval, the agent only picks from
  fixed actions, secrets are stored encrypted and never logged, and Cloudhop never deletes users' resources on its own.
- User-facing text is short and plain. Errors say what happened and what to do next.
- Dashboard changes should work at phone width (320 px), in light and dark themes, and with a keyboard. See
  [ACCESSIBILITY.md](ACCESSIBILITY.md).
- Keep the demo (`demo/mock.js`) in step with backend behaviour, and keep it clear what is demo-only.

## Pull requests

1. Branch from `main` and fill in the pull request template.
2. Link the issue it fixes (`Fixes #123`).
3. Say which checks you ran and which you could not run.
4. Expect review comments from maintainers and from automated review. Push fixes as new commits so reviews stay easy to follow.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
