import { GitHubProvider } from "../runtime/github-provider.mjs";

if (process.env.OMNISEED_GITHUB_IDENTITY_LIVE !== "1") {
  throw new Error("Set OMNISEED_GITHUB_IDENTITY_LIVE=1 to run the read-only identity acceptance test");
}

const args = new Map();
for (let index = 2; index < process.argv.length; index += 2) args.set(process.argv[index], process.argv[index + 1]);
const repository = args.get("--repo"), login = args.get("--login");
const token = process.env.GH_TOKEN || process.env.GITHUB_TOKEN;
if (!repository || !login) throw new Error("Identity acceptance requires --repo and --login");
if (!token) throw new Error("Identity acceptance requires GH_TOKEN or GITHUB_TOKEN");

// Identity reference observation needs only the scoped repository API. It must not
// depend on `/user`, which is unavailable to some valid GitHub App credentials.
const provider = new GitHubProvider({ configuration: { repository }, token });
const action = {
  id: "live-repository-collaborator-observation",
  family: "identity",
  resourceId: "contributor_identities",
  desired: { offers: ["contributor_identity"], spec: { kind: "repository_collaborator", repository, login } },
};
const validation = await provider.validate(action);
if (!validation.valid) throw new Error(`Identity fixture is invalid: ${validation.issues.map(issue => issue.code).join(", ")}`);
const plan = await provider.plan(action);
if (plan.mode !== "observe_only" || plan.mutation !== false) throw new Error("Identity lifecycle is not safely observation-only");
const observation = await provider.observe(await provider.apply(action));
const evidence = observation.evidence[0];
if (evidence.kind !== "repository_collaborator" || evidence.repository !== repository || evidence.login.toLowerCase() !== login.toLowerCase() || !evidence.subjectId || !evidence.permission) throw new Error("GitHub did not return the declared collaborator evidence");

process.stdout.write(`${JSON.stringify({ provider: "github", lifecycle: { validation: "valid", plan: "observe_only", apply: "non_mutating_bind", observation: observation.status }, evidence })}\n`);
