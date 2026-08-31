#!/usr/bin/env python3
"""Narrow GitHub Provider for OmniSeed Provider Protocol v1."""

import datetime
import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse

PROTOCOL = "omniseed.provider.protocol/1.0"
METHODS = [
    "provider.initialize", "provider.status", "provider.validate", "provider.plan",
    "provider.apply", "provider.observe", "provider.invoke", "provider.shutdown"
]
OPERATIONS = [
    "software.change.observe", "software.change.status", "software.change.evidence",
    "company.repository.inspect", "repository.connector.observe",
    "identity.subject.inspect", "company.change.merge"
]
FAMILIES = ["workflows", "connectors", "identity"]
VERSION = "0.1.0-alpha.7"
IDENTITY_KIND = "repository_collaborator"
IDENTITY_OFFERS = {"contributor_identity"}
SECRET_FIELD_PARTS = ("token", "secret", "password", "credential", "authorization")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def find_secret_fields(value, path="desired"):
    """Return paths for credential-shaped fields without ever returning their values."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if any(part in key.lower() for part in SECRET_FIELD_PARTS):
                found.append(child_path)
            else:
                found.extend(find_secret_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_secret_fields(child, f"{path}[{index}]"))
    return found


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

    def repository_collaborator(self, repository, login):
        return self.api(f"repos/{repository}/collaborators/{urllib.parse.quote(login, safe='')}/permission")


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
        workflow_resource = {
            "family": "workflows",
            "id": "github_software_change",
            "name": "GitHub Software Change",
            "offers": ["software_change_manage"],
            "risk": "medium",
            "spec": {
                "repository": self.repository,
                "baseBranch": self.base_branch,
                "expectedBaseSha": expected,
                "branchPrefix": self.configuration.get("branchPrefix", "omniseed/"),
                # Optional action fields retain the alpha.1 generated-plan fixture path.
                # Production Company Change supplies these fields in the exact approved action.
                "branch": self.configuration.get("branch"),
                "path": self.configuration.get("path") or self.configuration.get("fixturePath"),
                "content": self.configuration.get("content") if "content" in self.configuration else self.configuration.get("fixtureContent"),
                "commitMessage": self.configuration.get("commitMessage"),
                "pullRequestTitle": self.configuration.get("pullRequestTitle"),
                "pullRequestBody": self.configuration.get("pullRequestBody")
            }
        }
        return {
            "protocolVersion": PROTOCOL,
            "provider": {"id": "github", "name": "GitHub", "organisation": "GitHub", "version": VERSION},
            "primitiveFamilies": FAMILIES,
            "offerings": [
                {"family": "workflows", "id": "software_change_manage", "products": ["Repositories", "Checks", "Rulesets", "Apps/API"], "resource": workflow_resource},
                {"family": "workflows", "id": "governed_change_process", "products": ["Repositories", "Pull Requests", "Checks"]},
                {"family": "workflows", "id": "conformance_workflow", "products": ["Actions", "Checks"]},
                {"family": "workflows", "id": "approved_desired_state_resolution", "products": ["Repositories", "Actions"]},
                {"family": "workflows", "id": "deterministic_reconciliation", "products": ["Actions"]},
                {"family": "connectors", "id": "repository_access", "products": ["Repositories", "Apps/API"]},
                {"family": "connectors", "id": "public_repository_access", "products": ["Repositories", "Apps/API"]},
                {"family": "identity", "id": "contributor_identity", "products": ["Repository collaborators", "Apps/API"], "lifecycle": ["validate", "plan", "bind", "observe", "evidence"], "mutation": False}
            ],
            "operations": OPERATIONS,
            "methods": METHODS
        }

    def status(self):
        configured = bool(self.repository and self.base_branch)
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
        family = action.get("family")
        if family in ["connectors", "identity"]:
            issues = []
            if not action.get("resourceId"):
                issues.append({"code": "missing_field", "field": "resourceId", "message": "resourceId is required"})
            desired = action.get("desired") or {}
            supported = {
                "connectors": {"repository_access", "public_repository_access"},
                "identity": IDENTITY_OFFERS
            }
            unsupported = sorted(set(desired.get("offers") or []) - supported[family])
            if unsupported:
                issues.append({"code": "unsupported_offering", "message": "GitHub does not supply the requested offering", "offerings": unsupported})
            if family == "identity":
                spec = desired.get("spec") or {}
                secret_fields = sorted(find_secret_fields(desired))
                if secret_fields:
                    issues.append({"code": "secret_field_forbidden", "message": "Identity desired state must not contain credentials or secrets", "fields": secret_fields})
                if spec.get("kind") != IDENTITY_KIND:
                    issues.append({"code": "unsupported_identity_kind", "message": "GitHub supplies only non-mutating repository collaborator references; select another Provider for this identity kind", "requestedKind": spec.get("kind"), "supportedKinds": [IDENTITY_KIND]})
                if not isinstance(spec.get("login"), str) or not spec.get("login", "").strip():
                    issues.append({"code": "missing_field", "field": "spec.login", "message": "spec.login is required"})
                if spec.get("repository") != self.repository:
                    issues.append({"code": "repository_scope_mismatch", "message": "Identity repository must match Provider configuration"})
            return {"valid": not issues, "issues": issues}
        issues = []
        spec = ((action or {}).get("desired") or {}).get("spec") or {}
        required = ["repository", "baseBranch", "expectedBaseSha", "branch", "path", "content", "commitMessage", "pullRequestTitle"]
        for field in required:
            if field == "content" and field in spec:
                continue
            if not spec.get(field):
                issues.append({"code": "missing_field", "field": field, "message": f"{field} is required"})
        if action.get("family") != "workflows" or action.get("resourceId") not in ["github_software_change", "github_company_change"]:
            issues.append({"code": "unsupported_action", "message": "Only a governed GitHub change workflow is supported"})
        if spec.get("repository") != self.repository or spec.get("baseBranch") != self.base_branch:
            issues.append({"code": "repository_scope_mismatch", "message": "Action repository and base branch must match Provider configuration"})
        prefix = self.configuration.get("branchPrefix", "omniseed/")
        if spec.get("branch") and not spec["branch"].startswith(prefix):
            issues.append({"code": "branch_scope_mismatch", "message": f"Change branches must start with {prefix}"})
        path = spec.get("path")
        if path and (path.startswith("/") or ".." in path.split("/")):
            issues.append({"code": "invalid_path", "message": "Change path must be repository-relative and cannot traverse parents"})
        if not issues:
            actual = self.base_sha()
            if actual != spec["expectedBaseSha"]:
                issues.append({
                    "code": "external_drift", "message": "Base branch changed after the approved action was created",
                    "expectedBaseSha": spec["expectedBaseSha"], "actualBaseSha": actual
                })
        return {"valid": not issues, "issues": issues}

    def plan(self, action):
        result = {"deterministic": True, "actionId": action.get("id"), "externalPrecondition": ((action.get("desired") or {}).get("spec") or {}).get("expectedBaseSha")}
        if action.get("family") == "identity":
            result.update({"mode": "observe_only", "mutation": False, "kind": IDENTITY_KIND})
        return result

    def apply(self, action):
        validation = self.validate(action)
        if not validation["valid"]:
            raise GitHubError("Action is no longer valid", {"issues": validation["issues"]})
        if action.get("family") in ["connectors", "identity"]:
            family = action["family"]
            attributes = {
                "family": family,
                "resourceId": action["resourceId"],
                "repository": self.repository,
                "baseBranch": self.base_branch,
                "offers": list((action.get("desired") or {}).get("offers") or [])
            }
            if family == "identity":
                spec = (action.get("desired") or {}).get("spec") or {}
                attributes.update({"kind": IDENTITY_KIND, "login": spec["login"], "product": "Repository collaborators"})
            return {"providerResourceId": f"github://{self.repository}/{family}/{action['resourceId']}", "status": "bound", "attributes": attributes}
        spec = action["desired"]["spec"]
        repo = spec["repository"]
        base_sha = spec["expectedBaseSha"]
        branch = spec["branch"]
        self.client.api(f"repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": base_sha})
        base_commit = self.client.api(f"repos/{repo}/git/commits/{base_sha}")
        tree = self.client.api(f"repos/{repo}/git/trees", "POST", {
            "base_tree": base_commit["tree"]["sha"],
            "tree": [{"path": spec["path"], "mode": "100644", "type": "blob", "content": spec["content"]}]
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
                "merged": bool(pull.get("merged")), "mergedAt": pull.get("merged_at"),
                "mergeCommitSha": pull.get("merge_commit_sha"),
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
        if attributes.get("family") == "connectors":
            snapshot = self.observe_repository()
            checked = snapshot["observedAt"]
            evidence = {"type": "github_repository_observation", "source": "github", "repository": snapshot["repository"], "baseBranch": snapshot["baseBranch"], "baseSha": snapshot["baseSha"], "reachable": True, "observedAt": checked}
            return {"status": "healthy", "checkedAt": checked, "providerResourceId": resource.get("providerResourceId"), "evidence": [evidence], "snapshot": snapshot}
        if attributes.get("family") == "identity":
            evidence = self.observe_identity(attributes)
            return {"status": "healthy", "checkedAt": evidence["observedAt"], "providerResourceId": resource.get("providerResourceId"), "evidence": [evidence], "snapshot": evidence}
        snapshot = self.observe_repository(attributes.get("branch"), attributes.get("commitSha"), attributes.get("pullRequestNumber"))
        merged = bool((snapshot.get("pullRequest") or {}).get("merged"))
        drift = snapshot["baseSha"] != attributes.get("expectedBaseSha") and not merged
        evidence = {
            "type": "software_change_state",
            "source": "github",
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
            "merged": merged,
            "mergedAt": (snapshot.get("pullRequest") or {}).get("mergedAt"),
            "mergeCommitSha": (snapshot.get("pullRequest") or {}).get("mergeCommitSha"),
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
        if operation == "company.change.merge":
            return self.merge_company_change(attributes, actor or {})
        if operation == "company.repository.inspect":
            requested_repository = attributes.get("repository", self.repository)
            requested_branch = attributes.get("baseBranch", self.base_branch)
            if requested_repository != self.repository or requested_branch != self.base_branch:
                raise GitHubError("Repository inspection is outside configured Provider scope", {
                    "repository": requested_repository, "baseBranch": requested_branch
                })
            result = self.observe_repository()
            path = attributes.get("path")
            if path:
                encoded_path = urllib.parse.quote(path, safe="/")
                document = self.client.api(f"repos/{self.repository}/contents/{encoded_path}?ref={urllib.parse.quote(self.base_branch, safe='')}")
                if document.get("encoding") != "base64" or not isinstance(document.get("content"), str):
                    raise GitHubError("Canonical company document is not a base64 Git blob", {"path": path})
                result["document"] = {"path": path, "sha": document.get("sha"), "content": base64.b64decode(document["content"]).decode("utf-8")}
            return result
        if operation == "repository.connector.observe":
            return self.observe_repository()
        if operation == "identity.subject.inspect":
            return self.observe_identity(attributes)
        snapshot = self.observe_repository(attributes.get("branch"), attributes.get("commitSha"), attributes.get("pullRequestNumber"))
        if operation == "software.change.evidence":
            return {"repository": self.repository, "snapshot": snapshot, "requestedBy": (actor or {}).get("actorId")}
        return snapshot

    def observe_identity(self, attributes):
        if attributes.get("kind") != IDENTITY_KIND or not attributes.get("login"):
            raise GitHubError("Unsupported identity kind", {"code": "unsupported_identity_kind", "supportedKinds": [IDENTITY_KIND]})
        repository = attributes.get("repository") or self.repository
        if repository != self.repository:
            raise GitHubError("Identity observation is outside configured Provider scope", {"code": "repository_scope_mismatch"})
        result = self.client.repository_collaborator(repository, attributes["login"])
        user = result.get("user") or {}
        return {
            "type": "github_identity_observation", "source": "github", "provider": "github",
            "resourceId": attributes.get("resourceId"), "kind": IDENTITY_KIND,
            "repository": repository, "login": user.get("login") or attributes["login"],
            "subjectId": user.get("id"), "subjectType": user.get("type"),
            "permission": result.get("permission"), "roleName": result.get("role_name"),
            "profileUrl": user.get("html_url"), "observedAt": now()
        }

    def merge_company_change(self, attributes, actor):
        permissions = actor.get("permissions") or []
        if "company_change.merge" not in permissions:
            raise GitHubError("Actor is not authorised to merge company changes", {"code": "insufficient_authority", "actorId": actor.get("actorId")})
        pull_number = attributes.get("pullRequestNumber")
        expected_head = attributes.get("expectedHeadSha")
        if not isinstance(pull_number, int) or not expected_head:
            raise GitHubError("Merge requires pullRequestNumber and expectedHeadSha", {"code": "invalid_merge_request"})
        pull = self.client.api(f"repos/{self.repository}/pulls/{pull_number}")
        if pull["head"]["sha"] != expected_head:
            raise GitHubError("Pull request head changed after approval", {"code": "external_drift", "expectedHeadSha": expected_head, "actualHeadSha": pull["head"]["sha"]})
        if pull.get("merged"):
            return {"merged": True, "alreadyMerged": True, "pullRequestNumber": pull_number, "pullRequestUrl": pull["html_url"], "mergeCommitSha": pull.get("merge_commit_sha"), "mergedAt": pull.get("merged_at")}
        policy = self.configuration.get("mergePolicy") or {}
        reviews = self.client.api(f"repos/{self.repository}/pulls/{pull_number}/reviews")
        approved_by = sorted({review["user"]["login"] for review in reviews if review.get("state") == "APPROVED" and review.get("user", {}).get("login")})
        checks = self.client.api(f"repos/{self.repository}/commits/{expected_head}/check-runs")
        status = self.client.api(f"repos/{self.repository}/commits/{expected_head}/status")
        check_runs = checks.get("check_runs", [])
        trusted = policy.get("trustedApprovalChecks") or []
        trusted_approvals = sorted({
            f"{item['app']['slug']}:{item['name']}"
            for item in check_runs
            for requirement in trusted
            if item.get("name") == requirement.get("name")
            and item.get("app", {}).get("slug") == requirement.get("appSlug")
            and item.get("status") == "completed"
            and item.get("conclusion") == "success"
        })
        if policy.get("requireApproval", True) and not approved_by and not trusted_approvals:
            raise GitHubError("Pull request lacks a required approval", {"code": "approval_required", "pullRequestNumber": pull_number})
        legacy_statuses = status.get("statuses", [])
        passing = bool(check_runs) and all(item.get("status") == "completed" and item.get("conclusion") in ["success", "neutral", "skipped"] for item in check_runs) and all(item.get("state") == "success" for item in legacy_statuses)
        check_evidence = {"state": status.get("state", "unknown"), "total": checks.get("total_count", 0), "runs": [{"name": item["name"], "status": item["status"], "conclusion": item.get("conclusion"), "url": item.get("html_url")} for item in check_runs], "legacyStatuses": [{"context": item.get("context"), "state": item.get("state"), "url": item.get("target_url")} for item in legacy_statuses]}
        if policy.get("requirePassingChecks", True) and not passing:
            raise GitHubError("Pull request checks are not passing", {"code": "checks_not_passing", "checks": check_evidence})
        result = self.client.api(f"repos/{self.repository}/pulls/{pull_number}/merge", "PUT", {"sha": expected_head, "merge_method": policy.get("mergeMethod", "squash")})
        if not result.get("merged"):
            raise GitHubError("GitHub did not merge the pull request", {"code": "merge_rejected", "message": result.get("message")})
        return {"merged": True, "alreadyMerged": False, "pullRequestNumber": pull_number, "pullRequestUrl": pull["html_url"], "mergeCommitSha": result.get("sha"), "mergedAt": now(), "approvedBy": approved_by, "trustedApprovals": trusted_approvals, "checks": check_evidence, "mergedBy": actor.get("actorId")}


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
