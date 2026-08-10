# Working on the OmniSeed GitHub Provider

Protect the Capability boundary.

- The Capability is `software.change.manage`.
- GitHub is one replaceable realisation.
- Do not expose a generic GitHub API through Provider operations.
- Do not add GitHub-specific methods to Provider Protocol v1.
- OmniSeed owns the plan, approval, apply lifecycle, and canonical state.
- The Provider may reject external drift. It may not rewrite the approved action.
- Never print credentials or place them in evidence.
- Stdout is JSON-RPC responses only. Diagnostics go to stderr.
- Live tests may mutate only `mikeajijola/omniseed-provider-github-sandbox` unless a human explicitly approves another target.
- Evidence must record repository, base SHA, branch, commit, PR identity, checks, timestamps, and drift.

Run `npm test` before the live acceptance test. Keep this Provider deliberately narrow until real usage proves a missing abstraction.

