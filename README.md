# OmniSeed GitHub Provider

This is a narrow real-world test of OmniSeed Provider Protocol v1-alpha.

It supplies one narrow Provider realisation for the `workflows` primitive family, used by the Company Capability:

`software.change.manage`

GitHub is one possible realisation of the governed change/review workflow requirement composed by that capability. The Provider does not define software development itself, bind the Capability directly to GitHub, or act as a general GitHub API wrapper. It advertises only `workflows`: the implementation actually progresses a change through branch, commit, pull-request, and observed review/check states. Its observation and evidence methods support that workflow lifecycle; they do not claim a separately selectable `observations` implementation.

## What it does

The Provider can:

- observe a target repository, open pull requests, branch protection, and checks;
- validate that an approved change still starts from the observed base commit;
- create one branch and commit an exact engine-approved company-definition candidate;
- open one pull request;
- observe the resulting PR, commit, checks, mergeability, and evidence;
- detect when the base branch changed after planning.
- merge an unchanged pull request only when actor authority and configured review/check policy pass.

It maps those behaviors to the unchanged Provider Protocol v1 methods: initialize, status, validate, plan, apply, observe, invoke, and shutdown.

[`provider-package.json`](provider-package.json) is the static discovery claim. It describes what this package can support; it does not claim that the Provider is installed, configured, connected, healthy, or capable for a particular company.

## What it does not do

It does not install itself, discover Providers, change protocol v1, or expose a generic GitHub API to OmniSeed. Repository inspection, exact-file change submission, and governed merge remain scoped to the configured repository and the `workflows` family. It does not touch production repositories in acceptance tests.

## Authentication

The reference implementation reuses an existing authenticated `gh` CLI session. Credentials remain outside protocol messages and stdout.

## Test

```sh
npm test
```

The live acceptance test is intentionally explicit because it mutates the disposable sandbox:

```sh
OMNISEED_GITHUB_LIVE=1 npm run acceptance -- \
  --repo mikeajijola/omniseed-provider-github-sandbox \
  --run-id manual-001
```

Each run writes exact external evidence to `evidence/latest.json` and `evidence/runs/<run-id>.json`.
Run IDs are immutable and cannot be reused. This prevents a dirty local state or repeated sandbox branch from overwriting earlier acceptance evidence.
