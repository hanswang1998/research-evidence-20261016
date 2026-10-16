"""Synthetic end-to-end harness tests. No models, research runs or platform calls."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

import test_research_frontier as frontier

s = frontier.s


class HarnessTests(unittest.TestCase):
    node = frontier.FrontierTests.node
    world = frontier.FrontierTests.world
    run_cycle = frontier.FrontierTests.run_cycle
    result = frontier.FrontierTests.result
    candidate = frontier.FrontierTests.candidate
    finish = frontier.FrontierTests.finish

    def setUp(self):
        frontier.FrontierTests.setUp(self)
        self.serial = 0
        self.base = self.save("base", s.read(Path(s.__file__).with_name("research_harness_default.json")))
        self.cost = self.save("cost", {"schema": "research_cost_contract.v1", "unit": "resource_seconds",
            "phases": list(s.COST_PHASES), "accounting": "nonoverlapping_active_resource_time"})
        self.input = self.make_case("case1")

    def save(self, name, value):
        self.serial += 1
        path = self.root / f"{self.serial:04d}-{name}.json"
        s.write(path, value)
        return s.reference(path)

    def make_case(self, ident):
        text = self.root / (ident + ".txt")
        text.write_text(f"Synthetic case {ident}: evidence, not model output.\n" + "detail " * 80, encoding="utf-8")
        source = s.reference(text)
        case = self.save("case", {"schema": "research_harness_case.v1", "branch": "structure", "id": ident,
            "question": "Which mechanism separates two synthetic explanations?",
            "context": [{"id": "goal", "section": "goal", "source": s.reference(self.goal), "lines": [1, 2]},
                        {"id": "failure", "section": "failures", "source": source, "lines": [1, 2]},
                        {"id": "success", "section": "successes", "source": source, "lines": [1, 1]}]})
        evaluation = self.save("evaluation", {"schema": "research_stage_evaluation.v1", "stage": "information",
            "protocol": s.reference(self.fact), "questions": [ident],
            "criteria": [{"id": key, "path": key, "op": "eq", "expected": True} for key in ("preserved", "distinct")]})
        return {"input": case, "evaluation_contract": evaluation}

    def plan(self, **updates):
        return {"schema": "research_harness_plan.v1", "branch": "structure", "kind": "development",
            "incumbent": self.base, "agent_model": "synthetic-test-model", "protocol": s.reference(self.fact),
            "evidence": [s.reference(self.fact)], "cases": [self.input], "cost_contract": self.cost,
            "selector": {"rule": "least_served_family_v1"},
            "limits": {"context_chars": 12000, "resource_seconds": 100, "candidates": 2}, **updates}

    def start_round(self, **updates):
        spec = self.save("plan", self.plan(**updates))
        directory = self.root / ("round-" + str(self.serial))
        s.harness_init(spec["path"], directory)
        return directory

    def call(self, directory, operation, **values):
        return s.harness_run(directory, {"operation": operation, **values})

    def edit_bundle(self, component="prompts", label="A", **updates):
        profile = s.read(self.base["path"])
        if component == "prompts":
            profile[component]["generation"] += "\n" + label + ": seek a counterexample before proposing."
            profile[component]["reflection"] += "\n" + label + ": preserve the known success."
        elif component == "generation":
            profile[component].update(max_candidates=2, min_families=2, questions=["distinguish mechanism " + label])
        else:
            profile[component].update(order=["goal", "failures", "successes", "unknowns", "evidence"], chars_per_item=128)
        profile.update(updates)
        return self.save("bundle", profile)

    def proposal_request(self, directory, bundle=None, component="prompts", accepted=True):
        bundle = bundle or self.edit_bundle(component)
        proposal = self.save("proposal", {"schema": "research_harness_proposal.v1", "parent": self.base,
            "reflection_task": s.reference(directory / "REFLECTION_TASK.json"), "evidence": [s.reference(self.fact)],
            "diagnosis": "synthetic weak distinction", "alternative_explanation": "input quality",
            "preserve": "successful identity check", "stop_rule": "one failing preserved check",
            "expected_effect": {"metric": "passed_checks", "direction": "increase"},
            "component": component, "bundle": bundle})
        critic = self.save("critic", {"schema": "research_harness_critic.v1", "proposal": proposal,
            "contract": s.reference(directory / "CONTRACT.json"), "independent": True,
            "verifier": "synthetic-independent-reviewer", "evidence": [s.reference(self.fact)],
            "checks": {"evidence_grounded": accepted, "bounded_change": True,
                       "preserves_success": True, "testable_prediction": True}})
        return bundle, {"proposal": proposal, "review": critic}

    def propose(self, directory, **kwargs):
        bundle, request = self.proposal_request(directory, **kwargs)
        self.call(directory, "propose", **request)
        return bundle

    def prepare(self, directory, bundle, entry=None, **kwargs):
        return self.call(directory, "prepare", bundle=bundle, case=(entry or self.input)["input"], **kwargs)["task"]

    def response(self, task, count=2, model="synthetic-test-model", include_task=True):
        pool = self.save("frontier", {"schema": "research_frontier.v1", "branch": "structure",
            "generation_evidence": [s.reference(self.fact)] + ([task] if include_task else []),
            "candidates": [self.candidate(f"A{self.serial}_{i}", f"family{i}") for i in range(count)],
            "shortfall_reason": "bounded synthetic fixture", "replenishment_action": "check next distinct evidence"})
        trace = self.save("trace", {"synthetic": True, "task": task, "note": "fixture; no real Agent invocation"})
        return self.save("response", {"schema": "research_harness_response.v1", "agent": "synthetic-generator",
            "agent_model": model, "task": task, "trace_evidence": [trace], "frontier": pool,
            "reflection": "Synthetic diagnosis preserves success and changes one question."})

    def generate(self, directory, task, **kwargs):
        return self.call(directory, "generation", task=task, response=self.response(task, **kwargs))

    def cost_receipt(self, task, seconds=10, missing=None, source=None):
        timer = source or self.save("timer", {"synthetic": True, "event": self.serial, "seconds": seconds})
        phases = {phase: [] for phase in s.COST_PHASES}
        phases["generation"] = [{"id": "generation-" + str(self.serial), "evidence": timer, "seconds_path": "seconds"}]
        if missing:
            phases[missing] = None
        return self.save("receipt", {"schema": "research_cost_receipt.v1", "cost_contract": self.cost,
            "kind": "harness_attempt", "subject": task, "phases": phases})

    def attempt(self, directory, bundle, entry=None, checks=(True, True), seconds=10,
                status="completed", missing=None, source=None, task=None, disagree=False):
        entry = entry or self.input
        task = task or self.prepare(directory, bundle, entry)
        evidence = None
        generation = None
        if status == "completed":
            generation = self.generate(directory, task)["generation"]
            question = s.read(entry["input"]["path"])["id"]
            bits = dict(zip(("preserved", "distinct"), checks))
            finding = self.save("finding", {"question": question, "generation": generation, **bits})
            verification = self.save("verification", {"schema": "research_stage_verification.v1",
                "question": question, "evaluation_contract": entry["evaluation_contract"], "finding": finding,
                "independent": True, "verifier": "synthetic-independent-checker",
                "checks": {key: not value for key, value in bits.items()} if disagree else bits})
            evidence = {"finding": finding, "verification": verification}
        return self.call(directory, "result", task=task, status=status, evidence=[s.reference(self.fact)],
            generation=generation, stage_evidence=evidence,
            cost_receipt=self.cost_receipt(task, seconds, missing, source) if seconds is not None else None,
            failure=None if status == "completed" else "synthetic failure remains visible")

    def compared(self, kind="development", old=(True, False), new=(True, True), old_cost=10, new_cost=10):
        directory = self.start_round(kind=kind)
        candidate = self.propose(directory)
        self.attempt(directory, self.base, checks=old, seconds=old_cost)
        self.attempt(directory, candidate, checks=new, seconds=new_cost)
        report = self.call(directory, "review")
        return directory, candidate, report

    def deployment(self):
        directory, candidate, report = self.compared()
        task = self.prepare(directory, candidate, self.make_case("deployment"), purpose="deployment",
                            review=s.reference(directory / "REVIEW.json"))
        generation = self.generate(directory, task)
        return directory, {"contract": s.reference(directory / "CONTRACT.json"),
            "review": s.reference(directory / "REVIEW.json"), "generation": generation["generation"]}, generation["frontier"]

    def boundary(self, deployment, pool):
        return {"branch": "structure", "status": "previous_batch_completed", "affects_running_experiments": False,
            "evidence": [s.reference(self.fact)], "quality_contract": s.reference(self.goal), "cost_contract": self.cost,
            "harness_deployment": deployment, "frontier_pilot": {"rule": "least_served_family_v1", "max_decisions": 2,
            "initial_frontier": pool, "recent_decisions": []}}

    def test_full_loop_reaches_original_next_observe_and_next_reflection(self):
        directory, deployment, pool = self.deployment()
        cycle = self.run_cycle()
        boundary = self.save("boundary", self.boundary(deployment, pool))
        batch = self.root / "batch"
        s.start_batch(cycle / "REPORT.json", boundary["path"], batch)
        frozen_batch = s.reference(batch / "BATCH.json")
        context = self.save("context", {"branch": "structure", "context_complete": True, "evidence": [s.reference(self.fact)]})
        for index in (1, 2):
            decision = s.next_decision(batch, context["path"])
            self.assertEqual(decision["frontier"], pool)
            self.assertEqual(decision["harness"]["generation"], deployment["generation"])
            self.assertEqual(s.next_decision(batch, context["path"])["status"], "pending_result")
            outcome = self.finish(batch, decision, index)
            self.assertEqual(outcome["harness"], decision["harness"])
        due = s.next_decision(batch, context["path"])
        self.assertEqual(due["status"], "reflection_due")
        evidence_paths = {Path(ref["path"]).name for ref in due["harness_reflection"]["evidence"]}
        self.assertTrue({"D0001.json", "O0001.json", "D0002.json", "O0002.json"} <= evidence_paths)
        review = s.review_pilot(batch)
        self.assertEqual(review["next_harness"], self.base)
        self.assertFalse(review["efficiency_improvement_claimed"])
        s.export_world(batch, self.root / "export.json")
        next_round = self.start_round(execution_feedback=s.reference(batch / "PILOT_REVIEW.json"),
            evidence=review["evidence"] + [s.reference(self.fact)])
        feedback = s.read(next_round / "REFLECTION_TASK.json")["execution_feedback"]
        self.assertEqual(feedback["evidence"], review["evidence"])
        self.assertEqual(frozen_batch, s.reference(batch / "BATCH.json"))

    def test_all_three_components_change_actual_agent_inputs(self):
        for component in ("prompts", "generation", "context"):
            with self.subTest(component=component):
                directory = self.start_round()
                candidate = self.propose(directory, component=component)
                old = s.read(self.prepare(directory, self.base)["path"])
                task = self.prepare(directory, candidate)
                new = s.read(task["path"])
                self.assertNotEqual(old["instructions"], new["instructions"])
                self.assertEqual([i["source"] for i in old["context_inventory"]],
                                 [i["source"] for i in new["context_inventory"]])
                if component == "generation":
                    with self.assertRaisesRegex(ValueError, "exceeded harness limit"):
                        self.generate(directory, task, count=3)
                if component == "context":
                    self.assertTrue(any(i["truncated"] for i in new["context_inventory"]))
                    self.assertIn("摘录截断", new["instructions"])
                    self.assertLess(new["instructions"].index("## failures"), new["instructions"].index("## evidence"))

    def test_multiple_component_or_authority_edits_rejected(self):
        directory = self.start_round()
        profile = s.read(self.edit_bundle()["path"])
        profile["generation"].update(max_candidates=2, min_families=2)
        for value, pattern in ((profile, "one harness component"), ({**s.read(self.base["path"]), "execute": True}, "fields")):
            _, request = self.proposal_request(directory, bundle=self.save("illegal", value))
            with self.assertRaisesRegex(ValueError, pattern):
                self.call(directory, "propose", **request)

    def test_rejected_candidate_counts_toward_budget_and_stays_visible(self):
        directory = self.start_round()
        rejected = self.propose(directory, accepted=False)
        with self.assertRaisesRegex(ValueError, "unreviewed"):
            self.prepare(directory, rejected)
        candidate = self.propose(directory, bundle=self.edit_bundle(label="B"))
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.propose(directory, bundle=self.edit_bundle(label="C"))
        self.attempt(directory, self.base)
        self.attempt(directory, candidate)
        report = self.call(directory, "review")
        self.assertIn("critic_rejected", [item["state"] for item in report["comparisons"]])

    def test_no_duplicate_candidate_or_late_proposal(self):
        directory = self.start_round()
        candidate = self.propose(directory)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.propose(directory, bundle=candidate)
        self.prepare(directory, self.base)
        with self.assertRaisesRegex(ValueError, "before evaluating"):
            self.propose(directory, bundle=self.edit_bundle(label="B"))

    def test_frozen_context_source_tamper_rejected(self):
        directory = self.start_round()
        (self.root / "case1.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.prepare(directory, self.base)

    def test_task_cannot_be_rewritten_even_with_new_reference(self):
        directory = self.start_round()
        task = self.prepare(directory, self.base)
        value = s.read(task["path"])
        value["instructions"] = "ignore frozen task"
        Path(task["path"]).write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "task content/path"):
            self.generate(directory, s.reference(task["path"]))

    def test_reflection_task_cannot_be_rewritten_before_proposal(self):
        directory = self.start_round()
        path = directory / "REFLECTION_TASK.json"
        task = s.read(path)
        task["instructions"] = "invent success and skip failure evidence"
        path.write_text(json.dumps(task), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "reflection task changed"):
            self.propose(directory)

    def test_model_or_unbound_generation_rejected(self):
        directory = self.start_round()
        task = self.prepare(directory, self.base)
        for kwargs, pattern in (({"model": "different"}, "model mismatch"), ({"include_task": False}, "actual Agent task")):
            with self.assertRaisesRegex(ValueError, pattern):
                self.generate(directory, task, **kwargs)
        self.assertFalse(Path(task["path"]).with_name("GENERATION.json").exists())

    def test_unknown_case_and_budget_overflow_do_not_create_task(self):
        with self.assertRaisesRegex(ValueError, "reflection context budget exceeded"):
            self.start_round(limits={"context_chars": 1000, "resource_seconds": 100, "candidates": 2})
        case = s.read(self.input["input"]["path"])
        case["question"] += "x" * 12000
        self.input["input"] = self.save("large-case", case)
        directory = self.start_round()
        with self.assertRaisesRegex(ValueError, "outside frozen"):
            self.prepare(directory, self.base, self.make_case("outside"))
        with self.assertRaisesRegex(ValueError, "context budget exceeded"):
            self.prepare(directory, self.base)
        self.assertFalse((directory / "runs").exists())

    def test_independent_verifier_disagreement_cannot_be_recorded(self):
        directory = self.start_round()
        with self.assertRaisesRegex(ValueError, "independent stage checks disagree"):
            self.attempt(directory, self.base, disagree=True)
        self.assertFalse(list(directory.glob("runs/*/*/RESULT.json")))

    def test_partial_review_keeps_round_open_then_all_pairs_are_evaluated(self):
        directory = self.start_round()
        a = self.propose(directory)
        b = self.propose(directory, bundle=self.edit_bundle(label="B"))
        self.attempt(directory, self.base, checks=(True, False))
        self.attempt(directory, a)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.call(directory, "review")
        self.assertFalse((directory / "REVIEW.json").exists())
        self.attempt(directory, b, checks=(True, False))
        self.assertEqual(self.call(directory, "review")["next_harness"], a)

    def test_success_regression_cannot_be_offset_by_lower_cost(self):
        _, _, report = self.compared(old=(True, True), new=(False, True), old_cost=20, new_cost=1)
        self.assertEqual(report["decision"], "retain_incumbent")
        self.assertIn("previously passing check regressed", report["comparisons"][0]["reasons"])

    def test_unknown_cost_and_failed_attempt_are_not_zero_or_passed(self):
        for status, seconds, missing, expected in (("completed", None, None, "unknown_cost"),
                ("completed", 5, "verification", "unknown_cost"), ("failed", 5, None, "failed_or_unknown"),
                ("unknown", 5, None, "failed_or_unknown")):
            with self.subTest(status=status, missing=missing, seconds=seconds):
                directory = self.start_round()
                candidate = self.propose(directory)
                self.attempt(directory, self.base, checks=(True, False))
                self.attempt(directory, candidate, status=status, seconds=seconds, missing=missing)
                report = self.call(directory, "review")
                self.assertEqual(report["comparisons"][0]["state"], expected)
                self.assertEqual(report["next_harness"], self.base)

    def test_same_timer_cannot_be_reused_between_arms(self):
        directory = self.start_round()
        candidate = self.propose(directory)
        timer = self.save("shared-timer", {"seconds": 5})
        self.attempt(directory, self.base, source=timer)
        self.attempt(directory, candidate, source=timer)
        with self.assertRaisesRegex(ValueError, "reused across paired"):
            self.call(directory, "review")

    def test_matched_cost_and_check_tradeoff_retains_baseline(self):
        directory = self.start_round()
        a = self.propose(directory)
        b = self.propose(directory, bundle=self.edit_bundle(label="B"))
        self.attempt(directory, self.base, checks=(True, False), seconds=20)
        self.attempt(directory, b, checks=(True, False), seconds=5)
        self.attempt(directory, a, checks=(True, True), seconds=10)
        self.assertEqual(self.call(directory, "review")["next_harness"], self.base)

    def test_prediction_miss_and_budget_overrun_remain_visible(self):
        _, _, report = self.compared(old=(True, True), new=(True, True), old_cost=150, new_cost=120)
        comparison = report["comparisons"][0]
        self.assertEqual(comparison["prediction_outcome"], "miss")
        self.assertIn("attempt exceeded frozen resource budget", comparison["reasons"])
        self.assertEqual(report["next_harness"], self.base)

    def test_synthetic_gain_cannot_deploy_or_become_real_effect_claim(self):
        directory, candidate, report = self.compared(kind="synthetic")
        self.assertEqual(report["fixture_winner"], candidate)
        self.assertEqual(report["next_harness"], self.base)
        self.assertFalse(report["efficiency_improvement_claimed"])
        with self.assertRaises(ValueError):
            self.prepare(directory, candidate, self.make_case("synthetic-deploy"), purpose="deployment",
                         review=s.reference(directory / "REVIEW.json"))

    def test_review_cannot_self_promote_against_bound_results(self):
        directory, candidate, report = self.compared(new=(True, False))
        report.update(decision="eligible_for_bounded_harness_pilot", next_harness=candidate)
        (directory / "REVIEW.json").write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "disagrees with bound"):
            self.prepare(directory, candidate, self.make_case("forged-deploy"), purpose="deployment",
                         review=s.reference(directory / "REVIEW.json"))

    def test_feedback_enters_next_actual_reflection_task(self):
        directory, candidate, report = self.compared()
        next_round = self.start_round(incumbent=candidate, previous_review=s.reference(directory / "REVIEW.json"))
        task = s.read(next_round / "REFLECTION_TASK.json")
        self.assertEqual(task["feedback"], report["comparisons"])
        self.assertIn("A: preserve", task["instructions"])

    def test_pilot_freezes_winner_and_uses_new_cases_then_rolls_back_on_failure(self):
        directory, candidate, _ = self.compared()
        offer = s.reference(directory / "REVIEW.json")
        with self.assertRaisesRegex(ValueError, "must be new"):
            self.start_round(kind="prospective_pilot", pilot_offer=offer)
        entry = self.make_case("new-pilot")
        pilot = self.start_round(kind="prospective_pilot", pilot_offer=offer, cases=[entry])
        with self.assertRaisesRegex(ValueError, "previously selected"):
            self.propose(pilot)
        self.attempt(pilot, self.base, entry)
        self.attempt(pilot, candidate, entry, status="failed")
        report = self.call(pilot, "review")
        self.assertEqual(report["decision"], "rollback")
        self.assertEqual(report["next_harness"], self.base)

    def test_pilot_success_is_bounded_and_not_economic_promotion(self):
        directory, candidate, _ = self.compared()
        entry = self.make_case("pilot-success")
        pilot = self.start_round(kind="prospective_pilot", pilot_offer=s.reference(directory / "REVIEW.json"), cases=[entry])
        self.attempt(pilot, self.base, entry, checks=(True, False))
        self.attempt(pilot, candidate, entry)
        report = self.call(pilot, "review")
        self.assertEqual(report["decision"], "supported_in_bounded_pilot")
        self.assertEqual(report["next_harness"], candidate)
        self.assertFalse(report["economic_promotion"])

    def test_new_case_identity_alone_cannot_relabel_old_inputs(self):
        directory, _, _ = self.compared()
        clone = copy.deepcopy(s.read(self.input["input"]["path"]))
        clone["id"] = "relabeled"
        clone["context"].reverse()
        for item in clone["context"]:
            item["id"] += "-new"
        entry = self.make_case("unused")
        entry["input"] = self.save("relabeled", clone)
        evaluation = s.read(entry["evaluation_contract"]["path"])
        evaluation["questions"] = ["relabeled"]
        entry["evaluation_contract"] = self.save("relabel-eval", evaluation)
        with self.assertRaisesRegex(ValueError, "must be new"):
            self.start_round(kind="prospective_pilot", pilot_offer=s.reference(directory / "REVIEW.json"), cases=[entry])

    def test_deployment_requires_exact_generated_frontier_and_cost_contract(self):
        _, deployment, pool = self.deployment()
        for key, value, pattern in (("branch", "ml", "branch/round"), ("cost_contract", None, "cost contract")):
            boundary = self.boundary(deployment, pool)
            boundary[key] = value
            with self.assertRaisesRegex(ValueError, pattern):
                s.check_harness_deployment(deployment, boundary)
        boundary = self.boundary(deployment, pool)
        boundary["frontier_pilot"]["initial_frontier"] = self.save("other-pool", s.read(pool["path"]))
        with self.assertRaisesRegex(ValueError, "harness-generated frontier"):
            s.check_harness_deployment(deployment, boundary)

    def test_harness_pool_cannot_be_hot_swapped_in_next(self):
        _, deployment, pool = self.deployment()
        boundary = self.save("boundary", self.boundary(deployment, pool))
        cycle = self.run_cycle()
        batch = self.root / "batch"
        s.start_batch(cycle / "REPORT.json", boundary["path"], batch)
        other = self.save("other-pool", s.read(pool["path"]))
        context = self.save("context", {"branch": "structure", "context_complete": True,
            "evidence": [s.reference(self.fact)], "frontier": other})
        with self.assertRaisesRegex(ValueError, "frozen Agent-generated"):
            s.next_decision(batch, context["path"])
        self.assertFalse((batch / "D0001.json").exists())

    def test_frozen_package_cli_runs_outside_project_and_rejects_runtime_tamper(self):
        directory = self.start_round()
        request = self.save("request", {"operation": "prepare", "bundle": self.base, "case": self.input["input"]})
        command = [sys.executable, "-X", "utf8", "-B", str(directory / "ENGINE.py"), "harness",
                   "--round", str(directory), "--request", request["path"]]
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("当前问题", json.loads(completed.stdout)["instructions"])
        with (directory / "HARNESS.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# changed\n")
        failed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("evidence changed", failed.stderr)

    def test_closed_round_and_duplicate_receipts_do_not_overwrite_history(self):
        directory, candidate, _ = self.compared()
        before = s.reference(directory / "REVIEW.json")
        with self.assertRaisesRegex(ValueError, "round closed"):
            self.call(directory, "review")
        with self.assertRaisesRegex(ValueError, "closed round"):
            self.prepare(directory, candidate)
        self.assertEqual(before, s.reference(directory / "REVIEW.json"))

    def test_selection_is_independent_of_registration_and_completion_order(self):
        a, b = self.edit_bundle(label="A"), self.edit_bundle(label="B")
        for equal_costs in (False, True):
            reports = []
            for order in ((a, b), (b, a)):
                directory = self.start_round()
                for candidate in order:
                    self.propose(directory, bundle=candidate)
                self.attempt(directory, self.base, checks=(True, False), seconds=20)
                for candidate in reversed(order):
                    self.attempt(directory, candidate, seconds=10 if equal_costs or candidate == a else 15)
                reports.append(self.call(directory, "review"))
            expected = min((a, b), key=lambda ref: ref["sha256"]) if equal_costs else a
            self.assertEqual(reports[0]["next_harness"], expected)
            self.assertEqual(reports[1]["next_harness"], expected)

    def test_case_level_success_cannot_be_hidden_by_aggregate_coverage(self):
        entry = self.make_case("second-case")
        directory = self.start_round(cases=[self.input, entry])
        candidate = self.propose(directory)
        self.attempt(directory, self.base, checks=(True, False))
        self.attempt(directory, self.base, entry, checks=(True, True))
        self.attempt(directory, candidate, checks=(True, True))
        self.attempt(directory, candidate, entry, checks=(True, False))
        self.assertEqual(self.call(directory, "review")["next_harness"], self.base)

    def test_round_overhead_is_recorded_separately_and_not_assumed_zero(self):
        directory = self.start_round()
        candidate = self.propose(directory)
        self.attempt(directory, self.base, checks=(True, False))
        self.attempt(directory, candidate)
        timer = self.save("overhead-timer", {"seconds": 7})
        phases = {phase: [] for phase in s.COST_PHASES}
        phases["maintenance"] = [{"id": "overhead", "evidence": timer, "seconds_path": "seconds"}]
        overhead = self.save("overhead", {"schema": "research_cost_receipt.v1", "cost_contract": self.cost,
            "kind": "harness_evolution", "subject": s.reference(directory / "CONTRACT.json"), "phases": phases})
        report = self.call(directory, "review", evolution_cost_receipt=overhead)
        self.assertEqual(report["evolution_cost"]["all_in_method_cost_seconds"], 7)
        self.assertEqual(report["whole_round_cost"]["all_in_method_cost_seconds"], 27)
        self.assertGreater(report["artifact_storage"]["logical_bytes"], 0)
        self.assertEqual(report["artifact_storage"]["rewritten_bytes"], 0)
        _, _, missing = self.compared()
        self.assertIsNone(missing["whole_round_cost"]["all_in_method_cost_seconds"])
        self.assertIn("evolution_cost_receipt", missing["whole_round_cost"]["missing"])

    def test_prospective_evaluator_and_budget_cannot_be_relaxed(self):
        directory, _, _ = self.compared()
        offer = s.reference(directory / "REVIEW.json")
        entry = self.make_case("pilot-evaluator")
        with self.assertRaisesRegex(ValueError, "budgets"):
            self.start_round(kind="prospective_pilot", pilot_offer=offer, cases=[entry],
                limits={"context_chars": 12000, "resource_seconds": 1000, "candidates": 2})
        evaluation = s.read(entry["evaluation_contract"]["path"])
        evaluation["criteria"] = evaluation["criteria"][:1]
        entry["evaluation_contract"] = self.save("easier-eval", evaluation)
        with self.assertRaisesRegex(ValueError, "evaluation criteria"):
            self.start_round(kind="prospective_pilot", pilot_offer=offer, cases=[entry])

    def test_only_one_new_deployment_pool_is_allowed_per_review(self):
        directory, deployment, _ = self.deployment()
        report = s.read(deployment["review"]["path"])
        with self.assertRaisesRegex(ValueError, "one deployment task"):
            self.prepare(directory, report["next_harness"], self.make_case("extra-deployment"),
                         purpose="deployment", review=deployment["review"])


if __name__ == "__main__":
    unittest.main()
