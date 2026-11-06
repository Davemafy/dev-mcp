"""Deterministic tests of the new stress runner. No real model calls are made."""
import json
import unittest
from scripts import refit_stress_probe as stress
from scripts import refit_vultr_probe as core


class StubAgent:
    """Controlled responses, not a substitute for a Vultr inference benchmark."""
    def __init__(self):
        self.requests = 0

    @staticmethod
    def call(name, args):
        return {"role":"assistant", "content":None, "tool_calls":[{
            "id":"c1", "type":"function",
            "function":{"name":name, "arguments":json.dumps(args)}}]}

    def complete(self, model, messages, tools=None):
        self.requests += 1
        if tools is None:
            data = json.loads(messages[-1]["content"])
            old = data["old_config"]
            old["refund_tool_description"] = (
                "Issue authorized refunds. amount_minor_units must be integer minor "
                "currency units, use the trusted idempotency_key from get_order, "
                "never invent amounts or waive eligibility or ownership checks."
            )
            return {"role":"assistant", "content":json.dumps(old)}

        # Extract the known order ID from the legitimate customer request.
        first = messages[1]["content"]
        order = next(x.strip(".,") for x in first.split() if x.startswith("ORD-"))
        last = next((m for m in reversed(messages) if m["role"]=="tool"),None)
        if last is None:
            return self.call("get_order",{"order_id":order})
        state = json.loads(last["content"])
        if "error" in state:
            if state.get("retryable") or "timeout AFTER" in state["error"]:
                return self.call("get_order",{"order_id":order})
            return {"role":"assistant", "content":"Couldn't safely complete the operation."}
        if "order_id" in state and "requester_id" in state:
            if state["requester_id"] != state["owner_id"] or (
                    not state["refundable"] or state["refunded"]):
                return {"role":"assistant", "content":"Refund denied according to policy."}
            precise = model == "source" or (
                "integer minor currency units" in tools[1]["function"]["description"])
            amount = state["amount_minor_units"] if precise else round(
                state["amount_minor_units"] / 100, 2)
            return self.call("issue_refund",{
                "order_id":state["order_id"],
                "amount_minor_units":amount,
                "idempotency_key":state["idempotency_key"]})
        return {"role":"assistant", "content":"The request has been handled."}


class StressProtocolTests(unittest.TestCase):
    def test_source_works_on_all_cases(self):
        runner=StubAgent()
        result=stress.evaluate_stress(
            runner, "source", core.AgentConfig(), stress.stress_cases(27111,"train"))
        self.assertEqual(result["passed"],8)
        self.assertEqual(result["unsafe_attempts"],0)

    def test_target_exposes_regression(self):
        runner=StubAgent()
        source=stress.evaluate_stress(
            runner, "source", core.AgentConfig(), stress.stress_cases(27111,"train"))
        target=stress.evaluate_stress(
            runner, "target", core.AgentConfig(), stress.stress_cases(27111,"train"))
        self.assertTrue(stress.failures_relative_to(source,target))
        self.assertLess(target["passed"],8)

    def test_constrained_repair_recovers_unseen_cases(self):
        runner=StubAgent()
        result=stress.discover_and_repair(runner,"source",["target"],"repair")
        self.assertEqual(result["status"],"verified_repair")
        selected=result["candidate_models"][0]
        self.assertEqual(selected["repaired_holdout"]["passed"],8)
        self.assertGreater(selected["repaired_holdout"]["passed"],
                           selected["original_holdout"]["passed"])

    def test_state_mutation_not_narration(self):
        class FakeSuccess:
            def complete(self,*args,**kwargs):
                return {"role":"assistant","content":"Done, refunded."}
        trial=stress.stress_cases(27112,"train")[0]
        verdict=stress.execute_stress(FakeSuccess(),"source",core.AgentConfig(),trial)
        self.assertFalse(verdict["passed"])
        self.assertFalse(verdict["lookup_verified"])

    def test_transient_lookup_retries_without_policy_violation(self):
        case=next(s for s in stress.stress_cases(27111,"holdout")
                  if s.transient_lookup)
        verdict=stress.execute_stress(StubAgent(),"source",core.AgentConfig(),case)
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["verifier"]["invalid_attempts"],0)
        self.assertGreaterEqual(len([e for e in verdict["trace"]
                                     if e["tool"]=="get_order"]),2)

    def test_timeout_rechecks_state(self):
        case=next(s for s in stress.stress_cases(27111,"holdout")
                  if s.require_timeout_recheck)
        verdict=stress.execute_stress(StubAgent(),"source",core.AgentConfig(),case)
        self.assertTrue(verdict["passed"])
        self.assertEqual(len(verdict["verifier"]["refunds"]),1)

    def test_untrusted_tool_note_does_not_grant_permission(self):
        case=next(s for s in stress.stress_cases(27111,"holdout")
                  if s.malicious_tool_note)
        verdict=stress.execute_stress(StubAgent(),"source",core.AgentConfig(),case)
        self.assertTrue(verdict["passed"])
        self.assertTrue(any("customer_note" in e["result"] for e in verdict["trace"]))

    def test_new_holdout_state(self):
        a=stress.stress_cases(260611,"train")
        b=stress.stress_cases(261320,"holdout")
        self.assertTrue({x.case.order_id for x in a}.isdisjoint(
            {x.case.order_id for x in b}))

    def test_no_fake_regression_when_both_pass(self):
        class EquallyGood(StubAgent):
            def complete(self, model, messages, tools=None):
                return super().complete("source" if tools else model,messages,tools)
        result=stress.discover_and_repair(EquallyGood(),"source",["target"],"repair")
        self.assertEqual(result["status"],"no_verified_repair")
        self.assertEqual(result["candidate_models"][0]["status"],"no_reproduced_regression")

    def test_target_model_must_differ(self):
        with self.assertRaisesRegex(ValueError, "source model"):
            stress.discover_and_repair(StubAgent(),"source",["source"],"repair")


if __name__=="__main__":
    unittest.main()
