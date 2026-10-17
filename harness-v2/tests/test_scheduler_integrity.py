"""Scheduler records must be reproducible from their bound inputs.

Regressions for the two holes reproduced in the 1.2.0 review (2026-10-17):
a budget edited into BATCH.json kept issuing decisions, and an O record edited
from valid_negative to valid_improvement steered the next decision. Also covers
pending decisions edited or written by hand. Uses only v1 features, so it runs
unchanged on the 10-16 baseline with patches/02_integrity.diff applied.
Run from the project root:  python -X utf8 -B -m unittest discover -s tests -p "test_scheduler_integrity.py" -v
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
spec = importlib.util.spec_from_file_location("research_scheduler", ENGINE / "research_scheduler.py")
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


def edit(path, change):
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    change(obj)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.serial = 0
        self.goal = self.text("goal.txt", "goal\n")

    def tearDown(self):
        self._tmp.cleanup()

    def save(self, name, value):
        self.serial += 1
        path = self.root / f"{self.serial:04d}-{name}.json"
        s.write(path, value)
        return s.reference(path)

    def text(self, name, body):
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        return s.reference(path)

    def start(self, max_attempts=4, **boundary):
        evidence = self.text("n1.txt", "n1")
        node = {"id": "n1", "parent": "root", "depends_on": [], "decision_at": "2026-10-01T00:00:00+00:00",
                "available_at": "2026-10-01T01:00:00+00:00",
                "observation": {"status": "verified_information", "quality": None, "question": "q",
                                "resolved": True, "substantive": True},
                "measured_seconds": 1.0, "total_seconds": 2.0, "evidence": [evidence]}
        s.write(self.root / "world.json", {"schema": s.SCHEMA, "id": "w", "branch": "structure",
                                          "admission": "diagnostic", "usage": "development", "limitations": [],
                                          "initial_quality": 0.5, "metric_contract": "t", "nodes": [node]})
        s.write(self.root / "incumbent.json", {**s.initial_policy(), "max_attempts": max_attempts})
        s.cycle([str(self.root / "world.json")], str(self.root / "incumbent.json"), self.root / "cycle", "structure")
        quality = self.save("quality", {"schema": "research_quality.v1", "kind": "risk_cap_return_v1",
                                        "drawdown_ceiling": 0.2, "annual_path": "annual", "drawdown_path": "mdd",
                                        "correctness_path": "ok", "goal_source": self.goal})
        frozen = self.save("boundary", {"branch": "structure", "status": "first_scheduler_batch",
                                        "affects_running_experiments": False, "evidence": [self.goal],
                                        "quality_contract": quality, **boundary})
        self.batch = self.root / "batch"
        s.start_batch(self.root / "cycle" / "REPORT.json", frozen["path"], self.batch)
        self.context = self.save("context", {"branch": "structure", "context_complete": True, "evidence": [self.goal]})

    def next(self):
        return s.next_decision(self.batch, self.context["path"])

    def observe(self, decision_id, status="verified_information", question="q1", **extra):
        observation = {"status": status, "quality": None, "question": question, "resolved": True, "substantive": True}
        result = {"branch": "structure", "decision_id": decision_id, "decision_followed": True,
                  "observation": observation, "measured_seconds": 1.0, "total_seconds": 2.0,
                  "evidence": [self.text(decision_id + ".txt", decision_id)], **extra}
        if status in {"valid_improvement", "valid_negative"}:
            result["grade_evidence"] = self.save("grade", {"annual": 0.1, "mdd": 0.15, "ok": True})
        return s.observe(self.batch, self.save("result", result)["path"])

    def rehash_chain(self, upto):
        """Re-write every downstream hash reference, as a careful manual edit would."""
        for index in range(1, upto + 1):
            edit(self.batch / f"D{index:04d}.json", lambda o: o.update(batch=s.reference(self.batch / "BATCH.json")))
            outcome = self.batch / f"O{index:04d}.json"
            if outcome.exists():
                edit(outcome, lambda o: o.update(decision=s.reference(self.batch / f"D{index:04d}.json")))

    def test_honest_flow_is_unchanged(self):
        self.start(max_attempts=2)
        self.assertEqual(self.next()["id"], "D0001")
        self.assertEqual(self.next()["status"], "pending_result")
        self.assertEqual(self.observe("D0001")["node"], "n0001")
        self.assertEqual(self.next()["id"], "D0002")
        self.observe("D0002", question="q2")
        self.assertEqual(self.next()["status"], "reflection_due")
        nodes, pending = s.ledger(self.batch)
        self.assertEqual(([n["id"] for n in nodes], pending), (["n0001", "n0002"], None))

    def test_budget_edited_before_first_decision_is_rejected(self):
        self.start(max_attempts=1)
        edit(self.batch / "BATCH.json", lambda o: o["policy"].update(max_attempts=2))
        with self.assertRaisesRegex(ValueError, "BATCH.json disagrees.*policy"):
            self.next()

    def test_budget_edited_after_decisions_with_rehashed_chain_is_rejected(self):
        self.start(max_attempts=1)
        self.next()
        self.observe("D0001")
        self.assertEqual(self.next()["status"], "reflection_due")
        edit(self.batch / "BATCH.json", lambda o: o["policy"].update(max_attempts=2))
        self.rehash_chain(1)
        with self.assertRaisesRegex(ValueError, "BATCH.json disagrees.*policy"):
            self.next()

    def test_mode_or_criteria_edits_are_rejected(self):
        self.start()
        edit(self.batch / "BATCH.json", lambda o: o.update(fallback_policy={**o["policy"], "id": "other"}))
        with self.assertRaisesRegex(ValueError, "fallback_policy"):
            self.next()

    def test_outcome_status_edited_to_improvement_is_rejected(self):
        self.start()
        self.next()
        honest = self.observe("D0001", status="valid_negative")
        self.assertEqual(honest["observation"]["status"], "valid_negative")
        edit(self.batch / "O0001.json", lambda o: o["observation"].update(status="valid_improvement"))
        with self.assertRaisesRegex(ValueError, "O0001.json disagrees with its bound result: observation"):
            self.next()
        with self.assertRaises(ValueError):
            s.ledger(self.batch)

    def edited_outcome_is_rejected(self, change, field):
        self.start()
        self.next()
        self.observe("D0001", status="valid_negative")
        edit(self.batch / "O0001.json", change)
        with self.assertRaisesRegex(ValueError, "O0001.json disagrees.*" + field):
            self.next()

    def test_outcome_quality_edit_is_rejected(self):
        self.edited_outcome_is_rejected(lambda o: o["observation"].update(quality=9.9), "observation")

    def test_outcome_cost_edit_is_rejected(self):
        self.edited_outcome_is_rejected(lambda o: o.update(total_seconds=0.5, measured_seconds=0.5), "measured_seconds")

    def test_outcome_override_edit_is_rejected(self):
        self.edited_outcome_is_rejected(lambda o: o.update(decision_followed=False, override_reason="x"),
                                        "decision_followed")

    def test_outcome_backdated_before_its_decision_is_rejected(self):
        self.start()
        self.next()
        self.observe("D0001")
        edit(self.batch / "O0001.json", lambda o: o.update(at="2000-01-01T00:00:00+00:00"))
        with self.assertRaisesRegex(ValueError, "outcome predates its decision"):
            self.next()

    def test_pending_decision_edited_to_another_anchor_is_rejected(self):
        self.start()
        self.next()
        self.observe("D0001", status="valid_negative")
        self.assertEqual(self.next()["selected"], {"anchor": "root", "action": "explore"})
        edit(self.batch / "D0002.json", lambda o: o.update(selected={"anchor": "n0001", "action": "follow"}))
        with self.assertRaisesRegex(ValueError, "D0002.json disagrees.*selected"):
            self.observe("D0002", question="q2")

    def test_hand_written_decision_beyond_budget_is_rejected(self):
        self.start(max_attempts=1)
        first = self.next()
        self.observe("D0001")
        forged = {**first, "id": "D0002", "at": s.now(), "visible_nodes": ["n0001"]}
        s.write(self.batch / "D0002.json", forged)
        with self.assertRaisesRegex(ValueError, "D0002.json disagrees"):
            self.next()

    def candidate(self, ident, family, information_value):
        spec = self.save("spec-" + ident, {"id": ident, "hypothesis_id": "h-" + ident, "family": family,
                                           "question": "q-" + ident, "mechanism": "m", "falsifier": "f",
                                           "data_clock": "d", "first_test": "t", "stop_rule": "s",
                                           "budget": {"resource_seconds": 10}, "source_evidence": [self.goal]})
        return {"id": ident, "hypothesis_id": "h-" + ident, "family": family, "question": "q-" + ident,
                "rationale": "r", "status": "ready", "action": "explore", "anchor": "root", "depends_on": [],
                "information_value": information_value, "cost_class": 1, "spec": spec}

    def test_frontier_pilot_flow_and_edited_selection(self):
        frontier = self.save("frontier", {"schema": "research_frontier.v1", "branch": "structure",
                                          "generation_evidence": [self.goal],
                                          "candidates": [self.candidate("c1", "fa", 3), self.candidate("c2", "fb", 2),
                                                         self.candidate("c3", "fc", 1)],
                                          "shortfall_reason": "small test pool", "replenishment_action": "add more"})
        self.start(frontier_pilot={"rule": "least_served_family_v1", "max_decisions": 3,
                                   "initial_frontier": frontier, "recent_decisions": []})
        first = self.next()
        self.assertEqual(first["selected"]["candidate_id"], "c1")
        self.observe("D0001", question="q-c1", candidate_id="c1")
        second = self.next()
        self.assertEqual(second["selected"]["candidate_id"], "c2")
        self.assertEqual(self.next()["status"], "pending_result")  # the recomputed history still matches
        self.observe("D0002", question="q-c2", candidate_id="c2")
        third = self.next()
        self.assertEqual(third["selected"]["candidate_id"], "c3")
        edit(self.batch / "D0003.json", lambda o: o["selected"].update(candidate_id="c1", family="fa",
                                                                          hypothesis_id="h-c1", question="q-c1"))
        with self.assertRaisesRegex(ValueError, "D0003.json disagrees.*selected"):
            self.observe("D0003", question="q-c1", candidate_id="c1")


if __name__ == "__main__":
    unittest.main()
