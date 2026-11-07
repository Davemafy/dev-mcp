"""Additional unattended-agent safety and restart acceptance tests."""
from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest

from agent_jobs import JobStore, AgentWorker, AgentJobs


class MemoryMachine:
    oauth=SimpleNamespace(
        sign=lambda claims,ttl:"signed-workspace",
        verify=lambda token,typ:{"sandbox_id":"sandbox-1"},
    )
    def status(self,handle):
        return {"ready":True}
    def run(self,handle,command,timeout_seconds=65):
        return {"exit_code":0,"stdout":"OK 2 tests passed","stderr":""}


class Browser:
    def __init__(self):
        self.streaming=self
        self.actions=[]
    def action(self,workspace,action,**values):
        self.actions.append(action)
        return {"ok":True,"url":"https://app.example/test","frame_version":3}
    def observe(self,workspace):
        return {"ok":True,"elements":[{"tag":"button","name":"Continue"}]}


def test_interactive_browser_actions_require_explicit_permission(tmp_path):
    machine=MemoryMachine()
    browser=Browser()
    worker=AgentWorker(JobStore(tmp_path/"jobs.sqlite"),machine,browser,SimpleNamespace())
    job={"sandbox_id":"sandbox-1","allow_shell":0,"allow_interaction":0}
    denied=worker.perform(job,{"kind":"browser_action","action":"click","selector":"button"})
    assert not denied["ok"]
    assert browser.actions==[]
    allowed=worker.perform(job,{"kind":"browser_action","action":"assert_text","selector":"body","value":"Hello"})
    assert allowed["ok"]
    job["allow_interaction"]=1
    assert worker.perform(job,{"kind":"browser_action","action":"click","selector":"button"})["ok"]
    assert browser.actions==["assert_text","click"]


def test_old_database_migrates_additional_permissions_without_deleting_jobs(tmp_path):
    path=tmp_path/"jobs.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE jobs (
          id TEXT PRIMARY KEY, sandbox_id TEXT NOT NULL, goal TEXT NOT NULL,
          status TEXT NOT NULL, steps INTEGER NOT NULL DEFAULT 0,
          max_steps INTEGER NOT NULL, allow_shell INTEGER NOT NULL DEFAULT 0,
          observation TEXT, result TEXT, error TEXT,
          in_flight INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
          lease_owner TEXT, lease_until REAL, next_run REAL NOT NULL DEFAULT 0,
          created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        db.execute("""INSERT INTO jobs(id,sandbox_id,goal,status,max_steps,created_at,updated_at)
                      VALUES ('old-job','sandbox-1','An existing agent task','paused',8,1,1)""")
    upgraded=JobStore(path)
    assert upgraded.raw("old-job")["allow_interaction"]==0
    assert upgraded.public("old-job")["goal"]=="An existing agent task"


def test_job_resumption_refuses_exhausted_budget(tmp_path):
    store=JobStore(tmp_path/"db")
    jid=store.submit("sandbox-1","Browser task with one step allowed",1,False)["id"]
    store.claim("worker")
    store.intent(jid,"worker",{"kind":"observe"})
    store.observed(jid,"worker",{"ok":True,"elements":[]})
    assert store.public(jid)["status"]=="needs_review"
    result=store.control(jid,"resume")
    assert not result["ok"] and "budget" in result["error"].lower()


def test_gateway_worker_requires_actual_mounted_persistent_volume(tmp_path,monkeypatch):
    monkeypatch.setattr("agent_jobs.os.path.ismount",lambda path:False)
    controller=AgentJobs(MemoryMachine(),Browser(),db_path=tmp_path/"jobs.sqlite",
                         planner=SimpleNamespace(configured=True),enabled=True)
    controller.start()
    assert not controller.status()["worker_running"]
    assert not controller.status()["persistent_storage"]


def test_unattended_worker_completes_without_followup_call(tmp_path):
    class Planner:
        configured=True
        def propose(self,job,events):
            if any(e["type"]=="observation" for e in events):
                return {"kind":"complete","summary":"Browser confirmation verified"}
            return {"kind":"browser_action","action":"assert_text",
                    "selector":"body","value":"Confirmed"}
    store=JobStore(tmp_path/"jobs.sqlite")
    jid=store.submit("sandbox-1","Verify page contains the confirmation",4,False)["id"]
    browser=Browser()
    worker=AgentWorker(store,MemoryMachine(),browser,Planner())
    worker.start()
    try:
        deadline=time.monotonic()+4
        while time.monotonic()<deadline and store.public(jid)["status"]!="completed":
            time.sleep(0.05)
        assert store.public(jid)["status"]=="completed"
        assert store.public(jid)["result"]["verified"] is True
        assert browser.actions==["assert_text"]
    finally:
        worker.stop()

def test_expired_read_only_planning_lease_is_retried_safely(tmp_path):
    store=JobStore(tmp_path/"jobs.sqlite")
    jid=store.submit("sandbox-1","Inspect and validate after a worker restart",5,False)["id"]
    store.claim("old-worker")
    # No action intent was dispatched before the worker disappeared.
    with store.connect() as db:
        db.execute("UPDATE jobs SET lease_until=? WHERE id=?",(time.time()-3,jid))
    assert JobStore(tmp_path/"jobs.sqlite").recover()==1
    assert store.public(jid)["status"]=="queued"
    assert store.claim("new-worker")["id"]==jid
    assert store.history(jid)[-2]["payload"]["resumed_automatically"] is True
