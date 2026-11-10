"""Read-only Railway diagnostics for one explicitly configured project/service.

API tokens stay on the MCP gateway; they never enter E2B workspaces or tool output.
"""
from __future__ import annotations

import json
import os
import re
from urllib.request import Request, urlopen

API_URL = "https://backboard.railway.com/graphql/v2"
UUID = re.compile(r"^[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}$")
SENSITIVE = [
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)(project-access-token:\s*)[^\s]+"),
    re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*)[^\s]+"),
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{12,})\b"),
]


def redact(message: str) -> str:
    value = str(message)[:3000]
    for pattern in SENSITIVE:
        value = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[redacted]", value)
    return value


class RailwayInspector:
    def __init__(self, environ=None, open_url=None):
        self.env = environ if environ is not None else os.environ
        self.open_url = open_url or urlopen

    @property
    def configured(self) -> bool:
        return bool(
            (self.env.get("RAILWAY_PROJECT_TOKEN") or self.env.get("RAILWAY_API_TOKEN"))
            and all(self.env.get("RAILWAY_" + part + "_ID") for part in
                    ("PROJECT", "SERVICE", "ENVIRONMENT"))
        )

    def _scope(self) -> dict:
        if not self.configured:
            raise RuntimeError(
                "Railway read-only diagnostics need a scoped project token and "
                "RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID, RAILWAY_ENVIRONMENT_ID on the gateway."
            )
        values = {key: self.env["RAILWAY_" + key.upper() + "_ID"]
                  for key in ("project", "service", "environment")}
        if any(not UUID.fullmatch(value) for value in values.values()):
            raise ValueError("The configured Railway scope must use valid UUID identifiers.")
        return {key + "Id": value for key, value in values.items()}

    def _query(self, query: str, variables: dict) -> dict:
        self._scope()
        headers = {"Content-Type": "application/json"}
        if self.env.get("RAILWAY_PROJECT_TOKEN"):
            headers["Project-Access-Token"] = self.env["RAILWAY_PROJECT_TOKEN"]
        else:
            headers["Authorization"] = "Bearer " + self.env["RAILWAY_API_TOKEN"]
        request = Request(
            API_URL,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with self.open_url(request, timeout=15) as response:
                result = json.loads(response.read(512_000))
        except Exception as error:
            # Do not include exception text: transport errors can include headers.
            raise RuntimeError("Railway API connection failed (" + type(error).__name__ + ").") from None
        if result.get("errors"):
            codes = [e.get("extensions", {}).get("code", "query-error")
                     for e in result["errors"][:3]]
            raise RuntimeError("Railway API rejected the query: " + ", ".join(codes))
        return result.get("data") or {}

    def deployments(self, limit: int = 10) -> dict:
        limit = max(1, min(int(limit), 50))
        data = self._query(
            """query($input: DeploymentListInput!, $first: Int) {
              deployments(input: $input, first: $first) {
                edges { node { id status createdAt } }
              }
            }""",
            {"input": self._scope(), "first": limit},
        )
        items = [edge["node"] for edge in
                 data.get("deployments", {}).get("edges", []) if "node" in edge]
        return {"configured": True, "scope": "one configured Railway service",
                "deployments": items, "returned": len(items)}

    def logs(self, deployment_id: str, kind: str = "build", limit: int = 100) -> dict:
        if not isinstance(deployment_id, str) or not UUID.fullmatch(deployment_id):
            raise ValueError("A valid deployment UUID is required.")
        if kind not in ("build", "runtime"):
            raise ValueError("Only build and runtime logs are exposed.")
        limit = max(1, min(int(limit), 200))
        # A provided deployment ID must belong to the configured service.
        known = {item["id"] for item in self.deployments(limit=50)["deployments"]}
        if deployment_id not in known:
            raise PermissionError("Deployment is not among recent deployments of this configured service.")
        field = "buildLogs" if kind == "build" else "deploymentLogs"
        data = self._query(
            """query($deploymentId: String!, $limit: Int) {
              FIELD(deploymentId: $deploymentId, limit: $limit) {
                timestamp message severity
              }
            }""".replace("FIELD", field),
            {"deploymentId": deployment_id, "limit": limit},
        )
        records = data.get(field, []) or []
        return {
            "deployment_id": deployment_id, "kind": kind, "returned": len(records),
            "logs": [{"timestamp": entry.get("timestamp"),
                      "severity": entry.get("severity"),
                      "message": redact(entry.get("message") or "")} for entry in records],
        }
