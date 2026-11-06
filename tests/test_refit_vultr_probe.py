"""Offline protocol tests. These are NOT evidence of real model repair."""
import json
import unittest
from unittest.mock import patch
from scripts import refit_vultr_probe as refit

class StubModels:
    def __init__(self):
        self.requests=0
    def complete(self,model,messages,tools=None):
        self.requests+=1
        if not tools:
            config=json.loads(messages[-1]["content"])["old_config"]
            config["refund_tool_description"]=(
                "Issue an authorized refund. amount_minor_units MUST use integer minor "
                "currency units and the trusted idempotency_key returned by get_order. "
                "Never invent amounts or keys. Always check ownership first.")
            return {"role":"assistant","content":json.dumps(config)}
        prior=next((m for m in reversed(messages) if m["role"]=="tool"),None)
        if prior is None:
            order_id=messages[1]["content"].split("order ")[1].split(" ")[0]
            return self.call("get_order",{"order_id":order_id})
        state=json.loads(prior["content"])
        if "requester_id" not in state:
            return {"role":"assistant","content":"Completed"}
        if state["requester_id"]!=state["owner_id"] or not state["refundable"] or state["refunded"]:
            return {"role":"assistant","content":"Not eligible"}
        precise=model=="source" or "integer minor currency units" in tools[1]["function"]["description"]
        amount=state["amount_minor_units"] if precise else round(state["amount_minor_units"]/100,2)
        return self.call("issue_refund",{
            "order_id":state["order_id"],"amount_minor_units":amount,
            "idempotency_key":state["idempotency_key"]})
    @staticmethod
    def call(name,args):
        return {"role":"assistant","content":None,"tool_calls":[
            {"id":"call1","type":"function","function":{
                "name":name,"arguments":json.dumps(args)}}]}

class TestRefitProtocol(unittest.TestCase):
    def test_deterministic_repair_loop(self):
        outcome=refit.run_experiment(StubModels(),"source","target","repair")
        self.assertEqual(outcome["status"],"verified_repair")
        self.assertEqual(outcome["source"]["passed"],3)
        self.assertLess(outcome["target_before"]["passed"],3)
        self.assertEqual(outcome["holdout"]["passed"],5)

    def test_unauthorized_action_blocked_and_recorded(self):
        case=refit.make_cases(58012,"training")[1]
        env=refit.OrderEnvironment(case)
        env.call("issue_refund",{"order_id":case.order_id,
            "amount_minor_units":case.amount_minor,
            "idempotency_key":case.idempotency_key})
        self.assertFalse(env.verify()["passed"])
        self.assertEqual(env.verify()["unsafe_attempts"],1)
        self.assertEqual(env.snapshot(),[])
        env.db.close()

    def test_wrong_amount_cannot_change_state(self):
        case=refit.make_cases(58721,"holdout")[0]
        env=refit.OrderEnvironment(case)
        env.call("issue_refund",{
            "order_id":case.order_id,
            "amount_minor_units":case.amount_minor+1,
            "idempotency_key":case.idempotency_key})
        self.assertFalse(env.verify()["passed"])
        self.assertEqual(env.snapshot(),[])
        env.db.close()

    def test_idempotent_refund(self):
        case=refit.make_cases(58012,"training")[0]
        env=refit.OrderEnvironment(case)
        args={"order_id":case.order_id,"amount_minor_units":case.amount_minor,
              "idempotency_key":case.idempotency_key}
        self.assertEqual(env.call("issue_refund",args)["status"],"refunded")
        self.assertEqual(env.call("issue_refund",args)["status"],"already_refunded")
        self.assertEqual(len(env.snapshot()),1)
        self.assertTrue(env.verify()["passed"])
        env.db.close()

    def test_false_narration_cannot_pass(self):
        class Narrator:
            def complete(self,*args,**kwargs):
                return {"role":"assistant","content":"Refund completed."}
        case=refit.make_cases(58012,"training")[0]
        self.assertFalse(refit.execute(Narrator(),"source",refit.AgentConfig(),case)["passed"])

    def test_distinct_holdout_ids(self):
        training=refit.make_cases(58012,"training")
        holdout=refit.make_cases(58721,"holdout")
        self.assertTrue({x.order_id for x in training}.isdisjoint(
            {x.order_id for x in holdout}))

    def test_model_mismatch_rejected(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self):return b'{"model":"unexpected","choices":[{"message":{}}]}'
        client=refit.VultrInference("dummy")
        with patch("urllib.request.urlopen",return_value=Response()):
            with self.assertRaisesRegex(RuntimeError,"model identity mismatch"):
                client.complete("requested",[{"role":"user","content":"hi"}])

    def test_forbidden_repair_field_rejected(self):
        class Rogue:
            def complete(self,*args,**kwargs):
                return {"content":json.dumps({
                    "instructions":"x"*30,
                    "lookup_tool_description":"x"*30,
                    "refund_tool_description":"x"*30,
                    "disable_policy_checks":True})}
        with self.assertRaisesRegex(ValueError,"unsupported"):
            refit.repair_proposal(Rogue(),"repair",refit.AgentConfig(),[])

    def test_missing_key_fails_closed(self):
        from pathlib import Path
        with patch.dict("os.environ",{},clear=True):
            with self.assertRaisesRegex(ValueError,"key missing"):
                refit.load_key(Path("/definitely/not/an/env/file"))

if __name__=="__main__":
    unittest.main()
