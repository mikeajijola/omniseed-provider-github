import assert from "node:assert/strict";
import test from "node:test";
import { GitHubProvider, GitHubProviderError } from "../runtime/github-provider.mjs";

const sha = value => value.repeat(40);
function response(value, status = 200) { return { ok: status >= 200 && status < 300, status, async json() { return value; } }; }
function fake(routes) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    const path = new URL(url).pathname + new URL(url).search;
    calls.push({ path, init });
    const key = `${init.method} ${path}`;
    if (!(key in routes)) return response({ message: key }, 404);
    const value = typeof routes[key] === "function" ? routes[key]({ path, init, calls }) : routes[key];
    return response(value);
  };
  return { fetchImpl, calls };
}
function baseRoutes(extra = {}) { return {
  "GET /user": { login: "runtime-app" },
  "GET /repos/example/company": { html_url: "https://github.com/example/company", default_branch: "main" },
  "GET /repos/example/company/git/ref/heads/main": { object: { sha: sha("a") } },
  ...extra,
}; }
async function connected(extra = {}, configuration = {}) {
  const transport = fake(baseRoutes(extra));
  const provider = await GitHubProvider.connect({ configuration: { repository: "example/company", ...configuration }, token: "not-a-real-token", fetchImpl: transport.fetchImpl });
  return { provider, ...transport };
}

test("connect derives healthy Provider status without exposing the credential", async () => {
  const { provider, calls } = await connected();
  assert.deepEqual(provider.status, { implementation_available: true, configured: true, connected: true, healthy: true });
  assert.equal(provider.identity, "runtime-app");
  assert.ok(calls.every(call => call.init.headers.authorization === "Bearer not-a-real-token"));
  assert.equal(JSON.stringify(provider.metadata).includes("not-a-real-token"), false);
});

test("identity lifecycle binds and externally observes only a repository collaborator", async () => {
  const collaborator = { permission: "write", role_name: "write", user: { id: 42, login: "octocat", type: "User", html_url: "https://github.com/octocat", email: "must-not-leak@example.test" } };
  const { provider, calls } = await connected({ "GET /repos/example/company/collaborators/octocat/permission": collaborator });
  const action = { id: "identity-1", family: "identity", resourceId: "contributors", desired: { offers: ["contributor_identity"], spec: { kind: "repository_collaborator", repository: "example/company", login: "octocat" } } };
  assert.deepEqual(await provider.validate(action), { valid: true, issues: [] });
  assert.deepEqual(await provider.plan(action), { deterministic: true, actionId: "identity-1", mode: "observe_only", mutation: false, kind: "repository_collaborator" });
  const applied = await provider.apply(action), observed = await provider.observe(applied);
  assert.equal(observed.status, "healthy"); assert.equal(observed.evidence[0].subjectId, 42);
  assert.equal("email" in observed.evidence[0], false);
  assert.equal(calls.filter(call => call.path.includes("/collaborators/")).every(call => call.init.method === "GET"), true);
});

test("identity rejects unsupported kinds and reports secret field paths without values", async () => {
  const { provider } = await connected();
  const result = await provider.validate({ id: "identity-2", family: "identity", resourceId: "reconciler", desired: { offers: ["reconciler_identity"], spec: { kind: "actions_oidc", repository: "example/company", login: "runner", accessToken: "never-print-this" } } });
  assert.equal(result.valid, false);
  assert.ok(result.issues.some(issue => issue.code === "unsupported_identity_kind"));
  assert.ok(result.issues.some(issue => issue.code === "secret_field_forbidden"));
  assert.equal(JSON.stringify(result).includes("never-print-this"), false);
});

test("repository inspection returns the exact canonical document", async () => {
  const content = "metadata:\n  name: Company\n";
  const { provider } = await connected({ "GET /repos/example/company/contents/omniform.yaml?ref=main": { sha: sha("b"), content: Buffer.from(content).toString("base64") } });
  const result = await provider.invoke("company.repository.inspect", { repository: "example/company", baseBranch: "main", path: "omniform.yaml" }, { actorId: "engine" });
  assert.equal(result.baseSha, sha("a")); assert.equal(result.document.content, content);
});

test("validation rejects drift before mutation", async () => {
  const { provider } = await connected();
  const result = await provider.validate({ family: "workflows", resourceId: "github_company_change", desired: { spec: { repository: "example/company", baseBranch: "main", expectedBaseSha: sha("b"), branch: "omniseed/change", path: "omniform.yaml", content: "x", commitMessage: "change", pullRequestTitle: "Change" } } });
  assert.equal(result.valid, false); assert.equal(result.issues.at(-1).code, "external_drift");
});

test("apply creates the exact branch, commit, and pull request", async () => {
  const routes = {
    "POST /repos/example/company/git/refs": { ref: "refs/heads/omniseed/change" },
    [`GET /repos/example/company/git/commits/${sha("a")}`]: { tree: { sha: sha("c") } },
    "POST /repos/example/company/git/trees": { sha: sha("d") },
    "POST /repos/example/company/git/commits": { sha: sha("e") },
    "PATCH /repos/example/company/git/refs/heads/omniseed%2Fchange": { object: { sha: sha("e") } },
    "POST /repos/example/company/pulls": { number: 12, html_url: "https://github.com/example/company/pull/12" },
  };
  const { provider, calls } = await connected(routes);
  const spec = { repository: "example/company", baseBranch: "main", expectedBaseSha: sha("a"), branch: "omniseed/change", path: "omniform.yaml", content: "metadata:\n  name: Changed\n", commitMessage: "change", pullRequestTitle: "Change" };
  const result = await provider.apply({ family: "workflows", resourceId: "github_company_change", desired: { spec } });
  assert.equal(result.attributes.commitSha, sha("e")); assert.equal(result.attributes.pullRequestNumber, 12);
  assert.equal(JSON.parse(calls.find(call => call.path.endsWith("/git/trees")).init.body).tree[0].content, spec.content);
});

test("merge is blocked without engine-granted merge authority", async () => {
  const { provider } = await connected();
  await assert.rejects(provider.invoke("company.change.merge", { pullRequestNumber: 1, expectedHeadSha: sha("a") }, { permissions: [] }), error => error instanceof GitHubProviderError && error.code === "authorization_denied");
});

test("merge is blocked without approval or non-empty passing checks", async () => {
  const common = { "GET /repos/example/company/pulls/4": { number: 4, merged: false, head: { sha: sha("b") } }, "GET /repos/example/company/pulls/4/reviews": [], [`GET /repos/example/company/commits/${sha("b")}/check-runs`]: { check_runs: [] }, [`GET /repos/example/company/commits/${sha("b")}/status`]: { state: "success", statuses: [] } };
  const { provider } = await connected(common);
  await assert.rejects(provider.invoke("company.change.merge", { pullRequestNumber: 4, expectedHeadSha: sha("b") }, { permissions: ["company_change.merge"] }), error => error.code === "github_merge_approval_required");
  const allowedApproval = await connected({ ...common, "GET /repos/example/company/pulls/4/reviews": [{ user: { login: "reviewer" }, state: "APPROVED" }] });
  await assert.rejects(allowedApproval.provider.invoke("company.change.merge", { pullRequestNumber: 4, expectedHeadSha: sha("b") }, { permissions: ["company_change.merge"] }), error => error.code === "github_merge_checks_required");
});

test("governed merge binds exact head, approval, and passing check", async () => {
  let pulls = 0;
  const routes = {
    "GET /repos/example/company/pulls/5": () => (++pulls === 1 ? { number: 5, merged: false, head: { sha: sha("b") } } : { number: 5, merged: true, merged_at: "2026-08-24T12:00:00Z", merge_commit_sha: sha("m") }),
    "GET /repos/example/company/pulls/5/reviews": [{ user: { login: "reviewer" }, state: "APPROVED" }],
    [`GET /repos/example/company/commits/${sha("b")}/check-runs`]: { check_runs: [{ name: "validate", status: "completed", conclusion: "success", html_url: "https://checks/1" }] },
    [`GET /repos/example/company/commits/${sha("b")}/status`]: { state: "success", statuses: [] },
    "PUT /repos/example/company/pulls/5/merge": { merged: true, sha: sha("m") },
  };
  const { provider, calls } = await connected(routes);
  const result = await provider.invoke("company.change.merge", { pullRequestNumber: 5, expectedHeadSha: sha("b") }, { permissions: ["company_change.merge"] });
  assert.equal(result.merged, true); assert.equal(result.alreadyMerged, false);
  assert.equal(result.mergeCommitSha, sha("m")); assert.deepEqual(result.approvedBy, ["reviewer"]);
  assert.equal(JSON.parse(calls.find(call => call.path.endsWith("/merge")).init.body).sha, sha("b"));
});

test("governed merge returns complete idempotent evidence for an already-merged exact head", async () => {
  const routes = { "GET /repos/example/company/pulls/9": { number: 9, merged: true, merged_at: "2026-08-25T09:39:09Z", merge_commit_sha: sha("m"), head: { sha: sha("b") } } };
  const { provider, calls } = await connected(routes);
  const result = await provider.invoke("company.change.merge", { pullRequestNumber: 9, expectedHeadSha: sha("b") }, { permissions: ["company_change.merge"] });
  assert.deepEqual({ merged: result.merged, alreadyMerged: result.alreadyMerged, mergeCommitSha: result.mergeCommitSha }, { merged: true, alreadyMerged: true, mergeCommitSha: sha("m") });
  assert.equal(calls.some(call => call.path.endsWith("/merge")), false);
});

test("governed merge accepts successful and skipped check runs when no legacy status contexts exist", async () => {
  let pulls = 0;
  const routes = {
    "GET /repos/example/company/pulls/8": () => (++pulls === 1 ? { number: 8, merged: false, head: { sha: sha("b") } } : { number: 8, merged: true, merged_at: "2026-08-25T09:00:00Z", merge_commit_sha: sha("m") }),
    "GET /repos/example/company/pulls/8/reviews": [{ user: { login: "reviewer" }, state: "APPROVED" }],
    [`GET /repos/example/company/commits/${sha("b")}/check-runs`]: { check_runs: [{ name: "validate", status: "completed", conclusion: "success" }, { name: "reconcile", status: "completed", conclusion: "skipped" }] },
    [`GET /repos/example/company/commits/${sha("b")}/status`]: { state: "pending", statuses: [] },
    "PUT /repos/example/company/pulls/8/merge": { merged: true, sha: sha("m") },
  };
  const { provider } = await connected(routes);
  const result = await provider.invoke("company.change.merge", { pullRequestNumber: 8, expectedHeadSha: sha("b") }, { permissions: ["company_change.merge"] });
  assert.equal(result.status, "merged");
  assert.equal(result.checks.state, "success");
  assert.equal(result.checks.total, 2);
});

test("governed merge accepts only the configured successful GitHub Actions approval check", async () => {
  let pulls = 0;
  const routes = {
    "GET /repos/example/company/pulls/6": () => (++pulls === 1 ? { number: 6, merged: false, head: { sha: sha("b") } } : { number: 6, merged: true, merged_at: "2026-08-24T12:00:00Z" }),
    "GET /repos/example/company/pulls/6/reviews": [],
    [`GET /repos/example/company/commits/${sha("b")}/check-runs`]: { check_runs: [{ name: "governed-company-change-approval", app: { slug: "github-actions" }, status: "completed", conclusion: "success", html_url: "https://checks/approval" }] },
    [`GET /repos/example/company/commits/${sha("b")}/status`]: { state: "success", statuses: [] },
    "PUT /repos/example/company/pulls/6/merge": { merged: true, sha: sha("m") },
  };
  const mergePolicy = { requireApproval: true, requirePassingChecks: true, mergeMethod: "squash", trustedApprovalChecks: [{ name: "governed-company-change-approval", appSlug: "github-actions" }] };
  const { provider } = await connected(routes, { mergePolicy });
  const result = await provider.invoke("company.change.merge", { pullRequestNumber: 6, expectedHeadSha: sha("b") }, { permissions: ["company_change.merge"] });
  assert.deepEqual(result.trustedApprovals, ["github-actions:governed-company-change-approval"]);
});
