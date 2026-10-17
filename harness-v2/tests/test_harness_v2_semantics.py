"""Tests for the opt-in v2 decision semantics and their v1 defaults.

Scheduler v2 is enabled by `cycle --replay-semantics v2`, boundary `branching` and
`walkforward_contract`; harness v2 by `decision_rule: {rule: paired_sign_wilson_v1}`.
Without them every path must behave exactly as v1. Includes regressions for the four
defects reproduced on 2026-10-17 (budget skip, A/B success, sparse calendar, unbound verdict).
Run from the project root:  python -X utf8 -B -m unittest discover -s tests -p "test_harness_v2_semantics.py" -v
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ENGINE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("research_scheduler")
h = load("research_harness")
outcomes = load("research_outcomes")
timing = load("research_timing")
walk = load("research_walkforward")
digest_mod = load("research_line_digest")
ab = load("research_online_ab")


class Temp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.serial = 0

    def tearDown(self):
        self._tmp.cleanup()

    def save(self, name, value):
        self.serial += 1
        path = self.root / f"{self.serial:04d}-{name}.json"
        s.write(path, value)
        return s.reference(path)

    def text(self, name, body="evidence\n"):
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        return s.reference(path)


def metrics(**values):
    base = {"best_quality": 1.0, "resolved_questions": 1, "observed_nodes": 2, "unsupported_actions": 0,
            "decision_calls": 2, "requests": 2, "unobserved_share": 0.0, "total_seconds": None,
            "measured_seconds": None, "all_in_method_cost_seconds": None, "implementation_errors": 0,
            "evaluation_stage": "economic", "cost_scope": "recorded_node_total_seconds"}
    base.update(values)
    return base


def run(world, **values):
    return {"world": world, "metrics": metrics(**values), "method_observations": [], "method_regression_questions": []}



class SchedulerComparisonTests(unittest.TestCase):
    def test_default_semantics_is_the_original_v1_rule(self):
        candidate = [run("w1", best_quality=1.2, unsupported_actions=1, decision_calls=4)]
        result = s.rrsi_compare(candidate, [run("w1")])
        self.assertIn("w1:unknown_quality_or_unobserved_path", result["reasons"])
        self.assertNotIn("cost_known", result)

    def test_unrecorded_choices_no_longer_void_the_candidate(self):
        # v1: any unsupported action -> "unknown_quality_or_unobserved_path" -> rejected.
        candidate = [run("w1", best_quality=1.2, unsupported_actions=1, decision_calls=4,
                         unobserved_share=0.25, observed_nodes=2, requests=3)]
        incumbent = [run("w1", best_quality=1.0, observed_nodes=3, requests=3, decision_calls=3)]
        result = s.rrsi_compare(candidate, incumbent, semantics="v2")
        self.assertTrue(result["accepted"], result)
        self.assertTrue(result["complete"])

    def test_mostly_unrecorded_replay_stays_incomplete(self):
        candidate = [run("w1", best_quality=2.0, unsupported_actions=3, decision_calls=4, unobserved_share=0.75)]
        incumbent = [run("w1")]
        result = s.rrsi_compare(candidate, incumbent, semantics="v2")
        self.assertFalse(result["accepted"])
        self.assertFalse(result["complete"])
        self.assertIn("w1:insufficient_observed_coverage", result["reasons"])

    def test_unknown_cost_is_flagged_not_blocking(self):
        result = s.rrsi_compare([run("w1", best_quality=1.5)], [run("w1")], semantics="v2")
        self.assertTrue(result["accepted"])
        self.assertFalse(result["cost_known"])
        self.assertEqual(result["notes"], ["w1:cost_unknown_not_blocking"])

    def test_known_cost_increase_still_rejects(self):
        result = s.rrsi_compare([run("w1", best_quality=1.5, total_seconds=20.0)],
                                [run("w1", total_seconds=10.0)], semantics="v2")
        self.assertFalse(result["accepted"])
        self.assertIn("w1:total_cost_increase:total_seconds", result["reasons"])

    def test_measured_seconds_used_when_total_is_missing(self):
        result = s.rrsi_compare([run("w1", measured_seconds=5.0)], [run("w1", measured_seconds=9.0)], semantics="v2")
        self.assertTrue(result["accepted"], result)  # same quality, measured cost strictly lower
        self.assertTrue(result["cost_known"])

    def test_unrecorded_choices_count_as_requests(self):
        # Revealing fewer nodes by wandering into unrecorded choices is not a saving.
        candidate = [run("w1", observed_nodes=1, unsupported_actions=1, decision_calls=2,
                         unobserved_share=0.5, requests=2)]
        result = s.rrsi_compare(candidate, [run("w1", observed_nodes=2, requests=2)], semantics="v2")
        self.assertIn("no_strict_advantage", result["reasons"])


class SchedulerBatchTests(Temp):
    def world_node(self, ident, parent, depends, status="verified_information", quality=None, question=None,
                   total=4.0, measured=2.0):
        evidence = self.text(ident + ".txt", ident)
        return {"id": ident, "parent": parent, "depends_on": depends,
                "decision_at": f"2026-10-0{1 + int(ident[1:]) % 8}T00:00:00+00:00",
                "available_at": f"2026-10-0{1 + int(ident[1:]) % 8}T01:00:00+00:00",
                "observation": {"status": status, "quality": quality, "question": question or ("q" + ident),
                                "resolved": True, "substantive": True},
                "measured_seconds": measured, "total_seconds": total, "evidence": [evidence]}

    def world(self, nodes, usage="development"):
        return {"schema": s.SCHEMA, "id": "w" + str(self.serial), "branch": "structure", "admission": "diagnostic",
                "usage": usage, "limitations": [], "initial_quality": 0.5, "metric_contract": "test", "nodes": nodes}

    def test_pessimistic_cost_charge_in_replay(self):
        nodes = [self.world_node("n1", "root", [], total=4.0, measured=2.0),
                 self.world_node("n2", "root", ["n1"], total=6.0, measured=3.0),
                 self.world_node("n3", "n2", ["n1", "n2"], total=10.0, measured=5.0)]
        result = s.replay(self.world(nodes), s.initial_policy(), semantics="v2")
        self.assertIsNone(s.replay(self.world(nodes), s.initial_policy())["metrics"]["total_seconds"])  # v1 default
        m = result["metrics"]
        # explore root -> n1; follow n1 -> unrecorded; explore root -> n2; follow n2 -> n3
        self.assertEqual([t["status"] for t in result["trace"]], ["revealed", "unsupported", "revealed", "revealed"])
        self.assertEqual((m["observed_nodes"], m["unsupported_actions"], m["requests"]), (3, 1, 4))
        self.assertEqual(m["total_seconds"], 4.0 + 6.0 + 10.0 + 1 * 10.0)
        self.assertEqual(m["measured_seconds"], 2.0 + 3.0 + 5.0 + 1 * 5.0)
        self.assertEqual(m["unobserved_share"], 0.25)
        self.assertEqual(m["replay_semantics"], s.REPLAY_SEMANTICS)
        # v1 would have reported total_seconds=None here and voided the whole comparison.

    def make_batch(self, siblings):
        nodes = [self.world_node("n1", "root", [])]
        world_path = self.root / "world.json"
        s.write(world_path, self.world(nodes))
        report = s.cycle([str(world_path)], None, self.root / "cycle", "structure")
        self.assertEqual(report["decision"], "retain_incumbent")
        goal = self.text("goal.txt", "goal\n")
        quality = self.save("quality", {"schema": "research_quality.v1", "kind": "risk_cap_return_v1",
                                        "drawdown_ceiling": 0.2, "annual_path": "annual", "drawdown_path": "mdd",
                                        "correctness_path": "ok", "goal_source": goal})
        boundary = self.save("boundary", {"branch": "structure", "status": "first_scheduler_batch",
                                          "affects_running_experiments": False, "evidence": [goal],
                                          "quality_contract": quality, "branching": {"siblings": siblings}})
        directory = self.root / "batch"
        s.start_batch(self.root / "cycle" / "REPORT.json", boundary["path"], directory)
        context = self.save("context", {"branch": "structure", "context_complete": True, "evidence": [goal]})
        return directory, context

    def result(self, decision_id, question):
        evidence = self.text(decision_id + "-out.txt", decision_id)
        return self.save("result", {"branch": "structure", "decision_id": decision_id, "decision_followed": True,
                                    "observation": {"status": "verified_information", "quality": None,
                                                    "question": question, "resolved": True, "substantive": True},
                                    "measured_seconds": 1.0, "total_seconds": 2.0, "evidence": [evidence]})

    def test_siblings_build_a_tree_that_replay_can_branch_on(self):
        directory, context = self.make_batch(siblings=2)
        opened = s.next_decision(directory, context["path"])
        self.assertEqual(opened["status"], "sibling_decisions")
        ids = [d["id"] for d in opened["decisions"]]
        self.assertEqual(ids, ["D0001", "D0002"])
        self.assertTrue(all(d["selected"] == {"anchor": "root", "action": "explore"} for d in opened["decisions"]))
        waiting = s.next_decision(directory, context["path"])
        self.assertEqual(waiting["status"], "pending_result")
        self.assertEqual(waiting["decisions"], ids)
        # Outcomes may arrive out of order; node ids follow decision ids.
        self.assertEqual(s.observe(directory, self.result("D0002", "qb")["path"])["node"], "n0002")
        self.assertEqual(s.observe(directory, self.result("D0001", "qa")["path"])["node"], "n0001")
        nodes, pending = s.ledger_all(directory)
        self.assertEqual([n["id"] for n in nodes], ["n0001", "n0002"])
        self.assertEqual(pending, [])
        exported = self.root / "exported.json"
        s.export_world(directory, exported)
        world = s.read(exported)
        self.assertEqual([n["parent"] for n in world["nodes"]], ["root", "root"])
        # Replay reaches the second recorded sibling instead of stopping at one path.
        replayed = s.replay(world, {**s.initial_policy(), "max_attempts": 4})
        self.assertEqual(replayed["metrics"]["observed_nodes"], 2)
        # The sibling width is part of the frozen boundary, not an editable batch field.
        width_edit = json.loads((directory / "BATCH.json").read_text(encoding="utf-8"))
        width_edit["branching"]["siblings"] = 3
        (directory / "BATCH.json").write_text(json.dumps(width_edit, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "BATCH.json disagrees.*branching"):
            s.batch(directory)

    def test_edited_or_extra_sibling_decision_is_rejected(self):
        directory, context = self.make_batch(siblings=2)
        s.next_decision(directory, context["path"])
        sibling = directory / "D0002.json"
        honest = sibling.read_text(encoding="utf-8")
        edited = json.loads(honest)
        edited["sibling"]["index"] = 1
        sibling.write_text(json.dumps(edited, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "D0002.json disagrees"):
            s.next_decision(directory, context["path"])
        sibling.write_text(honest, encoding="utf-8")
        extra = json.loads(honest)
        extra.update(id="D0003", sibling={**extra["sibling"], "index": 3})
        s.write(directory / "D0003.json", extra)
        with self.assertRaisesRegex(ValueError, "D0003.json disagrees"):
            s.observe(directory, self.result("D0001", "qa")["path"])

    def test_single_decision_flow_is_unchanged(self):
        directory, context = self.make_batch(siblings=1)
        opened = s.next_decision(directory, context["path"])
        self.assertEqual(opened["id"], "D0001")
        self.assertNotIn("sibling", opened)
        self.assertEqual(s.observe(directory, self.result("D0001", "qa")["path"])["node"], "n0001")

    def quality(self):
        goal = self.text("goal.txt", "goal\n")
        return goal, self.save("quality", {"schema": "research_quality.v1", "kind": "risk_cap_return_v1",
                                           "drawdown_ceiling": 0.2, "annual_path": "annual", "drawdown_path": "mdd",
                                           "correctness_path": "ok", "goal_source": goal})

    def two_branch_world(self, usage, quality, admission, limitations):
        failed = self.world_node("n1", "root", [], status="implementation_error")
        failed["observation"]["resolved"] = False
        good = self.world_node("n2", "root", ["n1"], status="valid_improvement", quality=0.8, total=6.0)
        world = {**self.world([failed, good], usage), "id": "w-" + usage, "admission": admission,
                 "limitations": limitations, "metric_contract": s.metric_identity("structure", quality)}
        if admission == "prospective":
            world.update(context_complete=True, batch_contract=self.text("batch.txt", "batch\n"),
                         quality_contract=quality)
        else:
            world["quality_contract"] = quality
        path = self.root / f"world-{usage}.json"
        s.write(path, world)
        return path

    def test_pilot_offer_is_reachable_with_diagnostic_development_worlds(self):
        # Incumbent order repair>follow>explore wastes a request repairing n1 (no recorded
        # repair), so its replay has an unrecorded choice. v1 voided this comparison
        # ("unknown_quality_or_unobserved_path") and also required every world, development
        # included, to be prospective with no limitation. v2 needs only a clean heldout.
        _, quality = self.quality()
        development = self.two_branch_world("development", quality, "diagnostic", ["policy override recorded"])
        heldout = self.two_branch_world("heldout", quality, "prospective", [])
        legacy = s.cycle([str(development), str(heldout)], None, self.root / "rrsi_v1", "structure", rrsi=True)
        self.assertEqual(legacy["decision"], "retain_incumbent")  # unchanged default behaviour
        report = s.cycle([str(development), str(heldout)], None, self.root / "rrsi", "structure", rrsi=True,
                         replay_semantics="v2")
        self.assertEqual(report["decision"], "eligible_for_bounded_pilot", report["rrsi"]["attempts"])
        self.assertNotEqual(report["next_policy"], report["incumbent"])
        self.assertEqual(report["contract"] and s.read(self.root / "rrsi" / "CONTRACT.json")["replay_semantics"],
                         s.REPLAY_SEMANTICS)

    def equity(self, name, yearly):
        from datetime import date, timedelta
        rows, value, d = [], 100000.0, date(2020, 1, 2)
        while d <= date(2025, 12, 31):
            if d.weekday() < 5:
                value *= yearly.get(d.year, 1.0) ** (1 / 250)
                rows.append({"date": d.isoformat(), "equity": value})
            d += timedelta(days=1)
        path = self.root / name
        path.write_text(json.dumps(rows), encoding="utf-8")
        return s.reference(path)

    def walkforward_batch(self):
        nodes = [self.world_node("n1", "root", [])]
        world_path = self.root / "world.json"
        s.write(world_path, self.world(nodes))
        s.cycle([str(world_path)], None, self.root / "cycle", "structure")
        goal, quality = self.quality()
        contract = self.save("walkforward", walk.DEFAULT_CONTRACT)
        boundary = {"branch": "structure", "status": "first_scheduler_batch",
                    "affects_running_experiments": False, "evidence": [goal],
                    "quality_contract": quality, "walkforward_contract": contract}
        with self.assertRaisesRegex(ValueError, "bind walkforward_runtime"):
            s.start_batch(self.root / "cycle" / "REPORT.json", self.save("unbound", boundary)["path"], self.root / "x")
        boundary["walkforward_runtime"] = s.reference(ENGINE / "research_walkforward.py")
        directory = self.root / "batch"
        s.start_batch(self.root / "cycle" / "REPORT.json", self.save("boundary", boundary)["path"], directory)
        context = self.save("context", {"branch": "structure", "context_complete": True, "evidence": [goal]})
        s.next_decision(directory, context["path"])
        return directory, goal

    def economic_result(self, status, verdict, evidence):
        grade = self.save("grade", {"annual": 2.5, "mdd": 0.15, "ok": True})
        return self.save("result", {"branch": "structure", "decision_id": "D0001", "decision_followed": True,
                                    "observation": {"status": status, "quality": None, "question": "q1",
                                                    "resolved": True, "substantive": True},
                                    "grade_evidence": grade, "walkforward": verdict,
                                    "measured_seconds": 1.0, "total_seconds": 2.0, "evidence": evidence})

    def test_development_gain_without_walkforward_pass_is_not_an_improvement(self):
        directory, goal = self.walkforward_batch()
        parent = self.equity("parent.json", {y: 2.0 for y in range(2020, 2026)})
        worse = self.equity("cand.json", {2020: 3, 2021: 3, 2022: 3, 2023: 1.5, 2024: 2.5, 2025: 1.5})
        verdict = walk.evaluate(walk.DEFAULT_CONTRACT, worse["path"], parent["path"])
        self.assertEqual(verdict["decision"], "fail")
        ref = self.save("verdict", walk.masked(verdict))  # the Agent-visible copy is enough to recompute
        with self.assertRaises(ValueError):
            s.observe(directory, self.economic_result("valid_improvement", ref, [goal, worse, parent])["path"])
        observed = s.observe(directory, self.economic_result("valid_negative", ref, [goal, worse, parent])["path"])
        self.assertEqual(observed["walkforward_decision"], "fail")
        # The stored verdict is recomputed on every read, not trusted from the O record.
        record = directory / "O0001.json"
        stored = json.loads(record.read_text(encoding="utf-8"))
        stored["walkforward_decision"] = "pass"
        stored["observation"]["status"] = "valid_improvement"
        record.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "O0001.json disagrees.*walkforward_decision"):
            s.ledger_all(directory)
        # So is the frozen scorer copy.
        record.write_text(json.dumps({**stored, "walkforward_decision": "fail",
                                      "observation": observed["observation"]}, indent=2) + "\n", encoding="utf-8")
        s.ledger_all(directory)
        with (directory / "WALKFORWARD.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# edited\n")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            s.batch(directory)

    def test_forged_or_foreign_walkforward_verdict_is_rejected(self):
        directory, goal = self.walkforward_batch()
        parent = self.equity("parent.json", {y: 2.0 for y in range(2020, 2026)})
        worse = self.equity("cand.json", {2020: 3, 2021: 3, 2022: 3, 2023: 1.5, 2024: 2.5, 2025: 1.5})
        # 1) a bare "pass" with no equity files (the reported counterexample)
        bare = self.save("bare", {"schema": "research_walkforward_verdict.v1", "contract": walk.DEFAULT_CONTRACT,
                                  "decision": "pass"})
        with self.assertRaises(ValueError):
            s.observe(directory, self.economic_result("valid_improvement", bare, [goal])["path"])
        # 2) a real verdict whose decision field was edited to pass
        forged = walk.evaluate(walk.DEFAULT_CONTRACT, worse["path"], parent["path"])
        forged["decision"], forged["fold_wins"] = "pass", 3
        forged_ref = self.save("forged", forged)
        with self.assertRaises(ValueError):
            s.observe(directory, self.economic_result("valid_improvement", forged_ref, [goal, worse, parent])["path"])
        # 3) a genuine pass for some other experiment's equity, not listed in this result
        better = self.equity("other.json", {y: 3.0 for y in range(2020, 2026)})
        other = self.save("other", walk.evaluate(walk.DEFAULT_CONTRACT, better["path"], parent["path"]))
        with self.assertRaises(ValueError):
            s.observe(directory, self.economic_result("valid_improvement", other, [goal, worse, parent])["path"])

    def test_branching_bounds(self):
        goal = self.text("goal.txt")
        with self.assertRaises(ValueError):
            nodes = [self.world_node("n1", "root", [])]
            world_path = self.root / "w.json"
            s.write(world_path, self.world(nodes))
            s.cycle([str(world_path)], None, self.root / "c", "structure")
            quality = self.save("q", {"schema": "research_quality.v1", "kind": "risk_cap_return_v1",
                                      "drawdown_ceiling": 0.2, "annual_path": "a", "drawdown_path": "m",
                                      "correctness_path": "ok", "goal_source": goal})
            boundary = self.save("b", {"branch": "structure", "status": "first_scheduler_batch",
                                       "affects_running_experiments": False, "evidence": [goal],
                                       "quality_contract": quality, "branching": {"siblings": 5}})
            s.start_batch(self.root / "c" / "REPORT.json", boundary["path"], self.root / "x")


class HarnessDecisionTests(unittest.TestCase):
    api = s.harness_api()

    def cfg(self, **rule):
        resolved = {"rule": h.DECISION_RULE, "min_cases": 16, "confidence": 0.9, "critical_checks": [],
                    "max_unpaired_share": 0.2, "cost_tolerance": 0.0, **rule}
        return {"decision_rule_resolved": resolved, "limits": {"resource_seconds": 100}, "kind": "development"}

    @staticmethod
    def attempt(passed, total=2, status="completed", cost=None):
        checks = {f"c{i}": i < passed for i in range(total)}
        return {"status": status, "checks": checks if status == "completed" else {},
                "cost": {"all_in_method_cost_seconds": cost}}

    def test_one_noisy_regression_no_longer_vetoes_a_clear_gain(self):
        baseline = [self.attempt(1) for _ in range(16)]
        candidate = [self.attempt(2) for _ in range(13)] + [self.attempt(0)] + [self.attempt(1)] * 2
        result = h.compare_attempts(self.api, baseline, candidate, self.cfg())
        self.assertEqual((result["wins"], result["losses"], result["ties"]), (13, 1, 2))
        self.assertTrue(result["accepted"], result)
        self.assertFalse(result["cost_known"])

    def test_small_or_balanced_samples_do_not_pass(self):
        few = h.compare_attempts(self.api, [self.attempt(1)] * 4, [self.attempt(2)] * 4, self.cfg())
        self.assertIn("fewer than 16 paired cases", few["reasons"])
        balanced = h.compare_attempts(self.api, [self.attempt(1)] * 16,
                                      [self.attempt(2)] * 9 + [self.attempt(0)] * 7, self.cfg())
        self.assertIn("no significant advantage", balanced["reasons"])

    def test_critical_check_is_a_hard_stop(self):
        baseline = [self.attempt(2) for _ in range(16)]
        candidate = [self.attempt(2) for _ in range(16)]
        candidate[3] = {"status": "completed", "checks": {"c0": False, "c1": True}, "cost": {"all_in_method_cost_seconds": None}}
        result = h.compare_attempts(self.api, baseline, candidate, self.cfg(critical_checks=["c0"]))
        self.assertIn("critical check regressed", result["reasons"])

    def test_failed_candidate_attempt_counts_as_loss(self):
        # 11 clean wins would pass if the 5 failures were dropped; counted as losses they do not.
        baseline = [self.attempt(1) for _ in range(16)]
        candidate = [self.attempt(2) for _ in range(11)] + [self.attempt(0, status="failed")] * 5
        result = h.compare_attempts(self.api, baseline, candidate, self.cfg())
        self.assertEqual((result["wins"], result["losses"]), (11, 5))
        self.assertFalse(result["accepted"])
        # A failed baseline attempt leaves the case unpaired instead of handing the candidate a win.
        baseline[0] = self.attempt(0, status="failed")
        unpaired = h.compare_attempts(self.api, baseline, [self.attempt(2)] * 16, self.cfg())
        self.assertEqual(unpaired["unpaired"], 1)

    def test_known_cost_saving_without_losses_is_a_gain(self):
        baseline = [self.attempt(1, cost=10.0) for _ in range(16)]
        candidate = [self.attempt(1, cost=6.0) for _ in range(16)]
        result = h.compare_attempts(self.api, baseline, candidate, self.cfg())
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["total_seconds"], [160.0, 96.0])

    def test_decision_rule_is_opt_in(self):
        self.assertEqual(h.decision_rule(self.api, {"kind": "development"}), {"rule": "legacy_v1"})
        v2 = {"rule": h.DECISION_RULE}
        self.assertEqual(h.decision_rule(self.api, {"kind": "development", "decision_rule": v2})["min_cases"], 16)
        with self.assertRaises(ValueError):
            h.decision_rule(self.api, {"kind": "development", "decision_rule": {**v2, "min_cases": 4}})
        self.assertEqual(h.decision_rule(self.api, {"kind": "synthetic", "decision_rule": v2})["min_cases"], 1)

    def test_without_rule_the_original_v1_comparison_applies(self):
        legacy = {"limits": {"resource_seconds": 100}, "kind": "development"}
        baseline = [self.attempt(1, cost=1.0) for _ in range(16)]
        candidate = [self.attempt(2, cost=1.0) for _ in range(15)] + [self.attempt(0, cost=1.0)]
        result = h.compare_attempts(self.api, baseline, candidate, legacy)
        self.assertIn("previously passing check regressed", result["reasons"])

    def test_measured_attempt_over_budget_rejects_even_if_other_costs_unknown(self):
        baseline = [self.attempt(1, cost=10.0) for _ in range(16)]
        candidate = [self.attempt(2, cost=10.0) for _ in range(16)]
        candidate[0] = self.attempt(2, cost=1000.0)
        candidate[1] = self.attempt(2, cost=None)
        result = h.compare_attempts(self.api, baseline, candidate, self.cfg())
        self.assertFalse(result["accepted"])
        self.assertIn("attempt exceeded frozen resource budget", result["reasons"])

    def test_pilot_cannot_pass_with_unknown_cost(self):
        cfg = {**self.cfg(min_cases=8), "kind": "prospective_pilot"}
        result = h.compare_attempts(self.api, [self.attempt(1) for _ in range(8)], [self.attempt(2) for _ in range(8)], cfg)
        self.assertIn("bounded pilot needs complete measured costs", result["reasons"])

    def test_wilson_lower_bound_thresholds(self):
        z = h.CONFIDENCE_Z[0.9]
        self.assertGreater(h.wilson_lower(12, 16, z), 0.5)
        self.assertLess(h.wilson_lower(11, 16, z), 0.5)


class HarnessProposalChecks(unittest.TestCase):
    def bundle(self, generation_prompt):
        base = json.loads((ENGINE / "research_harness_default.json").read_text(encoding="utf-8"))
        changed = json.loads(json.dumps(base))
        changed["prompts"]["generation"] = generation_prompt
        return base, changed

    def test_default_bundle_is_leak_free(self):
        base, _ = self.bundle("x")
        self.assertEqual(h.bundle_leakage(None, base), [])

    def test_codes_and_dates_in_a_new_prompt_are_flagged(self):
        base, changed = self.bundle("优先跟踪 600519.XSHG，在 2024-02-05 之后放宽止损")
        kinds = {item["kind"] for item in h.bundle_leakage(base, changed)}
        self.assertEqual(kinds, {"security_code", "calendar_date"})

    def test_plain_numbers_and_years_are_not_flagged(self):
        base, changed = self.bundle("最多 300000 字符；2023 年为验证段；候选上限 8")
        self.assertEqual(h.bundle_leakage(base, changed), [])

    def test_model_families(self):
        self.assertEqual(h.model_family("gpt-6.1-sol"), h.model_family("gpt-6-astra"))
        self.assertNotEqual(h.model_family("gpt-6.1-sol"), h.model_family("claude-opus-5-5"))
        self.assertEqual(h.model_family("txds/deepseek-v4-flash"), "deepseek")


if __name__ == "__main__":
    unittest.main()
