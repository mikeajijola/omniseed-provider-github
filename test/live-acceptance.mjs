import { execFileSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseOmniform } from "../../omniseed-ecosystem/omniform/src/index.js";
import { connectStdioProvider, JsonStateStore, OmniSeed, ProviderRegistry } from "../../omniseed-ecosystem/omniseed/src/index.js";

if (process.env.OMNISEED_GITHUB_LIVE !== "1") throw new Error("Set OMNISEED_GITHUB_LIVE=1 to authorize sandbox mutation");

const args = parseArgs(process.argv.slice(2));
const repository = args.repo ?? "mikeajijola/omniseed-provider-github-sandbox";
if (repository !== "mikeajijola/omniseed-provider-github-sandbox") throw new Error(`Live acceptance is restricted to the approved sandbox, received ${repository}`);
const runId = args.runId ?? new Date().toISOString().replace(/[^0-9]/g, "").slice(0, 14);
if (!/^[a-zA-Z0-9_-]+$/.test(runId)) throw new Error("run-id must contain only letters, digits, underscore, or hyphen");

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const providerScript = resolve(root, "provider/github_provider.py");
const python = process.env.PYTHON ?? "python3";
const owner = { actorId: "acceptance_owner", permissions: ["plan.create", "plan.approve", "plan.apply", "state.reconcile"] };
const startedAt = new Date().toISOString();

const successConfig = changeConfig({ repository, runId, label: "success" });
let provider = await connect(successConfig);
const preChange = await provider.invoke("software.change.observe", {}, owner);
const declaration = company();
const successStatePath = resolve(root, `.acceptance-state/${runId}-success.json`);
const successStore = new JsonStateStore(successStatePath);
let engine = new OmniSeed({ store: successStore, providers: new ProviderRegistry().register(provider) });
const before = await engine.inspect(declaration);
const plan = await engine.plan(declaration, owner);
const approval = await engine.approve(plan, plan.actions.map(action => action.id), owner);
const applied = await engine.apply(declaration, plan, approval, owner);
await provider.shutdown();

// Reconnect a new process and reconcile from persisted resource identity.
provider = await connect(successConfig);
engine = new OmniSeed({ store: successStore, providers: new ProviderRegistry().register(provider) });
const reconciled = await engine.reconcile(declaration, owner);
const persisted = await successStore.load("github_provider_acceptance");
const postChange = await provider.invoke("software.change.evidence", applied.state.deployed[0].attributes, owner);
await provider.shutdown();

// Create and approve a second exact plan, then move main outside OmniSeed before apply.
const driftConfig = changeConfig({ repository, runId, label: "drift" });
const driftProvider = await connect(driftConfig);
const beforeDrift = await driftProvider.invoke("software.change.observe", {}, owner);
const driftStatePath = resolve(root, `.acceptance-state/${runId}-drift.json`);
const driftStore = new JsonStateStore(driftStatePath);
const driftEngine = new OmniSeed({ store: driftStore, providers: new ProviderRegistry().register(driftProvider) });
const driftPlan = await driftEngine.plan(declaration, owner);
const driftApproval = await driftEngine.approve(driftPlan, driftPlan.actions.map(action => action.id), owner);
const externalCommit = commitDirectlyToBase({
  repository,
  baseBranch: "main",
  expectedBaseSha: beforeDrift.baseSha,
  path: `fixtures/${runId}-external-drift.txt`,
  content: `external drift fixture for ${runId}\n`,
  message: `test: create external drift for ${runId}`
});
const validationAfterDrift = await driftProvider.validate(driftPlan.actions[0]);
let applyFailure;
try {
  await driftEngine.apply(declaration, driftPlan, driftApproval, owner);
  throw new Error("Expected external drift to reject apply");
} catch (error) {
  if (error.message === "Expected external drift to reject apply") throw error;
  applyFailure = { code: error.code ?? "error", message: error.message, details: error.details ?? {} };
}
const afterDrift = await driftProvider.invoke("software.change.observe", {}, owner);
const driftState = await driftStore.load("github_provider_acceptance");
await driftProvider.shutdown();

const deployment = applied.state.deployed[0];
const observation = reconciled.resources.find(resource => resource.id === "github_software_change")?.observed;
const evidence = {
  evidenceVersion: "1",
  runId,
  startedAt,
  completedAt: new Date().toISOString(),
  target: {
    repository,
    repositoryUrl: preChange.repositoryUrl,
    baseBranch: "main",
    baseSha: preChange.baseSha,
    branchProtection: preChange.branchProtection
  },
  governedChange: {
    capability: "software.change.manage",
    provider: "github_protocol",
    planId: plan.id,
    planHash: plan.hash,
    approval: { actorId: approval.actorId, approvedActionIds: approval.approvedActionIds, approvedAt: approval.approvedAt },
    createdBranch: deployment.attributes.branch,
    commitSha: deployment.attributes.commitSha,
    pullRequestNumber: deployment.attributes.pullRequestNumber,
    pullRequestUrl: deployment.attributes.pullRequestUrl,
    observedChecks: observation.evidence[0].checks,
    mergeability: observation.evidence[0].mergeability,
    mergeableState: observation.evidence[0].mergeableState,
    appliedAt: deployment.attributes.appliedAt,
    observedAt: observation.checkedAt
  },
  lifecycleEvidence: {
    capabilityBefore: before.capabilities[0].state,
    capabilityAfter: applied.registry.capabilities[0].state,
    capabilityAfterReconnectAndReconcile: reconciled.capabilities[0].state,
    persisted: {
      version: persisted.version,
      deployed: persisted.deployed.length,
      observed: persisted.observed.length,
      evidence: persisted.evidence.length,
      evidenceRecord: persisted.evidence[0]
    },
    postChangeSnapshot: postChange.snapshot
  },
  externalDriftTest: {
    planId: driftPlan.id,
    planHash: driftPlan.hash,
    approvedAt: driftApproval.approvedAt,
    expectedBaseSha: beforeDrift.baseSha,
    externalCommitSha: externalCommit.sha,
    actualBaseSha: afterDrift.baseSha,
    detectedDrift: validationAfterDrift.issues.find(issue => issue.code === "external_drift") ?? null,
    applyFailure,
    canonicalStateAfterRejectedApply: {
      version: driftState.version,
      deployed: driftState.deployed.length,
      observed: driftState.observed.length,
      evidence: driftState.evidence.length,
      planStatus: driftState.plans[0].status
    }
  }
};

assertEvidence(evidence);
await writeEvidence(evidence);
console.log(JSON.stringify({
  runId,
  repository,
  branch: evidence.governedChange.createdBranch,
  commitSha: evidence.governedChange.commitSha,
  pullRequestNumber: evidence.governedChange.pullRequestNumber,
  pullRequestUrl: evidence.governedChange.pullRequestUrl,
  checks: evidence.governedChange.observedChecks,
  drift: evidence.externalDriftTest.detectedDrift,
  evidence: `evidence/runs/${runId}.json`
}, null, 2));

async function connect(configuration) {
  return connectStdioProvider({
    command: python,
    args: [providerScript],
    expectedProviderId: "github_protocol",
    configuration,
    context: { companyId: "github_provider_acceptance" },
    startupTimeoutMs: 30000,
    requestTimeoutMs: 15000,
    onDiagnostic: chunk => process.stderr.write(chunk)
  });
}

function company() {
  return parseOmniform(`apiVersion: omniform.org/v1alpha1
kind: Company
metadata: { id: github_provider_acceptance, name: GitHub Provider Acceptance }
spec:
  providers: { systems: { provider: github_protocol } }
  capabilities:
    - id: software_change_manage
      name: Software Change Management
      requires: [{ id: software_change_manage, primitiveFamily: systems }]
  operations:
    - { id: get_capability, capability: software_change_manage, description: Inspect capability, input: {}, output: {}, mutation: false, permissions: [], approval: none, interfaces: [api, cli, agent, machine] }
`);
}

function changeConfig({ repository, runId, label }) {
  return {
    repository,
    baseBranch: "main",
    branch: `omniseed/${runId}-${label}`,
    fixturePath: `fixtures/${runId}-${label}.txt`,
    fixtureContent: `OmniSeed governed ${label} fixture for ${runId}\n`,
    commitMessage: `test: add governed ${label} fixture ${runId}`,
    pullRequestTitle: `OmniSeed governed change ${runId}`,
    pullRequestBody: `Disposable Provider Protocol v1 acceptance evidence. Run: ${runId}.`
  };
}

function ghApi(endpoint, method = "GET", body) {
  const command = ["api", endpoint, "--method", method];
  if (body !== undefined) command.push("--input", "-");
  const output = execFileSync("gh", command, { input: body === undefined ? undefined : JSON.stringify(body), encoding: "utf8", maxBuffer: 10 * 1024 * 1024 });
  return output.trim() ? JSON.parse(output) : {};
}

function commitDirectlyToBase({ repository, baseBranch, expectedBaseSha, path, content, message }) {
  const live = ghApi(`repos/${repository}/git/ref/heads/${baseBranch}`).object.sha;
  if (live !== expectedBaseSha) throw new Error(`Sandbox moved before drift setup: expected ${expectedBaseSha}, found ${live}`);
  const base = ghApi(`repos/${repository}/git/commits/${live}`);
  const tree = ghApi(`repos/${repository}/git/trees`, "POST", { base_tree: base.tree.sha, tree: [{ path, mode: "100644", type: "blob", content }] });
  const commit = ghApi(`repos/${repository}/git/commits`, "POST", { message, tree: tree.sha, parents: [live] });
  ghApi(`repos/${repository}/git/refs/heads/${baseBranch}`, "PATCH", { sha: commit.sha, force: false });
  return commit;
}

function assertEvidence(value) {
  if (!value.target.repository || !value.target.baseSha) throw new Error("Evidence lacks target repository/base SHA");
  for (const field of ["createdBranch", "commitSha", "pullRequestNumber", "pullRequestUrl", "observedChecks", "observedAt"]) if (value.governedChange[field] === undefined || value.governedChange[field] === null) throw new Error(`Evidence lacks governedChange.${field}`);
  if (!value.externalDriftTest.detectedDrift || value.externalDriftTest.canonicalStateAfterRejectedApply.deployed !== 0) throw new Error("External drift was not detected safely");
}

async function writeEvidence(value) {
  const runPath = resolve(root, `evidence/runs/${runId}.json`), latestPath = resolve(root, "evidence/latest.json");
  await mkdir(dirname(runPath), { recursive: true });
  const serialized = `${JSON.stringify(value, null, 2)}\n`;
  await writeFile(runPath, serialized); await writeFile(latestPath, serialized);
  JSON.parse(await readFile(runPath, "utf8"));
}

function parseArgs(values) {
  const result = {};
  for (let index = 0; index < values.length; index += 1) {
    if (values[index] === "--repo") result.repo = values[++index];
    else if (values[index] === "--run-id") result.runId = values[++index];
    else throw new Error(`Unknown argument: ${values[index]}`);
  }
  return result;
}
