#!/usr/bin/env python3
"""Measure active work and assemble five-phase resource-time receipts. Missing phases stay unknown."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PHASES = ("generation", "execution", "verification", "maintenance", "communication")
DEFAULT_RESOURCE = {"generation": "agent", "communication": "agent", "maintenance": "agent",
                    "execution": "compute", "verification": "compute"}


def now():
    return datetime.now(timezone.utc)


def reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def write_once(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    return reference(path)


def check_identity(event_id, phase, resource):
    if not event_id or not isinstance(event_id, str):
        raise ValueError("event id required")
    if phase not in PHASES:
        raise ValueError("phase must be one of " + ", ".join(PHASES))
    resource = resource or DEFAULT_RESOURCE[phase]
    if not isinstance(resource, str) or not resource.strip():
        raise ValueError("resource id required")
    return resource


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timer timestamp needs timezone")
    return parsed.astimezone(timezone.utc)


def validate_event(value):
    if value.get("schema") != "research_timing.v1":
        raise ValueError("not a finished timer")
    check_identity(value["event_id"], value["phase"], value["resource"])
    seconds = value["seconds"]
    elapsed = (instant(value["finished_at"]) - instant(value["started_at"])).total_seconds()
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0 or abs(seconds - elapsed) > 0.000001:
        raise ValueError("timer seconds disagree with its interval")


def record(event_id, phase, resource, started, finished, command=None, exit_code=None, note=None):
    seconds = (finished - started).total_seconds()
    if seconds < 0:
        raise ValueError("timer stopped before it started")
    return {"schema": "research_timing.v1", "event_id": event_id, "phase": phase, "resource": resource,
            "started_at": started.isoformat(), "finished_at": finished.isoformat(), "seconds": seconds,
            "command": command, "exit_code": exit_code, "host": socket.gethostname(), "pid": os.getpid(),
            "note": note, "accounting": "wall time of this event on its resource; not a wait or a total"}


def run(event_id, phase, out, command, resource=None, note=None):
    """Run a command and record its time; the command's exit code is returned unchanged."""
    resource = check_identity(event_id, phase, resource)
    if not command:
        raise ValueError("command required after --")
    if Path(out).exists():
        raise ValueError("timer file already exists; use a new event id")
    # Reserve before execution. A crash or duplicate invocation never repeats the command.
    start_path = str(out) + ".start.json"
    begun = start(event_id, phase, start_path, resource, note)
    started = instant(json.loads(Path(start_path).read_text(encoding="utf-8"))["started_at"])
    try:
        completed = subprocess.run(command)
    except OSError as error:
        value = record(event_id, phase, resource, started, now(), command, note=str(error))
        value.update(start_file=begun, status="launch_failed")
        write_once(out, value)
        raise
    value = record(event_id, phase, resource, started, now(), command, completed.returncode, note)
    value.update(start_file=begun, status="completed" if completed.returncode == 0 else "failed")
    write_once(out, value)
    return completed.returncode


def start(event_id, phase, out, resource=None, note=None):
    resource = check_identity(event_id, phase, resource)
    value = {"schema": "research_timing_start.v1", "event_id": event_id, "phase": phase,
             "resource": resource, "started_at": now().isoformat(), "note": note}
    return write_once(out, value)


def stop(start_path, out, note=None):
    begun = json.loads(Path(start_path).read_text(encoding="utf-8"))
    if begun.get("schema") != "research_timing_start.v1":
        raise ValueError("not a timer start file")
    check_identity(begun["event_id"], begun["phase"], begun["resource"])
    started = instant(begun["started_at"])
    value = record(begun["event_id"], begun["phase"], begun["resource"], started, now(),
                   note=note or begun.get("note"))
    value["start_file"] = reference(start_path)
    if Path(out).exists():
        raise ValueError("timer file already exists")
    write_once(str(start_path) + ".closed.json", {"start": value["start_file"], "output": str(Path(out).resolve())})
    return write_once(out, value)


def overlapping(events):
    """Pairs of events on one resource whose intervals overlap."""
    clashes = []
    by_resource = {}
    for event in events:
        by_resource.setdefault(event["resource"], []).append(event)
    for resource, items in by_resource.items():
        items.sort(key=lambda e: instant(e["started_at"]))
        for left, right in zip(items, items[1:]):
            if instant(right["started_at"]) < instant(left["finished_at"]):
                clashes.append((resource, left["event_id"], right["event_id"]))
    return clashes


def receipt(cost_contract, subject, timers, out, kind="decision", none_phases=()):
    """Assemble research_cost_receipt.v1. Phases without timers stay null (unknown).

    ``none_phases`` lists phases the caller states had no activity at all; they are
    written as [] (measured zero), which is different from null (not measured).
    """
    contract = json.loads(Path(cost_contract).read_text(encoding="utf-8-sig"))
    if (contract.get("schema") != "research_cost_contract.v1" or contract.get("unit") != "resource_seconds"
            or contract.get("phases") != list(PHASES)
            or contract.get("accounting") != "nonoverlapping_active_resource_time"):
        raise ValueError("unsupported cost contract")
    if kind not in {"decision", "evolution", "harness_attempt", "harness_evolution"}:
        raise ValueError("unknown receipt kind")
    events = []
    for path in timers:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        validate_event(value)
        events.append({**value, "_path": path})
    ids = [e["event_id"] for e in events]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate event id across timers")
    clashes = overlapping(events)
    if clashes:
        raise ValueError("overlapping timers on one resource would double count: " + repr(clashes))
    phases = {phase: None for phase in PHASES}
    for phase in none_phases:
        if phase not in PHASES:
            raise ValueError("unknown phase " + phase)
        phases[phase] = []
    for event in events:
        if phases[event["phase"]] is None:
            phases[event["phase"]] = []
        if event["phase"] in none_phases:
            raise ValueError(f"phase {event['phase']} declared empty but has a timer")
        phases[event["phase"]].append({"id": event["event_id"], "evidence": reference(event["_path"]),
                                       "seconds_path": "seconds"})
    value = {"schema": "research_cost_receipt.v1", "cost_contract": reference(cost_contract),
             "kind": kind, "subject": reference(subject), "phases": phases}
    return write_once(out, value)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="time one executor command: run ... -- <command>")
    for name in ("--event-id", "--phase", "--out"):
        p.add_argument(name, required=True)
    p.add_argument("--resource")
    p.add_argument("--note")
    p = sub.add_parser("start", help="open a timer for non-subprocess work")
    for name in ("--event-id", "--phase", "--out"):
        p.add_argument(name, required=True)
    p.add_argument("--resource")
    p.add_argument("--note")
    p = sub.add_parser("stop", help="close a timer opened with start")
    p.add_argument("--start", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--note")
    p = sub.add_parser("receipt", help="assemble a five-phase cost receipt")
    p.add_argument("--cost-contract", required=True)
    p.add_argument("--subject", required=True, help="the D*.json decision (or REPORT / TASK) this cost belongs to")
    p.add_argument("--timer", action="append", default=[])
    p.add_argument("--none", action="append", default=[], help="phase with no activity at all (written as [])")
    p.add_argument("--kind", default="decision")
    p.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        created = [Path(args.out)]
        if args.command == "run":
            code = run(args.event_id, args.phase, args.out, command, args.resource, args.note)
            result = reference(args.out)
            created.append(Path(args.out + ".start.json"))
        elif args.command == "start":
            result = start(args.event_id, args.phase, args.out, args.resource, args.note)
        elif args.command == "stop":
            result = stop(args.start, args.out, args.note)
            created.append(Path(args.start + ".closed.json"))
        else:
            result = receipt(args.cost_contract, args.subject, args.timer, args.out, args.kind, args.none)
        print(json.dumps({"result": result, "artifact_usage": {"files": len(created),
            "logical_bytes": sum(path.stat().st_size for path in created), "rewritten_bytes": 0}}, ensure_ascii=False))
        return code if args.command == "run" else 0
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"status": "error", "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
