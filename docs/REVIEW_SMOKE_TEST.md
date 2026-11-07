# Review-trigger smoke test

Use a ready collaborator pull request with a small, harmless change to verify the review integration.

1. Open the PR and record its head commit.
2. In Actions, check the `Request Codex review` workflow run triggered by `pull_request_target`.
3. In the PR conversation, confirm that `github-actions[bot]` posted `@codex review` with the same head commit.
4. Confirm a response from `chatgpt-codex-connector` and check which commit it reviewed. A successful request workflow alone does not prove a review happened.
5. If no response arrives, record that the automatic integration remains unverified. A later manual `@codex review` comment tests the manual path separately.

The request workflow does not approve or merge the PR. Required CI checks and valid GitHub approvals remain separate merge conditions. Never infer approval from silence or a reaction.

This checklist introduces no runtime behavior or deployment changes.
