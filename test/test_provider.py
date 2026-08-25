import importlib.util
import base64
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).parents[1] / "provider" / "github_provider.py"
SPEC = importlib.util.spec_from_file_location("github_provider", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeClient:
    def __init__(self, base_sha="base-1"):
        self.current = base_sha

    def authenticated_user(self):
        return "fixture-user"

    def api(self, endpoint, method="GET", body=None, allow_failure=False):
        if endpoint == "repos/example/sandbox": return {"html_url": "https://github.com/example/sandbox", "default_branch": "main"}
        if endpoint.endswith("git/ref/heads/main"): return {"object": {"sha": self.current}}
        if endpoint.endswith("branches/main/protection") and allow_failure: return None
        if "pulls?state=open" in endpoint: return []
        if endpoint.endswith("check-runs"): return {"total_count": 0, "check_runs": []}
        if "/status" in endpoint: return {"state": "pending"}
        raise AssertionError(endpoint)


class MutationClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.requests = []

    def api(self, endpoint, method="GET", body=None, allow_failure=False):
        self.requests.append((endpoint, method, body))
        if endpoint.endswith("git/refs") and method == "POST": return {"ref": body["ref"]}
        if endpoint.endswith("git/commits/base-1") and method == "GET": return {"tree": {"sha": "tree-base"}}
        if endpoint.endswith("git/trees") and method == "POST": return {"sha": "tree-new"}
        if endpoint.endswith("git/commits") and method == "POST": return {"sha": "commit-new"}
        if endpoint.endswith("git/refs/heads/omniseed/test") and method == "PATCH": return {"object": {"sha": body["sha"]}}
        if endpoint.endswith("pulls") and method == "POST": return {"number": 7, "html_url": "https://github.com/example/sandbox/pull/7"}
        return super().api(endpoint, method, body, allow_failure)


class MergeClient(FakeClient):
    def __init__(self, *, approved=True, checks="success", merged=False, api_failure=False, legacy_statuses=None, trusted=False):
        super().__init__()
        self.approved = approved
        self.check_state = checks
        self.merged = merged
        self.api_failure = api_failure
        self.legacy_statuses = legacy_statuses
        self.trusted = trusted
        self.merge_calls = 0

    def api(self, endpoint, method="GET", body=None, allow_failure=False):
        if endpoint.endswith("pulls/7"):
            return {"number": 7, "html_url": "https://github.com/example/sandbox/pull/7", "state": "closed" if self.merged else "open", "merged": self.merged, "merged_at": "2026-08-15T00:00:00Z" if self.merged else None, "merge_commit_sha": "merge-7" if self.merged else None, "mergeable": True, "mergeable_state": "clean", "head": {"sha": "head-7"}, "base": {"sha": "base-1"}}
        if endpoint.endswith("pulls/7/reviews"):
            return [{"state": "APPROVED", "user": {"login": "reviewer"}}] if self.approved else []
        if endpoint.endswith("commits/head-7/check-runs"):
            conclusion = None if self.check_state == "pending" else self.check_state
            name = "governed-company-change-approval" if self.trusted else "conformance"
            app = {"slug": "github-actions" if self.trusted else "other-app"}
            return {"total_count": 1, "check_runs": [{"name": name, "app": app, "status": "completed" if conclusion else "in_progress", "conclusion": conclusion, "html_url": "https://checks/1"}]}
        if endpoint.endswith("commits/head-7/status"):
            statuses = self.legacy_statuses if self.legacy_statuses is not None else [{"context": "legacy", "state": self.check_state, "target_url": "https://status/1"}]
            return {"state": self.check_state if statuses else "pending", "statuses": statuses}
        if endpoint.endswith("pulls/7/merge") and method == "PUT":
            self.merge_calls += 1
            if self.api_failure: raise MODULE.GitHubError("merge failed", {"status": 500})
            self.merged = True
            return {"merged": True, "sha": "merge-7", "message": "Pull Request successfully merged"}
        return super().api(endpoint, method, body, allow_failure)


def config():
    return {
        "repository": "example/sandbox", "baseBranch": "main", "branchPrefix": "omniseed/"
    }


def change_spec():
    return {
        **config(), "expectedBaseSha": "base-1", "branch": "omniseed/test",
        "path": "omniform.yaml", "content": "{}\n", "commitMessage": "fixture",
        "pullRequestTitle": "Fixture"
    }


class ProviderTests(unittest.TestCase):
    def test_initialization_advertises_capability_semantics_and_observed_base(self):
        provider = MODULE.GitHubProvider(config(), FakeClient())
        result = provider.initialize({"protocolVersion": MODULE.PROTOCOL, "configuration": config(), "context": {"companyId": "test"}})
        self.assertEqual(result["provider"]["id"], "github")
        self.assertEqual(result["provider"]["version"], "0.1.0-alpha.5")
        self.assertEqual(result["primitiveFamilies"], ["workflows", "connectors", "identity"])
        self.assertEqual(result["offerings"][0]["family"], "workflows")
        self.assertEqual(result["operations"], MODULE.OPERATIONS)
        self.assertEqual(result["offerings"][0]["resource"]["spec"]["expectedBaseSha"], "base-1")

    def test_one_github_provider_supplies_repository_connector_and_identity_contracts(self):
        provider = MODULE.GitHubProvider(config(), FakeClient())
        connector = {"id": "connector-1", "family": "connectors", "resourceId": "github_repositories", "desired": {"offers": ["repository_access", "public_repository_access"]}}
        identity = {"id": "identity-1", "family": "identity", "resourceId": "operator_identities", "desired": {"offers": ["operator_identity"]}}
        for action in [connector, identity]:
            self.assertTrue(provider.validate(action)["valid"])
            applied = provider.apply(action)
            self.assertEqual(applied["attributes"]["family"], action["family"])
            observed = provider.observe(applied)
            self.assertEqual(observed["status"], "healthy")
            self.assertEqual(observed["evidence"][0]["source"], "github")

    def test_github_does_not_claim_steward_identity(self):
        provider = MODULE.GitHubProvider(config(), FakeClient())
        action = {"id": "identity-1", "family": "identity", "resourceId": "lily_identity", "desired": {"offers": ["steward_identity"]}}
        result = provider.validate(action)
        self.assertFalse(result["valid"])
        self.assertEqual(result["issues"][0]["code"], "unsupported_offering")

    def test_initialization_retains_alpha_1_fixture_configuration_as_action_fields(self):
        legacy = {**config(), "branch": "omniseed/legacy", "fixturePath": "fixture.txt", "fixtureContent": "legacy\n", "commitMessage": "legacy", "pullRequestTitle": "Legacy"}
        provider = MODULE.GitHubProvider(legacy, FakeClient())
        result = provider.initialize({"protocolVersion": MODULE.PROTOCOL, "configuration": legacy, "context": {"companyId": "test"}})
        spec = result["offerings"][0]["resource"]["spec"]
        self.assertEqual(spec["path"], "fixture.txt")
        self.assertEqual(spec["content"], "legacy\n")

    def test_validation_detects_external_base_drift(self):
        client = FakeClient("base-2")
        provider = MODULE.GitHubProvider(config(), client)
        action = {"family": "workflows", "resourceId": "github_company_change", "desired": {"spec": change_spec()}}
        result = provider.validate(action)
        self.assertFalse(result["valid"])
        self.assertEqual(result["issues"][0]["code"], "external_drift")
        self.assertEqual(result["issues"][0]["actualBaseSha"], "base-2")

    def test_status_does_not_collapse_configuration_connection_and_health(self):
        provider = MODULE.GitHubProvider({}, FakeClient())
        self.assertEqual(provider.status(), {"implementation_available": True, "configured": False, "connected": False, "healthy": False, "identity": None})

    def test_validation_rejects_repository_branch_and_path_escape(self):
        provider = MODULE.GitHubProvider(config(), FakeClient())
        spec = {**change_spec(), "repository": "example/other", "branch": "outside/test", "path": "../secret"}
        action = {"family": "workflows", "resourceId": "github_company_change", "desired": {"spec": spec}}
        codes = {item["code"] for item in provider.validate(action)["issues"]}
        self.assertEqual(codes, {"repository_scope_mismatch", "branch_scope_mismatch", "invalid_path"})

    def test_repository_inspection_is_scoped_to_configuration(self):
        provider = MODULE.GitHubProvider(config(), FakeClient())
        result = provider.invoke("company.repository.inspect", {"repository": "example/sandbox", "baseBranch": "main"}, {"actorId": "engine"})
        self.assertEqual(result["baseSha"], "base-1")
        with self.assertRaises(MODULE.GitHubError):
            provider.invoke("company.repository.inspect", {"repository": "example/other"}, {"actorId": "engine"})

    def test_repository_inspection_returns_exact_canonical_document(self):
        class DocumentClient(FakeClient):
            def api(self, endpoint, method="GET", body=None, allow_failure=False):
                if "/contents/omniform.yaml?ref=main" in endpoint:
                    return {"sha": "blob-1", "encoding": "base64", "content": base64.b64encode(b"# canonical\nkind: Company\n").decode("ascii")}
                return super().api(endpoint, method, body, allow_failure)
        provider = MODULE.GitHubProvider(config(), DocumentClient())
        result = provider.invoke("company.repository.inspect", {"repository": "example/sandbox", "baseBranch": "main", "path": "omniform.yaml"}, {"actorId": "engine"})
        self.assertEqual(result["document"], {"path": "omniform.yaml", "sha": "blob-1", "content": "# canonical\nkind: Company\n"})

    def test_apply_writes_exact_approved_path_and_content(self):
        client = MutationClient()
        provider = MODULE.GitHubProvider(config(), client)
        action = {"family": "workflows", "resourceId": "github_company_change", "desired": {"spec": change_spec()}}
        result = provider.apply(action)
        tree_request = next(item for item in client.requests if item[0].endswith("git/trees") and item[1] == "POST")
        self.assertEqual(tree_request[2]["tree"], [{"path": "omniform.yaml", "mode": "100644", "type": "blob", "content": "{}\n"}])
        self.assertEqual(result["attributes"]["commitSha"], "commit-new")
        self.assertEqual(result["attributes"]["pullRequestNumber"], 7)

    def test_governed_merge_requires_actor_authority(self):
        provider = MODULE.GitHubProvider({**config(), "mergePolicy": {"requireApproval": True, "requirePassingChecks": True}}, MergeClient())
        with self.assertRaises(MODULE.GitHubError) as raised:
            provider.invoke("company.change.merge", {"pullRequestNumber": 7, "expectedHeadSha": "head-7"}, {"actorId": "lily", "permissions": ["company_change.propose"]})
        self.assertEqual(raised.exception.details["code"], "insufficient_authority")

    def test_governed_merge_requires_approval(self):
        provider = MODULE.GitHubProvider({**config(), "mergePolicy": {"requireApproval": True, "requirePassingChecks": True}}, MergeClient(approved=False))
        with self.assertRaises(MODULE.GitHubError) as raised:
            provider.invoke("company.change.merge", {"pullRequestNumber": 7, "expectedHeadSha": "head-7"}, {"actorId": "owner", "permissions": ["company_change.merge"]})
        self.assertEqual(raised.exception.details["code"], "approval_required")

    def test_governed_merge_accepts_exact_head_trusted_actions_approval_check(self):
        client = MergeClient(approved=False, trusted=True)
        policy = {"requireApproval": True, "requirePassingChecks": True, "trustedApprovalChecks": [{"name": "governed-company-change-approval", "appSlug": "github-actions"}]}
        result = MODULE.GitHubProvider({**config(), "mergePolicy": policy}, client).invoke("company.change.merge", {"pullRequestNumber": 7, "expectedHeadSha": "head-7"}, {"actorId": "owner", "permissions": ["company_change.merge"]})
        self.assertEqual(result["trustedApprovals"], ["github-actions:governed-company-change-approval"])

    def test_governed_merge_rejects_same_named_check_from_untrusted_app(self):
        client = MergeClient(approved=False, trusted=True)
        client.trusted = False
        policy = {"requireApproval": True, "requirePassingChecks": True, "trustedApprovalChecks": [{"name": "governed-company-change-approval", "appSlug": "github-actions"}]}
        with self.assertRaises(MODULE.GitHubError) as raised:
            MODULE.GitHubProvider({**config(), "mergePolicy": policy}, client).invoke("company.change.merge", {"pullRequestNumber": 7, "expectedHeadSha": "head-7"}, {"actorId": "owner", "permissions": ["company_change.merge"]})
        self.assertEqual(raised.exception.details["code"], "approval_required")

    def test_governed_merge_rejects_failing_or_pending_checks(self):
        for state in ["failure", "pending"]:
            with self.subTest(state=state):
                provider = MODULE.GitHubProvider({**config(), "mergePolicy": {"requireApproval": True, "requirePassingChecks": True}}, MergeClient(checks=state))
                with self.assertRaises(MODULE.GitHubError) as raised:
                    provider.invoke("company.change.merge", {"pullRequestNumber": 7, "expectedHeadSha": "head-7"}, {"actorId": "owner", "permissions": ["company_change.merge"]})
                self.assertEqual(raised.exception.details["code"], "checks_not_passing")

    def test_governed_merge_accepts_successful_check_runs_without_legacy_status_contexts(self):
        client = MergeClient(approved=False, checks="success", legacy_statuses=[])
        provider = MODULE.GitHubProvider({**config(), "mergePolicy": {"requireApproval": False, "requirePassingChecks": True}}, client)
        result = provider.invoke("company.change.merge", {"pullRequestNumber": 7, "expectedHeadSha": "head-7"}, {"actorId": "owner", "permissions": ["company_change.merge"]})
        self.assertTrue(result["merged"])
        self.assertEqual(result["checks"]["total"], 1)
        self.assertEqual(result["checks"]["legacyStatuses"], [])

    def test_governed_merge_is_idempotent_when_already_merged(self):
        client = MergeClient(merged=True)
        provider = MODULE.GitHubProvider({**config(), "mergePolicy": {"requireApproval": True, "requirePassingChecks": True}}, client)
        result = provider.invoke("company.change.merge", {"pullRequestNumber": 7, "expectedHeadSha": "head-7"}, {"actorId": "owner", "permissions": ["company_change.merge"]})
        self.assertTrue(result["merged"])
        self.assertTrue(result["alreadyMerged"])
        self.assertEqual(client.merge_calls, 0)

    def test_governed_merge_returns_merge_evidence(self):
        client = MergeClient()
        provider = MODULE.GitHubProvider({**config(), "mergePolicy": {"requireApproval": True, "requirePassingChecks": True}}, client)
        result = provider.invoke("company.change.merge", {"pullRequestNumber": 7, "expectedHeadSha": "head-7"}, {"actorId": "owner", "permissions": ["company_change.merge"]})
        self.assertEqual(result["mergeCommitSha"], "merge-7")
        self.assertEqual(result["approvedBy"], ["reviewer"])
        self.assertEqual(result["checks"]["state"], "success")
        self.assertEqual(client.merge_calls, 1)

    def test_governed_merge_propagates_api_failure_without_success_evidence(self):
        provider = MODULE.GitHubProvider({**config(), "mergePolicy": {"requireApproval": True, "requirePassingChecks": True}}, MergeClient(api_failure=True))
        with self.assertRaises(MODULE.GitHubError) as raised:
            provider.invoke("company.change.merge", {"pullRequestNumber": 7, "expectedHeadSha": "head-7"}, {"actorId": "owner", "permissions": ["company_change.merge"]})
        self.assertEqual(str(raised.exception), "merge failed")


if __name__ == "__main__":
    unittest.main()
