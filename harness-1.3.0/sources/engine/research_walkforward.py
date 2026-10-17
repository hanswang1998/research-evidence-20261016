#!/usr/bin/env python3
"""Optional development-period segment checks, NOT rolling retraining or new OOS evidence.

All periods, criteria, calendar and account parameters are supplied by a bound
input. There are no new default goals. A report never promotes a strategy or a
Harness. Sparse/different calendars and borrowed reports are rejected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def bound(ref):
    require(isinstance(ref, dict) and set(ref) == {"path", "sha256"}, "hash reference required")
    require(Path(ref["path"]).is_absolute(), "absolute source path required")
    path = Path(ref["path"]).resolve()
    require(reference(path)["sha256"] == ref["sha256"], "source hash mismatch")
    return path


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def day(value):
    require(isinstance(value, str) and date.fromisoformat(value).isoformat() == value, "ISO date required")
    return value


def load_contract(ref):
    contract = read(bound(ref))
    require(contract.get("schema") == "research_segment_contract.v1", "invalid segment contract")
    bound(contract["account_contract"])
    calendar = read(bound(contract["calendar"]))
    require(calendar.get("schema") == "research_calendar.v1", "explicit calendar required")
    bound(calendar["source"])
    dates = calendar["dates"]
    require(isinstance(dates, list) and dates and dates == sorted(set(dates)), "calendar dates must be unique and ordered")
    for value in dates:
        day(value)
    anchor = day(calendar["anchor_date"])
    require(anchor < dates[0], "opening equity anchor must precede the calendar")
    require(type(contract["annual_days"]) is int and 1 <= contract["annual_days"] <= 366, "invalid annualization basis")
    windows = contract["windows"]
    require(isinstance(windows, list) and windows, "explicit windows required")
    covered, ids = [], set()
    for window in windows:
        require(isinstance(window["id"], str) and window["id"] and window["id"] not in ids, "duplicate window id")
        ids.add(window["id"])
        require(window["start"] in dates and window["end"] in dates and window["start"] <= window["end"],
                "window endpoints must be in the frozen calendar")
        covered.extend(d for d in dates if window["start"] <= d <= window["end"])
    require(covered == dates, "windows must partition the calendar once, without gaps or overlap")
    criteria = contract["criteria"]
    require(set(criteria) == {"min_wins", "max_drawdown", "min_mean_annual_delta"}, "explicit segment criteria required")
    require(type(criteria["min_wins"]) is int and 1 <= criteria["min_wins"] <= len(windows), "invalid win threshold")
    require(finite(criteria["max_drawdown"]) and 0 < criteria["max_drawdown"] < 1 and
            finite(criteria["min_mean_annual_delta"]), "invalid segment thresholds")
    return contract, [anchor] + dates


def load_run(ref, contract, dates):
    run = read(bound(ref))
    require(run.get("schema") == "research_equity_run.v1" and isinstance(run.get("run_id"), str) and run["run_id"],
            "run identity required")
    require(run["account_contract"] == contract["account_contract"], "account parameters differ")
    for key in ("implementation", "grade_evidence"):
        bound(run[key])
    rows = read(bound(run["equity"]))
    require(isinstance(rows, list) and [row["date"] for row in rows] == dates,
            "equity must match the complete frozen calendar and opening anchor")
    values = [row["equity"] for row in rows]
    require(all(finite(value) and value > 0 for value in values), "invalid equity")
    return run, values


def metrics(values, periods, annual_days):
    ratio = values[-1] / values[0]
    annual = ratio ** (annual_days / periods) - 1
    peak, drawdown = values[0], 0
    for value in values:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    require(finite(annual) and finite(drawdown), "nonfinite segment metrics")
    return {"total_return": ratio - 1, "annual_return": annual, "max_drawdown": drawdown, "periods": periods}


def evaluate(input_ref, runtime_ref=None):
    inputs = read(bound(input_ref))
    require(inputs.get("schema") == "research_segment_input.v1" and inputs.get("experiment_id"), "invalid segment input")
    bound(inputs["subject"])
    contract, dates = load_contract(inputs["contract"])
    parent, old = load_run(inputs["parent"], contract, dates)
    candidate, new = load_run(inputs["candidate"], contract, dates)
    require(parent["run_id"] != candidate["run_id"], "distinct account run identities required")
    require(old[0] == new[0], "opening equity differs")
    results = []
    for window in contract["windows"]:
        start, end = dates.index(window["start"]), dates.index(window["end"])
        periods = end - start + 1
        before = metrics(old[start - 1:end + 1], periods, contract["annual_days"])
        after = metrics(new[start - 1:end + 1], periods, contract["annual_days"])
        results.append({"window": window, "parent": before, "candidate": after,
                        "annual_delta": after["annual_return"] - before["annual_return"]})
    criteria = contract["criteria"]
    wins = sum(row["annual_delta"] > 0 for row in results)
    mean_delta = sum(row["annual_delta"] for row in results) / len(results)
    passed = (wins >= criteria["min_wins"] and mean_delta >= criteria["min_mean_annual_delta"] and
              all(row["candidate"]["max_drawdown"] <= criteria["max_drawdown"] for row in results))
    runtime_ref = runtime_ref or reference(__file__)
    require(reference(bound(runtime_ref))["sha256"] == reference(__file__)["sha256"], "use the bound segment scorer")
    return {"schema": "research_segment_check.v1", "input": input_ref, "runtime": runtime_ref,
            "subject": inputs["subject"], "contract": inputs["contract"],
            "candidate": inputs["candidate"], "parent": inputs["parent"], "windows": results,
            "wins": wins, "mean_annual_delta": mean_delta, "decision": "pass" if passed else "fail",
            "economic_promotion": False, "efficiency_improvement_claimed": False,
            "scope": "development-period segmented account diagnostic; no retraining or independent confirmation"}


def verify(report_ref, expected_input):
    report = read(bound(report_ref))
    require(report.get("schema") == "research_segment_check.v1" and report.get("input") == expected_input,
            "segment report input binding mismatch")
    require(report == evaluate(expected_input, report["runtime"]), "segment verdict or provenance mismatch")
    return report


def write_report(input_path, out):
    out = Path(out)
    runtime = out.with_suffix(out.suffix + ".scorer.py")
    require(not out.exists() and not runtime.exists(), "segment output already exists")
    result = evaluate(reference(input_path))
    out.parent.mkdir(parents=True, exist_ok=True)
    with runtime.open("xb") as handle:
        handle.write(Path(__file__).read_bytes())
    result["runtime"] = reference(runtime)
    with out.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    verify(reference(out), reference(input_path))
    return reference(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    score = sub.add_parser("evaluate")
    score.add_argument("--input", required=True)
    score.add_argument("--out", required=True)
    check = sub.add_parser("verify")
    check.add_argument("--report", required=True)
    check.add_argument("--expected-input", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "evaluate":
            result = write_report(args.input, args.out)
            paths = [Path(args.out), Path(args.out + ".scorer.py")]
            result = {"report": result, "artifact_usage": {"files": len(paths),
                "logical_bytes": sum(path.stat().st_size for path in paths), "rewritten_bytes": 0}}
        else:
            result = verify(reference(args.report), reference(args.expected_input))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as error:
        print(json.dumps({"status": "error", "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
