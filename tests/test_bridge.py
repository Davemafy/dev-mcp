"""Tests for owner-scoped diagnostics and browser journeys."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from browser_journey import browser_journey, validate_journey
from railway_diagnostics import RailwayInspector, redact

PROJECT = "11111111-1111-4111-8111-111111111111"
SERVICE = "22222222-2222-4222-8222-222222222222"
ENV = "33333333-3333-4333-8333-333333333333"
DEPLOY = "44444444-4444-4444-8444-444444444444"


def config(token_type="project"):
    env = {
        "RAILWAY_PROJECT_ID": PROJECT,
        "RAILWAY_SERVICE_ID": SERVICE,
        "RAILWAY_ENVIRONMENT_ID": ENV,
    }
    env["RAILWAY_PROJECT_TOKEN" if token_type == "project" else "RAILWAY_API_TOKEN"] = "test-secret"
    return env


class Response:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, count):
        return json.dumps(self.data).encode()[:count]


def fake_client(calls):
    def request(req, timeout=15):
        calls.append((dict(req.headers), json.loads(req.data)))
        query = calls[-1][1]["query"]
        if "deployments(" in query:
            return Response({"data": {"deployments": {"edges": [{"node": {
                "id": DEPLOY, "status": "SUCCESS", "createdAt": "2026-11-07"
            }}]}}})
        return Response({"data": {"buildLogs": [
            {"timestamp": "2026-11-07", "severity": "info",
             "message": "Authorization: Bearer sk-sensitive-secret-and-more"},
        ]}})
    return request


def test_scoped_railway_deployments_and_redacted_logs():
    calls = []
    inspector = RailwayInspector(config(), fake_client(calls))
    assert inspector.deployments()["deployments"][0]["id"] == DEPLOY
    details = inspector.logs(DEPLOY, "build")
    assert details["kind"] == "build"
    assert "sk-sensitive" not in details["logs"][0]["message"]
    assert "[redacted]" in details["logs"][0]["message"]
    assert calls[0][0]["Project-access-token"] == "test-secret"
    assert calls[0][1]["variables"]["input"]["projectId"] == PROJECT
    assert calls[1][1]["variables"]["input"]["serviceId"] == SERVICE


def test_prevents_cross_service_log_lookup():
    calls = []
    inspector = RailwayInspector(config(), fake_client(calls))
    with pytest.raises(PermissionError):
        inspector.logs("12345678-1234-1234-1234-123456789abc")
    assert len(calls) == 1


def test_missing_configuration_exposes_no_secret():
    inspector = RailwayInspector({})
    assert not inspector.configured
    with pytest.raises(RuntimeError, match="scoped project token"):
        inspector.deployments()


def test_bad_deployment_and_kind():
    inspector = RailwayInspector(config(), fake_client([]))
    with pytest.raises(ValueError):
        inspector.logs("javascript:alert(1)")
    with pytest.raises(ValueError):
        inspector.logs(DEPLOY, "delete")


def test_graphql_error_uses_code_not_sensitive_message():
    inspector = RailwayInspector(config(), lambda *_args, **_kwargs:
        Response({"errors": [{"message": "secret", "extensions": {"code": "BAD_USER_INPUT"}}]}))
    with pytest.raises(RuntimeError) as err:
        inspector.deployments()
    assert "BAD_USER_INPUT" in str(err.value)
    assert "secret" not in str(err.value)


def test_account_token_uses_bearer_header():
    calls = []
    RailwayInspector(config("account"), fake_client(calls)).deployments()
    assert calls[0][0]["Authorization"] == "Bearer test-secret"


def test_journey_rejects_invalid_input_and_excess():
    with pytest.raises(ValueError):
        validate_journey("file:///etc/passwd", [], "mobile")
    with pytest.raises(ValueError):
        validate_journey("https://example.com", [{"action": "click", "selector": ""}], "mobile")
    with pytest.raises(ValueError):
        validate_journey("https://example.com", [], "phoneXL")
    with pytest.raises(ValueError):
        validate_journey("https://example.com", [{"action": "click", "selector": "button"}] * 16, "mobile")
    validate_journey("https://example.com", [
        {"action": "fill", "selector": "input", "value": "Hello"},
        {"action": "click", "selector": "button"},
        {"action": "assert_text", "selector": "body", "value": "Hello"},
    ], "mobile")


def test_journey_executes_in_signed_workspace_and_keeps_observations():
    class Fake:
        def run(self, workspace_id, command, timeout_seconds=60):
            assert workspace_id == "signed"
            assert "playwright" not in command  # encoded: no source on gateway
            assert "mobile" in command
            assert len(command) < 10000
            assert timeout_seconds == 100
            return {"exit_code": 0, "stdout": json.dumps({
                "ok": True, "steps": [{"step": 1, "ok": True}],
                "screenshot_path": "/home/user/browser-captures/latest.jpg",
            }), "stderr": ""}
    data = browser_journey(Fake(), "signed", "https://example.com",
                           [{"action": "wait_for", "selector": "body"}])
    assert data["ok"] and data["steps"][0]["ok"]


def test_capture_reads_only_fixed_jpeg_and_enforces_size():
    from machine import Machine
    sample = b"\xff\xd8" + b"\x01" * 128
    sandbox = SimpleNamespace(commands=SimpleNamespace(run=lambda *_args, **_kwargs:
        SimpleNamespace(exit_code=0, stdout=base64.b64encode(sample).decode())))
    instance = object.__new__(Machine)
    instance.connect = lambda _workspace: sandbox
    assert instance.read_capture("signed") == sample
    sandbox.commands.run = lambda *_args, **_kwargs: SimpleNamespace(
        exit_code=0, stdout="invalid-base64"
    )
    with pytest.raises(ValueError):
        instance.read_capture("signed")
