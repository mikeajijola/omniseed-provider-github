#!/usr/bin/env python3
"""Narrow GitHub Provider for OmniSeed Provider Protocol v1."""

import datetime
import json
import os
import subprocess
import sys
import time

PROTOCOL = "omniseed.provider.protocol/1.0"
METHODS = [
    "provider.initialize", "provider.status", "provider.validate", "provider.plan",
    "provider.apply", "provider.observe", "provider.invoke", "provider.shutdown"
]
OPERATIONS = ["software.change.observe", "software.change.status", "software.change.evidence"]


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class GitHubError(RuntimeError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


class GitHubClient:
    def __init__(self, executable=None):
        self.executable = executable or os.environ.get("GH_BIN", "gh")

    def api(self, endpoint, method="GET", body=None, allow_failure=False):
        command = [self.executable, "api", endpoint, "--method", method]
        payload = None
        if body is not None:
            command.extend(["--input", "-"])
            payload = json.dumps(body)
        result = subprocess.run(command, input=payload, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            if allow_failure:
                return None
            raise GitHubError("GitHub API request failed", {"endpoint": endpoint, "method": method, "diagnostic": result.stderr.strip()})
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError as error:
            raise GitHubError("GitHub returned invalid JSON", {"endpoint": endpoint}) from error

    def authenticated_user(self):
        return self.api("user")["login"]


class GitHubProvider:
    def __init__(self, configuration=None, client=None):
        self.configuration = configuration or {}
        self.client = client or GitHubClient()
        self.company_id = None

    @property
    def repository(self):
        return self.configuration.get("repository")

    @property
    def base_branch(self):
        return self.configuration.get("baseBranch", "main")

    def initialize(self, params):
        requested = params.get("protocolVersion")
        if requested != PROTOCOL:
            raise GitHubError("Unsupported protocol version", {"requested": requested, "supported": PROTOCOL})
        self.configuration = params.get("configuration") or {}
        self.company_id = (params.get("context") or {}).get("companyId")
        observed = self.observe_repository() if self.repository else None
        expected = self.configuration.get("expectedBaseSha") or (observed or {}).get("baseSha")
        resource = {
            "family": "workflows",
            "id": "github_software_change",
            "name": "GitHub Software Change",
            "offers": ["software_change_manage"],
            "risk": "medium",
            "spec": {
                "repository": self.repository,
                "baseBranch": self.base_branch,
                "expectedBaseSha": expected,
                "branch": self.configuration.get("branch"),
                "fixturePath": self.configuration.get("fixturePath"),
                "fixtureContent": self.configuration.get("fixtureContent"),
                "commitMessage": self.configuration.get("commitMessage"),
                "pullRequestTitle": self.configuration.get("pullRequestTitle"),
                "pullRequestBody": self.configuration.get("pullRequestBody")
            }
        }
        return {
            "protocolVersion": PROTOCOL,
            "provider": {"id": "github_protocol", "name": "GitHub Software Change Provider", "version": "0.1.0-alpha.1"},
            "primitiveFamilies": ["workflows"],
            "offerings": [{"family": "workflows", "id": "software_change_manage", "resource": resource}],
            "operations": OPERATIONS,
            "methods": METHODS
        }

    def status(self):
        configured = bool(self.repository and self.configuration.get("branch") and self.configuration.get("fixturePath"))
        connected = False
        healthy = False
        identity = None
        if configured:
            try:
                identity = self.client.authenticated_user()
                connected = self.client.api("repos/" + self.repository) is not None
                healthy = connected and self.base_sha() is not None
            except (GitHubError, KeyError):
                pass
        return {
            "implementation_available": True,
            "configured": configured,
            "connected": connected,
            "healthy": healthy,
            "identity": identity
        }

    def base_sha(self):
        ref = self.client.api(f"repos/{self.repository}/git/ref/heads/{self.base_branch}")
        return ref["object"]["sha"]

    def validate(self, action):
        issues = []
        spec = ((action or {}).get("desired") or {}).get("spec") or {}
        required = ["repository", "baseBranch", "expectedBaseSha", "branch", "fixturePath", "fixtureContent", "commitMessage", "pullRequestTitle"]
        for field in required:
            if not spec.get(field):
                issues.append({"code": "missing_field", "field": field, "message": f"{field} is required"})
        if action.get("family") != "workflows" or action.get("resourceId") != "github_software_change":
            issues.append({"code": "unsupported_action", "message": "Only the GitHub software change resource is supported"})
        if not issues:
            actual = self.base_sha()
            if actual != spec["expectedBaseSha"]:
                issues.append({
                    "code": "external_drift", "message": "Base branch changed after the approved action was created",
                    "expectedBaseSha": spec["expectedBaseSha"], "actualBaseSha": actual
                })
        return {"valid": not issues, "issues": issues}

    def plan(self, action):
        return {"deterministic": True, "actionId": action.get("id"), "externalPrecondition": ((action.get("desired") or {}).get("spec") or {}).get("expectedBaseSha")}

    def apply(self, action):
        validation = self.validate(action)
        if not validation["valid"]:
            raise GitHubError("Action is no longer valid", {"issues": validation["issues"]})
        spec = action["desired"]["spec"]
        repo = spec["repository"]
        base_sha = spec["expectedBaseSha"]
        branch = spec["branch"]
        self.client.api(f"repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": base_sha})
        base_commit = self.client.api(f"repos/{repo}/git/commits/{base_sha}")
        tree = self.client.api(f"repos/{repo}/git/trees", "POST", {
            "base_tree": base_commit["tree"]["sha"],
            "tree": [{"path": spec["fixturePath"], "mode": "100644", "type": "blob", "content": spec["fixtureContent"]}]
        })
        commit = self.client.api(f"repos/{repo}/git/commits", "POST", {
            "message": spec["commitMessage"], "tree": tree["sha"], "parents": [base_sha]
        })
        self.client.api(f"repos/{repo}/git/refs/heads/{branch}", "PATCH", {"sha": commit["sha"], "force": False})
        owner = repo.split("/", 1)[0]
        pull = self.client.api(f"repos/{repo}/pulls", "POST", {
            "title": spec["pullRequestTitle"], "body": spec.get("pullRequestBody", ""),
            "head": f"{owner}:{branch}", "base": spec["baseBranch"]
        })
        return {
            "providerResourceId": f"github://{repo}/pull/{pull['number']}",
            "status": "proposed",
            "attributes": {
                **spec,
                "baseSha": base_sha,
                "commitSha": commit["sha"],
                "pullRequestNumber": pull["number"],
                "pullRequestUrl": pull["html_url"],
                "appliedAt": now()
            }
        }

    def observe_repository(self, branch=None, commit_sha=None, pull_number=None):
        repo = self.repository
        repository = self.client.api(f"repos/{repo}")
        base_sha = self.base_sha()
        protection = self.client.api(f"repos/{repo}/branches/{self.base_branch}/protection", allow_failure=True)
        open_pulls = self.client.api(f"repos/{repo}/pulls?state=open&per_page=100")
        pull = None
        if pull_number:
            for _ in range(4):
                pull = self.client.api(f"repos/{repo}/pulls/{pull_number}")
                if pull.get("mergeable") is not None:
                    break
                time.sleep(0.5)
        target_sha = commit_sha or (pull or {}).get("head", {}).get("sha") or base_sha
        checks = self.client.api(f"repos/{repo}/commits/{target_sha}/check-runs")
        status = self.client.api(f"repos/{repo}/commits/{target_sha}/status")
        return {
            "repository": repo,
            "repositoryUrl": repository["html_url"],
            "defaultBranch": repository["default_branch"],
            "baseBranch": self.base_branch,
            "baseSha": base_sha,
            "branchProtection": {"enabled": protection is not None},
            "openPullRequests": [{"number": item["number"], "url": item["html_url"], "head": item["head"]["ref"], "base": item["base"]["ref"]} for item in open_pulls],
            "branch": branch,
            "commitSha": target_sha,
            "pullRequest": None if not pull else {
                "number": pull["number"], "url": pull["html_url"], "state": pull["state"],
                "mergeable": pull.get("mergeable"), "mergeableState": pull.get("mergeable_state"),
                "headSha": pull["head"]["sha"], "baseSha": pull["base"]["sha"]
            },
            "checks": {
                "state": status.get("state", "unknown"),
                "total": checks.get("total_count", 0),
                "runs": [{"name": item["name"], "status": item["status"], "conclusion": item.get("conclusion"), "url": item.get("html_url")} for item in checks.get("check_runs", [])]
            },
            "observedAt": now()
        }

    def observe(self, resource):
        attributes = resource.get("attributes") or {}
        snapshot = self.observe_repository(attributes.get("branch"), attributes.get("commitSha"), attributes.get("pullRequestNumber"))
        drift = snapshot["baseSha"] != attributes.get("expectedBaseSha")
        evidence = {
            "type": "software_change_state",
            "source": "github_protocol",
            "repository": snapshot["repository"],
            "baseSha": attributes.get("baseSha") or attributes.get("expectedBaseSha"),
            "currentBaseSha": snapshot["baseSha"],
            "branch": attributes.get("branch"),
            "commitSha": attributes.get("commitSha"),
            "pullRequestNumber": attributes.get("pullRequestNumber"),
            "pullRequestUrl": attributes.get("pullRequestUrl"),
            "checks": snapshot["checks"],
            "mergeability": (snapshot.get("pullRequest") or {}).get("mergeable"),
            "mergeableState": (snapshot.get("pullRequest") or {}).get("mergeableState"),
            "drift": drift,
            "observedAt": snapshot["observedAt"]
        }
        return {
            "status": "degraded" if drift else "healthy",
            "checkedAt": snapshot["observedAt"],
            "providerResourceId": resource.get("providerResourceId"),
            "evidence": [evidence],
            "snapshot": snapshot
        }

    def invoke(self, operation, input_value, actor):
        if operation not in OPERATIONS:
            raise GitHubError("Unsupported capability operation", {"operation": operation})
        attributes = input_value or {}
        snapshot = self.observe_repository(attributes.get("branch"), attributes.get("commitSha"), attributes.get("pullRequestNumber"))
        if operation == "software.change.evidence":
            return {"repository": self.repository, "snapshot": snapshot, "requestedBy": (actor or {}).get("actorId")}
        return snapshot


def respond(request_id, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": request_id}
    message["error" if error is not None else "result"] = error if error is not None else result
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    provider = GitHubProvider()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}
            if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
                respond(request_id, error={"code": -32600, "message": "Invalid Request"})
                continue
            try:
                if method == "provider.initialize": result = provider.initialize(params)
                elif method == "provider.status": result = provider.status()
                elif method == "provider.validate": result = provider.validate(params.get("action") or {})
                elif method == "provider.plan": result = provider.plan(params.get("action") or {})
                elif method == "provider.apply": result = provider.apply(params.get("action") or {})
                elif method == "provider.observe": result = provider.observe(params.get("resource") or {})
                elif method == "provider.invoke": result = provider.invoke(params.get("operation"), params.get("input"), params.get("actor"))
                elif method == "provider.shutdown": result = {"shutdown": True}
                else:
                    respond(request_id, error={"code": -32601, "message": "Method not found"})
                    continue
                respond(request_id, result=result)
                if method == "provider.shutdown": break
            except GitHubError as error:
                respond(request_id, error={"code": -32010, "message": str(error), "data": error.details})
            except Exception as error:
                print(f"GitHub Provider error: {error}", file=sys.stderr, flush=True)
                respond(request_id, error={"code": -32000, "message": str(error)})
        except json.JSONDecodeError:
            respond(None, error={"code": -32700, "message": "Parse error"})


if __name__ == "__main__":
    main()
