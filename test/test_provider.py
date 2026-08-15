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


def config():
    return {
        "repository": "example/sandbox", "baseBranch": "main", "branch": "omniseed/test",
        "fixturePath": "fixtures/test.txt", "fixtureContent": "fixture\n", "commitMessage": "fixture",
        "pullRequestTitle": "Fixture"
    }


class ProviderTests(unittest.TestCase):
    def test_initialization_advertises_capability_semantics_and_observed_base(self):
        provider = MODULE.GitHubProvider(config(), FakeClient())
        result = provider.initialize({"protocolVersion": MODULE.PROTOCOL, "configuration": config(), "context": {"companyId": "test"}})
        self.assertEqual(result["provider"]["id"], "github_protocol")
        self.assertEqual(result["provider"]["version"], "0.1.0-alpha.1")
        self.assertEqual(result["primitiveFamilies"], ["workflows"])
        self.assertEqual(result["offerings"][0]["family"], "workflows")
        self.assertEqual(result["operations"], MODULE.OPERATIONS)
        self.assertEqual(result["offerings"][0]["resource"]["spec"]["expectedBaseSha"], "base-1")

    def test_validation_detects_external_base_drift(self):
        client = FakeClient("base-2")
        provider = MODULE.GitHubProvider(config(), client)
        action = {"family": "workflows", "resourceId": "github_software_change", "desired": {"spec": {**config(), "expectedBaseSha": "base-1"}}}
        result = provider.validate(action)
        self.assertFalse(result["valid"])
        self.assertEqual(result["issues"][0]["code"], "external_drift")
        self.assertEqual(result["issues"][0]["actualBaseSha"], "base-2")

    def test_status_does_not_collapse_configuration_connection_and_health(self):
        provider = MODULE.GitHubProvider({}, FakeClient())
        self.assertEqual(provider.status(), {"implementation_available": True, "configured": False, "connected": False, "healthy": False, "identity": None})


if __name__ == "__main__":
    unittest.main()
