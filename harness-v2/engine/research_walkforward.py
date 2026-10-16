#!/usr/bin/env python3
"""Walk-forward verdicts on a candidate and its parent. Standard library only.

Why: about 1,100 account candidates were ranked on the same 2020-2025 window.
Structure line accounts have a median 275% annualized return (max 336%); ML3's
median is 299% against a 316% baseline. The only true out-of-sample run, ML2 on
2026-01-05..2026-09-30 (181 trading days, native), returned 57.6% annualized for
E1002J (306% in development) and 91.2% for H543 (294% in development). Beating
the parent again on the window that produced the parent mostly selects noise.

This scorer splits one continuous account into a visible window and later
validation folds. Only the visible window is reported in full; the folds decide,
and by default the researcher-facing verdict shows each fold only as pass/fail
plus a coarse delta bucket, so validation numbers cannot be tuned against. Folds
may not overlap the visible window or each other (differencing overlapping
windows would reveal the masked numbers). Rows on or after the seal date are
never read.

Input equity: JSON list of {"date": "YYYY-MM-DD", "equity": number}, a JSON
object with such a list under "equity_curve" / "daily" / "series", or a CSV with
date and equity columns. Annualization uses 250 trading days, as the project does.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path

SCHEMA = "research_walkforward.v1"
DEFAULT_CONTRACT = {
    "schema": SCHEMA,
    # The only window whose numbers the researcher sees in full.
    "visible": ["2020-01-01", "2022-12-31"],
    # Each later year is a validation fold reported as pass/fail plus a coarse bucket.
    "folds": [
        {"id": "V2023", "window": ["2023-01-01", "2023-12-31"]},
        {"id": "V2024", "window": ["2024-01-01", "2024-12-31"]},
        {"id": "V2025", "window": ["2025-01-01", "2025-12-31"]},
    ],
    "seal_start": "2026-01-01",
    "min_fold_wins": 2,
    "min_mean_delta_pp": 0.0,
    "max_validation_mdd_pct": 20.0,
    "annualization_days": 250,
    "min_rows_per_window": 60,
    "mask_validation": True,
}
BUCKETS = ((-math.inf, -10, "<-10pp"), (-10, -2, "-10..-2pp"), (-2, 2, "-2..+2pp"),
           (2, 10, "+2..+10pp"), (10, math.inf, ">+10pp"))


def day(value):
    return date.fromisoformat(str(value)[:10])


def reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def validate_contract(contract):
    if contract.get("schema") != SCHEMA:
        raise ValueError("walk-forward contract schema mismatch")
    seal = day(contract["seal_start"])
    v0, v1 = map(day, contract["visible"])
    folds = contract["folds"]
    if not folds or len({f["id"] for f in folds}) != len(folds):
        raise ValueError("folds must be non-empty with unique ids")
    previous_end = v1
    if not v0 <= v1:
        raise ValueError("visible window is empty")
    for fold in folds:
        f0, f1 = map(day, fold["window"])
        # Folds must follow the visible window and each other; any overlap would let
        # the full visible numbers, or one fold's bucket, reveal another fold.
        if not (previous_end < f0 <= f1):
            raise ValueError(f"{fold['id']}: folds must be ordered, non-overlapping and after the visible window")
        if f1 >= seal:
            raise ValueError(f"{fold['id']}: validation touches the sealed period")
        previous_end = f1
    if not 1 <= contract["min_fold_wins"] <= len(folds):
        raise ValueError("min_fold_wins outside fold count")
    return contract


def load_equity(path, seal):
    """Sorted (date, equity) rows strictly before the seal; returns rows and sealed count."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    rows = []
    if path.suffix.lower() == ".csv":
        reader = csv.DictReader(text.splitlines())
        fields = {name.lower(): name for name in reader.fieldnames or []}
        date_key = next((fields[k] for k in ("date", "day", "trade_date") if k in fields), None)
        value_key = next((fields[k] for k in ("equity", "total_value", "value", "nav") if k in fields), None)
        if not date_key or not value_key:
            raise ValueError(f"{path}: need date and equity columns")
        rows = [(day(r[date_key]), float(r[value_key])) for r in reader if r[value_key] not in ("", None)]
    else:
        data = json.loads(text)
        if isinstance(data, dict):
            data = next((data[k] for k in ("equity_curve", "daily", "series", "equity") if k in data), None)
        if not isinstance(data, list):
            raise ValueError(f"{path}: no equity list found")
        for item in data:
            if isinstance(item, dict):
                rows.append((day(item["date"]), float(item.get("equity", item.get("value")))))
            else:
                rows.append((day(item[0]), float(item[1])))
    rows.sort()
    if len({d for d, _ in rows}) != len(rows):
        raise ValueError(f"{path}: duplicate dates")
    if any(not math.isfinite(v) or v <= 0 for _, v in rows):
        raise ValueError(f"{path}: equity must be positive and finite")
    kept = [r for r in rows if r[0] < seal]
    return kept, len(rows) - len(kept)


def window_metrics(rows, start, end, days, min_rows):
    """Annualized return and max drawdown of one window, rebased on the prior close."""
    start, end = day(start), day(end)
    inside = [(d, v) for d, v in rows if start <= d <= end]
    if len(inside) < min_rows:
        return {"rows": len(inside), "annual_return_pct": None, "mdd_pct": None, "status": "insufficient_rows"}
    before = [v for d, v in rows if d < start]
    base = before[-1] if before else inside[0][1]
    periods = len(inside) if before else len(inside) - 1
    growth = inside[-1][1] / base
    annual = (growth ** (days / periods) - 1) * 100 if periods > 0 else None
    peak, worst = base, 0.0
    for _, value in inside:
        peak = max(peak, value)
        worst = max(worst, 1 - value / peak)
    return {"rows": len(inside), "annual_return_pct": annual, "mdd_pct": worst * 100,
            "total_return_pct": (growth - 1) * 100, "status": "ok"}


def bucket(delta):
    return next(label for low, high, label in BUCKETS if low <= delta < high)


def evaluate(contract, candidate_path, parent_path):
    contract = validate_contract(contract)
    seal = day(contract["seal_start"])
    cand, cand_sealed = load_equity(candidate_path, seal)
    par, par_sealed = load_equity(parent_path, seal)
    days, min_rows = contract["annualization_days"], contract["min_rows_per_window"]
    visible = {"window": contract["visible"],
               "candidate": window_metrics(cand, *contract["visible"], days, min_rows),
               "parent": window_metrics(par, *contract["visible"], days, min_rows)}
    folds, wins, deltas, mdd_ok, complete = [], 0, [], True, True
    for fold in contract["folds"]:
        val_c = window_metrics(cand, *fold["window"], days, min_rows)
        val_p = window_metrics(par, *fold["window"], days, min_rows)
        known = val_c["status"] == val_p["status"] == "ok"
        delta = val_c["annual_return_pct"] - val_p["annual_return_pct"] if known else None
        fold_mdd_ok = known and val_c["mdd_pct"] <= contract["max_validation_mdd_pct"]
        win = bool(known and delta > 0 and fold_mdd_ok)
        complete &= known
        mdd_ok &= bool(fold_mdd_ok)
        wins += win
        if known:
            deltas.append(delta)
        folds.append({"id": fold["id"], "window": fold["window"], "candidate": val_c, "parent": val_p,
                      "delta_pp": delta, "win": win, "mdd_ok": bool(fold_mdd_ok)})
    mean_delta = sum(deltas) / len(deltas) if deltas else None
    passed = (complete and wins >= contract["min_fold_wins"] and mdd_ok and
              mean_delta is not None and mean_delta >= contract["min_mean_delta_pp"])
    decision = "pass" if passed else ("insufficient_data" if not complete else "fail")
    return {"schema": "research_walkforward_verdict.v1", "contract": contract, "decision": decision,
            "fold_wins": wins, "folds_required": contract["min_fold_wins"], "mean_validation_delta_pp": mean_delta,
            "validation_mdd_ok": mdd_ok, "visible": visible, "folds": folds,
            "candidate": reference(candidate_path), "parent": reference(parent_path),
            "sealed_rows_ignored": {"candidate": cand_sealed, "parent": par_sealed},
            "scope": "development-period walk-forward; not independent confirmation; 2026 stays sealed"}


def masked(verdict):
    """Researcher-facing copy: the visible window in full, each fold only as pass/fail and bucket."""
    view = {k: v for k, v in verdict.items() if k not in {"folds", "mean_validation_delta_pp"}}
    view["folds"] = [{"id": fold["id"], "window": fold["window"], "win": fold["win"], "mdd_ok": fold["mdd_ok"],
                      "delta_bucket": bucket(fold["delta_pp"]) if fold["delta_pp"] is not None else None}
                     for fold in verdict["folds"]]
    view["mean_validation_delta_bucket"] = (bucket(verdict["mean_validation_delta_pp"])
                                            if verdict["mean_validation_delta_pp"] is not None else None)
    view["masked"] = True
    return view


def write_once(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("default-contract", help="print the default contract: visible 2020-2022, folds 2023/2024/2025")
    p = sub.add_parser("evaluate", help="score a candidate against its parent")
    p.add_argument("--contract", help="frozen contract JSON; default visible 2020-2022, folds 2023/2024/2025")
    p.add_argument("--candidate", required=True, help="candidate daily equity (JSON or CSV)")
    p.add_argument("--parent", required=True, help="parent daily equity on the same account rules")
    p.add_argument("--out", required=True, help="researcher-facing verdict (masked unless the contract says no)")
    p.add_argument("--detail-out", help="full numbers for the controller; keep outside the Agent's reading path")
    args = parser.parse_args(argv)
    try:
        if args.command == "default-contract":
            print(json.dumps(DEFAULT_CONTRACT, ensure_ascii=False, indent=2))
            return 0
        contract = json.loads(Path(args.contract).read_text(encoding="utf-8-sig")) if args.contract else DEFAULT_CONTRACT
        verdict = evaluate(contract, args.candidate, args.parent)
        if args.detail_out:
            write_once(args.detail_out, verdict)
        public = masked(verdict) if contract.get("mask_validation", True) else verdict
        write_once(args.out, public)
        print(json.dumps({"decision": verdict["decision"], "fold_wins": verdict["fold_wins"],
                          "out": str(Path(args.out).resolve())}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"status": "error", "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
