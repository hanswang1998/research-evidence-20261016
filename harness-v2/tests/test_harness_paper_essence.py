"""Changes taken from the 2026 RSI papers (2026-10-17).

- Meta-Harness: the reflection task carries every earlier candidate, not a summary (lineage).
- Harness² / ModularRSI: passing and failing runs of the same case side by side (contrast).
- RRSI pruner: components whose edits never gained become removal hints; under the v2 rule a
  strictly smaller bundle passes on non-inferiority and ties prefer the smaller bundle.
- RRSI critic: under the v2 rule the critic also judges task specificity and inert additions.
- Rethinking Harness Evolution: the online A/B reports a budget-matched control and cost per success.

Unit tests run anywhere. The end-to-end tests reuse the original harness fixtures and are skipped
when the project's tests/test_research_frontier.py is not present.
Run from the project root:  python -X utf8 -B -m unittest discover -s tests -p "test_harness_paper_essence.py" -v
"""
import importlib.util
import json
from pathlib import Path
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
ab = load("research_online_ab")
Z = h.CONFIDENCE_Z[0.9]

try:
    import test_research_harness as original
except ImportError:  # test_research_frontier.py is not in every checkout
    original = None


def attempt(passed, total=2, status="completed", cost=None):
    return {"status": status, "checks": {f"c{i}": i < passed for i in range(total)} if status == "completed" else {},
            "cost": {"all_in_method_cost_seconds": cost}}


def rule_cfg(**rule):
    resolved = {"rule": h.DECISION_RULE, "min_cases": 16, "confidence": 0.9, "critical_checks": [],
                "max_unpaired_share": 0.2, "cost_tolerance": 0.0, "max_simplification_loss_share": 0.2, **rule}
    return {"decision_rule_resolved": resolved, "limits": {"resource_seconds": 100}, "kind": "development"}


class SimplificationRuleTests(unittest.TestCase):
    api = s.harness_api()

    def test_wilson_upper_matches_the_documented_thresholds(self):
        self.assertAlmostEqual(h.wilson_upper(0, 16, Z), 0.1447, places=3)
        self.assertGreater(h.wilson_upper(1, 16, Z), 0.2)
        self.assertLess(h.wilson_upper(2, 30, Z), 0.2)

    def test_smaller_bundle_with_no_losses_passes_without_proving_a_gain(self):
        result = h.compare_attempts(self.api, [attempt(1)] * 16, [attempt(1)] * 16, rule_cfg(), sizes=(5000, 4000))
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["advantage"], "simpler_non_inferior")
        self.assertEqual(result["bundle_size"], [5000, 4000])

    def test_simplification_still_needs_non_inferiority(self):
        one_loss = [attempt(1)] * 15 + [attempt(0)]
        result = h.compare_attempts(self.api, [attempt(1)] * 16, one_loss, rule_cfg(), sizes=(5000, 4000))
        self.assertIn("no significant advantage", result["reasons"])
        critical = [attempt(2)] * 15 + [{"status": "completed", "checks": {"c0": False, "c1": True},
                                         "cost": {"all_in_method_cost_seconds": None}}]
        result = h.compare_attempts(self.api, [attempt(2)] * 16, critical, rule_cfg(critical_checks=["c0"]),
                                    sizes=(5000, 4000))
        self.assertFalse(result["accepted"])

    def test_equal_or_larger_bundle_gets_no_simplification_credit(self):
        for sizes in ((5000, 5000), (5000, 6000), None):
            result = h.compare_attempts(self.api, [attempt(1)] * 16, [attempt(1)] * 16, rule_cfg(), sizes=sizes)
            self.assertIn("no significant advantage", result["reasons"])

    def test_v1_rule_ignores_sizes(self):
        cfg = {"limits": {"resource_seconds": 100}}
        result = h.compare_attempts(self.api, [attempt(1)], [attempt(1)], cfg, sizes=(5000, 1))
        self.assertNotIn("bundle_size", result)
        self.assertFalse(result["accepted"])

    def test_ties_prefer_the_smaller_bundle(self):
        small = {"wilson_lower": 0.7, "cost": None, "bundle_size": [5000, 3000], "bundle": {"sha256": "f" * 64}}
        large = {"wilson_lower": 0.7, "cost": None, "bundle_size": [5000, 6000], "bundle": {"sha256": "0" * 64}}
        stronger = {**large, "wilson_lower": 0.8}
        self.assertIs(min([large, small], key=h.preference), small)
        self.assertIs(min([small, stronger], key=h.preference), stronger)

    def test_rule_parameter_is_validated(self):
        spec = {"kind": "development", "decision_rule": {"rule": h.DECISION_RULE, "max_simplification_loss_share": 0.9}}
        with self.assertRaisesRegex(ValueError, "simplification loss share"):
            h.decision_rule(self.api, spec)
        resolved = h.decision_rule(self.api, {"kind": "development", "decision_rule": {"rule": h.DECISION_RULE}})
        self.assertEqual(resolved["max_simplification_loss_share"], 0.2)

    def test_bundle_size_counts_prompts_questions_and_context_budget(self):
        base = json.loads((ENGINE / "research_harness_default.json").read_text(encoding="utf-8"))
        shorter = json.loads(json.dumps(base))
        shorter["prompts"]["generation"] = shorter["prompts"]["generation"][:10]
        self.assertLess(h.bundle_size(shorter), h.bundle_size(base))
        self.assertEqual(h.bundle_size(base), sum(map(len, base["prompts"].values())) +
                         sum(map(len, base["generation"]["questions"])) + base["context"]["chars_per_item"])


class ReflectionHintTests(unittest.TestCase):
    @staticmethod
    def round_with(*candidates):
        return {"candidates": [{"component": c, "state": state, "size_change": grow} for c, state, grow in candidates]}

    def test_component_with_only_unsuccessful_measured_edits_is_a_prune_hint(self):
        rounds = [self.round_with(("generation", "gain", 10)),
                  self.round_with(("prompts", "no_gain", 40), ("generation", "critic_rejected", 5)),
                  self.round_with(("prompts", "no_gain", -5), ("context", "incomplete", 0))]
        hints = h.prune_hints(rounds)
        self.assertEqual([hint["component"] for hint in hints], ["prompts"])
        self.assertEqual((hints[0]["measured_edits"], hints[0]["edits_that_grew_the_bundle"]), (2, 1))

    def test_prune_window_forgets_old_rounds(self):
        rounds = [self.round_with(("context", "no_gain", 1))] + [self.round_with()] * h.PRUNE_WINDOW
        self.assertEqual(h.prune_hints(rounds), [])

    def test_contrast_pairs_and_recurring_failures(self):
        mine, other = "a" * 64, "b" * 64
        def run(passed, failed, status="completed", tag=""):
            return {"status": status, "passed": passed, "failed": failed, "result": {"path": tag, "sha256": tag}}
        review = {"case_matrix": [
            {"case": "c1", "runs": {mine: run(["p"], ["d"], tag="m1"), other: run(["d", "p"], [], tag="o1")}},
            {"case": "c2", "runs": {mine: run(["p"], ["d"], tag="m2"), other: run(["d"], ["p"], tag="o2")}},
            {"case": "c3", "runs": {mine: run([], [], status="failed", tag="m3"), other: run(["d", "p"], [], tag="o3")}}]}
        result = h.contrast(review, {"path": "x", "sha256": mine})
        self.assertEqual(result["recurring_failures"][0], {"check": "d", "cases": 2, "examples": ["c1", "c2"]})
        self.assertIn({"check": "attempt_failed", "cases": 1, "examples": ["c3"]}, result["recurring_failures"])
        gained = next(p for p in result["pairs"] if p["direction"] == "other_passes")
        self.assertEqual((gained["check"], gained["cases"]), ("d", 2))
        self.assertEqual(gained["examples"][0], {"case": "c1", "passing": {"path": "o1", "sha256": "o1"},
                                                 "failing": {"path": "m1", "sha256": "m1"}})
        lost = next(p for p in result["pairs"] if p["direction"] == "incumbent_passes")
        self.assertEqual((lost["check"], lost["cases"]), ("p", 1))
        self.assertIsNone(h.contrast({"comparisons": []}, {"sha256": mine}))


class BudgetMatchedControlTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def batch(self, name, statuses, seconds=60.0):
        directory = self.root / name
        s.write(directory / "BATCH.json", {"policy": {"id": name}})
        for i, status in enumerate(statuses, 1):
            s.write(directory / f"D{i:04d}.json", {"at": "2026-10-16T00:00:00+00:00"})
            s.write(directory / f"O{i:04d}.json", {"at": "2026-10-16T00:30:00+00:00", "total_seconds": seconds,
                                                    "observation": {"status": status}})
        return directory

    def test_candidate_that_only_matches_extra_budget_is_reported(self):
        old = self.batch("old", ["valid_negative"] * 36 + ["valid_improvement"] * 4)
        new = self.batch("new", ["valid_negative"] * 20 + ["valid_improvement"] * 20, seconds=120.0)
        control = self.batch("ctl", ["valid_negative"] * 19 + ["valid_improvement"] * 21, seconds=120.0)
        result = ab.compare([old], [new], control_dirs=[control])
        self.assertEqual(result["decision"], "prefer_candidate")  # unchanged rule against the incumbent
        self.assertEqual(result["budget_matched_control"]["result"], "no_difference")
        self.assertAlmostEqual(result["budget_matched_control"]["control_to_candidate_cost_ratio"], 1.0)
        per = result["cost"]["seconds_per_success"]
        self.assertEqual((per["incumbent"], per["candidate_including_overhead"], per["control"]),
                         (40 * 60.0 / 4, 40 * 120.0 / 20, 40 * 120.0 / 21))

    def test_overhead_is_charged_to_the_candidate_and_unknown_stays_unknown(self):
        old = self.batch("old", ["valid_improvement"] * 30)
        new = self.batch("new", ["valid_improvement"] * 30)
        review = self.root / "REVIEW.json"
        s.write(review, {"whole_round_cost": {"all_in_method_cost_seconds": 600.0}})
        unknown = self.root / "REPORT.json"
        s.write(unknown, {"all_in_evolution_cost_seconds": None})
        result = ab.compare([old], [new], candidate_overhead=[review])
        self.assertEqual(result["cost"]["seconds_per_success"]["candidate_including_overhead"], (30 * 60.0 + 600.0) / 30)
        self.assertEqual(result["budget_matched_control"]["result"], "not_run")
        result = ab.compare([old], [new], candidate_overhead=[review, unknown])
        self.assertIsNone(result["cost"]["candidate_overhead_seconds"])
        self.assertIsNone(result["cost"]["seconds_per_success"]["candidate_including_overhead"])


if original is not None:
    # Reuse the original fixtures without re-running the original tests under a new name.
    Fixture = type("Fixture", (unittest.TestCase,),
                   {k: v for k, v in vars(original.HarnessTests).items() if not k.startswith("test")})
else:
    Fixture = unittest.TestCase


@unittest.skipIf(original is None, "needs tests/test_research_frontier.py from the project")
class EndToEndTests(Fixture):
    def critic(self, directory, proposal, checks):
        return self.save("critic", {"schema": "research_harness_critic.v1", "proposal": proposal,
            "contract": s.reference(directory / "CONTRACT.json"), "independent": True,
            "verifier": "synthetic-independent-reviewer", "evidence": [s.reference(self.fact)],
            "checks": {key: True for key in checks}})

    def v2_proposal(self, directory, bundle, component, prediction):
        return self.save("proposal", {"schema": "research_harness_proposal.v1", "parent": self.base,
            "reflection_task": s.reference(directory / "REFLECTION_TASK.json"), "evidence": [s.reference(self.fact)],
            "diagnosis": "generation prompt repeats rules the guard already states",
            "alternative_explanation": "the repetition may still anchor attention",
            "preserve": "identity and counterexample checks", "stop_rule": "any case loses a passing check",
            "expected_effect": prediction, "component": component, "bundle": bundle})

    def test_review_matrix_feeds_lineage_and_contrast_of_the_next_round(self):
        directory, candidate, report = self.compared()
        self.assertEqual(len(report["case_matrix"]), 1)
        row = report["case_matrix"][0]["runs"]
        self.assertEqual(row[self.base["sha256"]]["failed"], ["distinct"])
        self.assertEqual(row[candidate["sha256"]]["failed"], [])
        next_round = self.start_round(incumbent=candidate, previous_review=s.reference(directory / "REVIEW.json"))
        task = s.read(next_round / "REFLECTION_TASK.json")
        self.assertEqual(len(task["lineage"]), 1)
        entry = task["lineage"][0]["candidates"][0]
        self.assertEqual((entry["component"], entry["state"], entry["bundle"]), ("prompts", "gain", candidate))
        self.assertGreater(entry["size_change"], 0)
        self.assertEqual(task["prune_hints"], [])
        pair = task["contrast"]["pairs"][0]
        self.assertEqual((pair["check"], pair["direction"]), ("distinct", "incumbent_passes"))
        self.assertEqual(task["incumbent_size"], h.bundle_size(s.read(candidate["path"])))
        # The task stays reproducible: every proposal re-derives the payload with the round's frozen runtime.
        contract = s.read(next_round / "CONTRACT.json")
        runtime = s.harness_runtime(contract["runtime"])
        self.assertEqual(runtime.reflection_payload(s.harness_api(), s.reference(next_round / "CONTRACT.json"), contract), task)

    def test_v2_critic_must_judge_task_specificity_and_inert_additions(self):
        directory = self.start_round(kind="synthetic", decision_rule={"rule": h.DECISION_RULE})
        bundle = self.edit_bundle()
        proposal = self.v2_proposal(directory, bundle, "prompts", {"metric": "passed_checks", "direction": "increase"})
        with self.assertRaisesRegex(ValueError, "invalid critic checks"):
            self.call(directory, "propose", proposal=proposal, review=self.critic(directory, proposal, h.CRITIC_CHECKS))
        accepted = self.call(directory, "propose", proposal=proposal,
                             review=self.critic(directory, proposal, h.CRITIC_CHECKS_V2))
        self.assertTrue(accepted["accepted"])
        legacy = self.start_round()
        legacy_proposal = self.v2_proposal(legacy, bundle, "prompts", {"metric": "bundle_size", "direction": "decrease"})
        with self.assertRaisesRegex(ValueError, "testable method prediction"):
            self.call(legacy, "propose", proposal=legacy_proposal,
                      review=self.critic(legacy, legacy_proposal, h.CRITIC_CHECKS))

    def test_v2_shorter_bundle_with_no_losses_wins_on_non_inferiority(self):
        cases = [self.make_case(f"simple{i}") for i in range(12)]
        directory = self.start_round(kind="synthetic", decision_rule={"rule": h.DECISION_RULE}, cases=cases)
        profile = s.read(self.base["path"])
        profile["prompts"]["generation"] = profile["prompts"]["generation"][:len(profile["prompts"]["generation"]) // 2]
        shorter = self.save("shorter", profile)
        proposal = self.v2_proposal(directory, shorter, "prompts", {"metric": "bundle_size", "direction": "decrease"})
        self.call(directory, "propose", proposal=proposal, review=self.critic(directory, proposal, h.CRITIC_CHECKS_V2))
        for entry in cases:
            self.attempt(directory, self.base, entry)
            self.attempt(directory, shorter, entry)
        report = self.call(directory, "review")
        comparison = report["comparisons"][0]
        self.assertEqual((comparison["state"], comparison["advantage"]), ("gain", "simpler_non_inferior"))
        self.assertEqual(comparison["prediction_outcome"], "hit")
        self.assertEqual(report["fixture_winner"], shorter)


if __name__ == "__main__":
    unittest.main()
