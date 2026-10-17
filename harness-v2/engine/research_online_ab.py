#!/usr/bin/env python3
"""Compare two scheduler policies or harness versions on real, interleaved batches.

Offline replay can only score paths that were actually recorded. Because research
here is cheap (an account job takes minutes), the more direct test is to run the
incumbent and the candidate on alternating real batches and compare outcome rates.

Each arm is a list of closed batch directories (BATCH.json, D*.json, O*.json).
Outcomes use the canonical categories of research_outcomes.py. The primary
success measure is ``improvement``; when observations carry a walk-forward verdict
(``walkforward`` reference written by observe) it is ``validated``, i.e. an
improvement whose walk-forward verdict is also ``pass``.
The secondary guard is the no-verdict share (engineering failures + evidence gaps).

Decision rule (frozen in --rule or defaults):
  insufficient_data  either arm has fewer than min_decisions observations
  prefer_candidate   success-rate difference lower bound > 0, the no-verdict share
                     did not rise by more than max_no_verdict_increase (point
                     estimate), and no rise in it is statistically detected
  prefer_incumbent   success-rate difference upper bound < 0
  no_difference      otherwise
Intervals are Newcombe hybrid score intervals built from Wilson intervals.

Budget-matched control (Rethinking the Evaluation of Harness Evolution, 2607.12227):
evolved harnesses often fail to beat the old harness given the same extra budget
(more samples or retries). --control takes batches run with the incumbent plus that
extra budget; the report states whether the candidate beats it. Costs are summed
from each O record's total_seconds, and --candidate-overhead adds the cost of
producing the candidate (a harness REVIEW's whole_round_cost or a cycle REPORT's
all_in_evolution_cost_seconds). The control result is reported, never a gate.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

STATUS_TO_CATEGORY = {"valid_improvement": "improvement", "valid_negative": "negative",
                      "verified_information": "information", "implementation_error": "engineering_failure",
                      "evidence_gap": "evidence_gap"}
NO_VERDICT = {"engineering_failure", "evidence_gap"}
DEFAULT_RULE = {"min_decisions": 30, "confidence": 0.9, "max_no_verdict_increase": 0.05}
Z = {0.8: 1.2816, 0.9: 1.6449, 0.95: 1.96}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def wilson(successes, trials, z):
    if trials == 0:
        return 0.0, 1.0
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def newcombe(x1, n1, x2, n2, z):
    """Interval for p1 - p2 (candidate minus incumbent)."""
    p1, p2 = x1 / n1, x2 / n2
    l1, u1 = wilson(x1, n1, z)
    l2, u2 = wilson(x2, n2, z)
    d = p1 - p2
    return d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2), d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)


def walkforward_pass(observation):
    ref = observation.get("walkforward")
    if not ref:
        return None
    verdict = read(ref["path"])
    return verdict.get("decision") == "pass"


def overhead_seconds(paths):
    """Summed cost of producing the candidate; None when any source has not measured it."""
    total = 0.0
    for path in paths:
        report = read(path)
        seconds = (report["whole_round_cost"]["all_in_method_cost_seconds"] if "whole_round_cost" in report
                   else report.get("all_in_evolution_cost_seconds"))
        if seconds is None:
            return None
        total += seconds
    return total


def arm_summary(directories):
    rows, minutes, seconds, policies, harnesses = [], [], [], set(), set()
    for directory in directories:
        directory = Path(directory)
        batch = read(directory / "BATCH.json")
        policies.add(batch["policy"]["id"])
        if batch.get("harness_deployment"):
            harnesses.add(batch["harness_deployment"]["bundle"]["sha256"])
        for outcome_path in sorted(directory.glob("O*.json")):
            outcome = read(outcome_path)
            decision = read(directory / ("D" + outcome_path.name[1:]))
            category = STATUS_TO_CATEGORY.get(outcome["observation"]["status"], "unclassified")
            rows.append({"category": category, "validated": walkforward_pass(outcome)})
            seconds.append(outcome.get("total_seconds"))
            started = datetime.fromisoformat(decision["at"].replace("Z", "+00:00"))
            finished = datetime.fromisoformat(outcome["at"].replace("Z", "+00:00"))
            minutes.append((finished - started).total_seconds() / 60)
    counts = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    has_walkforward = any(row["validated"] is not None for row in rows)
    # Walk-forward pass is a necessary extra condition, never a substitute: a valid_negative
    # account whose folds happen to pass is still not a success.
    success = sum(1 for row in rows if row["category"] == "improvement" and
                  (row["validated"] is True if has_walkforward else True))
    resource = None if not seconds or any(v is None for v in seconds) else sum(seconds)
    return {"batches": len(directories), "decisions": len(rows), "counts": counts,
            "success_measure": "validated" if has_walkforward else "improvement", "successes": success,
            "no_verdict": sum(counts.get(c, 0) for c in NO_VERDICT),
            "median_minutes_decision_to_outcome": statistics.median(minutes) if minutes else None,
            "resource_seconds": resource,
            "policies": sorted(policies), "harness_bundles": sorted(harnesses)}


def per_success(seconds, successes):
    return None if seconds is None or successes == 0 else seconds / successes


def control_comparison(candidate, control, z):
    """Candidate against the incumbent given the same extra budget."""
    if candidate["success_measure"] != control["success_measure"]:
        return {"result": "not_comparable", "reason": "arms use different success measures"}
    if min(candidate["decisions"], control["decisions"]) == 0:
        return {"result": "not_comparable", "reason": "an arm has no observed decisions"}
    interval = newcombe(candidate["successes"], candidate["decisions"], control["successes"], control["decisions"], z)
    verdict = "candidate_better" if interval[0] > 0 else "control_better" if interval[1] < 0 else "no_difference"
    return {"result": verdict, "success_rate_difference_interval": interval}


def compare(incumbent_dirs, candidate_dirs, rule=None, control_dirs=None, candidate_overhead=None):
    rule = {**DEFAULT_RULE, **(rule or {})}
    z = Z[rule["confidence"]]
    a, b = arm_summary(incumbent_dirs), arm_summary(candidate_dirs)
    result = {"schema": "research_online_ab.v1", "rule": rule, "incumbent": a, "candidate": b}
    # Report cost per success, with the candidate charged for its own evolution when measured.
    overhead = overhead_seconds(candidate_overhead) if candidate_overhead else None
    charged = (None if b["resource_seconds"] is None or (candidate_overhead and overhead is None)
               else b["resource_seconds"] + (overhead or 0.0))
    result["cost"] = {"candidate_overhead_seconds": overhead if candidate_overhead else "not_supplied",
                      "seconds_per_success": {"incumbent": per_success(a["resource_seconds"], a["successes"]),
                                              "candidate_including_overhead": per_success(charged, b["successes"])}}
    if control_dirs:
        c = arm_summary(control_dirs)
        result["control"] = c
        result["cost"]["seconds_per_success"]["control"] = per_success(c["resource_seconds"], c["successes"])
        result["budget_matched_control"] = control_comparison(b, c, z)
        if c["resource_seconds"] is not None and charged:
            result["budget_matched_control"]["control_to_candidate_cost_ratio"] = c["resource_seconds"] / charged
    else:
        result["budget_matched_control"] = {"result": "not_run",
            "reason": "without the incumbent given the same extra budget, a gain may come from spending more, not from the new version"}
    if a["success_measure"] != b["success_measure"]:
        result["decision"] = "insufficient_data"
        result["reason"] = "arms use different success measures; attach walk-forward verdicts to both"
        return result
    if min(a["decisions"], b["decisions"]) < rule["min_decisions"]:
        result["decision"] = "insufficient_data"
        result["reason"] = f"each arm needs at least {rule['min_decisions']} observed decisions"
        return result
    success = newcombe(b["successes"], b["decisions"], a["successes"], a["decisions"], z)
    no_verdict = newcombe(b["no_verdict"], b["decisions"], a["no_verdict"], a["decisions"], z)
    result["success_rate_difference_interval"] = success
    result["no_verdict_rate_difference_interval"] = no_verdict
    no_verdict_rise = b["no_verdict"] / b["decisions"] - a["no_verdict"] / a["decisions"]
    result["no_verdict_rate_difference"] = no_verdict_rise
    # An upper-bound guard would never pass at realistic sample sizes (with zero failures
    # in both arms of 40, the upper bound is still 6.3%); guard on estimate plus detection.
    if success[0] > 0 and no_verdict_rise <= rule["max_no_verdict_increase"] and no_verdict[0] <= 0:
        result["decision"] = "prefer_candidate"
    elif success[1] < 0:
        result["decision"] = "prefer_incumbent"
    else:
        result["decision"] = "no_difference"
    result["claims"] = "development-period research productivity only; not independent economic confirmation"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--incumbent", nargs="+", required=True, help="closed batch directories run with the incumbent")
    parser.add_argument("--candidate", nargs="+", required=True, help="closed batch directories run with the candidate")
    parser.add_argument("--control", nargs="+", help="closed batches run with the incumbent plus the candidate's extra budget")
    parser.add_argument("--candidate-overhead", nargs="+",
                        help="harness REVIEW.json or cycle REPORT.json files whose cost produced the candidate")
    parser.add_argument("--rule", help="frozen JSON overriding min_decisions / confidence / max_no_verdict_increase")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        rule = read(args.rule) if args.rule else None
        result = compare(args.incumbent, args.candidate, rule, args.control, args.candidate_overhead)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")
        print(text)
        return 0
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"status": "error", "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
