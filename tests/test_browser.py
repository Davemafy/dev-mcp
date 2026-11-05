"""Browser feature validation; the actual browser is tested in E2B before deploy."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from browser import validate_browser_input, browser_visit, browser_setup


def test_rejects_bad_urls_and_unsafe_actions():
    for url in ["file:///etc/passwd", "javascript:alert(1)", "https://username:pw@example.com", ""]:
        with pytest.raises(ValueError):
            validate_browser_input(url, "inspect", "", "")
    with pytest.raises(ValueError):
        validate_browser_input("https://example.com", "click", "", "")
    with pytest.raises(ValueError):
        validate_browser_input("https://example.com", "launch", "", "")
    validate_browser_input("https://example.com", "inspect", "", "")


def test_browser_runs_through_workspace_and_parses_result():
    class FakeMachine:
        def run(self, workspace_id, command, timeout_seconds=60):
            assert workspace_id == "signed-id"
            assert "python -c" in command
            assert "https://example.com" in command
            return {"exit_code": 0, "stdout": json.dumps({"ok": True, "title": "Example"}),
                    "stderr": ""}
    result = browser_visit(FakeMachine(), "signed-id", "https://example.com")
    assert result == {"ok": True, "title": "Example"}


def test_browser_failure_is_reported_not_hidden():
    class FakeMachine:
        def run(self, *_args, **_kwargs):
            return {"exit_code": 1, "stdout": "", "stderr": "chromium missing"}
    result = browser_visit(FakeMachine(), "id", "https://example.com")
    assert result["ok"] is False
    assert "chromium missing" in result["error"]


def test_setup_happens_inside_workspace():
    class FakeMachine:
        def run(self, workspace_id, command, timeout_seconds):
            assert workspace_id == "signed-id"
            assert "playwright" in command and "swap" in command
            return {"exit_code": 0, "stdout": "", "stderr": ""}
    assert browser_setup(FakeMachine(), "signed-id")["installed"]
