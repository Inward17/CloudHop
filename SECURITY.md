# Security policy

## Reporting vulnerabilities

Do not disclose vulnerability details, credentials or exploit instructions in public issues, PRs or review comments. Use this repository's GitHub Security tab and "Report a vulnerability" when available. If private reporting is unavailable, ask Inward17 for a private reporting channel before sharing the evidence. Do not assume that another private repository is an authorized disclosure channel.

Include affected commit, impact, minimal safe reproduction, code evidence, proposed remediation and regression-test criteria in the private report. Use mocked accounts and providers; do not test against production data or cloud resources.

## Security review expectations

Review authentication and per-user authorization, session expiry/revocation, encrypted provider credentials, least-privilege access, approval enforcement for cloud actions, input validation in generated scripts, outbound requests, and error/log redaction. Never execute untrusted repository code with live credentials.

## Agent-requested Codex review

For ordinary PRs and security fixes cleared by maintainers for the PR's visibility, the authoring agent should post a top-level PR comment:

```text
@codex review
```

Request again after fixes only once per new head commit, following AGENTS.md. Confirm the bot reviewed the current commit; silence is not a passing review. Keep undisclosed vulnerabilities in the approved private reporting/remediation channel, and confirm reviewer access before requesting review there. Do not copy private findings into a public PR to trigger Codex.

Codex review supplements tests and required human approvals. It does not authorize merging, deployment or bypassing branch protections.

## Scheduled findings

The nightly Codex app review uses `agent:codex` plus the appropriate issue type. Public non-sensitive defects use `bug` or `enhancement`; use `security` only in an appropriate disclosure context. Private reports need not become public issues to satisfy the labeling convention. Record an unavailable private reporting route or labeling capability as a limitation.
