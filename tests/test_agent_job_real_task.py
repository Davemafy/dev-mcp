"""End-to-end code repair using real subprocess tests and file edits.

The planner is intentionally deterministic here: verifies the execution
infrastructure without requiring an API key or charging an inference account.
"""
from __future__ import annotations
import subprocess
import sys

from agent_jobs import AgentWorker, JobStore


def test_background_agent_repairs_real_failing_unittest_fixture(tmp_path):
    (tmp_path/"calc.py").write_text(
        "def add(a, b):\n    return a - b\n"
    )
    (tmp_path/"test_calc.py").write_text(
        "import unittest\nfrom calc import add\n"
        "class Tests(unittest.TestCase):\n"
        "    def test_positive(self): self.assertEqual(add(4, 3), 7)\n"
        "    def test_negative(self): self.assertEqual(add(-2, 3), 1)\n"
    )

    class Machine:
        class OAuth:
            def sign(self,claims,ttl):
                assert claims["sandbox_id"]=="fixture"
                return "signed"
        oauth=OAuth()
        def run(self,handle,command,timeout_seconds=65):
            assert handle=="signed"
            args={
                "verify-tests":[sys.executable,"-m","unittest","discover","-v"],
                "read-source":[sys.executable,"-c",
                               "print(open('calc.py').read())"],
            }[command]
            p=subprocess.run(args,cwd=tmp_path,capture_output=True,
                             text=True,timeout=timeout_seconds)
            return {"exit_code":p.returncode,"stdout":p.stdout,"stderr":p.stderr}
        def write(self,handle,path,content):
            assert handle=="signed" and path=="calc.py"
            (tmp_path/path).write_text(content)
            return {"path":str(tmp_path/path),"bytes":len(content.encode())}

    class Planner:
        configured=True
        def propose(self,job,history):
            prior=[e["payload"] for e in history if e["type"]=="observation"]
            if not prior:
                return {"kind":"terminal","command":"verify-tests"}
            recent=prior[-1]
            if recent.get("exit_code")!=0:
                return {"kind":"terminal","command":"read-source"}
            if "return a - b" in recent.get("stdout",""):
                corrected=recent["stdout"].replace("return a - b","return a + b")
                return {"kind":"write_file","path":"calc.py","content":corrected}
            if recent.get("action")=="write_file":
                return {"kind":"terminal","command":"verify-tests"}
            if recent.get("action")=="terminal" and recent.get("exit_code")==0:
                return {"kind":"complete","summary":"Verified both real arithmetic unit tests"}
            return {"kind":"request_help","reason":"Unexpected fixture output"}

    class NoBrowser:
        def observe(self,*args):
            raise AssertionError("Browser not required in this test")

    store=JobStore(tmp_path/"checkpoints.sqlite")
    jid=store.submit("fixture","Repair the broken arithmetic operation and prove with tests",8,True)["id"]
    worker=AgentWorker(store,Machine(),NoBrowser(),Planner())
    assert worker.run_once()
    recorded=store.public(jid)
    assert recorded["status"]=="completed"
    assert recorded["steps"]==4
    assert recorded["result"]["verified"] is True
    assert "return a + b" in (tmp_path/"calc.py").read_text()
    actual=subprocess.run([sys.executable,"-m","unittest","discover","-q"],
                           cwd=tmp_path,capture_output=True,text=True,timeout=10)
    assert actual.returncode==0
    assert "Ran 2 tests" in actual.stderr
    history=store.history(jid)
    assert [item["type"] for item in history].count("observation")==4
    assert history[-1]["type"]=="completed"
