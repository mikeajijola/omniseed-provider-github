import importlib.util
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
        self.assertEqual(result["provider"]["id"], "github_protocol")
        self.assertEqual(result["provider"]["version"], "0.1.0-alpha.2")
        self.assertEqual(result["primitiveFamilies"], ["workflows"])
        self.assertEqual(result["offerings"][0]["family"], "workflows")
        self.assertEqual(result["operations"], MODULE.OPERATIONS)
        self.assertEqual(result["offerings"][0]["resource"]["spec"]["expectedBaseSha"], "base-1")

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

    def test_apply_writes_exact_approved_path_and_content(self):
        client = MutationClient()
        provider = MODULE.GitHubProvider(config(), client)
        action = {"family": "workflows", "resourceId": "github_company_change", "desired": {"spec": change_spec()}}
        result = provider.apply(action)
        tree_request = next(item for item in client.requests if item[0].endswith("git/trees") and item[1] == "POST")
        self.assertEqual(tree_request[2]["tree"], [{"path": "omniform.yaml", "mode": "100644", "type": "blob", "content": "{}\n"}])
        self.assertEqual(result["attributes"]["commitSha"], "commit-new")
        self.assertEqual(result["attributes"]["pullRequestNumber"], 7)


if __name__ == "__main__":
    unittest.main()
