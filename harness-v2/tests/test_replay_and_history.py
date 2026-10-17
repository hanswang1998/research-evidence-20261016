"""Regressions from the 1.3.0 review (2026-10-17).

1. Re-deriving a stored BATCH/D/O must not re-check the evidence lists it cited when it was written.
   Decision contexts cite living documents (39 of 2,804 real contexts cite shared rule docs or
   CURRENT_RULES.md, and each harness release edits those docs); re-checking them on every read
   stopped a running batch at the next doc edit. Tampering with the records is still rejected.
2. A new harness round must be able to learn from a pilot round's REVIEW: the pilot re-runs the
   development winner, whose proposal belongs to the round that offered the pilot.

Scheduler tests use only v1 features, so they run on 1.3.0 and on the harness-v2 branch alike.
The harness test reuses the original fixtures and is skipped without tests/test_research_frontier.py.
Run from the project root:  python -X utf8 -B -m unittest discover -s tests -p "test_replay_and_history.py" -v
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

try:
    import test_research_harness as original
except ImportError:  # test_research_frontier.py is not in every checkout
    original = None


class ReplayEvidenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.serial = 0
        self.doc = self.root / "研究调度运行手册.md"
        self.doc.write_text("shared runbook, release 1\n", encoding="utf-8")
        self.goal_doc = self.root / "当前研究目标.md"
        self.goal_doc.write_text("goal, release 1\n", encoding="utf-8")

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

    def start(self):
        evidence = self.text("n1.txt", "n1")
        node = {"id": "n1", "parent": "root", "depends_on": [], "decision_at": "2026-10-01T00:00:00+00:00",
                "available_at": "2026-10-01T01:00:00+00:00",
                "observation": {"status": "verified_information", "quality": None, "question": "q",
                                "resolved": True, "substantive": True},
                "measured_seconds": 1.0, "total_seconds": 2.0, "evidence": [evidence]}
        s.write(self.root / "world.json", {"schema": s.SCHEMA, "id": "w", "branch": "structure",
                                          "admission": "diagnostic", "usage": "development", "limitations": [],
                                          "initial_quality": 0.5, "metric_contract": "t", "nodes": [node]})
        s.cycle([str(self.root / "world.json")], None, self.root / "cycle", "structure")
        quality = self.save("quality", {"schema": "research_quality.v1", "kind": "risk_cap_return_v1",
                                        "drawdown_ceiling": 0.2, "annual_path": "annual", "drawdown_path": "mdd",
                                        "correctness_path": "ok", "goal_source": s.reference(self.goal_doc)})
        boundary = self.save("boundary", {"branch": "structure", "status": "first_scheduler_batch",
                                          "affects_running_experiments": False,
                                          "evidence": [s.reference(self.doc)], "quality_contract": quality})
        self.batch = self.root / "batch"
        s.start_batch(self.root / "cycle" / "REPORT.json", boundary["path"], self.batch)

    def context(self):
        return self.save("context", {"branch": "structure", "context_complete": True,
                                     "evidence": [s.reference(self.doc), s.reference(self.goal_doc)]})

    def observe(self, decision_id, status="verified_information"):
        result = {"branch": "structure", "decision_id": decision_id, "decision_followed": True,
                  "observation": {"status": status, "quality": None, "question": "q-" + decision_id,
                                  "resolved": True, "substantive": True},
                  "measured_seconds": 1.0, "total_seconds": 2.0,
                  "evidence": [self.text(decision_id + ".txt", decision_id)]}
        if status in {"valid_improvement", "valid_negative"}:
            result["grade_evidence"] = self.save("grade", {"annual": 0.1, "mdd": 0.15, "ok": True})
        return s.observe(self.batch, self.save("result", result)["path"])

    def test_editing_a_doc_cited_by_an_earlier_context_keeps_the_batch_running(self):
        self.start()
        first = s.next_decision(self.batch, self.context()["path"])
        self.observe(first["id"])
        self.doc.write_text("shared runbook, release 2\n", encoding="utf-8")
        self.assertEqual(s.next_decision(self.batch, self.context()["path"])["id"], "D0002")

    def test_editing_the_goal_doc_after_an_economic_outcome_keeps_the_batch_running(self):
        self.start()
        first = s.next_decision(self.batch, self.context()["path"])
        self.observe(first["id"], status="valid_negative")
        self.goal_doc.write_text("goal, release 2\n", encoding="utf-8")
        self.assertEqual(s.next_decision(self.batch, self.context()["path"])["selected"],
                         {"anchor": "root", "action": "explore"})

    def test_a_new_decision_still_checks_its_own_context_evidence(self):
        self.start()
        stale = self.context()
        self.doc.write_text("shared runbook, release 2\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            s.next_decision(self.batch, stale["path"])
        self.assertFalse((self.batch / "D0001.json").exists())
        self.assertFalse(s.REPLAY["active"])

    def test_edited_records_are_still_rejected_after_a_doc_edit(self):
        self.start()
        first = s.next_decision(self.batch, self.context()["path"])
        self.observe(first["id"], status="valid_negative")
        self.doc.write_text("shared runbook, release 2\n", encoding="utf-8")
        outcome = self.batch / "O0001.json"
        stored = json.loads(outcome.read_text(encoding="utf-8"))
        stored["observation"]["status"] = "valid_improvement"
        outcome.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "O0001.json disagrees"):
            s.next_decision(self.batch, self.context()["path"])
        self.assertFalse(s.REPLAY["active"])


if original is not None:
    Fixture = type("Fixture", (unittest.TestCase,),
                   {k: v for k, v in vars(original.HarnessTests).items() if not k.startswith("test")})
else:
    Fixture = unittest.TestCase


@unittest.skipIf(original is None, "needs tests/test_research_frontier.py from the project")
class PilotHistoryTests(Fixture):
    def test_new_round_can_learn_from_a_pilot_review(self):
        directory, candidate, _ = self.compared()
        entry = self.make_case("pilot-new")
        pilot = self.start_round(kind="prospective_pilot", pilot_offer=s.reference(directory / "REVIEW.json"), cases=[entry])
        self.attempt(pilot, self.base, entry, checks=(True, False))
        self.attempt(pilot, candidate, entry)
        self.assertEqual(self.call(pilot, "review")["decision"], "supported_in_bounded_pilot")
        follow = self.start_round(incumbent=candidate, previous_review=s.reference(pilot / "REVIEW.json"))
        task = s.read(follow / "REFLECTION_TASK.json")
        self.assertEqual(task["lineage"][-1]["kind"], "prospective_pilot")
        self.assertEqual(task["lineage"][-1]["candidates"][0]["bundle"], candidate)


if __name__ == "__main__":
    unittest.main()
