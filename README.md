# OmniSeed GitHub Provider

GitHub is one Provider organisation. Repositories, Actions, Checks, Rulesets,
Pull Requests, identities, and Apps/API are products/features beneath that
single boundary—not separate Providers. This package implements the canonical
`workflows`, `connectors`, and `identity` contracts it genuinely supplies.

Provider declaration:

- Supplying organisation: GitHub
- Canonical Provider ID: `github`
- Supported primitive families in this package: `workflows`
- Products/features used: Repositories, Apps/API, pull requests, Checks, and Rulesets; GitHub Actions may supply workflow execution where selected

GitHub Actions, Checks, Rulesets, Repositories, and Apps/API are not separate Providers. They are GitHub products/features used beneath the GitHub Provider. The authoritative distinction is defined by [ecosystem Provider semantics](https://github.com/mikeajijola/omniseed-ecosystem/blob/main/docs/provider-semantics.md).

This is a narrow real-world test of OmniSeed Provider Protocol v1-alpha.

It supplies narrow Provider realisations for `workflows`, repository `connectors`, and one `identity` reference kind, used by the Company Capability:

`software.change.manage`

GitHub is one possible realisation of the governed change/review workflow requirement composed by that capability. The Provider does not define software development itself, bind the Capability directly to GitHub, or act as a general GitHub API wrapper. Its observation and evidence methods support the advertised resource lifecycles; they do not claim a separately selectable `observations` implementation.

## Identity semantics

The identity implementation is deliberately reference-only. It supports `contributor_identity` when desired state declares `spec.kind: repository_collaborator`, the configured `spec.repository`, and a public GitHub `spec.login`. Validation, deterministic observation-only planning, non-mutating binding, observation, and evidence are implemented. GitHub's collaborator-permission API externally proves that the account is a collaborator of the configured repository. Evidence is limited to the declared login, GitHub subject ID/type, repository permission/role, and public profile URL.

The Provider does not create users, invite collaborators, change roles, or manage credentials. Credential-shaped fields are rejected and their values are never echoed. GitHub accounts do not prove OmniSeed steward or operator authority, and GitHub Actions OIDC is an assertion mechanism for a relying service rather than a GitHub-provisioned reconciler identity. Consequently `steward_identity`, `operator_identity`, `reconciler_identity`, GitHub App identities, organisation membership, and all other identity kinds return explicit unsupported outcomes. Those company resources must select a Provider that can truthfully provision or observe the requested identity semantics; the dependent governed Company Change is tracked in [omniseed-ecosystem-company#100](https://github.com/mikeajijola/omniseed-ecosystem-company/issues/100).

## What it does

The Provider can:

- observe a target repository, open pull requests, branch protection, and checks;
- validate that an approved change still starts from the observed base commit;
- create one branch and commit an exact engine-approved company-definition candidate;
- open one pull request;
- observe the resulting PR, commit, checks, mergeability, and evidence;
- detect when the base branch changed after planning.
- merge an unchanged pull request only when actor authority and configured review/check policy pass.

Approval may be evidenced either by a GitHub `APPROVED` review or by an exact-head
successful check explicitly allow-listed by both check name and GitHub App slug in
`mergePolicy.trustedApprovalChecks`. The latter supports the same pattern used by
1Page: a protected GitHub Actions review/gate produces independent, inspectable
evidence before the owner merge decision. A matching name from another App, a
pending/failed check, or a result for another head SHA does not satisfy approval.

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

Identity acceptance is read-only and separately gated. It requires a GitHub token through the environment and never writes the token or the full GitHub response:

```sh
OMNISEED_GITHUB_IDENTITY_LIVE=1 npm run acceptance:identity -- \
  --repo mikeajijola/omniseed-provider-github \
  --login mikeajijola
```
