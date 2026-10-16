"""Tests for the decision-neutral v2 tools (outcomes, timing, walk-forward, digest, online A/B).

These modules only add records; they do not change any scheduler or harness decision, and
these tests pass against the original v1 research_scheduler.py as well.
Run from the project root:  python -X utf8 -B -m unittest discover -s tests -p "test_harness_v2_tools.py" -v
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

    def test_dropping_candidate_rows_cannot_manufacture_wins(self):
        parent = self.curve("parent.json", {y: 1.25 for y in range(2020, 2027)})
        rows = json.loads(parent.read_text(encoding="utf-8"))
        by_year = {}
        for row in rows:
            by_year.setdefault(row["date"][:4], []).append(row)
        sparse = [r for y in sorted(by_year) for r in by_year[y][:60] + [by_year[y][-1]]]
        candidate = self.root / "sparse.json"
        candidate.write_text(json.dumps(sparse), encoding="utf-8")
        with self.assertRaises(ValueError):  # the reported counterexample: same curve, fewer rows
            walk.evaluate(walk.DEFAULT_CONTRACT, candidate, parent)

    def test_shared_but_gappy_calendar_is_insufficient(self):
        rows = json.loads(self.curve("p.json", {y: 1.25 for y in range(2020, 2027)}).read_text(encoding="utf-8"))
        gappy = [r for i, r in enumerate(rows) if i % 3 == 0]
        a, b = self.root / "a.json", self.root / "b.json"
        a.write_text(json.dumps(gappy), encoding="utf-8")
        b.write_text(json.dumps(gappy), encoding="utf-8")
        verdict = walk.evaluate(walk.DEFAULT_CONTRACT, a, b)
        self.assertEqual(verdict["decision"], "insufficient_data")
        self.assertEqual({f["candidate"]["status"] for f in verdict["folds"]}, {"calendar_gap"})

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

    def test_walkforward_pass_alone_is_not_success(self):
        def arm(name, passes):
            directory = self.root / name
            directory.mkdir()
            s.write(directory / "BATCH.json", {"policy": {"id": name}})
            for i in range(1, 41):
                verdict = self.root / f"{name}-v{i}.json"
                s.write(verdict, {"decision": "pass" if i <= passes else "fail"})
                s.write(directory / f"D{i:04d}.json", {"at": "2026-10-16T00:00:00+00:00"})
                s.write(directory / f"O{i:04d}.json", {"at": "2026-10-16T00:30:00+00:00",
                                                        "observation": {"status": "valid_negative"},
                                                        "walkforward": s.reference(verdict)})
            return directory
        result = ab.compare([arm("a", 4)], [arm("b", 20)])
        self.assertEqual(result["candidate"]["successes"], 0)
        self.assertNotEqual(result["decision"], "prefer_candidate")

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
