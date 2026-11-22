## What and why

<!-- What does this change, and what problem does it solve? -->

Fixes #

## How it works

<!-- The approach, and anything a reviewer should look at closely. -->

## Checks

- [ ] `ruff check cloudhop tests scripts` (from `backend`)
- [ ] `python -m pytest -q` (from `backend`)
- [ ] `node --check` on changed frontend and demo scripts, and `node demo/build.js _site`
- [ ] `shellcheck` and the infra checks, if `backend/templates` changed
- [ ] Regression test added for a bug fix (fails without the fix)

Checks I could not run, and why:

## Safety

- [ ] No secrets, tokens or personal data in code, tests, logs or this description
- [ ] Cloud and platform changes still need a person's approval; the agent still picks only from fixed actions
- [ ] Tests use faked providers; nothing ran against real accounts or cloud resources
- [ ] Security-sensitive details are not in this pull request (see SECURITY.md)

## Screenshots

<!-- For dashboard changes: phone width and desktop, light and dark. Say whether it is the demo or the real backend. -->
