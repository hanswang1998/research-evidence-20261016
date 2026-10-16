"""Tests for the v2 changes. Standard library only; no models, research runs or platform calls.

Run from the project root:  python -X utf8 -B -m unittest discover -s tests -p "test_harness_v2.py" -v
The pre-existing suite (test_research_harness.py) also needs test_research_frontier.py,
which lives in the project but was not part of the 2026-10-16 evidence package.
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
    def test_unrecorded_choices_no_longer_void_the_candidate(self):
        # v1: any unsupported action -> "unknown_quality_or_unobserved_path" -> rejected.
        candidate = [run("w1", best_quality=1.2, unsupported_actions=1, decision_calls=4,
                         unobserved_share=0.25, observed_nodes=2, requests=3)]
        incumbent = [run("w1", best_quality=1.0, observed_nodes=3, requests=3, decision_calls=3)]
        result = s.rrsi_compare(candidate, incumbent)
        self.assertTrue(result["accepted"], result)
        self.assertTrue(result["complete"])

    def test_mostly_unrecorded_replay_stays_incomplete(self):
        candidate = [run("w1", best_quality=2.0, unsupported_actions=3, decision_calls=4, unobserved_share=0.75)]
        incumbent = [run("w1")]
        result = s.rrsi_compare(candidate, incumbent)
        self.assertFalse(result["accepted"])
        self.assertFalse(result["complete"])
        self.assertIn("w1:insufficient_observed_coverage", result["reasons"])

    def test_unknown_cost_is_flagged_not_blocking(self):
        result = s.rrsi_compare([run("w1", best_quality=1.5)], [run("w1")])
        self.assertTrue(result["accepted"])
        self.assertFalse(result["cost_known"])
        self.assertEqual(result["notes"], ["w1:cost_unknown_not_blocking"])

    def test_known_cost_increase_still_rejects(self):
        result = s.rrsi_compare([run("w1", best_quality=1.5, total_seconds=20.0)],
                                [run("w1", total_seconds=10.0)])
        self.assertFalse(result["accepted"])
        self.assertIn("w1:total_cost_increase:total_seconds", result["reasons"])

    def test_measured_seconds_used_when_total_is_missing(self):
        result = s.rrsi_compare([run("w1", measured_seconds=5.0)], [run("w1", measured_seconds=9.0)])
        self.assertTrue(result["accepted"], result)  # same quality, measured cost strictly lower
        self.assertTrue(result["cost_known"])

    def test_unrecorded_choices_count_as_requests(self):
        # Revealing fewer nodes by wandering into unrecorded choices is not a saving.
        candidate = [run("w1", observed_nodes=1, unsupported_actions=1, decision_calls=2,
                         unobserved_share=0.5, requests=2)]
        result = s.rrsi_compare(candidate, [run("w1", observed_nodes=2, requests=2)])
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
        result = s.replay(self.world(nodes), s.initial_policy())
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
        report = s.cycle([str(development), str(heldout)], None, self.root / "rrsi", "structure", rrsi=True)
        self.assertEqual(report["decision"], "eligible_for_bounded_pilot", report["rrsi"]["attempts"])
        self.assertNotEqual(report["next_policy"], report["incumbent"])
        self.assertEqual(report["contract"] and s.read(self.root / "rrsi" / "CONTRACT.json")["replay_semantics"],
                         s.REPLAY_SEMANTICS)

    def test_development_gain_without_walkforward_pass_is_not_an_improvement(self):
        nodes = [self.world_node("n1", "root", [])]
        world_path = self.root / "world.json"
        s.write(world_path, self.world(nodes))
        s.cycle([str(world_path)], None, self.root / "cycle", "structure")
        goal, quality = self.quality()
        contract = self.save("walkforward", walk.DEFAULT_CONTRACT)
        boundary = self.save("boundary", {"branch": "structure", "status": "first_scheduler_batch",
                                          "affects_running_experiments": False, "evidence": [goal],
                                          "quality_contract": quality, "walkforward_contract": contract})
        directory = self.root / "batch"
        s.start_batch(self.root / "cycle" / "REPORT.json", boundary["path"], directory)
        context = self.save("context", {"branch": "structure", "context_complete": True, "evidence": [goal]})
        s.next_decision(directory, context["path"])
        grade = self.save("grade", {"annual": 2.5, "mdd": 0.15, "ok": True})
        failed = self.save("verdict", {"schema": "research_walkforward_verdict.v1", "contract": walk.DEFAULT_CONTRACT,
                                       "decision": "fail"})
        def result(status, verdict):
            return self.save("result", {"branch": "structure", "decision_id": "D0001", "decision_followed": True,
                                        "observation": {"status": status, "quality": None, "question": "q1",
                                                        "resolved": True, "substantive": True},
                                        "grade_evidence": grade, "walkforward": verdict,
                                        "measured_seconds": 1.0, "total_seconds": 2.0, "evidence": [goal]})
        with self.assertRaises(ValueError):
            s.observe(directory, result("valid_improvement", failed)["path"])
        observed = s.observe(directory, result("valid_negative", failed)["path"])
        self.assertEqual(observed["walkforward_decision"], "fail")

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

    def test_decision_rule_floor(self):
        spec = {"kind": "development"}
        self.assertEqual(h.decision_rule(self.api, spec)["min_cases"], 16)
        with self.assertRaises(ValueError):
            h.decision_rule(self.api, {"kind": "development", "decision_rule": {"min_cases": 4}})
        self.assertEqual(h.decision_rule(self.api, {"kind": "synthetic"})["min_cases"], 1)

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


class OutcomeTests(unittest.TestCase):
    def test_canonical_mapping(self):
        cases = [("valid_improvement", "account", "improvement"), ("valid_negative", "account", "negative"),
                 ("closed_complete_account_profit_or_drawdown_gate_failed", "account", "negative"),
                 ("complete_risk_limit_failed", "account", "risk_limit"),
                 ("implementation_error", "engineering", "engineering_failure"),
                 ("closed_right_censored_lifecycle_economically_unscored", "account", "evidence_gap"),
                 ("verified_information", "diagnostic", "information"),
                 ("closed_valid_information_negative", "diagnostic", "information"),
                 ("prepared_or_under_review", "preparation", "not_executed"),
                 ("completed", "account", "unclassified")]
        for status, kind, expected in cases:
            self.assertEqual(outcomes.classify(status, kind), expected, status)

    def test_summary_shares(self):
        summary = outcomes.summarize([("valid_negative", "account"), ("implementation_error", "engineering"),
                                      ("verified_information", "diagnostic"), ("completed", "account")])
        self.assertEqual(summary["verdict_share"], 0.5)
        self.assertEqual(summary["no_verdict_share"], 0.25)


class TimingTests(Temp):
    def test_run_start_stop_and_receipt_feed_the_scheduler(self):
        executed = self.root / "exec.json"
        code = timing.run("exec-1", "execution", executed, [sys.executable, "-c", "pass"])
        self.assertEqual(code, 0)
        begun = self.root / "gen.start.json"
        timing.start("gen-1", "generation", begun)
        generated = self.root / "gen.json"
        timing.stop(begun, generated)
        contract = self.save("cost", {"schema": "research_cost_contract.v1", "unit": "resource_seconds",
                                      "phases": list(s.COST_PHASES), "accounting": "nonoverlapping_active_resource_time"})
        subject = self.save("decision", {"id": "D0001"})
        out = self.root / "receipt.json"
        timing.receipt(contract["path"], subject["path"], [executed, generated], out,
                       none_phases=["communication"])
        events, missing = s.read_cost_receipt(s.reference(out), contract, subject)
        self.assertEqual(sorted(e["phase"] for e in events), ["execution", "generation"])
        self.assertEqual(sorted(missing), ["maintenance", "verification"])

    def test_overlapping_timers_on_one_resource_are_rejected(self):
        a = {"schema": "research_timing.v1", "event_id": "a", "phase": "execution", "resource": "compute",
             "started_at": "2026-10-16T00:00:00+00:00", "finished_at": "2026-10-16T00:10:00+00:00", "seconds": 600}
        b = {**a, "event_id": "b", "started_at": "2026-10-16T00:05:00+00:00", "finished_at": "2026-10-16T00:20:00+00:00"}
        for name, value in (("a.json", a), ("b.json", b)):
            (self.root / name).write_text(json.dumps(value), encoding="utf-8")
        contract = self.save("cost", {"schema": "research_cost_contract.v1", "unit": "resource_seconds",
                                      "phases": list(s.COST_PHASES), "accounting": "nonoverlapping_active_resource_time"})
        subject = self.save("decision", {"id": "D0001"})
        with self.assertRaises(ValueError):
            timing.receipt(contract["path"], subject["path"], [self.root / "a.json", self.root / "b.json"],
                           self.root / "r.json")


class WalkForwardTests(Temp):
    def curve(self, name, yearly):
        """Daily equity with a constant daily growth per year; yearly maps year -> annual growth factor."""
        from datetime import date, timedelta
        rows, equity, d = [], 100000.0, date(2020, 1, 2)
        while d <= date(2026, 9, 30):
            if d.weekday() < 5:
                equity *= yearly.get(d.year, 1.0) ** (1 / 250)
                rows.append({"date": d.isoformat(), "equity": equity})
            d += timedelta(days=1)
        path = self.root / name
        path.write_text(json.dumps(rows), encoding="utf-8")
        return path

    def test_validation_folds_decide_and_are_masked(self):
        parent = self.curve("parent.json", {y: 2.0 for y in range(2020, 2027)})
        # Better in the visible window, worse in two of three validation years.
        candidate = self.curve("cand.json", {2020: 3, 2021: 3, 2022: 3, 2023: 1.5, 2024: 2.5, 2025: 1.5, 2026: 9})
        verdict = walk.evaluate(walk.DEFAULT_CONTRACT, candidate, parent)
        self.assertEqual(verdict["decision"], "fail")
        self.assertEqual(verdict["fold_wins"], 1)
        self.assertGreater(verdict["visible"]["candidate"]["annual_return_pct"],
                           verdict["visible"]["parent"]["annual_return_pct"])
        self.assertGreater(verdict["sealed_rows_ignored"]["candidate"], 0)
        public = walk.masked(verdict)
        text = json.dumps(public["folds"])
        self.assertNotIn("annual_return_pct", text)
        self.assertNotIn("delta_pp", text.replace("delta_bucket", ""))

    def test_overlapping_or_sealed_folds_are_rejected(self):
        bad = json.loads(json.dumps(walk.DEFAULT_CONTRACT))
        bad["folds"][1]["window"] = ["2023-06-01", "2024-12-31"]
        with self.assertRaises(ValueError):
            walk.validate_contract(bad)
        sealed = json.loads(json.dumps(walk.DEFAULT_CONTRACT))
        sealed["folds"].append({"id": "V2026", "window": ["2026-01-01", "2026-06-30"]})
        with self.assertRaises(ValueError):
            walk.validate_contract(sealed)


class DigestTests(Temp):
    def test_digest_is_bounded_and_lists_closed_expressions(self):
        state = {"status": "research_active", "baseline": "E11J", "next_action": "继续",
                 **{f"b{i}_closed_task": {"id": f"B{i}", "status": "closed_complete_risk_limit_failed",
                                          "purpose": "x" * 500} for i in range(1, 30)},
                 "huge": "y" * 200000}
        (self.root / "line").mkdir()
        (self.root / "line" / "STATE.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        for i in (1, 2, 10):
            (self.root / "line" / f"batch{i}").mkdir()
            (self.root / "line" / f"batch{i}" / "REFLECTION.md").write_text(f"# B{i} 反思\n结论 {i}", encoding="utf-8")
        config = {"schema": digest_mod.SCHEMA, "line": "t", "state": "line/STATE.json",
                  "fields": [{"pointer": "/status"}, {"pointer": "/baseline"}, {"pointer": "/missing"}],
                  "closed": {"key_pattern": r"^b\d+_closed_task$", "fields": ["id", "status", "purpose"]},
                  "reflections": {"glob": "line/batch*/REFLECTION.md", "latest": 2, "chars": 200},
                  "max_chars": 12000}
        text, receipt = digest_mod.build(config, self.root)
        self.assertLessEqual(len(text), 12100)
        self.assertIn("已关闭表达（29 项", text)
        self.assertIn("（原件无此字段）", text)
        self.assertLess(text.index("batch10"), text.index("batch2"))  # newest reflection first, natural order
        self.assertNotIn("batch1/", text)
        self.assertNotIn("y" * 1000, text)


class OnlineABTests(Temp):
    def batch(self, name, policy, statuses):
        directory = self.root / name
        directory.mkdir()
        s.write(directory / "BATCH.json", {"policy": {"id": policy}})
        for i, status in enumerate(statuses, 1):
            s.write(directory / f"D{i:04d}.json", {"at": "2026-10-16T00:00:00+00:00"})
            s.write(directory / f"O{i:04d}.json", {"at": "2026-10-16T00:30:00+00:00",
                                                    "observation": {"status": status}})
        return directory

    def test_decisions(self):
        a = self.batch("a", "old", ["valid_negative"] * 36 + ["valid_improvement"] * 4)
        b = self.batch("b", "new", ["valid_negative"] * 20 + ["valid_improvement"] * 20)
        self.assertEqual(ab.compare([a], [b])["decision"], "prefer_candidate")
        self.assertEqual(ab.compare([b], [a])["decision"], "prefer_incumbent")
        small = self.batch("c", "new", ["valid_improvement"] * 5)
        self.assertEqual(ab.compare([a], [small])["decision"], "insufficient_data")


class CliSmokeTests(unittest.TestCase):
    def test_every_new_cli_prints_help(self):
        for name in ("research_outcomes", "research_timing", "research_walkforward",
                     "research_line_digest", "research_online_ab"):
            completed = subprocess.run([sys.executable, "-B", str(ENGINE / (name + ".py")), "--help"],
                                       capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, name + completed.stderr)


if __name__ == "__main__":
    unittest.main()
