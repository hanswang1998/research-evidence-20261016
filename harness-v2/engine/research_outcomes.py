#!/usr/bin/env python3
"""Canonical research outcome categories. Standard library only.

Every research line writes its own outcome strings; the 2026-10-16 ledger holds
hundreds of spellings per line (cautious 370, structure 262, math 122). Rates
such as "share of decisions that produced a verdict" cannot be compared across
batches, lines or harness versions until each record maps to one fixed category.

New records should write ``outcome`` with one of CATEGORIES directly and keep
their detailed wording in a separate field. ``classify`` maps legacy wording on a
best-effort basis and reports ``unclassified`` instead of guessing.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter

CATEGORIES = (
    "improvement",          # frozen economic criteria passed against the frozen parent
    "negative",             # complete, valid account result that failed its criteria
    "risk_limit",           # complete account rejected only by the drawdown / risk cap
    "information",          # information or engineering question answered (positive or negative)
    "engineering_failure",  # code, data or execution error; no valid answer produced
    "evidence_gap",         # ran or partly ran, but the verdict is unknown / unscored / inadmissible
    "not_executed",         # prepared, withdrawn, deferred or still waiting
    "unclassified",         # legacy wording that does not determine a category
)
VERDICT = {"improvement", "negative", "risk_limit", "information"}
NO_VERDICT = {"engineering_failure", "evidence_gap"}

ACCOUNT_KINDS = {"account", "group"}

# Ordered (pattern, category). The first match wins; patterns see lowercase text.
_STRONG = [
    (r"not_executed|withdrawn|deferred|prepared_or|^prepared$|^preparation$|pending|awaiting|waiting|"
     r"not_account_ready|under_review|input_package_gap|no_new_ready|without_admitted|no_scientific_selection|"
     r"not_selected|running_unconfirmed|needs_independent", "not_executed"),
    (r"implementation_(error|invalid|fail)|repairs?_exhausted|preflight_failed|execution_failed|"
     r"execution_failure|engineering_failure|^failed$|crash|exception|semantic_qualification_error|"
     r"implementation_budget_exhausted|bootstrap_fail|collision|guard_failure|phase_failure|"
     r"repair_unresolved|mapping_failure", "engineering_failure"),
    (r"risk_limit|drawdown_only|mdd_exceeded|risk_cap", "risk_limit"),
    (r"valid_improvement|^improved$|^improvement$|return_improved|development_gate_met|economic_gate_passed|"
     r"development_gate_pass", "improvement"),
    (r"unscored|unknown|unverified|evidence_gap|source_evidence_gap|coverage_gap|admissib|qualification_fail|"
     r"right_censored|censored|economics_unknown|financial_unknown|unresolved_lifecycle|incomplete_economics|"
     r"not_admitted|unadmitted|ineligible|metadata_qualification|admission_fail|source_unknown|readiness_gap|"
     r"coverage_failed|unaccepted|space_insufficient|not_qualified", "evidence_gap"),
]
# A frozen criterion that failed is a valid (negative) answer, not a missing one.
_NEGATIVE = (r"valid_negative|^negative$|economic_failure|profit_or_drawdown|gates?_fail|failed_development_goal|"
             r"goal_failed|reject|closed_valid_negative|negative|close_expression|dual_failed|profit_or_risk_failed|"
             r"rejected_candidate|close_fixed|closed_fixed|fixed_direction|source_activity|coactivity|"
             r"distinction_gate|not_supported|no_positive|stop_exact|close_exact|close_invalid")
_ANSWERED = (r"verified|information|^pass|_pass|passed|^complete|completed|^evaluated$|recorded_result|"
             r"result_recorded|^primary$|descriptive|delivered|method_evidence|original_verdict_recorded|"
             r"mechanics|derivation|diagnostic|^closed?$|^closed_|^close_|^stop|^retain|^accepted|census|"
             r"computed|^allow|explains|counterexample|discovery_only|source_lead|^finding")
_MISSING = r"fail|truncated|incomplete|missing|gap|unproven|ambiguous|insufficient"
_STRONG_COMPILED = [(re.compile(pattern), category) for pattern, category in _STRONG]


def classify(status, record_kind=None):
    """Map one legacy outcome string (plus optional record kind) to a canonical category.

    Information-stage records answer questions even when the answer is negative,
    so their negatives count as ``information``. Account records that only say
    "completed" or "recorded" do not reveal the verdict and stay ``unclassified``.
    """
    text = (status or "").strip().lower()
    if not text:
        return "unclassified"
    if text in CATEGORIES:
        return text
    kind = (record_kind or "").strip().lower()
    account = kind in ACCOUNT_KINDS
    for pattern, category in _STRONG_COMPILED:
        if pattern.search(text):
            return "information" if category == "risk_limit" and kind and not account else category
    if re.search(_NEGATIVE, text):
        return "negative" if account or not kind else "information"
    if account:
        # "completed" or "recorded" on an account record does not reveal the economic verdict.
        if re.search(r"verified_information|mechanics", text):
            return "information"
        return "evidence_gap" if re.search(_MISSING, text) else "unclassified"
    if re.search(_MISSING, text):
        return "evidence_gap"
    if re.search(_ANSWERED, text):
        return "information"
    return "unclassified"


def summarize(rows):
    """rows: iterable of (status, record_kind). Returns counts and verdict / no-verdict shares."""
    counts = Counter(classify(status, kind) for status, kind in rows)
    total = sum(counts.values())
    verdict = sum(counts[c] for c in VERDICT)
    missing = sum(counts[c] for c in NO_VERDICT)
    return {"total": total, "counts": {c: counts.get(c, 0) for c in CATEGORIES},
            "verdict_share": verdict / total if total else None,
            "no_verdict_share": missing / total if total else None,
            "unclassified_share": counts.get("unclassified", 0) / total if total else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("classify", help="classify one status string")
    one.add_argument("status")
    one.add_argument("--kind")
    table = sub.add_parser("ledger", help="summarize a CSV ledger by line")
    table.add_argument("--csv", required=True)
    table.add_argument("--status-column", default="result_classification")
    table.add_argument("--kind-column", default="record_kind")
    table.add_argument("--line-column", default="research_line")
    table.add_argument("--show-unclassified", type=int, default=0)
    args = parser.parse_args(argv)
    if args.command == "classify":
        print(json.dumps({"status": args.status, "category": classify(args.status, args.kind)}, ensure_ascii=False))
        return 0
    csv.field_size_limit(10 ** 9)
    by_line, unclassified = {}, Counter()
    with open(args.csv, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            status, kind = row.get(args.status_column, ""), row.get(args.kind_column, "")
            by_line.setdefault(row.get(args.line_column, ""), []).append((status, kind))
            if classify(status, kind) == "unclassified":
                unclassified[(status, kind)] += 1
    report = {line: summarize(rows) for line, rows in sorted(by_line.items())}
    report["_all"] = summarize([pair for rows in by_line.values() for pair in rows])
    if args.show_unclassified:
        report["_unclassified_examples"] = [{"status": s, "kind": k, "rows": n}
                                            for (s, k), n in unclassified.most_common(args.show_unclassified)]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
