#!/usr/bin/env python3
"""Exact status categories for descriptive summaries. Unknown legacy wording stays unclassified."""
from __future__ import annotations

import argparse
import csv
import json
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

# Never promote substring matches such as "not_improved" or "pass_unverified".
EXACT_STATUS = {
    "valid_improvement": "improvement", "valid_negative": "negative",
    "verified_information": "information", "implementation_error": "engineering_failure",
    "evidence_gap": "evidence_gap", "not_executed": "not_executed",
}


def classify(status, record_kind=None):
    text = (status or "").strip().lower()
    # An explicit canonical category remains a label, not independently verified evidence.
    if text in CATEGORIES:
        return text
    return EXACT_STATUS.get(text, "unclassified")


def summarize(rows):
    """rows: iterable of (status, record_kind). Returns counts and verdict / no-verdict shares."""
    counts = Counter(classify(status, kind) for status, kind in rows)
    total = sum(counts.values())
    verdict = sum(counts[c] for c in VERDICT)
    missing = sum(counts[c] for c in NO_VERDICT)
    return {"verification": "labels_only; no economic or efficiency proof", "total": total, "counts": {c: counts.get(c, 0) for c in CATEGORIES},
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
    csv.field_size_limit(10 ** 6)
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
