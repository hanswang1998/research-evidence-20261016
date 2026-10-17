#!/usr/bin/env python3
"""Read-only, source-checked comparison of closed batches.

This is descriptive instrumentation, not a randomized trial. Decisions within
one research tree are dependent; no per-decision confidence interval or automatic
prefer_candidate verdict is produced. Promotion still uses the frozen Harness
trial, its hard checks, independent review and complete costs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def bound(ref):
    path = Path(ref["path"]).resolve()
    require(reference(path) == ref, "source hash/path mismatch")
    return path


def runtime(path):
    spec = importlib.util.spec_from_file_location("closed_batch_reader", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def verified_improvement(s, cfg, decision, outcome, prior):
    observation = outcome["observation"]
    if observation["status"] not in {"valid_improvement", "valid_negative"}:
        return False
    score = s.grade(cfg["quality_contract"], outcome["grade_evidence"])
    require(score == observation["quality"], "recorded account score disagrees with frozen evaluator")
    if s.quality_contract(cfg["quality_contract"])["schema"] == "research_quality.v2":
        review = s.review_account(cfg["quality_contract"], outcome["grade_evidence"])
        improved = review["decision"] in {"return_improved", "risk_repair_tradeoff"}
        require(observation["status"] == ("valid_improvement" if improved else "valid_negative"),
                "recorded status disagrees with frozen account review")
    else:
        anchor = decision["selected"]["anchor"]
        previous = prior.get(anchor, cfg.get("initial_quality") if anchor == "root" else None)
        # An unbound legacy comparator cannot certify an improvement.
        if previous is None:
            return None if observation["status"] == "valid_improvement" else False
        improved = score > previous
        require(observation["status"] != "valid_improvement" or improved, "improvement label lacks measured advantage")
    return observation["status"] == "valid_improvement" and improved


def segment_pass(outcome_ref, outcome, config, entry):
    if entry is None:
        return None
    wf = runtime(Path(__file__).with_name("research_walkforward.py"))
    report = wf.verify(entry["report"], entry["input"])
    inputs = wf.read(wf.bound(entry["input"]))
    contract = wf.read(wf.bound(inputs["contract"]))
    candidate = wf.read(wf.bound(inputs["candidate"]))
    require(report["subject"] == outcome_ref and candidate["grade_evidence"] == outcome["grade_evidence"] and
            contract["account_contract"] == config["quality_contract"], "segment report belongs to another account outcome")
    return report["decision"] == "pass"


def arm_summary(directories, seen, require_segment=False, segment_index=None):
    require(directories, "nonempty arm required")
    counts, policies, harnesses, scopes, engines = Counter(), set(), set(), set(), set()
    latencies, sources, total_costs = [], [], []
    successes = unknown = units = 0
    for item in directories:
        directory = Path(item).resolve()
        require(str(directory) not in seen["directories"], "duplicate or shared batch directory")
        seen["directories"].add(str(directory))
        batch_ref = reference(directory / "BATCH.json")
        require(batch_ref["sha256"] not in seen["batches"], "duplicate batch identity")
        seen["batches"].add(batch_ref["sha256"])
        cfg = read(bound(batch_ref))
        cycle = read(bound(cfg["cycle_report"]))
        contract = read(bound(cycle["contract"]))
        engine_path = bound(contract["engine"])
        require(contract["engine"]["sha256"] == cfg["engine_sha256"], "batch engine identity mismatch")
        s = runtime(engine_path)
        s.batch(directory)
        nodes, pending = s.ledger(directory)
        require(pending is None and nodes, "batch is unfinished or empty")
        outcomes = {ref["path"] for node in nodes for ref in node["evidence"][1:2]}
        require(outcomes == {str(p.resolve()) for p in directory.glob("O*.json")}, "orphan outcome in batch")
        close = directory / "EXPORT.json"
        if close.exists():
            receipt = read(close)
            world = read(bound(receipt["world"]))
            require(world["batch_contract"] == batch_ref and world["nodes"] == nodes and receipt["nodes"] == len(nodes),
                    "closed export differs from current ledger")
        else:
            close = directory / "PILOT_REVIEW.json"
            require(close.exists(), "closed batch receipt required")
            review = read(close)
            require(review["batch"] == batch_ref, "close receipt batch mismatch")
            cited = {str(s.bound(ref)) for ref in review["evidence"]}
            require(all(node["evidence"][0]["path"] in cited and node["evidence"][1]["path"] in cited for node in nodes),
                    "close receipt omits observations")
        policies.add(canonical(cfg["policy"]))
        harnesses.add(cfg.get("harness_deployment", {}).get("bundle", {}).get("sha256", "none"))
        scopes.add(canonical([cfg["branch"], cfg["metric_contract"], s.cost_identity(cfg),
                             cfg.get("evaluation_contract"), cfg["quality_contract"]]))
        engines.add(cfg["engine_sha256"])
        sources.extend([batch_ref, reference(close)])
        total_costs.append(s.node_costs(cfg, nodes)["all_in_method_cost_seconds"])
        prior = {}
        for node in nodes:
            decision_ref, outcome_ref = node["evidence"][:2]
            require(decision_ref["sha256"] not in seen["decisions"], "duplicate or shared decision identity")
            seen["decisions"].add(decision_ref["sha256"])
            decision, outcome = read(bound(decision_ref)), read(bound(outcome_ref))
            if outcome.get("grade_evidence"):
                identity = outcome["grade_evidence"]["sha256"]
                require(identity not in seen["accounts"], "account evidence reused as independent observation")
                seen["accounts"].add(identity)
            measured = verified_improvement(s, cfg, decision, outcome, prior)
            prior[node["id"]] = outcome["observation"]["quality"]
            status = outcome["observation"]["status"]
            counts[status] += 1
            validated = True
            if require_segment and status in {"valid_improvement", "valid_negative"}:
                validated = segment_pass(outcome_ref, outcome, cfg, (segment_index or {}).get(outcome_ref["sha256"]))
            # Never substitute validation for account improvement, even for negative observations.
            successes += measured is True and validated is True
            unknown += measured is None or (measured is True and validated is None)
            seconds = (s.timestamp(outcome["at"]) - s.timestamp(decision["at"])).total_seconds()
            require(seconds >= 0, "outcome precedes decision")
            latencies.append(seconds / 60)
            sources.extend([decision_ref, outcome_ref])
        units += len(nodes)
    require(len(policies) == len(harnesses) == len(scopes) == len(engines) == 1,
            "arm mixes versions, evaluation scope or cost definitions")
    return {"batches": len(directories), "decisions": units, "counts": dict(counts), "successes": successes,
            "unknown_success": unknown, "policy": json.loads(next(iter(policies))),
            "harness_bundle": next(iter(harnesses)), "scope": json.loads(next(iter(scopes))),
            "engine_sha256": next(iter(engines)), "sources": sources,
            "all_in_method_cost_seconds": None if any(v is None for v in total_costs) else sum(total_costs),
            "median_minutes_decision_to_outcome": statistics.median(latencies),
            "latency_is_resource_cost": False}


def compare(incumbent_dirs, candidate_dirs, require_segment=False, segment_index=None):
    seen = {key: set() for key in ("directories", "batches", "decisions", "accounts")}
    old = arm_summary(incumbent_dirs, seen, require_segment, segment_index)
    new = arm_summary(candidate_dirs, seen, require_segment, segment_index)
    require(old["scope"] == new["scope"] and old["engine_sha256"] == new["engine_sha256"],
            "arms must share the same evaluation, costs and engine")
    changes = [key for key in ("policy", "harness_bundle") if old[key] != new[key]]
    require(len(changes) <= 1, "do not change scheduler and Harness simultaneously")
    return {"schema": "research_online_ab.v2", "runtime": reference(__file__),
            "success_measure": "account_improvement_and_segment_pass" if require_segment else "account_improvement",
            "incumbent": old, "candidate": new, "changed_components": changes,
            "decision": "diagnostic_only", "efficiency_improvement_claimed": False, "economic_promotion": False,
            "limitations": ["No prospective allocation contract or independent trial units established.",
                            "Decision counts are descriptive; repeated/adaptive tree observations are not independent trials.",
                            "A segment pass alone is never a successful account improvement.",
                            "Latency includes waiting; incomplete resource costs remain unknown."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incumbent", nargs="+", required=True)
    parser.add_argument("--candidate", nargs="+", required=True)
    parser.add_argument("--require-segment", action="store_true")
    parser.add_argument("--segment-index", help="JSON: outcome SHA256 -> {report: reference, input: reference}")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        require(not args.segment_index or args.require_segment, "segment index requires an explicit common measure")
        index = read(args.segment_index) if args.segment_index else None
        result = compare(args.incumbent, args.candidate, args.require_segment, index)
        if args.segment_index:
            result["segment_index"] = reference(args.segment_index)
        with Path(args.out).open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        print(json.dumps({"output": reference(args.out), "decision": result["decision"],
            "artifact_usage": {"files": 1, "logical_bytes": Path(args.out).stat().st_size, "rewritten_bytes": 0}},
            ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
