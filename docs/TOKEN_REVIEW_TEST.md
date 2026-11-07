# User-authenticated review trigger test

Verify that CI posts exactly `@codex review` using the configured review-request user token, then check the Codex response against this PR's head commit. Distinguish the workflow request from any native review triggered by opening the PR.

A successful comment step is not a completed review or a formal approval. This documentation change requires no deployment.
