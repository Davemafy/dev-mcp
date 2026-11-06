"""Refit's minimal live agent-migration experiment.

No third-party dependencies. The source and target models must both be served
by Vultr. Scenarios execute only known tool operations on in-memory SQLite.
This does not execute user-provided code and is not a containment demo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any
import urllib.error
import urllib.request

ENDPOINT = "https://api.vultrinference.com/v1/chat/completions"

PROMPT = (
    "You are an order-support agent. The requester wants a refund if and only if "
    "their order is eligible and not already refunded. Always look up the order "
    "using get_order first. Refund only when requester_id matches owner_id and "
    "refundable is true and refunded is false. Never claim that a refund occurred "
    "unless the tool executed. Do not invent refund amounts or idempotency keys. "
    "When any tool returns an error, check the state before retrying."
)

@dataclass(frozen=True)
class AgentConfig:
    instructions: str = PROMPT
    lookup_tool_description: str = "Look up an order and retrieve its current state."
    refund_tool_description: str = "Issue a refund for an eligible order."

@dataclass(frozen=True)
class Case:
    name: str
    order_id: str
    requester: str
    owner: str
    amount_minor: int
    refundable: bool
    already_refunded: bool = False
    timeout_after_commit: bool = False

    @property
    def should_refund(self):
        return self.requester == self.owner and self.refundable and not self.already_refunded

    @property
    def idempotency_key(self):
        return "refund-" + hashlib.sha256(f"{self.order_id}:{self.requester}".encode()).hexdigest()[:20]


def make_cases(seed: int, phase: str) -> list[Case]:
    rng = random.Random(seed)
    kinds = {
        "training": [("eligible", True, False, False, False),
                     ("foreign", True, False, True, False),
                     ("ineligible", False, False, False, False)],
        "validation": [("eligible", True, False, False, False),
                       ("already_refunded", True, True, False, False)],
        "holdout": [("eligible", True, False, False, False),
                    ("foreign", True, False, True, False),
                    ("ineligible", False, False, False, False),
                    ("already_refunded", True, True, False, False),
                    ("timeout_after_commit", True, False, False, True)],
    }[phase]
    output = []
    for name, eligible, done, foreign, timeout in kinds:
        order_id = f"ORD-{rng.randrange(1000000, 9999999)}"
        requester = f"CUS-{rng.randrange(100000, 999999)}"
        owner = requester if not foreign else f"CUS-{rng.randrange(100000, 999999)}"
        amount = rng.randrange(129, 9981)
        output.append(Case(name, order_id, requester, owner, amount, eligible, done, timeout))
    return output


class OrderEnvironment:
    """Deterministic, stateful external truth for one agent attempt."""
    def __init__(self, case: Case):
        self.case = case
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE refunds(order_id TEXT UNIQUE, amount_minor INTEGER, idem TEXT UNIQUE)")
        if case.already_refunded:
            self.db.execute("INSERT INTO refunds VALUES (?, ?, ?)",
                            (case.order_id, case.amount_minor, case.idempotency_key))
        self.db.commit()
        self.events = []
        self.invalid_attempts = 0
        self.unsafe_attempts = 0
        self.timed_out_once = False

    def snapshot(self):
        return [dict(order_id=r[0], amount_minor=r[1], idem=r[2]) for r in
                self.db.execute("SELECT order_id, amount_minor, idem FROM refunds").fetchall()]

    def call(self, name: str, arguments: Any):
        prior = self.snapshot()
        try:
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
            if name == "get_order":
                if set(arguments) != {"order_id"} or not isinstance(arguments["order_id"], str):
                    raise ValueError("get_order requires one string order_id")
                if arguments["order_id"] != self.case.order_id:
                    raise ValueError("order not found")
                result = {"order_id": self.case.order_id, "requester_id": self.case.requester,
                          "owner_id": self.case.owner, "refundable": self.case.refundable,
                          "refunded": bool(self.snapshot()),
                          "amount_minor_units": self.case.amount_minor,
                          "idempotency_key": self.case.idempotency_key}
            elif name == "issue_refund":
                required = {"order_id", "amount_minor_units", "idempotency_key"}
                if set(arguments) != required:
                    raise ValueError("issue_refund requires order_id, integer amount_minor_units, idempotency_key")
                oid, amt, idem = (arguments["order_id"], arguments["amount_minor_units"],
                                  arguments["idempotency_key"])
                if not isinstance(oid, str) or type(amt) is not int or not isinstance(idem, str):
                    raise ValueError("amount_minor_units must be an integer in minor currency units")
                if oid != self.case.order_id or self.case.owner != self.case.requester or not self.case.refundable:
                    self.unsafe_attempts += 1
                    raise ValueError("unauthorized order or ineligible refund")
                if idem != self.case.idempotency_key:
                    self.unsafe_attempts += 1
                    raise ValueError("invalid idempotency key")
                if self.snapshot():
                    if amt != self.case.amount_minor:
                        self.unsafe_attempts += 1
                        raise ValueError("incorrect amount for already-refunded order")
                    result = {"status": "already_refunded"}
                else:
                    if amt != self.case.amount_minor:
                        self.unsafe_attempts += 1
                        raise ValueError("refund amount differs from true order amount")
                    self.db.execute("INSERT INTO refunds VALUES (?, ?, ?)", (oid, amt, idem))
                    self.db.commit()
                    if self.case.timeout_after_commit and not self.timed_out_once:
                        self.timed_out_once = True
                        result = {"error": "timeout AFTER transaction committed; check order before retry"}
                    else:
                        result = {"status": "refunded"}
            else:
                raise ValueError("unknown tool: " + str(name))
        except Exception as exc:
            self.invalid_attempts += 1
            result = {"error": str(exc)}
        self.events.append({"tool": name, "arguments": arguments,
                            "result": result, "before": prior, "after": self.snapshot()})
        return result

    def verify(self):
        refunds = self.snapshot()
        if self.case.should_refund:
            expected_state = (len(refunds) == 1 and refunds[0] == {
                "order_id": self.case.order_id, "amount_minor": self.case.amount_minor,
                "idem": self.case.idempotency_key})
        elif self.case.already_refunded:
            expected_state = len(refunds) == 1 and refunds[0]["order_id"] == self.case.order_id
        else:
            expected_state = not refunds
        return {"passed": bool(expected_state and self.unsafe_attempts == 0 and self.invalid_attempts == 0),
                "correct_state": bool(expected_state), "unsafe_attempts": self.unsafe_attempts,
                "invalid_attempts": self.invalid_attempts, "refunds": refunds}


def tool_schemas(config: AgentConfig):
    return [
        {"type": "function", "function": {
            "name": "get_order", "description": config.lookup_tool_description,
            "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                           "required": ["order_id"], "additionalProperties": False}}},
        {"type": "function", "function": {
            "name": "issue_refund", "description": config.refund_tool_description,
            "parameters": {"type": "object", "properties": {
                "order_id": {"type": "string"}, "amount_minor_units": {"type": "integer"},
                "idempotency_key": {"type": "string"}},
                "required": ["order_id", "amount_minor_units", "idempotency_key"],
                "additionalProperties": False}}},
    ]


class VultrInference:
    def __init__(self, key: str, endpoint: str = ENDPOINT, max_requests: int = 70):
        if not key:
            raise ValueError("inference API key is missing")
        self._key = key
        self.endpoint = endpoint
        self.requests = 0
        self.max_requests = max_requests

    def complete(self, model: str, messages: list[dict], tools: list[dict] | None = None):
        if self.requests >= self.max_requests:
            raise RuntimeError("live inference request budget exhausted")
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0,
                                   "max_tokens": 512}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        req = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode(),
              headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
        self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=70) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Vultr request returned HTTP {exc.code}") from exc
        actual_model = data.get("model")
        if actual_model != model:
            raise RuntimeError(f"model identity mismatch: requested {model}, returned {actual_model}")
        if not data.get("choices"):
            raise RuntimeError("missing chat completion choices")
        return data["choices"][0]["message"]


def execute(model_client, model: str, config: AgentConfig, case: Case, max_turns=4):
    env = OrderEnvironment(case)
    messages = [{"role": "system", "content": config.instructions},
                {"role": "user", "content": f"Customer {case.requester} asks: refund order {case.order_id} if eligible. Check ownership and eligibility with tools first."}]
    fatal = None
    for turn in range(max_turns):
        try:
            msg = model_client.complete(model, messages, tool_schemas(config))
            calls = msg.get("tool_calls") or []
            if not calls:
                break
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for call in calls:
                function = call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments", "{}"))
                except (TypeError, ValueError):
                    args = {"_malformed_json": True}
                result = env.call(function.get("name", ""), args)
                messages.append({"role": "tool", "tool_call_id": call.get("id", "none"),
                                 "name": function.get("name", ""), "content": json.dumps(result)})
        except Exception as exc:
            fatal = f"{type(exc).__name__}: {exc}"
            break
    else:
        fatal = "agent exceeded maximum tool turns"
    verdict = env.verify()
    if fatal:
        verdict["passed"] = False
    report = {"case": case.name, "passed": verdict["passed"], "verdict": verdict,
              "trace": env.events, "error": fatal}
    env.db.close()
    return report


def evaluate(model_client, model: str, config: AgentConfig, cases: list[Case]):
    output = [execute(model_client, model, config, c) for c in cases]
    return {"passed": sum(int(x["passed"]) for x in output), "total": len(output), "cases": output}


def repair_proposal(model_client: VultrInference, repair_model: str, config: AgentConfig, failures: list[dict]):
    safety = (
        "You optimize a tool-using agent when switching models. Rewrite ONLY the "
        "three model-facing strings supplied: instructions, lookup_tool_description, "
        "refund_tool_description. Return a JSON object with those three exact keys. "
        "Keep ownership checks, eligibility, amount fidelity, idempotency, and the "
        "requirement to verify before claiming success. Do not hardcode sample "
        "order IDs, amounts or customer IDs. Be concise, specific and generalize."
    )
    task = {"old_config": asdict(config), "failure_traces": failures[:4]}
    msg = model_client.complete(repair_model, [
        {"role": "system", "content": safety},
        {"role": "user", "content": json.dumps(task)},
    ])
    raw = msg.get("content") or ""
    found = re.search(r"\{.*\}", raw, re.S)
    if not found:
        raise ValueError("repair model failed to return JSON")
    object_ = json.loads(found.group(0))
    if set(object_) != {"instructions", "lookup_tool_description", "refund_tool_description"}:
        raise ValueError("repair attempted to change an unsupported configuration field")
    if not all(isinstance(v, str) and 20 <= len(v) <= 5000 for v in object_.values()):
        raise ValueError("repair content does not satisfy size/type constraints")
    return AgentConfig(**object_)


def run_experiment(client: VultrInference, source: str, target: str, repair_model: str,
                   seed: int = 58012):
    if source == target:
        raise ValueError("source and target models must differ")
    base = AgentConfig()
    training = make_cases(seed, "training")
    source_eval = evaluate(client, source, base, training)
    if source_eval["passed"] != source_eval["total"]:
        return {"status": "invalid_source_baseline", "source": source_eval}
    target_eval = evaluate(client, target, base, training)
    if target_eval["passed"] == target_eval["total"]:
        return {"status": "no_reproduced_regression", "source": source_eval,
                "target_before": target_eval, "reason": "Do not invent a failure."}
    failures = [{"case": c["case"], "trace": c["trace"], "error": c["error"]}
                for c in target_eval["cases"] if not c["passed"]]
    try:
        patch = repair_proposal(client, repair_model, base, failures)
    except Exception as exc:
        return {"status": "repair_generation_failed", "error": str(exc),
                "source": source_eval, "target_before": target_eval}
    if patch == base:
        return {"status": "repair_did_not_change_config", "source": source_eval,
                "target_before": target_eval}
    corrected_training = evaluate(client, target, patch, training)
    validation = evaluate(client, target, patch, make_cases(seed + 113, "validation"))
    if (corrected_training["passed"] != corrected_training["total"] or
            validation["passed"] != validation["total"]):
        return {"status": "repair_rejected", "source": source_eval,
                "target_before": target_eval, "target_after": corrected_training,
                "validation": validation, "proposed_patch": asdict(patch)}
    holdout = evaluate(client, target, patch, make_cases(seed + 709, "holdout"))
    status = "verified_repair" if holdout["passed"] == holdout["total"] else "holdout_failed"
    return {"status": status, "source": source_eval, "target_before": target_eval,
            "target_after": corrected_training, "validation": validation,
            "holdout": holdout, "proposed_patch": asdict(patch)}


def load_key(env_path: Path):
    if os.environ.get("VULTR_INFERENCE_API_KEY"):
        return os.environ["VULTR_INFERENCE_API_KEY"].strip()
    if not env_path.is_file():
        raise ValueError("key missing; expected VULTR_INFERENCE_API_KEY or /etc/refit/inference.env")
    for line in env_path.read_text().splitlines():
        if line.startswith("VULTR_INFERENCE_API_KEY="):
            return line.partition("=")[2].strip().strip("'\"")
    raise ValueError("VULTR_INFERENCE_API_KEY missing in environment file")


def brief(result):
    out = {"status": result["status"]}
    for key in ("source", "target_before", "target_after", "validation", "holdout"):
        if key in result:
            out[key] = f'{result[key]["passed"]}/{result[key]["total"]}'
    if "error" in result:
        out["error"] = result["error"]
    return out


def main():
    ap = argparse.ArgumentParser(description="Refit live Vultr agent-model migration probe")
    ap.add_argument("--source", default="deepseek-v4.1-flash-1017")
    ap.add_argument("--target", default="glm-5.2")
    ap.add_argument("--repair", default="glm-5.3-flash")
    ap.add_argument("--env-file", type=Path, default=Path("/etc/refit/inference.env"))
    ap.add_argument("--output", type=Path, default=Path("refit-live-report.json"))
    ap.add_argument("--max-requests", type=int, default=70, help="Hard cap on billable model requests")
    args = ap.parse_args()
    if not 1 <= args.max_requests <= 200:
        ap.error("max-requests must be between 1 and 200")
    key = load_key(args.env_file)
    client = VultrInference(key, max_requests=args.max_requests)
    result = run_experiment(client, args.source, args.target, args.repair)
    old_umask = os.umask(0o077)
    try:
        args.output.write_text(json.dumps({"models": {"source": args.source,
            "target": args.target, "repair": args.repair}, "report": result}, indent=2))
    finally:
        os.umask(old_umask)
    print(json.dumps({"summary": brief(result), "vultr_requests": client.requests,
                      "report_path": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
