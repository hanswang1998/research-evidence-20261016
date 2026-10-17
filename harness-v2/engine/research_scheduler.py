#!/usr/bin/env python3
"""Bounded research-policy evolution. Standard library only; never runs experiments.

The v1 choose() function drives action-only replay and legacy decision packets.
An opt-in frontier pilot selects concrete hypotheses; v1 replay cannot certify it.
Policies are a small executable decision language, not arbitrary Python plugins.
All artifacts are immutable. Existing research control remains the execution gate.

v2 (2026-10-16): replay scores a choice with no recorded continuation
pessimistically (no quality, worst observed node cost) instead of discarding
the whole candidate; unknown cost no longer blocks a bounded pilot offer (the
pilot itself must measure it); batches may record sibling branches so that
replay has more than one recorded path to compare.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace


SCHEMA = "research_scheduler.v1"
BRANCHES = {"structure", "ml"}
STATUSES = {"valid_improvement", "valid_negative", "verified_information",
            "implementation_error", "evidence_gap"}
POLICY_KEYS = {"id", "priority", "max_depth", "max_attempts", "reflection_every"}
COST_PHASES = ("generation", "execution", "verification", "maintenance", "communication")
REPLAY_SEMANTICS = "pessimistic_unobserved_v2"
# A candidate whose replay mostly walks into unrecorded choices is not comparable.
MAX_UNOBSERVED_SHARE = 0.5
MAX_SIBLINGS = 3


def require(ok, message):
    if not ok:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "timestamp requires timezone")
    return result


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, "duplicate JSON key: " + key)
            out[key] = value
        return out
    def invalid_constant(value):
        raise ValueError("nonfinite JSON number: " + value)
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=pairs,
                      parse_constant=invalid_constant)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": digest(path.read_bytes())}


def bound(ref):
    path = Path(ref["path"])
    require(path.is_absolute(), "evidence paths must be absolute")
    require(digest(path.read_bytes()) == ref["sha256"], "evidence changed: " + str(path))
    return path


def drift(stored, expected):
    """Top-level fields of a stored record that its bound inputs no longer reproduce (time excluded)."""
    expected = json.loads(json.dumps(expected, ensure_ascii=False, allow_nan=False))
    return sorted(key for key in stored.keys() | expected.keys()
                  if key != "at" and (key not in stored or key not in expected or stored[key] != expected[key]))


def number(value, nullable=False):
    return ((nullable and value is None) or
            (type(value) in (int, float) and math.isfinite(value) and value >= 0))


def score_number(value):
    return value is None or (type(value) in (int, float) and math.isfinite(value))


def field_value(obj, path):
    require(isinstance(path, str) and path and all(path.split(".")), "field path required")
    for key in path.split("."):
        obj = obj[key]
    return obj


def stage_contract(ref):
    obj = read(bound(ref))
    require(obj["schema"] == "research_stage_evaluation.v1" and
            obj["stage"] in {"information", "engineering"}, "unsupported evaluation stage")
    require(isinstance(obj["questions"], list) and obj["questions"] and
            all(isinstance(q, str) and q for q in obj["questions"]) and
            len(set(obj["questions"])) == len(obj["questions"]), "frozen questions required")
    criteria = obj["criteria"]
    require(isinstance(criteria, list) and criteria, "frozen stage criteria required")
    ids = []
    for criterion in criteria:
        require(set(criterion) == {"id", "path", "op", "expected"}, "invalid stage criterion")
        require(isinstance(criterion["id"], str) and criterion["id"], "criterion id required")
        require(isinstance(criterion["path"], str) and criterion["path"], "criterion path required")
        require(criterion["op"] in {"eq", "ge", "le"}, "unsupported criterion operation")
        expected = criterion["expected"]
        require(type(expected) is bool or (expected is not None and score_number(expected)),
                "criterion must compare a boolean or finite number")
        require(criterion["op"] == "eq" or type(expected) is not bool, "ordered boolean criterion")
        ids.append(criterion["id"])
    require(len(ids) == len(set(ids)), "duplicate stage criterion")
    bound(obj["protocol"])
    return obj


def evaluation_stage(config):
    return stage_contract(config["evaluation_contract"])["stage"] if config.get("evaluation_contract") else "economic"


def metric_identity(branch, quality_ref, evaluation_ref=None):
    identity = branch + ":" + quality_ref["sha256"]
    return identity + ":stage:" + evaluation_ref["sha256"] if evaluation_ref else identity


def evaluate_stage(contract_ref, evidence, observation):
    """Check frozen facts and a separately hash-bound verification, without an economic score."""
    contract = stage_contract(contract_ref)
    require(observation["quality"] is None and observation["question"] in contract["questions"],
            "stage question/quality mismatch")
    require(observation["status"] not in {"valid_improvement", "valid_negative"},
            "economic outcome in non-economic stage")
    if observation["status"] != "verified_information":
        require(not evidence, "unresolved stage cannot carry a passing verification")
        return None
    require(observation["resolved"] and observation["substantive"], "stage information must answer a substantive question")
    require(isinstance(evidence, dict) and set(evidence) == {"finding", "verification"},
            "stage finding and independent verification required")
    finding = read(bound(evidence["finding"]))
    verification = read(bound(evidence["verification"]))
    require(evidence["finding"]["sha256"] != evidence["verification"]["sha256"], "separate verification required")
    require(finding["question"] == verification["question"] == observation["question"], "stage evidence question mismatch")
    require(verification["schema"] == "research_stage_verification.v1" and
            verification["evaluation_contract"] == contract_ref and verification["finding"] == evidence["finding"],
            "stage verification binding mismatch")
    require(verification["independent"] is True and isinstance(verification["verifier"], str) and
            verification["verifier"].strip(), "independent verifier required")
    checks = {}
    for criterion in contract["criteria"]:
        actual, expected = field_value(finding, criterion["path"]), criterion["expected"]
        if type(expected) is bool:
            require(type(actual) is bool, "criterion boolean type mismatch")
        else:
            require(actual is not None and score_number(actual), "criterion finite number required")
        op = criterion["op"]
        if op == "eq":
            passed = actual == expected
        elif op == "ge":
            passed = actual >= expected
        else:
            passed = actual <= expected
        checks[criterion["id"]] = passed
    require(set(verification["checks"]) == set(checks) and
            all(type(value) is bool for value in verification["checks"].values()) and
            verification["checks"] == checks, "independent stage checks disagree")
    return {"stage": contract["stage"], "question": observation["question"],
            "checks": checks, "passed": all(checks.values()), "economic_quality": None}


def cost_contract(ref):
    obj = read(bound(ref))
    require(obj["schema"] == "research_cost_contract.v1" and obj["unit"] == "resource_seconds" and
            obj["phases"] == list(COST_PHASES) and obj["accounting"] == "nonoverlapping_active_resource_time",
            "unsupported cost contract")
    return obj


def cost_identity(config):
    return ("all_in_resource_seconds:" + config["cost_contract"]["sha256"]
            if config.get("cost_contract") else "recorded_node_total_seconds")


def read_cost_receipt(ref, contract_ref, subject_ref, kind="decision"):
    cost_contract(contract_ref)
    obj = read(bound(ref))
    require(obj["schema"] == "research_cost_receipt.v1" and obj["cost_contract"] == contract_ref and
            obj["kind"] == kind and obj["subject"] == subject_ref, "cost receipt binding mismatch")
    bound(subject_ref)
    require(set(obj["phases"]) == set(COST_PHASES), "all cost phases must be explicit; unknown uses null")
    events, missing = [], []
    for phase in COST_PHASES:
        entries = obj["phases"][phase]
        if entries is None:
            missing.append(phase)
            continue
        require(isinstance(entries, list), "cost phase must be a list or null")
        for entry in entries:
            require(set(entry) == {"id", "evidence", "seconds_path"} and
                    isinstance(entry["id"], str) and entry["id"], "invalid cost event")
            seconds = field_value(read(bound(entry["evidence"])), entry["seconds_path"])
            require(number(seconds), "negative/nonfinite/unknown cost event")
            events.append({**entry, "phase": phase, "seconds": seconds})
    return events, missing


def summarize_cost(events, missing):
    unique, sources = {}, {}
    for event in events:
        event_id = event["id"]
        require(event_id not in unique or unique[event_id] == event, "conflicting duplicate cost event")
        source = (event["evidence"]["sha256"], event["seconds_path"])
        require(source not in sources or sources[source] == event_id, "cost measurement reused under another event id")
        unique[event_id], sources[source] = event, event_id
    phases = {phase: sum(e["seconds"] for e in unique.values() if e["phase"] == phase) for phase in COST_PHASES}
    total = sum(phases.values())
    require(number(total), "nonfinite accumulated cost")
    return {"unit": "resource_seconds", "known_phase_seconds": phases,
            "missing": sorted(set(missing)), "events": [unique[k] for k in sorted(unique)],
            "complete": not missing, "all_in_method_cost_seconds": None if missing else total}


def node_costs(config, nodes, unsupported=0):
    events, missing = [], []
    if config.get("cost_contract"):
        cost_contract(config["cost_contract"])
        for node in nodes:
            if not node.get("cost_receipt"):
                missing.append(node["id"] + ":receipt")
                continue
            measured, gaps = read_cost_receipt(node["cost_receipt"], config["cost_contract"], node["evidence"][0])
            events.extend(measured)
            missing.extend(node["id"] + ":" + phase for phase in gaps)
    else:
        missing.append("cost_contract")
    if unsupported:
        missing.append("unsupported_replay_path")
    return summarize_cost(events, missing)


def review_cost(report_path, receipt_path, output):
    """Append evolution cost accounting after timers close; never rewrite the frozen cycle."""
    report_ref, receipt_ref = reference(report_path), reference(receipt_path)
    report, receipt = read(bound(report_ref)), read(bound(receipt_ref))
    contract = read(bound(report["contract"]))
    contract_ref = contract.get("cost_contract")
    require(contract_ref is not None, "cycle needs a frozen cost contract")
    events, missing = read_cost_receipt(receipt_ref, contract_ref, report_ref, "evolution")
    require(number(report["engine_seconds"]), "invalid engine cost")
    engine_event = {"id": "engine:" + report_ref["sha256"], "evidence": report_ref,
                    "seconds_path": "engine_seconds", "phase": "execution", "seconds": report["engine_seconds"]}
    events.append(engine_event)
    accounting = summarize_cost(events, missing)
    result = {"schema": "research_evolution_cost_review.v1", "at": now(), "report": report_ref,
              "receipt": receipt_ref, "cost_contract": contract_ref, "accounting": accounting,
              "all_in_evolution_cost_seconds": accounting["all_in_method_cost_seconds"],
              "efficiency_improvement_claimed": False}
    write(output, result)
    return result


def quality_contract(ref):
    contract = read(bound(ref))
    if contract.get("schema") == "research_quality.v2":
        validate_account_contract(contract)
        return contract
    require(contract["schema"] == "research_quality.v1" and
            contract["kind"] in {"joint_goal_margin_v1", "risk_cap_return_v1"},
            "unsupported quality evaluator")
    require(number(contract["drawdown_ceiling"]) and 0 < contract["drawdown_ceiling"] < 1,
            "invalid frozen drawdown ceiling")
    if contract["kind"] == "joint_goal_margin_v1":
        require(number(contract["annual_floor"]) and contract["annual_floor"] > 0,
                "invalid frozen annual floor")
    for field in ["annual_path", "drawdown_path", "correctness_path"]:
        require(isinstance(contract[field], str) and contract[field], "metric field path required")
    bound(contract["goal_source"])
    return contract


def grade(contract_ref, evidence_ref):
    """Score a hash-bound account review using its frozen, branch-specific goal."""
    contract = quality_contract(contract_ref)
    if contract["schema"] == "research_quality.v2":
        return review_account(contract_ref, evidence_ref)["quality"]
    evidence = read(bound(evidence_ref))
    def value(path):
        item = evidence
        for part in path.split("."):
            item = item[part]
        return item
    require(value(contract["correctness_path"]) is True, "economic result not independently verified")
    require(type(value(contract["annual_path"])) is not bool and
            type(value(contract["drawdown_path"])) is not bool, "boolean is not an account metric")
    annual_value = value(contract["annual_path"])
    drawdown_value = value(contract["drawdown_path"])
    annual = float(annual_value)
    drawdown = float(drawdown_value)
    require(score_number(annual) and number(drawdown) and drawdown <= 1, "invalid account metrics")
    if contract["kind"] == "joint_goal_margin_v1":
        return min(annual / contract["annual_floor"] - 1,
                   1 - drawdown / contract["drawdown_ceiling"])
    require(annual >= -1, "annual return below the account loss floor")
    exact_drawdown = Decimal(str(drawdown_value))
    exact_cap = Decimal(str(contract["drawdown_ceiling"]))
    return annual if exact_drawdown <= exact_cap else -1 - drawdown


def sha256_string(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate_account_contract(contract):
    """Prospective development score only; never confers independent confirmation."""
    require(contract.get("kind") == "account_profit_v1", "unsupported account evaluator")
    require(contract.get("phase") in {"development", "selection_validation"},
            "independent confirmation needs a separate prospectively approved protocol")
    require(contract.get("prior_data_use") in {"used_development", "used_selection", "unknown"},
            "declare prior data use; historical relabelling cannot certify independence")
    policy = read(bound(contract["goal_source"]))
    require(policy.get("schema") == "research_objective.ms028.v1" and
            policy.get("id") == "MS028" and policy.get("max_drawdown") == 0.20 and
            contract.get("drawdown_ceiling") == policy["max_drawdown"], "MS028 risk cap mismatch")
    window = contract["window"]
    require(set(window) == {"start", "end", "trading_days", "calendar_sha256"}, "invalid window fields")
    start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
    require(date(2020, 1, 1) <= start <= end <= date(2025, 12, 31), "2026 data remains sealed")
    require(type(window["trading_days"]) is int and 0 < window["trading_days"] <= (end - start).days + 1,
            "invalid trading-day count")
    require(sha256_string(window["calendar_sha256"]), "calendar hash required")
    account = contract["account"]
    require(set(account) == {"initial_equity", "fees_sha256", "execution_sha256", "cashflows"},
            "invalid account assumptions")
    require(number(account["initial_equity"]) and account["initial_equity"] > 0 and
            account["cashflows"] == "none", "positive initial cash and no external cashflows required")
    require(sha256_string(account["fees_sha256"]) and sha256_string(account["execution_sha256"]),
            "freeze fee and execution assumptions")
    require(sha256_string(contract["baseline_source_sha256"]), "fixed baseline source hash required")
    bound(contract["protocol"])


def review_account(contract_ref, evidence_ref):
    """Validate a small adapter of independently checked complete account outputs.

    This checks metric comparability, not the truth of an accountant's attestations.
    Original review references are mandatory; no raw market data is read here.
    """
    contract = quality_contract(contract_ref)
    require(contract["schema"] == "research_quality.v2", "review-account requires a v2 contract")
    evidence = read(bound(evidence_ref))
    require(evidence.get("schema") == "research_account_comparison.v1" and
            evidence.get("contract_sha256") == contract_ref["sha256"], "account contract identity mismatch")
    checks = ("complete_account_verified", "prebuy_observability_verified", "full_population_verified")
    for name in ["candidate", "baseline"]:
        account = evidence[name]
        require(account["window"] == contract["window"] and account["account"] == contract["account"],
                name + ": window, calendar, cash, fees or execution differ")
        require(sha256_string(account["source_sha256"]), name + ": source hash required")
        require(all(account.get(key) is True for key in checks), name + ": account verification incomplete")
        refs = account.get("verification_refs")
        require(isinstance(refs, list) and refs, name + ": original independent review required")
        for ref in refs:
            bound(ref)
        for key in ["net_return", "net_profit"]:
            require(account.get(key) is not None and score_number(account[key]), name + ": missing net result")
        require(account["net_return"] >= -1 and number(account.get("max_drawdown")) and
                account["max_drawdown"] <= 1, name + ": invalid return or drawdown")
        require(Decimal(str(account["max_drawdown"])) >= max(Decimal(0), -Decimal(str(account["net_return"]))),
                name + ": drawdown omits the initial equity peak")
        expected_profit = Decimal(str(account["net_return"])) * Decimal(str(contract["account"]["initial_equity"]))
        require(abs(Decimal(str(account["net_profit"])) - expected_profit) <= Decimal("0.01"),
                name + ": net profit and return do not reconcile to one cent")
        support = account["support"]
        for key in ["trades", "active_dates", "opportunities", "unknown_opportunities"]:
            require(type(support.get(key)) is int and support[key] >= 0, name + ": support must retain unknowns")
        require(support["active_dates"] <= contract["window"]["trading_days"] and
                support["active_dates"] <= support["trades"] <= support["opportunities"] and
                support["unknown_opportunities"] <= support["opportunities"], name + ": inconsistent support")
        require((support["trades"] == 0) == (support["active_dates"] == 0), name + ": inconsistent participation")
        require("concentration" in account and "cost_sensitivity" in account,
                name + ": disclose concentration/cost evidence or explicit null")
    candidate, baseline = evidence["candidate"], evidence["baseline"]
    require(baseline["source_sha256"] == contract["baseline_source_sha256"], "fixed comparator changed")
    cap = Decimal(str(contract["drawdown_ceiling"]))
    risk_pass = Decimal(str(candidate["max_drawdown"])) <= cap
    baseline_risk_pass = Decimal(str(baseline["max_drawdown"])) <= cap
    increment = float(Decimal(str(candidate["net_return"])) - Decimal(str(baseline["net_return"])))
    profit_increment = float(Decimal(str(candidate["net_profit"])) - Decimal(str(baseline["net_profit"])))
    if not risk_pass:
        decision = "risk_limit_failed"
    elif candidate["support"]["trades"] == 0:
        decision = "no_participation"
    elif candidate["net_return"] <= 0:
        decision = "non_positive_account"
    elif increment > 0:
        decision = "return_improved"
    elif not baseline_risk_pass:
        decision = "risk_repair_tradeoff"
    else:
        decision = "return_not_improved"
    quality = candidate["net_return"] if risk_pass else -1 - candidate["max_drawdown"]
    return {"schema": "research_account_scorecard.v1", "phase": contract["phase"],
            "contract": contract_ref, "evidence": evidence_ref, "quality": quality,
            "quality_metric": "same_window_fee_net_account_return_under_risk_cap",
            "decision": decision, "risk_pass": risk_pass, "baseline_risk_pass": baseline_risk_pass,
            "net_return": candidate["net_return"], "net_profit": candidate["net_profit"],
            "incremental_return": increment, "incremental_profit": profit_increment,
            "max_drawdown": candidate["max_drawdown"], "support": candidate["support"],
            "concentration": candidate["concentration"], "cost_sensitivity": candidate["cost_sensitivity"],
            "baseline_summary": {k: baseline[k] for k in ["net_return", "net_profit", "max_drawdown", "support"]},
            "independent_confirmation_passed": None, "formal_adoption_authorized": False,
            "limitations": ["Development/selection score only; no minimum confirmation return or sample support is inferred.",
                            "Hashes and declarations do not prove data truth or statistical independence.",
                            "Non-positive, no-participation and over-cap outcomes do not meet the profit objective."]}


def initial_policy():
    return {"id": "initial_executable_v1", "priority": ["repair", "follow", "explore"],
            "max_depth": 4, "max_attempts": 16, "reflection_every": 3}


def validate_policy(policy):
    require(set(policy) == POLICY_KEYS, "unknown or missing policy fields")
    require(isinstance(policy["id"], str) and policy["id"], "policy id required")
    require(sorted(policy["priority"]) == ["explore", "follow", "repair"], "bad priority")
    for key, low, high in [("max_depth", 1, 8), ("max_attempts", 1, 100),
                           ("reflection_every", 3, 5)]:
        require(type(policy[key]) is int and low <= policy[key] <= high, "bad " + key)


def validate_observation(obs):
    require(set(obs) == {"status", "quality", "question", "resolved", "substantive"},
            "observation must contain only status/quality/question/resolved/substantive")
    require(obs["status"] in STATUSES, "unknown outcome status")
    require(score_number(obs["quality"]), "quality must be finite or null")
    require(isinstance(obs["question"], str) and obs["question"], "question id required")
    require(type(obs["resolved"]) is bool and type(obs["substantive"]) is bool, "bad flags")
    if obs["status"] in {"implementation_error", "evidence_gap"}:
        require(obs["quality"] is None and not obs["resolved"], "unknown is not a score")


def validate_world(world, verify=True):
    require(world["schema"] == SCHEMA and world["branch"] in BRANCHES, "world scope")
    require(world["admission"] in {"diagnostic", "prospective"}, "world admission")
    require(world["usage"] in {"development", "heldout"}, "world usage")
    require(isinstance(world["limitations"], list), "limitations required")
    require(score_number(world["initial_quality"]), "initial quality")
    regression_questions = world.get("method_regression_questions", [])
    require(isinstance(regression_questions, list) and
            all(isinstance(q, str) and q for q in regression_questions) and
            len(regression_questions) == len(set(regression_questions)), "invalid method regression questions")
    stage = evaluation_stage(world)
    if stage != "economic":
        require(world["initial_quality"] is None, "non-economic stage has no initial economic quality")
    if world.get("cost_contract"):
        cost_contract(world["cost_contract"])
    nodes = world["nodes"]
    ids = [n["id"] for n in nodes]
    require(len(ids) == len(set(ids)) and "root" not in ids, "duplicate/root node")
    previous = {}
    for node in nodes:
        require(node["id"].startswith("n") and node["id"][1:].isdigit(), "opaque node id required")
        require(node["parent"] == "root" or node["parent"] in previous, "parent not earlier")
        dependencies = node["depends_on"]
        require(len(dependencies) == len(set(dependencies)), "duplicate dependency")
        require(all(d in previous for d in dependencies), "dependency not earlier")
        if node["parent"] != "root":
            require(node["parent"] in dependencies, "parent dependency missing")
        decision = timestamp(node["decision_at"])
        available = timestamp(node["available_at"])
        require(available >= decision, "result precedes decision")
        require(all(timestamp(previous[d]["available_at"]) <= decision for d in dependencies),
                "dependency was not available at decision time")
        validate_observation(node["observation"])
        if stage != "economic":
            evaluate_stage(world["evaluation_contract"], node.get("stage_evidence"), node["observation"])
        else:
            require(not node.get("stage_evidence"), "stage evidence requires a frozen evaluation contract")
        require(not node.get("cost_receipt") or world.get("cost_contract"), "cost receipt requires a frozen contract")
        require(number(node["measured_seconds"], True) and number(node["total_seconds"], True),
                "negative/nonfinite cost")
        if node["total_seconds"] is not None and node["measured_seconds"] is not None:
            require(node["total_seconds"] >= node["measured_seconds"], "total below measured cost")
        require(bool(node["evidence"]), "node evidence required")
        if verify:
            for ref in node["evidence"]:
                bound(ref)
        previous[node["id"]] = node
    if world["admission"] == "prospective":
        require(world.get("context_complete") is True, "prospective context incomplete")
        require(bool(world.get("batch_contract")), "prospective batch contract missing")
        if verify:
            bound(world["batch_contract"])
            quality_contract(world["quality_contract"])
        require(world["metric_contract"] == metric_identity(world["branch"], world["quality_contract"],
                                                           world.get("evaluation_contract")),
                "quality contract identity mismatch")
    if world.get("evaluation_contract") or world.get("cost_contract"):
        require(world["metric_contract"] == metric_identity(world["branch"], world["quality_contract"],
                                                           world.get("evaluation_contract")), "evaluation identity mismatch")
        node_costs(world, nodes)
    return world


def public_view(nodes, blocked, attempts, budget):
    """No world, filenames, future node IDs, hidden children or future costs reach choose()."""
    parents = {node["parent"] for node in nodes}
    depth = {"root": 0}
    leaves = []
    for node in nodes:
        depth[node["id"]] = depth[node["parent"]] + 1
        if node["id"] not in parents and node["id"] not in blocked:
            leaves.append({"id": node["id"], "depth": depth[node["id"]],
                           "observation": copy.deepcopy(node["observation"])})
    return {"leaves": leaves, "root_available": "root" not in blocked,
            "attempts": attempts, "remaining": max(0, budget - attempts)}


def choose(view, policy):
    validate_policy(policy)
    if view["remaining"] <= 0:
        return None
    groups = {"repair": [], "follow": [], "explore": []}
    for leaf in view["leaves"]:
        if leaf["depth"] >= policy["max_depth"]:
            continue
        status = leaf["observation"]["status"]
        if status == "implementation_error":
            groups["repair"].append(leaf["id"])
        elif status in {"valid_improvement", "verified_information", "evidence_gap"}:
            groups["follow"].append(leaf["id"])
    if view["root_available"]:
        groups["explore"].append("root")
    for group in policy["priority"]:
        if groups[group]:
            return {"anchor": groups[group][-1], "action": group}
    return None


def validate_frontier(frontier, branch):
    """A versioned candidate pool, including ideas that are not executable yet."""
    require(frontier["schema"] == "research_frontier.v1" and frontier["branch"] == branch,
            "frontier scope mismatch")
    require(bool(frontier["generation_evidence"]), "candidate generation evidence required")
    for ref in frontier["generation_evidence"]:
        bound(ref)
    candidates = frontier["candidates"]
    require(isinstance(candidates, list) and candidates, "empty candidate frontier")
    seen = set()
    families = {}
    active = {}
    for candidate in candidates:
        for field in ("id", "hypothesis_id", "family", "question", "rationale"):
            require(isinstance(candidate[field], str) and candidate[field].strip(), "missing " + field)
        require(candidate["id"] not in seen, "duplicate candidate id")
        seen.add(candidate["id"])
        hypothesis = candidate["hypothesis_id"]
        require(hypothesis not in families or families[hypothesis] == candidate["family"],
                "one hypothesis cannot count as multiple families")
        families[hypothesis] = candidate["family"]
        require(candidate["status"] in {"ready", "needs_evidence", "waiting_dependency", "deferred", "closed"},
                "invalid candidate status")
        if candidate["status"] not in {"closed", "deferred"}:
            active[hypothesis] = candidate["family"]
        require(candidate["action"] in {"explore", "follow", "repair"}, "invalid candidate action")
        require(isinstance(candidate["anchor"], str) and candidate["anchor"], "anchor required")
        require(isinstance(candidate["depends_on"], list) and
                len(candidate["depends_on"]) == len(set(candidate["depends_on"])), "bad candidate dependencies")
        for field in ("information_value", "cost_class"):
            require(type(candidate[field]) is int and 1 <= candidate[field] <= 3, "invalid " + field)
        if candidate["status"] != "ready":
            require(bool(candidate["gap"]), "unready candidate needs a concrete gap")
        spec = read(bound(candidate["spec"]))
        require(all(spec[field] == candidate[field] for field in ("id", "hypothesis_id", "family", "question")),
                "candidate spec identity mismatch")
        for field in ("mechanism", "falsifier", "data_clock", "first_test", "stop_rule", "budget"):
            require(bool(spec.get(field)), "candidate spec missing " + field)
        require(bool(spec["source_evidence"]), "candidate evidence required")
        for ref in spec["source_evidence"]:
            bound(ref)
    require(len(active) <= 8, "keep at most eight active hypotheses; preserve others as deferred")
    shortfall = len(active) < 6 or len(set(active.values())) < 3
    if shortfall:
        require(bool(frontier.get("shortfall_reason")) and bool(frontier.get("replenishment_action")),
                "shortfall requires a concrete replenishment action")
    return {"hypotheses": len(families), "active_hypotheses": len(active), "families": len(set(active.values())),
            "ready_actions": sum(c["status"] == "ready" for c in candidates), "shortfall": shortfall}


def frontier_history(config, nodes):
    decisions = [read(bound(ref)) for ref in config["frontier_pilot"]["recent_decisions"]]
    decisions += [read(bound(node["evidence"][0])) for node in nodes]
    history = []
    for decision in decisions:
        require(decision["branch"] == config["branch"], "frontier history branch mismatch")
        selected = decision["selected"]
        if "candidate_id" in selected:
            history.append(selected)
    return history


def frontier_selector(config):
    pilot = config["frontier_pilot"]
    rule = pilot["rule"]
    require(rule in {"least_served_family_v1", "bottleneck_evidence_cost_v1"}, "unsupported frontier rule")
    if rule == "least_served_family_v1":
        require("selection_contract" not in pilot, "legacy selector cannot consume a new selection contract")
        return None
    contract = read(bound(pilot["selection_contract"]))
    require(contract["schema"] == "research_frontier_selector.v1" and contract["rule"] == rule and
            contract["branch"] == config["branch"], "selector contract scope mismatch")
    bound(contract["plan"])
    bottleneck = contract["bottleneck"]
    require(all(isinstance(bottleneck.get(k), str) and bottleneck[k].strip() for k in ("id", "statement")),
            "selector needs an explicit economic bottleneck")
    require(bool(bottleneck["evidence"]), "bottleneck evidence required")
    for ref in bottleneck["evidence"]:
        bound(ref)
    require(contract["cost_basis"] == "spec_budget_resource_seconds", "unsupported estimated cost basis")
    return contract


def selection_basis(candidate, contract):
    """Read predeclared judgments from the existing SPEC; never infer profit or probability."""
    spec = read(bound(candidate["spec"]))
    basis = spec.get("selection_basis")
    if not basis:
        return None, "selection_basis_missing"
    require(basis["bottleneck_id"] == contract["bottleneck"]["id"], "candidate bottleneck mismatch")
    require(basis["relationship"] in {"direct", "enabling", "unrelated"}, "invalid bottleneck relationship")
    require(isinstance(basis["rationale"], str) and basis["rationale"].strip(), "selection rationale required")
    require(bool(basis["evidence"]), "selection evidence required")
    for ref in basis["evidence"]:
        bound(ref)
    contrast = basis["discrimination"]
    require(contrast["level"] in {"separates", "constrains", "none"}, "invalid discrimination level")
    require(isinstance(contrast["rationale"], str) and contrast["rationale"].strip(), "contrast rationale required")
    alternatives = contrast["alternatives"]
    require(isinstance(alternatives, list), "alternatives must be a list")
    for item in alternatives:
        require(all(isinstance(item.get(k), str) and item[k].strip() for k in ("id", "expected_observation")),
                "alternative needs an identity and predicted observation")
    require(len({a["id"] for a in alternatives}) == len(alternatives), "duplicate competing explanation")
    if contrast["level"] != "none":
        require(len(alternatives) >= 2, "discrimination requires competing explanations")
    if contrast["level"] == "separates":
        require(len({a["expected_observation"].strip() for a in alternatives}) >= 2,
                "separating test needs different predicted observations")
    seconds = spec["budget"].get("resource_seconds") if isinstance(spec["budget"], dict) else None
    if seconds is None:
        return None, "estimated_cost_unknown"
    require(number(seconds), "invalid estimated resource cost")
    return {"relationship": basis["relationship"], "discrimination": contrast["level"],
            "estimated_resource_seconds": seconds, "rationale": basis["rationale"],
            "basis": basis, "spec": candidate["spec"]}, None


def frontier_selection(candidate):
    if candidate is None:
        return None
    return {"anchor": candidate["anchor"], "action": candidate["action"], "candidate_id": candidate["id"],
            "hypothesis_id": candidate["hypothesis_id"], "family": candidate["family"],
            "question": candidate["question"], "spec": candidate["spec"]}


def choose_frontier(frontier, config, nodes):
    """Choose an actual eligible candidate; branch width does not grant more workers."""
    summary = validate_frontier(frontier, config["branch"])
    history = frontier_history(config, nodes)
    used = {entry["candidate_id"] for entry in history}
    recent = history[-6:]
    counts = {}
    for entry in recent:
        counts[entry["family"]] = counts.get(entry["family"], 0) + 1
    known = {node["id"]: node for node in nodes}
    depth = {"root": 0}
    for node in nodes:
        depth[node["id"]] = depth[node["parent"]] + 1
    contract = frontier_selector(config)
    questions = set(stage_contract(config["evaluation_contract"])["questions"]) if config.get("evaluation_contract") else None
    eligible, excluded = [], []
    for candidate in frontier["candidates"]:
        reason = None
        anchor, action = candidate["anchor"], candidate["action"]
        parent = known.get(anchor)
        if candidate["status"] != "ready":
            reason = candidate["status"]
        elif questions is not None and candidate["question"] not in questions:
            reason = "question_outside_frozen_stage"
        elif candidate["id"] in used:
            reason = "already_dispatched"
        elif not set(candidate["depends_on"]) <= known.keys():
            reason = "dependency_not_observed"
        elif any(not known[d]["observation"]["resolved"] for d in candidate["depends_on"] if d != anchor):
            reason = "dependency_unresolved"
        elif anchor not in depth or depth[anchor] >= config["policy"]["max_depth"]:
            reason = "anchor_unknown_or_depth_limit"
        elif action == "explore" and anchor != "root":
            reason = "exploration_requires_root"
        elif action in {"repair", "follow"} and (parent is None or anchor not in candidate["depends_on"]):
            reason = "parent_dependency_required"
        elif action == "repair" and (parent["observation"]["status"] != "implementation_error" or
                                      candidate["question"] != parent["observation"]["question"]):
            reason = "repair_must_keep_failed_question"
        elif action == "follow" and parent["observation"]["status"] not in {
                "valid_improvement", "verified_information", "evidence_gap"}:
            reason = "parent_does_not_support_continuation"
        if reason:
            excluded.append({"candidate_id": candidate["id"], "reason": reason})
        else:
            eligible.append(candidate)
    # Fixed ordinal judgments, not probabilities or predicted economic scores.
    control = sorted(eligible, key=lambda c: (counts.get(c["family"], 0),
                                              -c["information_value"], c["cost_class"], c["id"]))
    ranked, bases = control, {}
    if contract:
        for candidate in eligible:
            basis, reason = selection_basis(candidate, contract)
            if reason:
                excluded.append({"candidate_id": candidate["id"], "reason": reason})
            else:
                bases[candidate["id"]] = basis
        def order(candidate):
            basis = bases[candidate["id"]]
            # An observed implementation failure must be repaired before optional exploration.
            return (candidate["action"] != "repair",
                    {"direct": 0, "enabling": 1, "unrelated": 2}[basis["relationship"]],
                    {"separates": 0, "constrains": 1, "none": 2}[basis["discrimination"]],
                    basis["estimated_resource_seconds"], config["policy"]["priority"].index(candidate["action"]),
                    counts.get(candidate["family"], 0), candidate["id"])
        ranked = sorted((c for c in eligible if c["id"] in bases), key=order)
    comparison = {"summary": summary, "recent_family_dispatches": counts, "excluded": excluded,
                  "ranking": [{"candidate_id": c["id"], "family": c["family"],
                               "information_value": c["information_value"], "cost_class": c["cost_class"],
                               "rationale": c["rationale"]} for c in ranked]}
    comparison["rule"] = config["frontier_pilot"]["rule"]
    selected = frontier_selection(ranked[0] if ranked else None)
    if contract:
        for row in comparison["ranking"]:
            row.update(bases[row["candidate_id"]])
            candidate = next(c for c in ranked if c["id"] == row["candidate_id"])
            row["sort_key"] = list(order(candidate))
        comparison.update(selection_contract=config["frontier_pilot"]["selection_contract"],
                          sort_dimensions=["required_repair", "bottleneck_relationship", "discrimination",
                                           "estimated_resource_seconds", "policy_priority", "family_dispatches", "id"],
                          control={"rule": "least_served_family_v1", "selected": frontier_selection(control[0] if control else None),
                                   "ranking": [c["id"] for c in control]},
                          deferred_candidates=[c["id"] for c in ranked[1:]],
                          unexecuted_outcomes="unknown", efficiency_improvement_claimed=False)
    return selected, comparison


def shadow_frontier(directory, context_path, output):
    """Compare two selectors on one bound snapshot without creating a research decision."""
    directory = Path(directory)
    with lock(directory):
        config = batch(directory)
        require(config["mode"] == "frontier_pilot" and frontier_selector(config) is not None,
                "shadow comparison needs the new boundary-frozen selector")
        require(not (directory / "PILOT_REVIEW.json").exists() and not (directory / "EXPORT.json").exists(), "batch closed")
        nodes, pending = ledger(directory)
        require(pending is None, "shadow requires a completed visible prefix")
        completed = {n["observation"]["question"] for n in nodes if n["observation"]["resolved"] and n["observation"]["substantive"]}
        require(not has_override(nodes) and len(completed) < config["policy"]["reflection_every"] and
                len(nodes) < min(config["frontier_pilot"]["max_decisions"], config["policy"]["max_attempts"]),
                "reflection required before another shadow selection")
        context_ref = reference(context_path)
        context = read(bound(context_ref))
        require(context["branch"] == config["branch"] and context["context_complete"] is True and
                context["evidence"], "visible context required")
        for ref in context["evidence"]:
            bound(ref)
        frontier_ref = context.get("frontier", config["frontier_pilot"]["initial_frontier"])
        selected, comparison = choose_frontier(read(bound(frontier_ref)), config, nodes)
        result = {"schema": "research_frontier_shadow.v1", "at": now(), "branch": config["branch"],
                  "batch": reference(directory / "BATCH.json"), "context": context_ref, "frontier": frontier_ref,
                  "visible_nodes": [n["id"] for n in nodes], "selected": selected, "comparison": comparison,
                  "decision_created": False, "research_executed": False, "efficiency_improvement_claimed": False}
        bound(context_ref)
        write(output, result)
        return result


def replay(world, policy, verify=True, semantics="v1"):
    validate_world(world, verify)
    validate_policy(policy)
    visible, blocked, trace = [], set(), []
    budget = policy["max_attempts"]
    for step in range(budget):
        view = public_view(visible, blocked, step, budget)
        selected = choose(copy.deepcopy(view), policy)
        if selected is None:
            break
        seen = {n["id"] for n in visible}
        children = [n for n in world["nodes"] if n["parent"] == selected["anchor"]
                    and n["id"] not in seen]
        # Earliest recorded child, never choose a favorable unseen child.
        child = children[0] if children else None
        if child is None or not set(child["depends_on"]) <= seen:
            reason = "no_recorded_continuation" if child is None else "dependency_unrevealed"
            # Dependency blocks may become legal later; clear them after new observations.
            blocked.add(selected["anchor"])
            trace.append({"step": step + 1, "view": view, "selected": selected,
                          "status": "unsupported", "reason": reason})
            continue
        visible.append(child)
        blocked = {t["selected"]["anchor"] for t in trace
                   if t.get("reason") == "no_recorded_continuation"}
        trace.append({"step": step + 1, "view": view, "selected": selected,
                      "status": "revealed", "node": child["id"]})
        if len(visible) == len(world["nodes"]):
            break
    observations = [n["observation"] for n in visible]
    qualities = [o["quality"] for o in observations if o["quality"] is not None]
    if world["initial_quality"] is not None:
        qualities.append(world["initial_quality"])
    resolved = {o["question"] for o in observations if o["resolved"]}
    unknown = sum(n["total_seconds"] is None for n in visible)
    unsupported = sum(t["status"] == "unsupported" for t in trace)
    # v1 (default) discards every candidate that ever chose an unrecorded continuation. Because
    # live batches ran one decision at a time, almost every alternative policy did, and 2,227 of
    # 2,330 rejected candidates (2026-10-16 audit) failed on this alone. Opt-in v2: an unrecorded
    # choice reveals nothing (no quality, no resolved question) yet still counts as a request and
    # is charged the worst cost observed in this world, so it can never look like a saving.
    worst_total = max((n["total_seconds"] for n in world["nodes"] if n["total_seconds"] is not None), default=None)
    worst_measured = max((n["measured_seconds"] for n in world["nodes"] if n["measured_seconds"] is not None),
                         default=None)
    def charged(values, worst):
        if any(v is None for v in values) or (unsupported and worst is None):
            return None
        return sum(values) + unsupported * (worst or 0)
    metrics = {"best_quality": max(qualities) if qualities else None,
               "resolved_questions": len(resolved), "observed_nodes": len(visible),
               "unsupported_actions": unsupported, "decision_calls": len(trace),
               "known_measured_seconds": sum(n["measured_seconds"] or 0 for n in visible),
               "missing_measured_cost_nodes": sum(n["measured_seconds"] is None for n in visible),
               "missing_total_cost_nodes": unknown,
               "total_seconds": None if unknown or unsupported else sum(n["total_seconds"] for n in visible),
               "implementation_errors": sum(o["status"] == "implementation_error" for o in observations)}
    require(semantics in {"v1", "v2"}, "unknown replay semantics")
    if semantics == "v2":
        metrics.update({"requests": len(visible) + unsupported,
                        "unobserved_share": unsupported / len(trace) if trace else 0.0,
                        "total_seconds": charged([n["total_seconds"] for n in visible], worst_total),
                        "measured_seconds": charged([n["measured_seconds"] for n in visible], worst_measured),
                        "unobserved_cost_charge": {"choices": unsupported, "worst_total_seconds": worst_total,
                                                   "worst_measured_seconds": worst_measured},
                        "replay_semantics": REPLAY_SEMANTICS})
    metrics["evaluation_stage"] = evaluation_stage(world)
    metrics["cost_scope"] = cost_identity(world)
    accounting = node_costs(world, visible, unsupported)
    metrics["all_in_method_cost_seconds"] = accounting["all_in_method_cost_seconds"]
    if world.get("evaluation_contract"):
        checks = [evaluate_stage(world["evaluation_contract"], n.get("stage_evidence"), n["observation"]) for n in visible]
        metrics["stage_passed_questions"] = len({c["question"] for c in checks if c and c["passed"]})
    method_observations = [{"node": n["id"], **n["observation"]} for n in visible]
    if world.get("evaluation_contract"):
        for observation, check in zip(method_observations, checks):
            observation["stage_passed"] = check["passed"] if check else None
    return {"world": world["id"], "branch": world["branch"], "policy": policy["id"],
            "metrics": metrics, "trace": trace, "cost_accounting": accounting,
            "method_observations": method_observations,
            "method_regression_questions": world.get("method_regression_questions", []),
            "unrevealed": [n["id"] for n in world["nodes"] if n not in visible]}


def propose(policy, feedback):
    """Small, bounded policy-language mutations informed by actual replay feedback."""
    errors = sum(r["metrics"]["implementation_errors"] for r in feedback)
    unsupported = sum(r["metrics"]["unsupported_actions"] for r in feedback)
    orders = ([["repair", "explore", "follow"], ["explore", "repair", "follow"]]
              if errors else [["explore", "repair", "follow"], ["follow", "repair", "explore"]])
    variants = []
    orders += [["follow", "explore", "repair"], ["repair", "follow", "explore"]]
    for i, order in enumerate(orders, 1):
        candidate = copy.deepcopy(policy)
        candidate["priority"] = order
        if i == 2 and unsupported:
            candidate["max_depth"] = max(1, policy["max_depth"] - 1)
        settings = {k: v for k, v in candidate.items() if k != "id"}
        existing = [policy] + [p["policy"] for p in variants]
        if any(settings == {k: v for k, v in p.items() if k != "id"} for p in existing):
            continue
        candidate["id"] = "candidate_" + digest(json.dumps(settings, sort_keys=True).encode())[:12]
        variants.append({"policy": candidate, "reason": {
            "observed_implementation_errors": errors, "observed_unsupported_actions": unsupported,
            "hypothesis": "compare repair, exploration and continuation order under the same budget"}})
        if len(variants) == 2:
            break
    return variants


def dominates_v1(candidate, incumbent):
    """Fixed request-count objective; unknown seconds never become zero-cost savings.

    Quality and resolved coverage must not decline. At least one conclusion is
    required. This only qualifies a policy for a pilot, not a time/money claim.
    """
    strict = False
    for new, old in zip(candidate, incumbent):
        a, b = new["metrics"], old["metrics"]
        if any(m["best_quality"] is None or m["unsupported_actions"] for m in (a, b)):
            return False
        if (a["best_quality"] < b["best_quality"] or
                a["resolved_questions"] < max(1, b["resolved_questions"]) or
                a["observed_nodes"] > b["observed_nodes"]):
            return False
        strict |= (a["best_quality"] > b["best_quality"] or
                   a["observed_nodes"] < b["observed_nodes"])
    return strict


def rrsi_compare_v1(candidate, incumbent, floor=None):
    """Require non-regression and known comparable node costs; novelty is not evidence."""
    require(bool(candidate) and len(candidate) == len(incumbent), "unpaired RRSI comparison")
    reasons, strict, complete = [], False, True
    for new, old in zip(candidate, incumbent):
        require(new["world"] == old["world"], "RRSI world mismatch")
        world_id, a, b = new["world"], new["metrics"], old["metrics"]
        stage = a.get("evaluation_stage", "economic")
        require(stage == b.get("evaluation_stage", "economic"), "RRSI evaluation stage mismatch")
        scope = a.get("cost_scope", "recorded_node_total_seconds")
        require(scope == b.get("cost_scope", "recorded_node_total_seconds"), "RRSI cost scope mismatch")
        for check in method_regressions(new, old):
            if check["passed"] is None:
                reasons.append(world_id + ":unknown_required_regression:" + check["question"])
                complete = False
            elif not check["passed"]:
                reasons.append(world_id + ":required_regression:" + check["question"])
        if any(m["unsupported_actions"] or (stage == "economic" and m["best_quality"] is None) for m in (a, b)):
            reasons.append(world_id + ":unknown_quality_or_unobserved_path")
            complete = False
            continue
        if stage == "economic":
            minimum = b["best_quality"]
            if floor and floor.get(world_id) is not None:
                minimum = max(minimum, floor[world_id])
            if a["best_quality"] < minimum:
                reasons.append(world_id + ":quality_floor")
            strict |= a["best_quality"] > b["best_quality"]
        else:
            require(stage in {"information", "engineering"} and a["best_quality"] is None and b["best_quality"] is None,
                    "non-economic stage cannot carry economic quality")
            if a.get("stage_passed_questions") is None or b.get("stage_passed_questions") is None:
                reasons.append(world_id + ":unknown_stage_verification")
                complete = False
            elif a["stage_passed_questions"] < max(1, b["stage_passed_questions"]):
                reasons.append(world_id + ":stage_criteria_coverage")
            else:
                strict |= a["stage_passed_questions"] > b["stage_passed_questions"]
        if a["resolved_questions"] < max(1, b["resolved_questions"]):
            reasons.append(world_id + ":resolved_coverage")
        if a["observed_nodes"] > b["observed_nodes"]:
            reasons.append(world_id + ":request_cost_increase")
        if a["implementation_errors"] > b["implementation_errors"]:
            reasons.append(world_id + ":more_implementation_errors")
        cost_key = "total_seconds" if scope == "recorded_node_total_seconds" else "all_in_method_cost_seconds"
        if a.get(cost_key) is None or b.get(cost_key) is None:
            reasons.append(world_id + ":unknown_total_cost")
            complete = False
        else:
            if a[cost_key] > b[cost_key]:
                reasons.append(world_id + ":total_cost_increase")
            strict |= a[cost_key] < b[cost_key]
        strict |= (a["resolved_questions"] > b["resolved_questions"] or a["observed_nodes"] < b["observed_nodes"])
    if not strict:
        reasons.append("no_strict_advantage")
    return {"accepted": not reasons, "complete": complete, "reasons": reasons}


def select_candidate_v1(candidates, runs, comparisons, development, rrsi):
    """Resolve the admissible Pareto set without using iteration order or heldout feedback."""
    eligible = [i for i, comparison in enumerate(comparisons) if comparison["accepted"]]
    def better(left, right):
        a, b = [runs[left][k] for k in development], [runs[right][k] for k in development]
        return rrsi_compare(a, b)["accepted"] if rrsi else dominates(a, b)
    frontier = [i for i in eligible if not any(better(j, i) for j in eligible if i != j)]
    # Equivalent measurements use canonical policy content; incomparable tradeoffs keep the parent.
    def signature(index):
        values = []
        for k in development:
            m = runs[index][k]["metrics"]
            if not rrsi:
                values.append((m["best_quality"], m["resolved_questions"], m["observed_nodes"]))
                continue
            cost_key = ("total_seconds" if m.get("cost_scope", "recorded_node_total_seconds") ==
                        "recorded_node_total_seconds" else "all_in_method_cost_seconds")
            values.append((m["best_quality"], m.get("stage_passed_questions"), m["resolved_questions"],
                           m["observed_nodes"], m["implementation_errors"], m[cost_key]))
        return values
    selected = None
    if frontier and all(signature(i) == signature(frontier[0]) for i in frontier):
        selected = min(frontier, key=lambda i: json.dumps(
            {k: v for k, v in candidates[i]["policy"].items() if k != "id"}, sort_keys=True))
    reason = "no_admissible_candidate"
    if selected is not None:
        reason = "admissible_choice"
    elif frontier:
        reason = "incomparable_tradeoff"
    receipt = {"rule": "unique_pareto_or_equivalent_v1", "baseline": "frozen_incumbent",
               "admissible": sorted(candidates[i]["policy"]["id"] for i in eligible),
               "pareto_frontier": sorted(candidates[i]["policy"]["id"] for i in frontier),
               "selected": candidates[selected]["policy"]["id"] if selected is not None else None,
               "reason": reason}
    return selected, receipt


def dominates(candidate, incumbent, semantics="v1"):
    """v1: original rule (default). v2: unrecorded choices charged pessimistically."""
    require(semantics in {"v1", "v2"}, "unknown replay semantics")
    return dominates_v2(candidate, incumbent) if semantics == "v2" else dominates_v1(candidate, incumbent)


def rrsi_compare(candidate, incumbent, floor=None, semantics="v1"):
    """v1: original admission (default). v2: opt-in via cycle --replay-semantics v2."""
    require(semantics in {"v1", "v2"}, "unknown replay semantics")
    if semantics == "v2":
        return rrsi_compare_v2(candidate, incumbent, floor)
    return rrsi_compare_v1(candidate, incumbent, floor)


def select_candidate(candidates, runs, comparisons, development, rrsi, semantics="v1"):
    if semantics == "v2":
        return select_candidate_v2(candidates, runs, comparisons, development, rrsi)
    return select_candidate_v1(candidates, runs, comparisons, development, rrsi)


def requests(metrics):
    """Requests a policy would really have issued: revealed nodes plus unrecorded choices."""
    return metrics.get("requests", metrics["observed_nodes"] + metrics.get("unsupported_actions", 0))


def unobserved_share(metrics):
    calls = metrics.get("decision_calls") or 0
    return metrics.get("unobserved_share", metrics.get("unsupported_actions", 0) / calls if calls else 0.0)


def dominates_v2(candidate, incumbent, max_unobserved_share=MAX_UNOBSERVED_SHARE):
    """Fixed request-count objective; unknown seconds never become zero-cost savings.

    Quality and resolved coverage must not decline. At least one conclusion is
    required. This only qualifies a policy for a pilot, not a time/money claim.
    Unrecorded choices are scored pessimistically by replay(); a replay that is
    mostly unrecorded is not comparable at all.
    """
    strict = False
    for new, old in zip(candidate, incumbent):
        a, b = new["metrics"], old["metrics"]
        if any(m["best_quality"] is None or unobserved_share(m) > max_unobserved_share for m in (a, b)):
            return False
        if (a["best_quality"] < b["best_quality"] or
                a["resolved_questions"] < max(1, b["resolved_questions"]) or
                requests(a) > requests(b)):
            return False
        strict |= (a["best_quality"] > b["best_quality"] or requests(a) < requests(b))
    return strict


def policy_changes(parent, candidate):
    return [key for key in sorted(POLICY_KEYS - {"id"}) if parent[key] != candidate[key]]


def rrsi_plan(policy, worlds, development, history_path, branch):
    """Use prior development feedback only; heldout scores never drive proposals.

    This is a conservative project adaptation, not a reproduction of RRSI.
    Frozen request budgets, reflection cadence and economic graders are immutable.
    """
    previous = reference(history_path) if history_path else None
    cursor, history, seen = previous, [], set()
    while cursor:
        path = bound(cursor)
        require(str(path) not in seen and len(history) < 100, "invalid/overlong RRSI history chain")
        seen.add(str(path))
        report = read(path)
        contract = read(bound(report["contract"]))
        require(report["branch"] == branch and contract["branch"] == branch and
                contract["metric_contract"] == worlds[0]["metric_contract"], "RRSI history scope mismatch")
        require(cost_identity(contract) == cost_identity(worlds[0]), "RRSI history cost scope mismatch")
        item = report["rrsi"]
        require(item["schema"] in {"research_rrsi.v1", "research_rrsi.v2", "research_rrsi.v3"}, "unsupported RRSI history")
        if item["schema"] == "research_rrsi.v3":
            feedback = item["feedback"]
            bound(feedback["baseline"])
            for source in feedback["candidate_sources"]:
                bound(source["proposal"])
                bound(source["replay"])
        require(timestamp(report["at"]) <= timestamp(now()), "future RRSI history")
        history.append(item)
        cursor = item["previous_report"]
    history.reverse()
    require(all(item["round"] == i for i, item in enumerate(history, 1)), "broken RRSI round lineage")
    # Canonical content hashes make the same development inputs comparable across paths.
    inputs = {worlds[i]["id"]: digest(json.dumps(worlds[i], sort_keys=True).encode()) for i in development}
    comparable = [h for h in history if h["development_inputs"] == inputs]
    measured = [h for h in comparable if h["schema"] in {"research_rrsi.v2", "research_rrsi.v3"}]
    recent = history[-3:]
    stalled = len(recent) == 3 and all(h["schema"] in {"research_rrsi.v2", "research_rrsi.v3"} and h["development_inputs"] == inputs and
               h["development_summary"]["status"] == "no_gain" for h in recent)
    attempted = {field for h in comparable for p in h["proposals"] for field in p["changed_fields"]}
    evaluated = {field for h in measured for attempt in h["attempts"] if attempt["complete"]
                 for field in attempt["changed_fields"]}
    admitted = {field for h in measured for attempt in h["attempts"] if attempt["accepted"]
                for field in attempt["changed_fields"]}
    floor = {worlds[i]["id"]: None for i in development}
    for item in measured:
        for world_id, value in item["development_summary"]["admissible_best_quality"].items():
            if value is not None:
                require(score_number(value), "invalid historical quality")
                floor[world_id] = value if floor[world_id] is None else max(value, floor[world_id])
    prior_settings = [p["policy"] for h in comparable if not policy_changes(h["parent_policy"], policy)
                      for p in h["proposals"]]
    guidance = {field: {"hits": 0, "misses": 0, "regressions": 0} for field in ("priority", "max_depth")}
    for item in comparable:
        if item["schema"] != "research_rrsi.v3" or policy_changes(item["parent_policy"], policy):
            continue
        for check in item.get("feedback", {}).get("prediction_checks", []):
            require(check["component"] in guidance, "invalid historical feedback component")
            if not check["comparison_complete"] or check["outcome"] == "unknown":
                continue
            require(check["outcome"] in {"hit", "miss"}, "invalid historical prediction outcome")
            counts = guidance[check["component"]]
            counts["misses"] += check["outcome"] == "miss"
            counts["hits"] += check["outcome"] == "hit" and check["admitted"]
            counts["regressions"] += bool(check["regressions"])
    return {"schema": "research_rrsi.v3", "previous_report": previous, "round": len(history) + 1,
            "edit_budget": 1, "candidate_limit": 2,
            "mutable_fields": ["priority", "max_depth"], "noise_margin": 0,
            "stalled": stalled, "attempted_fields": sorted(attempted), "evaluated_fields": sorted(evaluated),
            "admitted_fields": sorted(admitted), "tried_fields": sorted(evaluated), "quality_floor": floor,
            "legacy_statistics_excluded": sum(h["schema"] == "research_rrsi.v1" for h in comparable),
            "development_inputs": inputs, "parent_policy": policy, "prior_settings": prior_settings,
            "cost_scope": cost_identity(worlds[0]), "feedback_guidance": guidance}


def diagnose_method(policy, feedback, plan):
    diagnoses = []
    for run in feedback:
        source = {"world": run["world"], "input_sha256": plan["development_inputs"][run["world"]]}
        for step in run.get("trace", []):
            if step.get("status") == "unsupported" and step["selected"]["anchor"] != "root":
                leaf = next((v for v in step["view"]["leaves"] if v["id"] == step["selected"]["anchor"]), None)
                if leaf and leaf["depth"] < policy["max_depth"]:
                    diagnoses.append({"failure": "unsupported_continuation", "component": "max_depth",
                        "value": max(1, leaf["depth"]), "evidence": {**source, "trace_step": step["step"]},
                        "prediction_metric": "unsupported_actions", "expected": "decrease"})
        observations = run.get("method_observations", [])
        failed = [o["node"] for o in observations if o["status"] == "implementation_error"]
        if failed and policy["priority"][0] != "repair":
            diagnoses.append({"failure": "observed_implementation_error", "component": "priority",
                "value": ["repair"] + [a for a in policy["priority"] if a != "repair"],
                "evidence": {**source, "nodes": failed}, "prediction_metric": "repair_actions", "expected": "increase"})
        unresolved = [o["question"] for o in observations if not o["resolved"]]
        repeated = sorted({q for q in unresolved if unresolved.count(q) >= 2})
        if repeated and policy["priority"].index("follow") < policy["priority"].index("explore"):
            diagnoses.append({"failure": "repeated_unresolved_question", "component": "priority",
                "value": ["repair", "explore", "follow"], "evidence": {**source, "questions": repeated},
                "prediction_metric": "unresolved_revisits", "expected": "decrease"})
    return diagnoses


def behavior_value(run, metric):
    if metric == "trace_choices":
        return [{"selected": t["selected"], "node": t.get("node"), "status": t["status"]} for t in run.get("trace", [])]
    if metric == "repair_actions":
        return sum(t["selected"]["action"] == "repair" for t in run.get("trace", []))
    if metric == "unresolved_revisits":
        questions = [o["question"] for o in run.get("method_observations", []) if not o["resolved"]]
        return len(questions) - len(set(questions))
    return run["metrics"].get(metric)


def method_prediction(proposal, feedback, plan):
    component = proposal["changed_fields"][0]
    diagnostic = proposal["reason"].get("diagnosis")
    metric, expected = "trace_choices", "change"
    if diagnostic:
        metric, expected = diagnostic["prediction_metric"], diagnostic["expected"]
    elif component == "max_depth":
        metric, expected = "decision_calls", "decrease"
    targets = [r for r in feedback if not diagnostic or r["world"] == diagnostic["evidence"]["world"]]
    return {"schema": "research_method_prediction.v1", "component": component, "metric": metric, "expected": expected,
            "targets": [{"world": r["world"], "input_sha256": plan["development_inputs"][r["world"]],
                         "before": behavior_value(r, metric)} for r in targets],
            "required_regression_questions": {r["world"]: r.get("method_regression_questions", []) for r in feedback},
            "scope": "observed_development_paths_only"}


def method_regressions(candidate, incumbent):
    checks = []
    for question in incumbent.get("method_regression_questions", []):
        prior = [o for o in incumbent.get("method_observations", []) if o["question"] == question and o["resolved"] and o["substantive"]]
        current = [o for o in candidate.get("method_observations", []) if o["question"] == question and o["resolved"] and o["substantive"]]
        passed = None if not prior else bool(current)
        qualities = [o["quality"] for o in prior if o["quality"] is not None]
        if passed and qualities:
            passed = any(o["quality"] is not None and o["quality"] >= max(qualities) for o in current)
        if passed and any(o.get("stage_passed") is True for o in prior):
            passed = any(o.get("stage_passed") is True for o in current)
        checks.append({"world": incumbent["world"], "question": question, "passed": passed})
    return checks


def review_prediction(proposal, candidate, incumbent, comparison, semantics="v1"):
    """Prediction accuracy is separate from admission; unknown costs never count as a saving."""
    prediction = proposal.get("prediction")
    if not prediction:
        return None  # Read-only compatibility for legacy/external test proposals.
    new = {r["world"]: r for r in candidate}
    old = {r["world"]: r for r in incumbent}
    outcomes, differences = [], []
    for target in prediction["targets"]:
        a, b = new[target["world"]], old[target["world"]]
        before, after = behavior_value(b, prediction["metric"]), behavior_value(a, prediction["metric"])
        require(before == target["before"], "prediction baseline changed")
        known = before is not None and after is not None
        if prediction["metric"] in {"trace_choices", "repair_actions", "unresolved_revisits"}:
            known &= "trace" in a and "trace" in b
        hit = None
        if known:
            if prediction["expected"] == "change":
                hit = after != before
            elif prediction["expected"] == "decrease":
                hit = after < before
            else:
                require(prediction["expected"] == "increase", "invalid prediction direction")
                hit = after > before
        outcomes.append(hit)
        differences.append({"world": target["world"], "before": before, "after": after, "hit": hit})
    regressions, cost_changes, regression_checks = [], [], []
    for world, b in old.items():
        a = new[world]
        am, bm = a["metrics"], b["metrics"]
        for metric, lower_is_better in (("best_quality", False), ("resolved_questions", False),
                ("stage_passed_questions", False), ("requests" if semantics == "v2" else "observed_nodes", True),
                ("implementation_errors", True)):
            av, bv = (requests(am), requests(bm)) if metric == "requests" else (am.get(metric), bm.get(metric))
            if av is not None and bv is not None and (av > bv if lower_is_better else av < bv):
                regressions.append(world + ":" + metric)
        checks = method_regressions(a, b)
        regression_checks.extend(checks)
        regressions.extend(world + ":required_question:" + c["question"] for c in checks if c["passed"] is False)
        key = "total_seconds" if bm.get("cost_scope", "recorded_node_total_seconds") == "recorded_node_total_seconds" else "all_in_method_cost_seconds"
        av, bv = am.get(key), bm.get(key)
        delta = av - bv if av is not None and bv is not None else None
        cost_changes.append({"world": world, "scope": bm.get("cost_scope", "recorded_node_total_seconds"),
                             "before": bv, "after": av, "delta": delta})
        if delta is not None and delta > 0:
            regressions.append(world + ":cost_growth")
    outcome = "unknown"
    if outcomes and all(v is not None for v in outcomes):
        outcome = "hit" if all(outcomes) else "miss"
    return {"component": prediction["component"], "outcome": outcome, "actual_behavior": differences,
            "comparison_complete": comparison["complete"], "admitted": comparison["accepted"],
            "regressions": regressions, "regression_checks": regression_checks, "cost_changes": cost_changes,
            "attribution": "whole_candidate_only", "scope": "development_only"}


def rrsi_propose(policy, feedback, plan):
    proposals = []
    for diagnosis in diagnose_method(policy, feedback, plan):
        variant = copy.deepcopy(policy)
        variant[diagnosis["component"]] = diagnosis["value"]
        proposals.append({"policy": variant, "reason": {"diagnosis": diagnosis}})
    proposals += propose(policy, feedback)
    for order in (["repair", "follow", "explore"], ["repair", "explore", "follow"],
                  ["explore", "repair", "follow"], ["explore", "follow", "repair"],
                  ["follow", "repair", "explore"], ["follow", "explore", "repair"]):
        variant = copy.deepcopy(policy)
        variant["priority"] = order
        proposals.append({"policy": variant, "reason": {"hypothesis": "compare an untried action order"}})
    pruning = None
    if policy["max_depth"] > 1:
        reduced = copy.deepcopy(policy)
        reduced["max_depth"] -= 1
        pruning = {"policy": reduced, "reason": {"hypothesis":
                   "test shallower continuation with the same evidence and correctness checks"}}
    unused = set(plan["mutable_fields"]) - set(plan["tried_fields"])
    # Reserve an existing slot, never an extra experiment or an unready hypothesis.
    if pruning:
        proposals.append(pruning)
    guidance = plan.get("feedback_guidance", {})
    def order(proposal):
        changes = policy_changes(policy, proposal["policy"])
        component = changes[0] if changes else "priority"
        counts = guidance.get(component, {"regressions": 0, "misses": 0, "hits": 0})
        return (counts["regressions"], counts["misses"], -counts["hits"])
    proposals.sort(key=order)
    if pruning and ((plan["stalled"] and "max_depth" in unused) or plan["round"] % 3 == 0):
        proposals.insert(0, pruning)
    variants = []
    previous = [policy] + plan["prior_settings"]
    for proposal in proposals:
        candidate = copy.deepcopy(proposal["policy"])
        changes = policy_changes(policy, candidate)
        if len(changes) > plan["edit_budget"]:
            # Late rounds test one component. No result is attributed to each bundled edit.
            candidate = copy.deepcopy(policy)
            candidate[changes[0]] = proposal["policy"][changes[0]]
            changes = policy_changes(policy, candidate)
        if not changes or any(not policy_changes(p, candidate) for p in previous):
            continue
        require(set(changes) <= set(plan["mutable_fields"]), "RRSI changed a frozen component")
        validate_policy(candidate)
        candidate["id"] = "rrsi_" + digest(json.dumps(
            {k: v for k, v in candidate.items() if k != "id"}, sort_keys=True).encode())[:12]
        variants.append({"policy": candidate, "reason": proposal["reason"], "changed_fields": changes,
                         "attribution": "whole_candidate_only", "pruning_candidate": changes == ["max_depth"]
                         and candidate["max_depth"] < policy["max_depth"]})
        variants[-1]["prediction"] = method_prediction(variants[-1], feedback, plan)
        previous.append(candidate)
        if len(variants) == plan["candidate_limit"]:
            break
    return variants


def comparable_cost(a, b, scope):
    """Pick the cost both arms actually measured; never mix scopes between the two arms.

    Returns (key, a_value, b_value) or (None, None, None) when neither scope is known
    for both. Unknown cost is reported, not converted to zero and not used as a saving.
    """
    keys = (["total_seconds", "measured_seconds"] if scope == "recorded_node_total_seconds"
            else ["all_in_method_cost_seconds"])
    for key in keys:
        if a.get(key) is not None and b.get(key) is not None:
            return key, a[key], b[key]
    return None, None, None


def rrsi_compare_v2(candidate, incumbent, floor=None, max_unobserved_share=MAX_UNOBSERVED_SHARE):
    """Require non-regression on the observed tree; novelty is not evidence.

    v2: an unrecorded choice is charged pessimistically by replay() instead of voiding
    the comparison; only a replay that is mostly unrecorded stays incomplete. Unknown
    cost is recorded in ``notes`` and blocks savings claims, but no longer blocks a
    bounded pilot offer. A pilot must freeze and then actually measure a cost cap.
    """
    require(bool(candidate) and len(candidate) == len(incumbent), "unpaired RRSI comparison")
    reasons, notes, strict, complete, cost_known = [], [], False, True, True
    for new, old in zip(candidate, incumbent):
        require(new["world"] == old["world"], "RRSI world mismatch")
        world_id, a, b = new["world"], new["metrics"], old["metrics"]
        stage = a.get("evaluation_stage", "economic")
        require(stage == b.get("evaluation_stage", "economic"), "RRSI evaluation stage mismatch")
        scope = a.get("cost_scope", "recorded_node_total_seconds")
        require(scope == b.get("cost_scope", "recorded_node_total_seconds"), "RRSI cost scope mismatch")
        for check in method_regressions(new, old):
            if check["passed"] is None:
                reasons.append(world_id + ":unknown_required_regression:" + check["question"])
                complete = False
            elif not check["passed"]:
                reasons.append(world_id + ":required_regression:" + check["question"])
        if any(unobserved_share(m) > max_unobserved_share for m in (a, b)):
            reasons.append(world_id + ":insufficient_observed_coverage")
            complete = False
            continue
        if stage == "economic" and any(m["best_quality"] is None for m in (a, b)):
            reasons.append(world_id + ":unknown_quality")
            complete = False
            continue
        if stage == "economic":
            minimum = b["best_quality"]
            if floor and floor.get(world_id) is not None:
                minimum = max(minimum, floor[world_id])
            if a["best_quality"] < minimum:
                reasons.append(world_id + ":quality_floor")
            strict |= a["best_quality"] > b["best_quality"]
        else:
            require(stage in {"information", "engineering"} and a["best_quality"] is None and b["best_quality"] is None,
                    "non-economic stage cannot carry economic quality")
            if a.get("stage_passed_questions") is None or b.get("stage_passed_questions") is None:
                reasons.append(world_id + ":unknown_stage_verification")
                complete = False
            elif a["stage_passed_questions"] < max(1, b["stage_passed_questions"]):
                reasons.append(world_id + ":stage_criteria_coverage")
            else:
                strict |= a["stage_passed_questions"] > b["stage_passed_questions"]
        if a["resolved_questions"] < max(1, b["resolved_questions"]):
            reasons.append(world_id + ":resolved_coverage")
        if requests(a) > requests(b):
            reasons.append(world_id + ":request_cost_increase")
        if a["implementation_errors"] > b["implementation_errors"]:
            reasons.append(world_id + ":more_implementation_errors")
        cost_key, a_cost, b_cost = comparable_cost(a, b, scope)
        if cost_key is None:
            cost_known = False
            notes.append(world_id + ":cost_unknown_not_blocking")
        else:
            if a_cost > b_cost:
                reasons.append(world_id + ":total_cost_increase:" + cost_key)
            strict |= a_cost < b_cost
        strict |= (a["resolved_questions"] > b["resolved_questions"] or requests(a) < requests(b))
    if not strict:
        reasons.append("no_strict_advantage")
    return {"accepted": not reasons, "complete": complete, "reasons": reasons,
            "cost_known": cost_known, "notes": notes}


def select_candidate_v2(candidates, runs, comparisons, development, rrsi):
    """Resolve the admissible Pareto set without using iteration order or heldout feedback."""
    eligible = [i for i, comparison in enumerate(comparisons) if comparison["accepted"]]
    def better(left, right):
        a, b = [runs[left][k] for k in development], [runs[right][k] for k in development]
        return rrsi_compare_v2(a, b)["accepted"] if rrsi else dominates_v2(a, b)
    frontier = [i for i in eligible if not any(better(j, i) for j in eligible if i != j)]
    # Equivalent measurements use canonical policy content; incomparable tradeoffs keep the parent.
    def signature(index):
        values = []
        for k in development:
            m = runs[index][k]["metrics"]
            if not rrsi:
                values.append((m["best_quality"], m["resolved_questions"], requests(m)))
                continue
            cost_keys = (("total_seconds", "measured_seconds") if m.get("cost_scope", "recorded_node_total_seconds") ==
                         "recorded_node_total_seconds" else ("all_in_method_cost_seconds",))
            values.append((m["best_quality"], m.get("stage_passed_questions"), m["resolved_questions"],
                           requests(m), m["implementation_errors"], *(m.get(k) for k in cost_keys)))
        return values
    selected = None
    if frontier and all(signature(i) == signature(frontier[0]) for i in frontier):
        selected = min(frontier, key=lambda i: json.dumps(
            {k: v for k, v in candidates[i]["policy"].items() if k != "id"}, sort_keys=True))
    reason = "no_admissible_candidate"
    if selected is not None:
        reason = "admissible_choice"
    elif frontier:
        reason = "incomparable_tradeoff"
    receipt = {"rule": "unique_pareto_or_equivalent_v1", "baseline": "frozen_incumbent",
               "admissible": sorted(candidates[i]["policy"]["id"] for i in eligible),
               "pareto_frontier": sorted(candidates[i]["policy"]["id"] for i in frontier),
               "selected": candidates[selected]["policy"]["id"] if selected is not None else None,
               "reason": reason}
    return selected, receipt


def cycle(world_paths, incumbent_path, output, branch, rrsi=False, history_path=None, replay_semantics="v1"):
    started = time.perf_counter()
    require(branch in BRANCHES, "branch required")
    require(replay_semantics in {"v1", "v2"}, "replay semantics must be v1 or v2")
    v2 = replay_semantics == "v2"
    worlds = [validate_world(read(p)) for p in world_paths]
    require(bool(worlds) and len({w["id"] for w in worlds}) == len(worlds), "worlds missing/duplicated")
    require(all(w["branch"] == branch for w in worlds), "cannot mix research branches")
    require(len({w["metric_contract"] for w in worlds}) == 1, "cannot mix evaluators")
    require(len({cost_identity(w) for w in worlds}) == 1, "cannot mix cost contracts")
    require(rrsi or evaluation_stage(worlds[0]) == "economic", "staged method evaluation requires --rrsi")
    policy = read(incumbent_path) if incumbent_path else initial_policy()
    validate_policy(policy)
    development = [i for i, w in enumerate(worlds) if w["usage"] == "development"]
    require(bool(development), "development worlds required")
    require(rrsi or history_path is None, "RRSI history requires --rrsi")
    regularization = rrsi_plan(policy, worlds, development, history_path, branch) if rrsi else None
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve()
    engine_copy = output / "ENGINE.py"
    engine_copy.write_bytes(source.read_bytes())
    contract = {"schema": SCHEMA, "at": now(), "branch": branch,
                "worlds": [reference(p) for p in world_paths], "engine": reference(engine_copy),
                "metric_contract": worlds[0]["metric_contract"],
                "incumbent": policy, "candidate_limit": 2, "parallel_workers": 1,
                "selection": "unique_pareto_or_equivalent_v1; frozen incumbent; quality, coverage and requests",
                "cost_unit": "recorded_generation_evaluation_requests_not_tokens_or_seconds",
                "model_revision_cost_seconds": None, "new_experiments": 0,
                "independent_validation": False}
    if v2:
        contract.update(replay_semantics=REPLAY_SEMANTICS, max_unobserved_share=MAX_UNOBSERVED_SHARE,
                        pilot_offer_rule="development strict advantage + clean heldout non-regression; cost may be unknown")
    if rrsi:
        contract.update(rrsi=regularization,
                        selection="unique_pareto_or_equivalent_v1; each candidate against frozen incumbent; incomparable tradeoffs retain incumbent",
                        cost_unit=cost_identity(worlds[0]))
    for key in ("evaluation_contract", "cost_contract"):
        if worlds[0].get(key):
            contract[key] = worlds[0][key]
    write(output / "CONTRACT.json", contract)
    base = [replay(w, policy, semantics=replay_semantics) for w in worlds]
    write(output / "INCUMBENT.json", {"policy": policy, "replays": base})
    feedback = [base[i] for i in development]
    candidates = rrsi_propose(policy, feedback, regularization) if rrsi else propose(policy, feedback)
    winner, winning_results = policy, base
    results, candidate_runs, comparisons, prediction_checks = [], [], [], []
    for i, proposal in enumerate(candidates, 1):
        write(output / f"CANDIDATE_{i}.json", proposal)  # frozen before execution
        runs = [replay(w, proposal["policy"], semantics=replay_semantics) for w in worlds]
        write(output / f"REPLAY_{i}.json", runs)
        new_dev, old_dev = [runs[j] for j in development], [base[j] for j in development]
        comparison = (rrsi_compare(new_dev, old_dev, regularization["quality_floor"], replay_semantics) if rrsi else
                      {"accepted": dominates(new_dev, old_dev, replay_semantics)})
        results.append({"id": proposal["policy"]["id"], "dominates_incumbent": dominates(runs, base, replay_semantics),
                        "metrics": [r["metrics"] for r in runs]})
        if rrsi:
            results[-1].update(development_comparison=comparison, proposal=reference(output / f"CANDIDATE_{i}.json"))
            prediction_check = review_prediction(proposal, new_dev, old_dev, comparison, replay_semantics)
            if prediction_check:
                prediction_checks.append(prediction_check)
                results[-1]["prediction_check"] = prediction_check
        candidate_runs.append(runs)
        comparisons.append(comparison)
    selected, selection = select_candidate(candidates, candidate_runs, comparisons, development, rrsi, replay_semantics)
    if selected is not None:
        winner, winning_results = candidates[selected]["policy"], candidate_runs[selected]
    heldout = [i for i, w in enumerate(worlds) if w["usage"] == "heldout"]
    if v2:
        # v1 requires every world, development included, to be prospective with no limitation;
        # exported worlds carry a limitation whenever any decision was overridden, so the offer
        # is unreachable. Development worlds only generate and rank proposals; the clean,
        # untouched evidence that matters is the heldout set, so v2 requires only heldout clean.
        clean = bool(heldout) and all(worlds[j]["admission"] == "prospective" and not worlds[j]["limitations"]
                                      for j in heldout)
    else:
        clean = all(w["admission"] == "prospective" and not w["limitations"] for w in worlds)
    heldout_comparison = None
    if rrsi and heldout:
        heldout_comparison = rrsi_compare([winning_results[j] for j in heldout], [base[j] for j in heldout],
                                          semantics=replay_semantics)
    if rrsi and v2:
        # Heldout confirms non-regression; the strict advantage was required on development.
        heldout_pass = bool(heldout_comparison and heldout_comparison["complete"] and
                            set(heldout_comparison["reasons"]) <= {"no_strict_advantage"})
    elif rrsi:
        heldout_pass = bool(heldout_comparison and heldout_comparison["accepted"])
    else:
        heldout_pass = bool(heldout) and dominates([winning_results[j] for j in heldout], [base[j] for j in heldout],
                                                   replay_semantics)
    # Historical replay alone never changes a live batch. Only produce a pilot offer.
    eligible = winner != policy and clean and heldout_pass
    for ref in contract["worlds"]:
        bound(ref)
    report = {"schema": SCHEMA, "at": now(), "branch": branch,
              "contract": reference(output / "CONTRACT.json"),
              "incumbent": policy, "replay_best": winner,
              "next_policy": winner if eligible else policy,
              "decision": "eligible_for_bounded_pilot" if eligible else "retain_incumbent",
              "reason": "validated replay advantage" if eligible else
                        "no admissible advantage: inspect diagnostics, unknown cost, coverage and heldout evidence",
              "baseline_metrics": [r["metrics"] for r in base], "candidates": results,
              "engine_seconds": time.perf_counter() - started,
              "all_in_evolution_cost_seconds": None,
              "evaluation_stage": evaluation_stage(worlds[0]), "economic_promotion": False,
              "selection": selection,
              "limitations": sorted({s for w in worlds for s in w["limitations"]}),
              "live_batch_changed": False, "efficiency_improvement_claimed": False,
              "online_validation": False}
    if rrsi:
        observed_quality, admissible_quality = {}, {}
        base_check = rrsi_compare(feedback, feedback, semantics=replay_semantics)
        base_admissible = base_check["complete"] and base_check["reasons"] == ["no_strict_advantage"]
        for j in development:
            measurements = [base[j]["metrics"]] + [r["metrics"][j] for r in results]
            known = [m["best_quality"] for m in measurements if m["best_quality"] is not None and
                     (unobserved_share(m) <= MAX_UNOBSERVED_SHARE if v2 else not m["unsupported_actions"]) and
                     m["resolved_questions"] > 0]
            observed_quality[worlds[j]["id"]] = max(known) if known else None
            admitted = ([base[j]["metrics"]] if base_admissible else []) + [
                r["metrics"][j] for r in results if r["development_comparison"]["accepted"]]
            scores = [m["best_quality"] for m in admitted if m["best_quality"] is not None]
            admissible_quality[worlds[j]["id"]] = max(scores) if scores else None
        status = "gain" if winner != policy else "no_gain"
        if selection["reason"] == "incomparable_tradeoff":
            status = "tradeoff"
        if not candidates:
            status = "exhausted"
        elif status == "no_gain" and any(not r["development_comparison"]["complete"] for r in results):
            status = "unknown"
        report["rrsi"] = {**regularization, "proposals": candidates,
                          "feedback": {"schema": "research_method_feedback.v1",
                              "baseline": reference(output / "INCUMBENT.json"),
                              "diagnoses": diagnose_method(policy, feedback, regularization),
                              "candidate_sources": [{"proposal": reference(output / f"CANDIDATE_{i}.json"),
                                                     "replay": reference(output / f"REPLAY_{i}.json")} for i in range(1, len(candidates) + 1)],
                              "prediction_checks": prediction_checks,
                              "next_proposal_guidance": "same development inputs and parent only; measured misses/regressions lower component priority; unknown is neutral",
                              "unsupported_components": ["evidence_retrieval", "context_loading", "verification_reuse"]},
                          "attempts": [{"policy": p["policy"], "changed_fields": p["changed_fields"],
                                        "complete": c["complete"], "accepted": c["accepted"]}
                                       for p, c in zip(candidates, comparisons)],
                          "development_summary": {"status": status, "best_quality": admissible_quality,
                                                  "observed_best_quality": observed_quality,
                                                  "admissible_best_quality": admissible_quality},
                          "selection": selection,
                          "heldout_comparison": heldout_comparison,
                          "candidate_space_exhausted": len(candidates) < 2,
                          "all_in_method_cost_seconds": None,
                          "method_cost_by_world": {r["world"]: r["metrics"]["all_in_method_cost_seconds"] for r in winning_results},
                          "adaptation": "zero quality tolerance; no cost growth; novelty only proposes, never admits"}
        if v2:
            report["rrsi"].update(
                adaptation="zero quality tolerance; no known cost growth; unrecorded choices charged "
                           "pessimistically; unknown cost flagged, not admitted as saving; novelty only proposes, never admits",
                cost_unknown_candidates=sum(not c.get("cost_known", True) for c in comparisons))
        if regularization["previous_report"]:
            bound(regularization["previous_report"])
    write(output / "REPORT.json", report)
    write(output / "NEXT_POLICY.json", report["next_policy"])
    return report


@contextmanager
def lock(directory):
    path = Path(directory) / ".scheduler.lock"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(now())
    try:
        yield
    finally:
        path.unlink()


def harness_runtime(ref):
    path = bound(ref)
    spec = importlib.util.spec_from_file_location("research_harness_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def harness_api():
    return SimpleNamespace(**globals())


def harness_init(spec_path, output):
    runtime = Path(__file__).with_name("research_harness.py")
    if not runtime.exists():
        frozen = read(Path(__file__).with_name("CONTRACT.json"))
        runtime = bound(frozen["runtime"])
    return harness_runtime(reference(runtime)).initialize(harness_api(), spec_path, output)


def harness_run(directory, request):
    cfg = read(Path(directory) / "CONTRACT.json")
    if not isinstance(request, dict):
        request = read(request)
    return harness_runtime(cfg["runtime"]).dispatch(harness_api(), directory, request)


def check_harness_deployment(deployment, boundary):
    cfg = read(bound(deployment["contract"]))
    return harness_runtime(cfg["runtime"]).validate_deployment(harness_api(), deployment, boundary)


def batch(directory):
    obj = read(Path(directory) / "BATCH.json")
    require(digest(Path(__file__).read_bytes()) == obj["engine_sha256"],
            "engine changed: use the frozen cycle ENGINE.py")
    validate_policy(obj["policy"])
    timestamp(obj["at"])
    # Every field must still be what the bound report and boundary produce: a budget,
    # policy or evaluator edited into BATCH.json is not a new boundary.
    changed = drift(obj, frozen_batch(bound(obj["cycle_report"]), bound(obj["boundary"]), directory))
    require(not changed, "BATCH.json disagrees with its bound cycle report and boundary: " + ", ".join(changed))
    if obj.get("walkforward_runtime"):
        bound(obj["walkforward_runtime"])
    return obj


def start_batch(report_path, boundary_path, output):
    obj = frozen_batch(report_path, boundary_path, output)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    if obj.get("walkforward_runtime"):
        # Freeze the scorer with the contract so observe() can recompute every verdict.
        (output / "WALKFORWARD.py").write_bytes(bound(read(boundary_path)["walkforward_runtime"]).read_bytes())
    write(output / "BATCH.json", obj)
    return obj


def frozen_batch(report_path, boundary_path, output):
    """BATCH.json content implied by a cycle report and a frozen boundary. Writes nothing."""
    report = read(report_path)
    contract = read(bound(report["contract"]))
    require(digest(Path(__file__).read_bytes()) == contract["engine"]["sha256"], "engine version mismatch")
    boundary = read(boundary_path)
    require(boundary["branch"] == report["branch"], "boundary branch mismatch")
    require(boundary["status"] in {"previous_batch_completed", "first_scheduler_batch"}, "batch still running")
    require(boundary["affects_running_experiments"] is False, "cannot replace a running batch")
    require(bool(boundary["evidence"]), "boundary evidence required")
    for ref in boundary["evidence"]:
        bound(ref)
    quality_contract(boundary["quality_contract"])
    stage = evaluation_stage(boundary)
    if boundary.get("cost_contract"):
        cost_contract(boundary["cost_contract"])
    metric_id = metric_identity(report["branch"], boundary["quality_contract"], boundary.get("evaluation_contract"))
    require(stage == "economic" or not boundary.get("initial_grade_evidence"), "stage boundary cannot import economic quality")
    require(report["decision"] in {"retain_incumbent", "eligible_for_bounded_pilot"}, "bad cycle decision")
    pilot_criteria = boundary.get("pilot_criteria")
    frontier_pilot = boundary.get("frontier_pilot")
    if frontier_pilot:
        require(report["decision"] == "retain_incumbent", "one method pilot at a time")
        fields = {"rule", "max_decisions", "initial_frontier", "recent_decisions"}
        if frontier_pilot["rule"] == "bottleneck_evidence_cost_v1":
            fields.add("selection_contract")
        require(set(frontier_pilot) == fields,
                "invalid frontier pilot fields")
        frontier_selector({"frontier_pilot": frontier_pilot, "branch": report["branch"]})
        require(type(frontier_pilot["max_decisions"]) is int and
                1 <= frontier_pilot["max_decisions"] <= min(6, report["next_policy"]["max_attempts"]),
                "invalid frontier pilot budget")
        require(isinstance(frontier_pilot["recent_decisions"], list) and
                len(frontier_pilot["recent_decisions"]) <= 6, "keep at most six recent decision references")
        for ref in frontier_pilot["recent_decisions"]:
            decision = read(bound(ref))
            require(decision["branch"] == report["branch"] and timestamp(decision["at"]) <= timestamp(now()),
                    "invalid prior frontier decision")
        validate_frontier(read(bound(frontier_pilot["initial_frontier"])), report["branch"])
    if report["decision"] == "eligible_for_bounded_pilot":
        require(metric_id == contract["metric_contract"], "cannot change evaluator at pilot deployment")
        require(cost_identity(boundary) == cost_identity(contract), "cannot change cost contract at pilot deployment")
        require(isinstance(pilot_criteria, dict), "pilot criteria must be frozen before deployment")
        require(type(pilot_criteria.get("min_resolved_questions")) is int and
                pilot_criteria["min_resolved_questions"] >= 1 and
                type(pilot_criteria.get("max_requests")) is int and
                1 <= pilot_criteria["max_requests"] <= report["next_policy"]["max_attempts"], "invalid pilot criteria")
        if stage == "economic":
            require(pilot_criteria.get("min_quality") is not None and score_number(pilot_criteria["min_quality"]),
                    "economic pilot needs a frozen minimum quality")
        else:
            require(pilot_criteria.get("min_quality") is None and
                    type(pilot_criteria.get("min_stage_passed_questions")) is int and
                    pilot_criteria["min_stage_passed_questions"] >= 1, "stage pilot needs frozen verified criteria coverage")
        if report.get("rrsi"):
            cap_key = "max_all_in_method_cost_seconds" if boundary.get("cost_contract") else "max_total_seconds"
            require(number(pilot_criteria.get(cap_key)), "RRSI pilot needs a frozen node total-cost cap: " + cap_key)
    harness = None
    if boundary.get("harness_deployment"):
        require(report["decision"] == "retain_incumbent" and report["next_policy"] == report["incumbent"],
                "keep the scheduler policy fixed during a harness trial")
        harness = check_harness_deployment(boundary["harness_deployment"], boundary)
    walkforward = boundary.get("walkforward_contract")
    if walkforward is not None:
        require(read(bound(walkforward)).get("schema") == "research_walkforward.v1", "invalid walk-forward contract")
        # The scorer is bound in the boundary too, so a recomputed BATCH.json names the same bytes.
        require(isinstance(boundary.get("walkforward_runtime"), dict), "bind walkforward_runtime in the boundary")
        bound(boundary["walkforward_runtime"])
    branching = boundary.get("branching")
    if branching is not None:
        # Parallel siblings give replay more than one recorded child per decision point.
        require(isinstance(branching, dict) and set(branching) == {"siblings"} and
                type(branching["siblings"]) is int and 1 <= branching["siblings"] <= MAX_SIBLINGS,
                "branching needs 1..%d siblings" % MAX_SIBLINGS)
    obj = {"schema": SCHEMA, "at": now(), "branch": report["branch"],
           "cycle_report": reference(report_path), "boundary": reference(boundary_path),
           "engine_sha256": contract["engine"]["sha256"], "policy": report["next_policy"],
           "fallback_policy": report["incumbent"], "pilot_criteria": pilot_criteria,
           "quality_contract": boundary["quality_contract"], "metric_contract": metric_id,
           "initial_quality": grade(boundary["quality_contract"], boundary["initial_grade_evidence"])
                              if boundary.get("initial_grade_evidence") else None,
           "mode": "pilot" if report["decision"] == "eligible_for_bounded_pilot" else "initial_executable_rule",
           "execution_authorized_by_this_file": False}
    for key in ("evaluation_contract", "cost_contract"):
        if boundary.get(key):
            obj[key] = boundary[key]
    if report.get("rrsi"):
        obj["rrsi_enabled"] = True
    if frontier_pilot:
        obj.update(mode="frontier_pilot", frontier_pilot=frontier_pilot)
    if harness:
        obj["harness_deployment"] = harness
    if branching is not None:
        obj["branching"] = branching
    if walkforward is not None:
        obj["walkforward_contract"] = walkforward
        obj["walkforward_runtime"] = {"path": str(Path(output).resolve() / "WALKFORWARD.py"),
                                      "sha256": boundary["walkforward_runtime"]["sha256"]}
    return obj


def walkforward_decision(config, result):
    """Walk-forward decision for this economic result, recomputed from its own equity files.

    A verdict file is not trusted by its "decision" field: it must name candidate and parent
    equity files that are themselves listed as this result's evidence, and the frozen scorer
    must reproduce the same decision and fold wins from those exact bytes.
    """
    if not config.get("walkforward_contract"):
        require(not result.get("walkforward"), "walk-forward verdict requires a boundary-frozen contract")
        return None
    require(bool(result.get("walkforward")), "this batch froze a walk-forward contract; bind its verdict")
    verdict = read(bound(result["walkforward"]))
    contract = read(bound(config["walkforward_contract"]))
    require(verdict.get("schema") == "research_walkforward_verdict.v1" and verdict.get("contract") == contract,
            "verdict uses another contract")
    require(verdict.get("decision") in {"pass", "fail", "insufficient_data"}, "invalid walk-forward decision")
    require(all(isinstance(verdict.get(k), dict) for k in ("candidate", "parent")),
            "verdict must name its candidate and parent equity files")
    candidate, parent = bound(verdict["candidate"]), bound(verdict["parent"])
    require(verdict["candidate"] != verdict["parent"], "candidate and parent equity must differ")
    require(verdict["candidate"] in result["evidence"] and verdict["parent"] in result["evidence"],
            "list both equity files in this result's evidence; a verdict from another experiment cannot be reused")
    expected = harness_runtime(config["walkforward_runtime"]).evaluate(contract, candidate, parent)
    require(expected["decision"] == verdict["decision"] and expected["fold_wins"] == verdict["fold_wins"],
            "verdict does not match recomputation from its equity files")
    return expected["decision"]


def ledger(directory):
    """Observed nodes and the first pending decision (single-decision callers)."""
    nodes, pending = ledger_all(directory)
    return nodes, (pending[0] if pending else None)


def ledger_all(directory):
    """Observed nodes in decision order and every pending decision of the open sibling group.

    Node ids follow decision ids (D0005 -> n0005). With one decision at a time this is
    identical to v1 numbering; with siblings, outcomes may arrive in any order.
    """
    directory = Path(directory)
    config = read(directory / "BATCH.json")
    decisions = sorted(directory.glob("D*.json"))
    nodes, pending, issued = [], [], {}
    for path in decisions:
        decision = read(path)
        require(bound(decision["batch"]) == (directory / "BATCH.json").resolve(), "wrong batch binding")
        bound(decision["context"])
        if decision.get("frontier"):
            validate_frontier(read(bound(decision["frontier"])), decision["branch"])
            bound(decision["selected"]["spec"])
        # Records are recomputed from their bound inputs before they steer anything:
        # an edited selection or outcome (even with re-hashed references) is rejected.
        group = decision.get("sibling", {"group": decision["id"], "index": 1})
        first = int(group["group"][1:])
        visible = [n for n in nodes if int(n["id"][1:]) < first]
        require(len(visible) == first - 1, path.name + " was issued while an earlier decision was pending")
        require(all(timestamp(n["available_at"]) <= timestamp(decision["at"]) for n in visible),
                "decision predates a visible result")
        if group["group"] not in issued:
            issued[group["group"]] = decision_records(directory, config, visible, bound(decision["context"]))
        expected = issued[group["group"]]
        if isinstance(expected, list):
            expected = expected[group["index"] - 1] if group["index"] <= len(expected) else {}
        changed = drift(decision, expected)
        require(not changed, path.name + " disagrees with the state and context it was issued from: " + ", ".join(changed))
        outcome_path = directory / ("O" + path.name[1:])
        if not outcome_path.exists():
            pending.append(decision)
            continue
        outcome = read(outcome_path)
        require(bound(outcome["decision"]) == path.resolve(), "decision binding mismatch")
        bound(outcome["input"])
        if outcome.get("grade_evidence"):
            bound(outcome["grade_evidence"])
        for ref in outcome["evidence"]:
            bound(ref)
        require(timestamp(outcome["at"]) >= timestamp(decision["at"]), "outcome predates its decision")
        changed = drift(outcome, outcome_record(directory, config, decision, bound(outcome["input"])))
        require(not changed, outcome_path.name + " disagrees with its bound result: " + ", ".join(changed))
        nodes.append({"id": outcome["node"], "parent": decision["selected"]["anchor"],
                      "depends_on": decision["visible_nodes"], "decision_at": decision["at"],
                      "available_at": outcome["at"], "observation": outcome["observation"],
                      "measured_seconds": outcome["measured_seconds"], "total_seconds": outcome["total_seconds"],
                      "evidence": [reference(path), reference(outcome_path)] + outcome["evidence"] +
                                  ([outcome["grade_evidence"]] if outcome.get("grade_evidence") else [])})
        for key in ("stage_evidence", "cost_receipt"):
            if outcome.get(key):
                nodes[-1][key] = outcome[key]
        if config.get("evaluation_contract"):
            require(decision["question"] == outcome["observation"]["question"], "decision stage question mismatch")
            evaluate_stage(config["evaluation_contract"], outcome.get("stage_evidence"), outcome["observation"])
    node_costs(config, nodes)
    return nodes, pending


def has_override(nodes):
    return any(not read(n["evidence"][1]["path"]).get("decision_followed", True) for n in nodes)


def reflection_due(config, nodes, action):
    result = {"status": "reflection_due", "action": action}
    if config.get("harness_deployment"):
        result["harness_reflection"] = {
            "deployment": config["harness_deployment"],
            "evidence": [ref for node in nodes for ref in node["evidence"]],
            "action": "close this batch; use its evidence in harness-init at the next unfrozen boundary"}
    return result


def next_decision(directory, context_path):
    directory = Path(directory)
    with lock(directory):
        config = batch(directory)
        if (directory / "EXPORT.json").exists() or (directory / "PILOT_REVIEW.json").exists():
            return {"status": "batch_closed", "action": "use the recorded next policy in a new boundary-frozen batch"}
        nodes, pending = ledger_all(directory)
        if pending:
            waiting = {"status": "pending_result", "decision": pending[0]["id"], "resubmit": False}
            if len(pending) > 1:
                waiting["decisions"] = [d["id"] for d in pending]
            return waiting
        records = decision_records(directory, config, nodes, context_path)
        if isinstance(records, dict):
            return records
        for obj in records:
            write(directory / (obj["id"] + ".json"), obj)
        if len(records) == 1:
            return records[0]
        return {"status": "sibling_decisions", "decisions": records,
                "action": "run every sibling under existing authorization; observe each result by its own decision id"}


def decision_records(directory, config, nodes, context_path):
    """Decisions (or a stop status) implied by observed nodes and a context. Writes nothing."""
    if has_override(nodes):
        return reflection_due(config, nodes, "override recorded; export diagnostic and re-evaluate")
    completed = {n["observation"]["question"] for n in nodes if
                 n["observation"]["substantive"] and n["observation"]["resolved"]}
    if len(completed) >= config["policy"]["reflection_every"]:
        return reflection_due(config, nodes, "export then cycle; freeze next batch at boundary")
    if config["mode"] == "pilot" and len(nodes) >= config["pilot_criteria"]["max_requests"]:
        return reflection_due(config, nodes, "pilot request budget reached; review-pilot before continuing")
    if config["mode"] == "frontier_pilot" and len(nodes) >= config["frontier_pilot"]["max_decisions"]:
        return reflection_due(config, nodes, "frontier budget reached; review-pilot then export")
    context = read(context_path)
    require(context["branch"] == config["branch"], "context branch mismatch")
    require(context["context_complete"] is True and context["evidence"], "visible context required")
    for ref in context["evidence"]:
        bound(ref)
    if config.get("evaluation_contract") and config["mode"] != "frontier_pilot":
        require(context.get("question") in stage_contract(config["evaluation_contract"])["questions"],
                "context needs a boundary-frozen stage question")
    view = public_view(nodes, set(), len(nodes), config["policy"]["max_attempts"])
    width = sibling_width(config, nodes, view)
    frontier_ref, comparison = None, None
    if config["mode"] == "frontier_pilot":
        frontier_ref = context.get("frontier", config["frontier_pilot"]["initial_frontier"])
        if config.get("harness_deployment"):
            require(frontier_ref == config["frontier_pilot"]["initial_frontier"],
                    "harness batch must use its frozen Agent-generated frontier")
        selections, comparison = choose_frontier_siblings(read(bound(frontier_ref)), config, nodes, width)
    else:
        require("frontier" not in context, "frontier requires a boundary-frozen frontier pilot")
        selected = choose(view, config["policy"])
        selections = [selected] * width if selected else []
    if not selections:
        if comparison is not None:
            return {"status": "frontier_exhausted", "comparison": comparison,
                    "action": "prepare independent hypotheses or close this bounded pilot; do not duplicate a decision"}
        return reflection_due(config, nodes, "policy stopped or decision budget exhausted")
    first = len(nodes) + 1
    records = []
    for offset, selected in enumerate(selections):
        index = first + offset
        obj = {"schema": SCHEMA, "at": now(), "branch": config["branch"], "id": f"D{index:04d}",
               "batch": reference(directory / "BATCH.json"), "context": reference(context_path),
               "policy_id": config["policy"]["id"], "view": view,
               "visible_nodes": [n["id"] for n in nodes], "selected": copy.deepcopy(selected),
               "instruction": "Generate one authorized research action from this anchor; bind this receipt in its frozen protocol. Log any override; do not silently ignore it."}
        if frontier_ref:
            obj.update(frontier=frontier_ref, comparison=comparison,
                       instruction="Execute only the selected candidate specification under existing authorization. Bind this decision and candidate hash. Preserve all other hypotheses; preparation is independent of pending execution.")
        if len(selections) > 1:
            obj["sibling"] = {"group": f"D{first:04d}", "index": offset + 1, "size": len(selections)}
            obj["instruction"] += (f" This is sibling {offset + 1} of {len(selections)} from the same visible state: "
                                   "each sibling must test a different mechanism, not a parameter variant of another.")
        if config.get("evaluation_contract"):
            obj["question"] = selected["question"] if frontier_ref else context["question"]
        if config.get("harness_deployment"):
            obj["harness"] = config["harness_deployment"]
        records.append(obj)
    return records


def sibling_width(config, nodes, view):
    """Decisions to open from one visible state, bounded by every frozen request budget."""
    width = config.get("branching", {}).get("siblings", 1)
    width = min(width, view["remaining"])
    if config["mode"] == "pilot":
        width = min(width, config["pilot_criteria"]["max_requests"] - len(nodes))
    if config["mode"] == "frontier_pilot":
        width = min(width, config["frontier_pilot"]["max_decisions"] - len(nodes))
    return max(1, width)


def choose_frontier_siblings(frontier, config, nodes, limit):
    """Top-ranked eligible candidates from distinct hypotheses; no extra budget is created."""
    selected, comparison = choose_frontier(frontier, config, nodes)
    if selected is None:
        return [], comparison
    picks = [selected]
    by_id = {candidate["id"]: candidate for candidate in frontier["candidates"]}
    for row in comparison["ranking"]:
        if len(picks) >= limit:
            break
        candidate = by_id[row["candidate_id"]]
        if any(p["candidate_id"] == candidate["id"] or p["hypothesis_id"] == candidate["hypothesis_id"] for p in picks):
            continue
        picks.append(frontier_selection(candidate))
    return picks, comparison


def observe(directory, result_path):
    directory = Path(directory)
    with lock(directory):
        config = batch(directory)
        nodes, pendings = ledger_all(directory)
        require(bool(pendings), "no pending decision")
        pending = next((d for d in pendings if d["id"] == read(result_path).get("decision_id")), None)
        require(pending is not None, "result identity mismatch")
        obj = outcome_record(directory, config, pending, result_path)
        cost_node = {"id": obj["node"], "cost_receipt": obj.get("cost_receipt"), "evidence": [obj["decision"]]}
        node_costs(config, nodes + [cost_node])
        write(directory / ("O" + pending["id"][1:] + ".json"), obj)
        return obj


def outcome_record(directory, config, pending, result_path):
    """The O record implied by a pending decision and its result file. Writes nothing."""
    result = read(result_path)
    require(result["branch"] == config["branch"] and result["decision_id"] == pending["id"], "result identity mismatch")
    index = int(pending["id"][1:])
    require(type(result["decision_followed"]) is bool, "decision_followed required")
    if not result["decision_followed"]:
        require(bool(result.get("override_reason")), "override reason required")
    observation = copy.deepcopy(result["observation"])
    validate_observation(observation)
    if pending.get("frontier"):
        require(result.get("candidate_id") == pending["selected"]["candidate_id"] and
                observation["question"] == pending["selected"]["question"], "candidate result identity mismatch")
    economic = observation["status"] in {"valid_improvement", "valid_negative"}
    computed_quality = None
    account_scorecard = None
    validated = walkforward_decision(config, result) if economic else None
    if economic:
        if quality_contract(config["quality_contract"])["schema"] == "research_quality.v2":
            account_scorecard = review_account(config["quality_contract"], result["grade_evidence"])
            computed_quality = account_scorecard["quality"]
            improved = account_scorecard["decision"] in {"return_improved", "risk_repair_tradeoff"}
            # With a frozen walk-forward contract, a development-window gain alone is not an improvement.
            if validated is not None:
                improved = improved and validated == "pass"
            expected = "valid_improvement" if improved else "valid_negative"
            require(observation["status"] == expected, "outcome status disagrees with account scorecard / walk-forward")
        else:
            computed_quality = grade(config["quality_contract"], result["grade_evidence"])
            require(observation["status"] != "valid_improvement" or validated in {None, "pass"},
                    "valid_improvement requires a walk-forward pass in this batch")
    require(observation["quality"] is None or observation["quality"] == computed_quality,
            "manual quality disagrees with frozen evaluator")
    observation["quality"] = computed_quality
    stage_review = None
    if config.get("evaluation_contract"):
        require(observation["question"] == pending["question"], "decision stage question mismatch")
        stage_review = evaluate_stage(config["evaluation_contract"], result.get("stage_evidence"), observation)
    else:
        require(not result.get("stage_evidence"), "stage evidence requires a frozen evaluation contract")
    require(number(result["measured_seconds"], True) and number(result["total_seconds"], True), "invalid cost")
    if result["total_seconds"] is not None and result["measured_seconds"] is not None:
        require(result["total_seconds"] >= result["measured_seconds"], "total below measured cost")
    require(result["evidence"], "result evidence required")
    for ref in result["evidence"]:
        bound(ref)
    require(not result.get("cost_receipt") or config.get("cost_contract"), "cost receipt requires a frozen contract")
    obj = {"schema": SCHEMA, "at": now(), "branch": config["branch"], "node": f"n{index:04d}",
           "decision": reference(directory / (pending["id"] + ".json")),
           "input": reference(result_path), "observation": observation,
           "grade_evidence": result.get("grade_evidence") if economic else None,
           "decision_followed": result["decision_followed"], "override_reason": result.get("override_reason"),
           "measured_seconds": result["measured_seconds"], "total_seconds": result["total_seconds"],
           "evidence": result["evidence"]}
    if account_scorecard is not None:
        obj["account_scorecard"] = account_scorecard
    if result.get("walkforward"):
        obj["walkforward"] = result["walkforward"]
        obj["walkforward_decision"] = validated
    if stage_review is not None:
        obj.update(stage_review=stage_review, stage_evidence=result["stage_evidence"])
    if result.get("cost_receipt"):
        obj["cost_receipt"] = result["cost_receipt"]
    if pending.get("harness"):
        obj["harness"] = pending["harness"]
    return obj


def review_pilot(directory):
    directory = Path(directory)
    with lock(directory):
        config = batch(directory)
        require(config["mode"] in {"pilot", "frontier_pilot"}, "not a pilot batch")
        nodes, pending = ledger(directory)
        require(pending is None and nodes, "pilot unfinished")
        accounting = node_costs(config, nodes)
        if config["mode"] == "frontier_pilot":
            decisions = [read(bound(n["evidence"][0])) for n in nodes]
            selections = [d["selected"] for d in decisions]
            resolved = {n["observation"]["question"] for n in nodes if n["observation"]["resolved"]}
            families = sorted({s["family"] for s in selections})
            result = {"schema": SCHEMA, "at": now(), "branch": config["branch"],
                      "batch": reference(directory / "BATCH.json"),
                      "checks": {"multiple_families_dispatched": len(families) >= 2,
                                 "policy_followed": not has_override(nodes),
                                 "request_budget": len(nodes) <= config["frontier_pilot"]["max_decisions"]},
                      "families": families, "resolved_questions": len(resolved),
                      "hypotheses_dispatched": len({s["hypothesis_id"] for s in selections}),
                      "experiment_total_seconds": None if any(n["total_seconds"] is None for n in nodes)
                          else sum(n["total_seconds"] for n in nodes),
                      "all_in_method_cost_seconds": accounting["all_in_method_cost_seconds"],
                      "cost_accounting": accounting,
                      "decision": "return_to_incumbent_after_unvalidated_frontier_pilot",
                      "next_policy": config["fallback_policy"],
                      "efficiency_improvement_claimed": False,
                      "limitations": ["Candidate generation, communication and review costs require separate accounting.",
                                      "Family coverage is not a matched-budget efficiency or economic comparison.",
                                      "Further frontier pilots require an explicit new boundary; no automatic adoption."],
                      "evidence": [ref for n in nodes for ref in n["evidence"]]}
            if config["frontier_pilot"]["rule"] == "bottleneck_evidence_cost_v1":
                result["selector_review"] = {"rule": config["frontier_pilot"]["rule"],
                    "selection_contract": config["frontier_pilot"]["selection_contract"],
                    "different_from_control": sum(d["selected"] != d["comparison"]["control"]["selected"] for d in decisions),
                    "decisions": [n["evidence"][0] for n in nodes],
                    "control_outcomes": "unknown; shadow selections were not executed",
                    "matched_budget_effect": "unknown"}
                result["evaluation_stage"] = evaluation_stage(config)
                result["economic_promotion"] = False
                if config.get("evaluation_contract"):
                    checks = [evaluate_stage(config["evaluation_contract"], n.get("stage_evidence"), n["observation"]) for n in nodes]
                    result["stage_passed_questions"] = len({c["question"] for c in checks if c and c["passed"]})
            if config.get("harness_deployment"):
                result.update(harness=config["harness_deployment"],
                              next_harness=config["harness_deployment"]["fallback_harness"],
                              harness_effect="unknown; use these outcomes in a new frozen matched harness round")
            write(directory / "PILOT_REVIEW.json", result)
            write(directory / "NEXT_POLICY.json", result["next_policy"])
            return result
        criteria = config["pilot_criteria"]
        observations = [n["observation"] for n in nodes]
        qualities = [o["quality"] for o in observations if o["quality"] is not None]
        resolved = {o["question"] for o in observations if o["resolved"]}
        checks = {"resolved_questions": len(resolved) >= criteria["min_resolved_questions"],
                  "request_budget": len(nodes) <= criteria["max_requests"],
                  "policy_followed": not has_override(nodes)}
        stage = evaluation_stage(config)
        if stage == "economic":
            checks["quality"] = bool(qualities) and max(qualities) >= criteria["min_quality"]
        else:
            reviews = [evaluate_stage(config["evaluation_contract"], n.get("stage_evidence"), n["observation"]) for n in nodes]
            passed_questions = {r["question"] for r in reviews if r and r["passed"]}
            checks["stage_criteria_coverage"] = len(passed_questions) >= criteria["min_stage_passed_questions"]
        if config.get("rrsi_enabled"):
            if config.get("cost_contract"):
                checks["all_in_method_cost"] = accounting["complete"] and (
                    accounting["all_in_method_cost_seconds"] <= criteria["max_all_in_method_cost_seconds"])
            else:
                known_cost = all(n["total_seconds"] is not None for n in nodes)
                checks["recorded_total_cost"] = known_cost and sum(n["total_seconds"] for n in nodes) <= criteria["max_total_seconds"]
        passed = all(checks.values())
        result = {"schema": SCHEMA, "at": now(), "branch": config["branch"],
                  "batch": reference(directory / "BATCH.json"), "checks": checks,
                  "decision": "retain_after_bounded_pilot" if passed else "rollback",
                  "next_policy": config["policy"] if passed else config["fallback_policy"],
                  "evaluation_stage": stage, "economic_promotion": False,
                  "all_in_method_cost_seconds": accounting["all_in_method_cost_seconds"],
                  "cost_accounting": accounting,
                  "efficiency_improvement_claimed": False,
                  "evidence": [ref for n in nodes for ref in n["evidence"]]}
        write(directory / "PILOT_REVIEW.json", result)
        write(directory / "NEXT_POLICY.json", result["next_policy"])
        return result


def export_world(directory, output, usage="development"):
    directory = Path(directory)
    with lock(directory):
        config = batch(directory)
        require(not (directory / "EXPORT.json").exists(), "batch already exported")
        nodes, pending = ledger(directory)
        require(pending is None and nodes, "cannot export unfinished/empty batch")
        if config["mode"] in {"pilot", "frontier_pilot"}:
            require((directory / "PILOT_REVIEW.json").exists(), "review pilot before exporting")
        overridden = has_override(nodes)
        limitations = ["policy override recorded"] if overridden else []
        if config["mode"] == "frontier_pilot":
            limitations.append("Concrete-candidate frontier selection is not reproduced by the v1 action-only replayer.")
            require(usage == "development", "frontier pilot cannot be relabeled as heldout for v1 replay")
        world = {"schema": SCHEMA, "id": directory.name, "branch": config["branch"],
                 "admission": "diagnostic" if limitations else "prospective", "usage": usage, "context_complete": True,
                 "batch_contract": reference(directory / "BATCH.json"),
                 "quality_contract": config["quality_contract"],
                 "metric_contract": config["metric_contract"], "initial_quality": config["initial_quality"],
                 "limitations": limitations, "nodes": nodes}
        for key in ("evaluation_contract", "cost_contract"):
            if config.get(key):
                world[key] = config[key]
        validate_world(world)
        write(output, world)
        receipt = {"world": reference(output), "nodes": len(nodes), "branch": config["branch"]}
        write(directory / "EXPORT.json", receipt)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("harness-init", help="freeze evidence, harness versions and matched evaluation cases")
    p.add_argument("--spec", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("harness", help="prepare Agent tasks, record generation, evaluate and review")
    p.add_argument("--round", required=True)
    p.add_argument("--request", required=True)
    p = commands.add_parser("review-cost", help="bind closed evolution timers to a frozen cycle report")
    p.add_argument("--report", required=True)
    p.add_argument("--receipt", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("review-account", help="check comparable net account results; no experiment or promotion")
    p.add_argument("--quality-contract", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("check-quality", help="validate a frozen quality contract without reading results")
    p.add_argument("--quality-contract", required=True)
    p = commands.add_parser("cycle")
    p.add_argument("--world", action="append", required=True)
    p.add_argument("--incumbent")
    p.add_argument("--branch", choices=sorted(BRANCHES), required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--rrsi", action="store_true", help="bounded RRSI-inspired proposal and cost selection")
    p.add_argument("--rrsi-history", help="previous RRSI REPORT.json; retain its immutable history chain")
    p.add_argument("--replay-semantics", choices=["v1", "v2"], default="v1",
                   help="v1 (default): original; v2: pessimistic unrecorded choices, cost-unknown not blocking, clean heldout only")
    p = commands.add_parser("review-pilot")
    p.add_argument("--batch", required=True)
    p = commands.add_parser("shadow-frontier", help="compare frozen selectors without dispatching research")
    p.add_argument("--batch", required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("check-frontier")
    p.add_argument("--frontier", required=True)
    p.add_argument("--branch", choices=sorted(BRANCHES), required=True)
    p = commands.add_parser("start-batch")
    p.add_argument("--report", required=True)
    p.add_argument("--boundary", required=True)
    p.add_argument("--out", required=True)
    for name in ["next", "observe", "export"]:
        p = commands.add_parser(name)
        p.add_argument("--batch", required=True)
        p.add_argument({"next": "--context", "observe": "--result", "export": "--out"}[name], required=True)
        if name == "export":
            p.add_argument("--usage", choices=["development", "heldout"], default="development")
    args = parser.parse_args(argv)
    try:
        if args.command == "harness-init":
            result = harness_init(args.spec, args.out)
        elif args.command == "harness":
            result = harness_run(args.round, args.request)
        elif args.command == "review-cost":
            result = review_cost(args.report, args.receipt, args.out)
        elif args.command == "check-quality":
            ref = reference(args.quality_contract)
            contract = quality_contract(ref)
            result = {"status": "valid_contract", "schema": contract["schema"], "kind": contract["kind"], "contract": ref}
        elif args.command == "review-account":
            result = review_account(reference(args.quality_contract), reference(args.evidence))
            write(args.out, result)
        elif args.command == "check-frontier":
            result = validate_frontier(read(args.frontier), args.branch)
        elif args.command == "cycle":
            result = cycle(args.world, args.incumbent, args.out, args.branch, args.rrsi, args.rrsi_history,
                           args.replay_semantics)
            result = {k: result[k] for k in ["decision", "branch", "engine_seconds", "limitations"]}
        elif args.command == "start-batch":
            result = start_batch(args.report, args.boundary, args.out)
        elif args.command == "next":
            result = next_decision(args.batch, args.context)
        elif args.command == "observe":
            result = observe(args.batch, args.result)
        elif args.command == "review-pilot":
            result = review_pilot(args.batch)
        elif args.command == "shadow-frontier":
            result = shadow_frontier(args.batch, args.context, args.out)
        else:
            result = export_world(args.batch, args.out, args.usage)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({"status": "error", "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
