"""Browser-debugging bridge stays within the owner-isolated E2B desktop."""
from pathlib import Path


def test_headful_browser_launches_own_cdp_process_for_code_reuse():
    source = Path("stream_browser_server.py").read_text()
    assert '"--remote-debugging-address=127.0.0.1"' in source
    assert '"--remote-debugging-port=9222"' in source
    assert '"--user-data-dir=/home/user/.devmcp-chrome-profile"' in source
    assert 'p.chromium.connect_over_cdp("http://127.0.0.1:9222")' in source
    assert "state.initial_url" in source
    assert "chrome_process.terminate()" in source
    assert 'headful = os.getenv("DEVMCP_HEADFUL") == "1"' in source


def test_agent_can_reuse_same_desktop_through_existing_owner_terminal():
    source = Path("main.py").read_text()
    runtime = Path("stream_browser.py").read_text()
    assert "def run_terminal_command(" in source
    assert "def desktop_browser_open(" in source
    assert "DEVMCP_HEADFUL=1" in runtime
    assert 'def browser_adaptive_run(' in source
    assert 'def browser_observe(' in source
    assert 'def browser_capture(' in source


def test_only_private_development_machine_runs_chrome_and_desktop():
    gateway = Path("main.py").read_text()
    server = Path("desktop_runtime.py").read_text()
    assert "import desktop_runtime" not in gateway
    assert '"-localhost"' in server
    assert '"-nolisten", "tcp"' in server
    assert '0.0.0.0:3488' not in gateway
    assert 'remote-debugging-port=9222' not in gateway
