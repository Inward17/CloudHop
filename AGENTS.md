# Agent instructions

## Changes and validation

Read SECURITY.md before reviewing or changing code. Keep changes focused and include regression tests for behavior fixes. Use isolated fixtures and mocked providers; do not access production data or run cloud deploy, switch, rollback or destroy operations as part of a review.

Backend code is in backend/cloudhop, tests in backend/tests, the dashboard in frontend/public, deployment templates in backend/templates and the browser demo in demo. Check relevant backend changes with `ruff check cloudhop tests scripts` and `python -m pytest -q` from backend. Use the applicable checks in .github/workflows/ci.yml for frontend, scripts and infrastructure. Report what ran and what did not run.

## Requesting Codex PR reviews

After opening a non-draft implementation PR, or marking it ready, the authoring agent (including Claude) should post a top-level PR conversation comment containing exactly:

```text
@codex review
```

After addressing findings and pushing the fixes, request a fresh review once for the new head commit. Check existing comments and reviews first; do not post duplicate requests for the same head or create a review loop. A reviewing agent must not request a review of its own review comment.

Record the PR head SHA and confirm that the bot actually reviewed that commit. If there is no bot response or access fails, report the limitation; do not claim that review passed. A bot comment or thumbs-up does not replace CI, required approvals or branch protections. Do not merge or deploy as part of this review workflow.

This instruction does not enable the GitHub integration by itself. The repository owner must grant the Codex GitHub integration repository access and enable code review. No OpenAI API key is required by these instructions.

## Review priorities

Report actionable defects with evidence, rather than style preferences. Prioritize authentication and user isolation, session/token handling, approval enforcement, deployment action validation, command injection, outbound request boundaries, concurrency and failure recovery, and frontend/API behavior. Keep the demo and real backend distinctions explicit. Verify suspected defects through code tracing and safe mocked reproductions.

## Nightly issue workflow

The Codex app schedule runs at 22:00 Asia/Kolkata; it is separate from GitHub Actions. Review changes since the last completed audit and rotate deeper inspection across the areas above. Read current instructions and open/closed issues plus active PRs before filing anything.

Every issue created by Codex must carry `agent:codex` and an appropriate type label: `bug`, `enhancement` or `security`. If labels cannot be applied, report that limitation. Check existing `agent:codex` issues with these type labels, follow their linked fix PRs, and avoid duplicates.

For each confirmed non-security defect, include severity, reviewed commit, pinned file/line evidence, reproduction, expected versus actual behavior, fix guidance, and regression-test acceptance criteria so Claude can implement a focused PR. Follow SECURITY.md for sensitive findings; labels never authorize public disclosure. Keep a per-repository record of reviewed commit, coverage, findings and pending follow-ups.
