const API = "https://api.github.com";
const FAMILIES = ["workflows", "connectors", "identity"];
const OPERATIONS = [
  "software.change.observe", "software.change.status", "software.change.evidence",
  "company.repository.inspect", "repository.connector.observe", "identity.subject.inspect",
  "company.change.merge",
];
const IDENTITY_KIND = "repository_collaborator";
const IDENTITY_OFFERS = new Set(["contributor_identity"]);
const SECRET_FIELD = /token|secret|password|credential|authorization/i;

export class GitHubProviderError extends Error {
  constructor(code, message, details = {}) { super(message); this.name = "GitHubProviderError"; this.code = code; this.details = details; }
}

/** In-process GitHub Provider implementation for serverless OmniSeed runtimes. */
export class GitHubProvider {
  constructor({ configuration, token, fetchImpl = fetch, identity = null, status = null } = {}) {
    if (!configuration?.repository) throw new GitHubProviderError("github_configuration_invalid", "GitHub Provider requires a repository");
    if (!token) throw new GitHubProviderError("github_credential_unavailable", "GitHub Provider credential is unavailable");
    this.configuration = { baseBranch: "main", branchPrefix: "omniseed/", mergePolicy: { requireApproval: true, requirePassingChecks: true, mergeMethod: "squash" }, ...configuration };
    this.token = token;
    this.fetchImpl = fetchImpl;
    this.identity = identity;
    this.status = status ?? { implementation_available: true, configured: true, connected: false, healthy: false };
    this.metadata = {
      id: "github", name: "GitHub", organisation: "GitHub", version: "0.1.0-alpha.7",
      families: FAMILIES, operations: OPERATIONS,
      offerings: [
        { family: "workflows", id: "governed_change_process" },
        { family: "workflows", id: "software_change_manage" },
        { family: "workflows", id: "conformance_workflow" },
        { family: "connectors", id: "repository_access" },
        { family: "identity", id: "contributor_identity", products: ["Repository collaborators", "Apps/API"], lifecycle: ["validate", "plan", "bind", "observe", "evidence"], mutation: false },
      ],
    };
  }

  static async connect(options = {}) {
    const provider = new GitHubProvider(options);
    const [user] = await Promise.all([provider.#request("/user"), provider.#request(`/repos/${provider.configuration.repository}`)]);
    provider.identity = user.login;
    provider.status = { implementation_available: true, configured: true, connected: true, healthy: true };
    return provider;
  }

  async validate(action) {
    if (action?.family === "identity") return this.#validateIdentity(action);
    const spec = action?.desired?.spec ?? {};
    const issues = [];
    for (const field of ["repository", "baseBranch", "expectedBaseSha", "branch", "path", "content", "commitMessage", "pullRequestTitle"]) {
      if (field === "content" ? !(field in spec) : !spec[field]) issues.push({ code: "missing_field", field, message: `${field} is required` });
    }
    if (action?.family !== "workflows" || !["github_software_change", "github_company_change"].includes(action?.resourceId)) issues.push({ code: "unsupported_action", message: "Only a governed GitHub change workflow is supported" });
    if (spec.repository !== this.configuration.repository || spec.baseBranch !== this.configuration.baseBranch) issues.push({ code: "repository_scope_mismatch", message: "Action repository and base branch must match Provider configuration" });
    if (spec.branch && !spec.branch.startsWith(this.configuration.branchPrefix)) issues.push({ code: "branch_scope_mismatch", message: `Change branches must start with ${this.configuration.branchPrefix}` });
    if (spec.path && (spec.path.startsWith("/") || spec.path.split("/").includes(".."))) issues.push({ code: "invalid_path", message: "Change path must be repository-relative and cannot traverse parents" });
    if (!issues.length) {
      const actualBaseSha = await this.#baseSha();
      if (actualBaseSha !== spec.expectedBaseSha) issues.push({ code: "external_drift", message: "Base branch changed after the approved action was created", expectedBaseSha: spec.expectedBaseSha, actualBaseSha });
    }
    return { valid: issues.length === 0, issues };
  }

  async plan(action) {
    if (action?.family === "identity") return { deterministic: true, actionId: action.id, mode: "observe_only", mutation: false, kind: IDENTITY_KIND };
    return { deterministic: true, actionId: action.id, externalPrecondition: action?.desired?.spec?.expectedBaseSha };
  }

  async apply(action) {
    const validation = await this.validate(action);
    if (!validation.valid) throw new GitHubProviderError("github_action_invalid", "Action is no longer valid", { issues: validation.issues });
    const spec = action.desired.spec;
    if (action.family === "identity") return { providerResourceId: `github://${spec.repository}/identity/${action.resourceId}`, status: "bound", attributes: { family: "identity", resourceId: action.resourceId, offers: [...(action.desired.offers ?? [])], kind: IDENTITY_KIND, repository: spec.repository, login: spec.login, product: "Repository collaborators" } };
    const repo = spec.repository;
    await this.#request(`/repos/${repo}/git/refs`, { method: "POST", body: { ref: `refs/heads/${spec.branch}`, sha: spec.expectedBaseSha } });
    const base = await this.#request(`/repos/${repo}/git/commits/${spec.expectedBaseSha}`);
    const tree = await this.#request(`/repos/${repo}/git/trees`, { method: "POST", body: { base_tree: base.tree.sha, tree: [{ path: spec.path, mode: "100644", type: "blob", content: spec.content }] } });
    const commit = await this.#request(`/repos/${repo}/git/commits`, { method: "POST", body: { message: spec.commitMessage, tree: tree.sha, parents: [spec.expectedBaseSha] } });
    await this.#request(`/repos/${repo}/git/refs/heads/${encodeURIComponent(spec.branch)}`, { method: "PATCH", body: { sha: commit.sha, force: false } });
    const pull = await this.#request(`/repos/${repo}/pulls`, { method: "POST", body: { title: spec.pullRequestTitle, body: spec.pullRequestBody ?? "", head: spec.branch, base: spec.baseBranch } });
    return { providerResourceId: `github://${repo}/pull/${pull.number}`, status: "proposed", attributes: { ...spec, baseSha: spec.expectedBaseSha, commitSha: commit.sha, pullRequestNumber: pull.number, pullRequestUrl: pull.html_url, appliedAt: new Date().toISOString() } };
  }

  async observe(resource) {
    const attributes = resource?.attributes ?? {};
    if (attributes.family === "identity") {
      const evidence = await this.#observeIdentity(attributes);
      return { status: "healthy", checkedAt: evidence.observedAt, providerResourceId: resource.providerResourceId, snapshot: evidence, evidence: [evidence] };
    }
    const snapshot = await this.#observeRepository({ branch: attributes.branch, commitSha: attributes.commitSha, pullRequestNumber: attributes.pullRequestNumber });
    const merged = Boolean(snapshot.pullRequest?.merged);
    return {
      status: merged ? "merged" : snapshot.pullRequest?.state ?? "observed",
      checkedAt: snapshot.observedAt,
      providerResourceId: resource.providerResourceId,
      snapshot,
      drift: snapshot.baseSha !== attributes.expectedBaseSha && !merged ? [{ type: "base_revision_changed", expected: attributes.expectedBaseSha, observed: snapshot.baseSha }] : [],
      evidence: [{ type: "software_change_state", source: "github", repository: snapshot.repository, baseSha: attributes.baseSha ?? attributes.expectedBaseSha, currentBaseSha: snapshot.baseSha, branch: attributes.branch, commitSha: attributes.commitSha, pullRequestNumber: attributes.pullRequestNumber, pullRequestUrl: attributes.pullRequestUrl, checks: snapshot.checks, merged }],
    };
  }

  async invoke(operation, input, actor = {}) {
    if (!OPERATIONS.includes(operation)) throw new GitHubProviderError("github_operation_unsupported", `Unsupported GitHub Provider operation: ${operation}`);
    if (operation === "company.repository.inspect") return this.#inspectRepository(input);
    if (operation === "company.change.merge") return this.#merge(input, actor);
    if (operation === "identity.subject.inspect") return this.#observeIdentity(input ?? {});
    return this.#observeRepository(input ?? {});
  }

  async #inspectRepository(input) {
    this.#assertScope(input);
    const baseSha = await this.#baseSha();
    let document = null;
    if (input.path) {
      const file = await this.#request(`/repos/${this.configuration.repository}/contents/${encodePath(input.path)}?ref=${encodeURIComponent(input.baseBranch)}`);
      document = { path: input.path, content: Buffer.from(file.content, "base64").toString("utf8"), sha: file.sha };
    }
    return { repository: this.configuration.repository, baseBranch: this.configuration.baseBranch, baseSha, document, observedAt: new Date().toISOString() };
  }

  #validateIdentity(action) {
    const desired = action?.desired ?? {}, spec = desired.spec ?? {}, issues = [];
    const unsupported = (desired.offers ?? []).filter(offer => !IDENTITY_OFFERS.has(offer)).sort();
    if (!action.resourceId) issues.push({ code: "missing_field", field: "resourceId", message: "resourceId is required" });
    if (unsupported.length) issues.push({ code: "unsupported_offering", message: "GitHub does not supply the requested offering", offerings: unsupported });
    const secretFields = findSecretFields(desired);
    if (secretFields.length) issues.push({ code: "secret_field_forbidden", message: "Identity desired state must not contain credentials or secrets", fields: secretFields });
    if (spec.kind !== IDENTITY_KIND) issues.push({ code: "unsupported_identity_kind", message: "GitHub supplies only non-mutating repository collaborator references; select another Provider for this identity kind", requestedKind: spec.kind, supportedKinds: [IDENTITY_KIND] });
    if (typeof spec.login !== "string" || !spec.login.trim()) issues.push({ code: "missing_field", field: "spec.login", message: "spec.login is required" });
    if (spec.repository !== this.configuration.repository) issues.push({ code: "repository_scope_mismatch", message: "Identity repository must match Provider configuration" });
    return { valid: issues.length === 0, issues };
  }

  async #observeIdentity(attributes) {
    if (attributes.kind !== IDENTITY_KIND || !attributes.login) throw new GitHubProviderError("github_identity_kind_unsupported", "GitHub supplies only repository collaborator identity references", { supportedKinds: [IDENTITY_KIND] });
    const repository = attributes.repository ?? this.configuration.repository;
    if (repository !== this.configuration.repository) throw new GitHubProviderError("github_repository_scope_mismatch", "Identity observation is outside configured Provider scope");
    const result = await this.#request(`/repos/${repository}/collaborators/${encodeURIComponent(attributes.login)}/permission`), user = result.user ?? {};
    return { type: "github_identity_observation", source: "github", provider: "github", resourceId: attributes.resourceId, kind: IDENTITY_KIND, repository, login: user.login ?? attributes.login, subjectId: user.id, subjectType: user.type, permission: result.permission, roleName: result.role_name, profileUrl: user.html_url, observedAt: new Date().toISOString() };
  }

  async #merge(input, actor) {
    if (!(actor?.permissions ?? []).includes("company_change.merge")) throw new GitHubProviderError("authorization_denied", "Missing permissions: company_change.merge", { missing: ["company_change.merge"] });
    const number = Number(input?.pullRequestNumber);
    if (!Number.isInteger(number) || number < 1 || !/^[0-9a-f]{40}$/.test(input?.expectedHeadSha ?? "")) throw new GitHubProviderError("github_merge_invalid", "Merge requires pullRequestNumber and exact expectedHeadSha");
    const repo = this.configuration.repository;
    const pull = await this.#request(`/repos/${repo}/pulls/${number}`);
    if (pull.merged) return { merged: true, alreadyMerged: true, status: "already_merged", mergeCommitSha: pull.merge_commit_sha, mergedAt: pull.merged_at, approvedBy: [], checks: null };
    if (pull.head?.sha !== input.expectedHeadSha) throw new GitHubProviderError("github_merge_head_changed", "Pull request head no longer matches the approved submission", { expected: input.expectedHeadSha, observed: pull.head?.sha });
    const [reviews, checks, combined] = await Promise.all([
      this.#request(`/repos/${repo}/pulls/${number}/reviews`),
      this.#request(`/repos/${repo}/commits/${input.expectedHeadSha}/check-runs`),
      this.#request(`/repos/${repo}/commits/${input.expectedHeadSha}/status`),
    ]);
    const approvedBy = latestApprovals(reviews);
    const policy = this.configuration.mergePolicy;
    const summary = checkSummary(checks, combined);
    const trustedApprovals = trustedApprovalChecks(checks, policy.trustedApprovalChecks);
    if (policy.requireApproval && approvedBy.length === 0 && trustedApprovals.length === 0) throw new GitHubProviderError("github_merge_approval_required", "Required pull request approval is missing");
    if (policy.requirePassingChecks && (!summary.total || summary.state !== "success")) throw new GitHubProviderError("github_merge_checks_required", "Required non-empty checks are not passing", { checks: summary });
    const result = await this.#request(`/repos/${repo}/pulls/${number}/merge`, { method: "PUT", body: { sha: input.expectedHeadSha, merge_method: policy.mergeMethod } });
    if (!result.merged) throw new GitHubProviderError("github_merge_failed", "GitHub did not merge the approved pull request");
    const observed = await this.#request(`/repos/${repo}/pulls/${number}`);
    return { merged: true, alreadyMerged: false, status: "merged", mergeCommitSha: result.sha, mergedAt: observed.merged_at, approvedBy, trustedApprovals, checks: summary };
  }

  async #observeRepository({ branch = null, commitSha = null, pullRequestNumber = null } = {}) {
    const repo = this.configuration.repository;
    const [repository, baseSha] = await Promise.all([this.#request(`/repos/${repo}`), this.#baseSha()]);
    const pull = pullRequestNumber ? await this.#request(`/repos/${repo}/pulls/${pullRequestNumber}`) : null;
    const targetSha = commitSha ?? pull?.head?.sha ?? baseSha;
    const [checks, combined] = await Promise.all([this.#request(`/repos/${repo}/commits/${targetSha}/check-runs`), this.#request(`/repos/${repo}/commits/${targetSha}/status`)]);
    return { repository: repo, repositoryUrl: repository.html_url, defaultBranch: repository.default_branch, baseBranch: this.configuration.baseBranch, baseSha, branch, commitSha: targetSha, pullRequest: pull && { number: pull.number, url: pull.html_url, state: pull.state, merged: Boolean(pull.merged), mergedAt: pull.merged_at, mergeCommitSha: pull.merge_commit_sha, mergeable: pull.mergeable, mergeableState: pull.mergeable_state, headSha: pull.head.sha, baseSha: pull.base.sha }, checks: checkSummary(checks, combined), observedAt: new Date().toISOString() };
  }

  #assertScope(input) {
    if (input?.repository !== this.configuration.repository || input?.baseBranch !== this.configuration.baseBranch) throw new GitHubProviderError("github_repository_scope_mismatch", "Repository inspection is outside configured Provider scope");
  }
  async #baseSha() { return (await this.#request(`/repos/${this.configuration.repository}/git/ref/heads/${encodeURIComponent(this.configuration.baseBranch)}`)).object.sha; }
  async #request(path, { method = "GET", body } = {}) {
    const response = await this.fetchImpl(`${API}${path}`, { method, headers: { accept: "application/vnd.github+json", authorization: `Bearer ${this.token}`, "content-type": "application/json", "x-github-api-version": "2022-11-28" }, body: body === undefined ? undefined : JSON.stringify(body) });
    let payload = null;
    try { payload = await response.json(); } catch { /* normalized below */ }
    if (!response.ok) throw new GitHubProviderError("github_api_failed", "GitHub Provider request failed", { status: response.status, method, path });
    return payload ?? {};
  }
}

function encodePath(path) { return path.split("/").map(encodeURIComponent).join("/"); }
function findSecretFields(value, path = "desired", found = []) {
  if (Array.isArray(value)) value.forEach((child, index) => findSecretFields(child, `${path}[${index}]`, found));
  else if (value && typeof value === "object") for (const [key, child] of Object.entries(value)) SECRET_FIELD.test(key) ? found.push(`${path}.${key}`) : findSecretFields(child, `${path}.${key}`, found);
  return found.sort();
}
function latestApprovals(reviews = []) {
  const latest = new Map();
  for (const review of reviews) if (review.user?.login) latest.set(review.user.login, review.state);
  return [...latest].filter(([, state]) => state === "APPROVED").map(([login]) => login).sort();
}
function trustedApprovalChecks(checks = {}, requirements = []) {
  return (checks.check_runs ?? []).flatMap(run => requirements.some(requirement => run.name === requirement.name && run.app?.slug === requirement.appSlug && run.status === "completed" && run.conclusion === "success") ? [`${run.app.slug}:${run.name}`] : []).sort();
}
function checkSummary(checks = {}, combined = {}) {
  const runs = (checks.check_runs ?? []).map(item => ({ name: item.name, status: item.status, conclusion: item.conclusion ?? null, url: item.html_url ?? null }));
  const statuses = combined.statuses ?? [];
  const total = runs.length + statuses.length;
  const passingRuns = runs.every(item => item.status === "completed" && ["success", "neutral", "skipped"].includes(item.conclusion));
  const passingStatuses = statuses.every(item => item.state === "success");
  const legacyStatePassing = statuses.length === 0 || combined.state === "success";
  return { state: total && passingRuns && passingStatuses && legacyStatePassing ? "success" : combined.state ?? "unknown", total, runs };
}
