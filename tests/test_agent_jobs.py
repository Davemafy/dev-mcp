"""Durable worker: checkpoints, crash recovery, authorizations and autonomous repair."""
from __future__ import annotations
import json
import sqlite3
import time
from types import SimpleNamespace

import httpx
import pytest

from agent_jobs import JobStore, AgentWorker, AgentJobs, ModelPlanner


class FakeMachine:
    oauth = None
    def __init__(self):
        self.oauth=SimpleNamespace(
            verify=lambda token,typ: {"sandbox_id":"owned-sandbox-1"},
            sign=lambda values,seconds: "signed-workspace")
        self.terminal=[]
        self.files={}
        self.status_calls=0

    def status(self, handle):
        self.status_calls+=1
        return {"ready":True}

    def run(self,handle,command,timeout_seconds=65):
        assert handle=="signed-workspace"
        self.terminal.append(command)
        return {"exit_code":0,"stdout":"3 tests passed","stderr":""}

    def write(self,handle,path,content):
        assert handle=="signed-workspace"
        self.files[path]=content
        return {"path":"/home/user/"+path,"bytes":len(content)}


class FakeBrowser:
    def __init__(self):
        self.action_log=[]
        self.streaming=self
        self.pressed=False
    def observe(self,handle):
        assert handle=="signed-workspace"
        return {"ok":True, "url":"http://127.0.0.1:3000",
                "elements":[{"tag":"button","name":"Continue","testId":"new-continue"}],
                "visible_text":"Verified result" if self.pressed else "Not completed"}
    def action(self,handle,action,**opts):
        assert handle=="signed-workspace"
        self.action_log.append((action,opts))
        if opts.get("selector")=="#old-id":
            return {"ok":False,"error":"TimeoutError"}
        if action=="click" and opts.get("selector")=="[data-testid='new-continue']":
            self.pressed=True
            return {"ok":True,"url":"http://127.0.0.1:3000","frame_version":7}
        if action=="assert_text" and self.pressed:
            return {"ok":True,"url":"http://127.0.0.1:3000","frame_version":8}
        return {"ok":False,"error":"Element not found"}


class ObservationPlanner:
    configured=True
    def propose(self,job,history):
        observations=[e["payload"] for e in history if e["type"]=="observation"]
        if not observations:
            return {"kind":"browser_action","action":"click","selector":"#old-id"}
        last=observations[-1]
        if last.get("error"):
            elements=last["current_page"]["elements"]
            testid=next(x["testId"] for x in elements if x.get("testId"))
            return {"kind":"browser_action","action":"click","selector":"[data-testid='"+testid+"']"}
        if last.get("action")=="click":
            return {"kind":"browser_action","action":"assert_text","selector":"body","value":"Verified result"}
        return {"kind":"complete","summary":"Clicked the corrected element and verified visible text"}


def test_durable_records_are_scoped_and_survive_new_store(tmp_path):
    path=tmp_path/"jobs.sqlite"
    store=JobStore(path)
    job=store.submit("owned-sandbox-1","Fix the broken browser behavior",6,False)
    assert job["status"]=="queued"
    assert "sandbox_id" not in job
    assert JobStore(path).public(job["id"])["goal"]==job["goal"]
    assert store.history(job["id"])[0]["type"]=="submitted"
    assert not store.public("not-a-job")


def test_claims_are_unique_with_transactional_lease(tmp_path):
    path=tmp_path/"jobs.sqlite"
    store=JobStore(path)
    jid=store.submit("sandbox","Inspect the existing browser",4,False)["id"]
    assert store.claim("worker-a")["id"]==jid
    assert JobStore(path).claim("worker-b") is None
    assert store.raw(jid)["lease_owner"]=="worker-a"
    assert store.control(jid,"cancel")["status"]=="cancelled"
    assert store.claim("worker-c") is None


def test_uncertain_inflight_side_effect_must_not_replay_after_restart(tmp_path):
    path=tmp_path/"jobs.sqlite"
    store=JobStore(path)
    jid=store.submit("sandbox","Click a button and verify result",5,False)["id"]
    store.claim("worker-a")
    assert store.intent(jid,"worker-a",{"kind":"browser_action","action":"click","selector":"#pay"})
    with store.connect() as db:
        db.execute("UPDATE jobs SET lease_until=? WHERE id=?",(time.time()-1,jid))
    restarted=JobStore(path)
    assert restarted.recover()==1
    assert restarted.public(jid)["status"]=="needs_review"
    assert restarted.claim("worker-b") is None
    assert restarted.history(jid)[-1]["payload"]["previous_action_uncertain"] is True
    assert restarted.control(jid,"resume")["ok"]
    assert restarted.claim("worker-b")["id"]==jid


def test_action_logs_do_not_store_full_code_body(tmp_path):
    store=JobStore(tmp_path/"jobs.sqlite")
    jid=store.submit("sandbox","Write an updated source file",4,True)["id"]
    store.claim("worker")
    store.intent(jid,"worker",{"kind":"write_file","path":"repo/a.py","content":"a secret string"})
    events=store.history(jid)
    assert "a secret string" not in json.dumps(events)
    assert events[-1]["payload"]["path"]=="repo/a.py"


def test_agent_repairs_failed_selector_and_verifies_without_a_chat_turn(tmp_path):
    store=JobStore(tmp_path/"jobs.sqlite")
    jid=store.submit("owned-sandbox-1","Repair navigation and verify its result",8,False,True)["id"]
    browser=FakeBrowser()
    worker=AgentWorker(store,FakeMachine(),browser,ObservationPlanner())
    assert worker.run_once() is True
    final=store.public(jid)
    assert final["status"]=="completed"
    assert final["result"]["verified"] is True
    assert len([e for e in store.history(jid) if e["type"]=="observation"])==3
    assert browser.action_log[0][1]["selector"]=="#old-id"
    assert browser.action_log[1][1]["selector"]=="[data-testid='new-continue']"
    assert browser.action_log[2][0]=="assert_text"


def test_pause_cancel_and_resumption_have_explicit_transitions(tmp_path):
    store=JobStore(tmp_path/"jobs.sqlite")
    jid=store.submit("sandbox","Inspect and validate the task",4,False)["id"]
    assert store.control(jid,"pause")["ok"]
    assert store.claim("worker") is None
    assert store.control(jid,"resume")["ok"]
    assert store.claim("worker")["id"]==jid
    assert store.control(jid,"cancel")["ok"]
    assert store.public(jid)["status"]=="cancelled"
    assert not store.control(jid,"resume")["ok"]


def test_shell_and_file_edit_permissions_are_off_by_default(tmp_path):
    browser=FakeBrowser()
    machine=FakeMachine()
    worker=AgentWorker(JobStore(tmp_path/"db"),machine,browser,ObservationPlanner())
    job={"sandbox_id":"owned-sandbox-1","allow_shell":0}
    assert not worker.perform(job,{"kind":"terminal","command":"pwd"})["ok"]
    assert not worker.perform(job,{"kind":"write_file","path":"repo/a.py","content":"x"})["ok"]
    assert machine.terminal==[] and machine.files=={}
    job["allow_shell"]=1
    assert worker.perform(job,{"kind":"terminal","command":"pytest -q"})["ok"]
    assert worker.perform(job,{"kind":"write_file","path":"repo/a.py","content":"fixed"})["ok"]
    assert machine.files["repo/a.py"]=="fixed"


def test_bounded_step_budget_prevents_endless_runs(tmp_path):
    store=JobStore(tmp_path/"db")
    jid=store.submit("sandbox","Attempt repeatedly with a tight budget",1,False)["id"]
    worker=AgentWorker(store,FakeMachine(),FakeBrowser(),ObservationPlanner())
    worker.run_once()
    assert store.public(jid)["steps"]==1
    assert store.public(jid)["status"]=="needs_review"


def test_transport_failure_is_requeued_but_not_inflight_actions(tmp_path):
    store=JobStore(tmp_path/"db")
    jid=store.submit("sandbox","Recover transient inference errors",4,False)["id"]
    store.claim("worker")
    store.fault(jid,"worker","model_TimeoutException")
    assert store.public(jid)["status"]=="queued"
    assert store.public(jid)["failures"]==1
    with store.connect() as db:
        db.execute("UPDATE jobs SET next_run=0 WHERE id=?",(jid,))
    store.claim("worker")
    store.intent(jid,"worker",{"kind":"browser_action","action":"click","selector":"#a"})
    store.fault(jid,"worker","tool_TimeoutError")
    assert store.public(jid)["status"]=="needs_review"


def test_model_transport_parses_strict_json_and_keeps_credentials_off_sandbox():
    requests=[]
    def fake(req):
        requests.append(req)
        return httpx.Response(200,json={"choices":[{"message":{"content":
            json.dumps({"kind":"observe"})}}]})
    client=httpx.Client(transport=httpx.MockTransport(fake))
    planner=ModelPlanner(endpoint="https://models.example/v1/chat/completions",
                         model="example",key="gateway-only-secret",client=client)
    result=planner.propose({"goal":"Inspect broken UI","steps":0,"max_steps":4,"allow_shell":0,"allow_interaction":0},[])
    assert result=={"kind":"observe"}
    assert requests[0].headers["authorization"]=="Bearer gateway-only-secret"
    assert b"gateway-only-secret" not in requests[0].content


def test_unconfigured_worker_refuses_jobs(tmp_path):
    controller=AgentJobs(FakeMachine(),FakeBrowser(),db_path=tmp_path/"jobs.sqlite",
                         planner=ModelPlanner(),enabled=False)
    reply=controller.submit("signed-workspace","Attempt real browser task")
    assert not reply["created"]
    assert not controller.status()["worker_running"]
