"""Capability-gating and fast browser-tool regression checks."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from capabilities import install_capabilities


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


def setup(monkeypatch, *, backend="e2b", stateless=False, live=True):
    monkeypatch.setenv("WORKSPACE_BACKEND", backend)
    monkeypatch.setenv("STATELESS_GATEWAY", "1" if stateless else "0")
    monkeypatch.setenv("LIVE_BROWSER_ENABLED", "1" if live else "0")
    monkeypatch.delenv("DESKTOP_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_JOBS_ENABLED", raising=False)
    return ToolRegistry()


def test_fast_browser_journey_in_one_call(monkeypatch):
    registry = setup(monkeypatch, live=False)
    calls = []
    machine = SimpleNamespace(run=lambda *a, **k: calls.append((a,k)) or {
        "exit_code": 0, "stdout": '{"ok":true,"steps":[]}', "stderr": ""
    })
    routes, jobs, enabled = install_capabilities(registry, machine, SimpleNamespace())
    result = registry.tools["browser_journey"]("signed-e2b-handle", "https://example.com", [])
    assert result["ok"]
    assert len(calls) == 1
    assert jobs is None and routes == []
    assert enabled["browser_journey"]
    assert "browser_capture" in registry.tools
    assert "browser_stream_start" not in registry.tools


def test_live_browser_available_only_on_stateful_e2b(monkeypatch):
    registry = setup(monkeypatch)
    routes, jobs, enabled = install_capabilities(registry, SimpleNamespace(), SimpleNamespace())
    assert enabled["browser_stream"]
    assert not enabled["desktop"] and not enabled["agent_jobs"]
    assert "browser_stream_start" in registry.tools
    assert "browser_adaptive_run" in registry.tools
    assert "browser_observe" in registry.tools
    assert "browser_stream_stop" in registry.tools
    assert [r.path for r in routes] == [
        "/browser/{session}/", "/browser/{session}/{path:path}"
    ]


def test_stateless_and_generic_worker_are_lightweight(monkeypatch):
    for backend, stateless in [("e2b", True), ("worker", False)]:
        registry = setup(monkeypatch, backend=backend, stateless=stateless)
        routes, jobs, enabled = install_capabilities(
            registry, SimpleNamespace(), SimpleNamespace())
        assert not enabled["browser_stream"]
        assert not enabled["agent_jobs"]
        assert routes == [] and jobs is None
        assert ("browser_journey" in registry.tools) == (backend == "e2b")


def test_desktop_and_autonomous_jobs_fail_closed(monkeypatch):
    registry = setup(monkeypatch, stateless=True)
    monkeypatch.setenv("DESKTOP_TOOLS_ENABLED", "1")
    with pytest.raises(RuntimeError, match="stateful"):
        install_capabilities(registry, SimpleNamespace(), SimpleNamespace())
    monkeypatch.delenv("DESKTOP_TOOLS_ENABLED")
    monkeypatch.setenv("AGENT_JOBS_ENABLED", "1")
    with pytest.raises(RuntimeError, match="stateful"):
        install_capabilities(registry, SimpleNamespace(), SimpleNamespace())


def test_capture_rejects_invalid_data(monkeypatch, tmp_path):
    from machine import Machine
    from auth import OAuth, Settings
    oauth = OAuth(Settings("https://example.test", "x"*24, "y"*42,
                           str(tmp_path / "tokens.sqlite")))
    instance = Machine(oauth, sandbox_factory=SimpleNamespace())
    bad = SimpleNamespace(commands=SimpleNamespace(
        run=lambda *args, **kwargs: SimpleNamespace(
            exit_code=0, stdout="bm90LWEtanBlZw=="
        )))
    monkeypatch.setattr(instance, "connect", lambda handle: bad)
    with pytest.raises(ValueError, match="JPEG"):
        instance.read_capture("dummy")
