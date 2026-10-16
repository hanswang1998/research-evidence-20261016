"""Bounded guidance packets and local observation, without model or research calls.

Reader output, assembled tasks and literal output citations are different events.
None establishes model attention, understanding, compliance or economic benefit.
Frozen packets contain their text; later global documentation edits cannot rewrite it.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys


VERSION = "guidance-20261016-v1"
MAX_SOURCE_BYTES = 2_000_000
MAX_JOURNAL_BYTES = 16_000_000
CATALOG = {
    "goal.core": ("docs/harness/目标与评价.md", None),
    "selection.core": ("docs/harness/研判与选题.md", None),
    "evaluation.thresholds": ("docs/研究验收标准.md", "### 先分资格，再判断改进是否足以改变决定"),
    "evaluation.failures": ("docs/研究验收标准.md", "### 执行失败与经济反证"),
    "evaluation.account": ("docs/研究验收标准.md", "## 账户评价顺序"),
    "evaluation.metrics": ("docs/研究验收标准.md", "## 固定比较与数值口径"),
    "evaluation.confirmation": ("docs/研究验收标准.md", "### 重复开发与独立确认"),
    "judgment.diagnosis": ("docs/研究能力改进规范.md", "## 2. 每轮从收益来源、失效诊断与竞争解释开始"),
    "judgment.learning": ("docs/研究能力改进规范.md", "## 3. 结果必须更新策略认识与下一选择，但不能扩大判词"),
    "method.comparison": ("docs/研究能力改进规范.md", "## 4. 策略实验与研究方法实验分开验收"),
    "evidence.independence": ("docs/研究能力改进规范.md", "## 5. 三种证据不得混同"),
    "harness.adoption": ("docs/研究能力改进规范.md", "## 6. 实际接入、程序能力与汇报"),
}
PHASES = {
    "goal": ["goal.core"],
    "select": ["goal.core", "selection.core"],
    "evaluate": ["goal.core", "evaluation.thresholds", "evaluation.failures"],
    "reflect": ["goal.core", "selection.core", "judgment.learning"],
    "confirm": ["goal.core", "evaluation.thresholds", "evaluation.confirmation", "evidence.independence",
                "evaluation.account", "evaluation.metrics"],
    "method": ["goal.core", "selection.core", "method.comparison", "evidence.independence", "harness.adoption"],
}
LIMITATION = "仅观测本入口；绕过入口的读取未知，宿主截断及模型注意、理解、遵守和提效均未证明。"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def read_bytes(path):
    path = Path(path)
    require(path.stat().st_size <= MAX_SOURCE_BYTES, "source too large; use a bounded original artifact")
    return path.read_bytes()


def reference(path, data=None):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": digest(read_bytes(path) if data is None else data)}


def read_bound(ref):
    require(Path(ref["path"]).is_absolute(), "source path must be absolute")
    data = read_bytes(ref["path"])
    require(digest(data) == ref["sha256"], "bound artifact changed: " + ref["path"])
    return data


def section_lines(text, heading):
    lines = text.splitlines()
    if heading is None:
        return 1, len(lines)
    positions = [i for i, line in enumerate(lines) if line == heading]
    require(len(positions) == 1, "missing or ambiguous guidance heading: " + heading)
    start = positions[0]
    level = len(heading.split(" ", 1)[0])
    end = len(lines)
    for i in range(start + 1, len(lines)):
        match = re.match(r"^(#{1,6}) ", lines[i])
        if match and len(match[1]) <= level:
            end = i
            break
    return start + 1, end


def citation(item):
    return f"[guidance:{item['id']}:{item['excerpt_sha256'][:16]}]"


def render(packet):
    parts = [f"研究指引 {packet['version']}；本线 {packet['line']}；动作 {packet['phase']}。",
             "仅在判断实际依赖某条规则时，在原 PLAN／SPEC／结果相应段引用下面的 guidance 标记；不为埋点堆引用。"]
    for item in packet["items"]:
        parts.extend([f"\n{citation(item)} {item['source']['path']} L{item['lines'][0]}-L{item['lines'][1]} "
                      f"SHA256={item['source']['sha256']}", item["text"]])
    return "\n".join(parts)


def validate_packet(packet):
    require(packet["schema"] == "research_guidance_packet.v1" and packet["version"] == VERSION, "invalid guidance packet")
    require(packet["phase"] in PHASES and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", packet["line"]), "invalid guidance scope")
    require(packet["stage"] in {None, "information", "engineering", "economic"}, "invalid evidence stage")
    require(packet["phase"] != "evaluate" or packet["stage"] is not None, "evaluate requires an evidence stage")
    items = packet["items"]
    ids = [item["id"] for item in items]
    require(len(ids) == len(set(ids)) and set(PHASES[packet["phase"]]) <= set(ids) <= set(CATALOG),
            "packet omitted mandatory guidance or added unknown sections")
    if packet["phase"] == "evaluate" and packet["stage"] == "economic":
        require({"evaluation.account", "evaluation.metrics"} <= set(ids), "economic stage requires account details")
    for item in items:
        require(isinstance(item["text"], str) and item["text"] and item["truncated"] is False and
                item["excerpt_sha256"] == digest(item["text"].encode("utf-8")) and
                item["chars"] == len(item["text"]), "guidance excerpt damaged or truncated")
        require(Path(item["source"]["path"]).is_absolute() and re.fullmatch(r"[a-f0-9]{64}", item["source"]["sha256"]),
                "invalid guidance source")
        start, end = item["lines"]
        require(type(start) is int and type(end) is int and 1 <= start <= end and
                end - start + 1 == len(item["text"].split("\n")), "invalid guidance line range")
    return packet


def build_packet(root, line, phase="select", stage=None, details=(), max_chars=20000):
    require(phase in PHASES, "unknown guidance phase")
    ids = list(PHASES[phase])
    if phase == "evaluate" and stage == "economic":
        ids.extend(["evaluation.account", "evaluation.metrics"])
    ids = list(dict.fromkeys(ids + list(details)))
    require(set(ids) <= set(CATALOG), "unknown guidance detail")
    sources, items = {}, []
    for ident in ids:
        relative, heading = CATALOG[ident]
        path = (Path(root) / relative).resolve()
        if path not in sources:
            data = read_bytes(path)
            sources[path] = (reference(path, data), data.decode("utf-8-sig"))
        source, text = sources[path]
        start, end = section_lines(text, heading)
        excerpt = "\n".join(text.splitlines()[start - 1:end])
        items.append({"id": ident, "source": source, "lines": [start, end], "text": excerpt,
                      "excerpt_sha256": digest(excerpt.encode("utf-8")), "chars": len(excerpt), "truncated": False})
    packet = validate_packet({"schema": "research_guidance_packet.v1", "version": VERSION,
                              "line": line, "phase": phase, "stage": stage, "items": items})
    require(len(render(packet)) <= max_chars, "guidance exceeds output budget; request fewer optional details, never truncate core")
    return packet


def verify_sources(packet, root):
    """Validate externally supplied new-boundary packets against actual source bytes."""
    validate_packet(packet)
    for item in packet["items"]:
        relative, heading = CATALOG[item["id"]]
        require(Path(item["source"]["path"]).resolve() == (Path(root) / relative).resolve(), "guidance source outside catalog")
        text = read_bound(item["source"]).decode("utf-8-sig")
        start, end = section_lines(text, heading)
        require(item["lines"] == [start, end] and item["text"] == "\n".join(text.splitlines()[start - 1:end]),
                "guidance snapshot disagrees with original section")


def inventory(packet):
    return [{key: value for key, value in item.items() if key != "text"} for item in packet["items"]]


@contextmanager
def journal_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    try:
        yield
    finally:
        lock.unlink()


def events(path):
    path = Path(path)
    if not path.exists():
        return []
    require(path.stat().st_size <= MAX_JOURNAL_BYTES, "journal limit reached; continue at a new legitimate run boundary")
    data = path.read_bytes()
    require(not data or data.endswith(b"\n"), "partial telemetry tail; preserve and repair explicitly")
    out, previous = [], None
    for line in data.splitlines():
        event = json.loads(line)
        require(isinstance(event, dict) and {"schema", "sequence", "previous", "event_sha256", "kind", "packet"} <= set(event),
                "malformed telemetry event")
        require(event["kind"] in {"reader_output", "task_assembled", "output_citations"}, "unknown telemetry event")
        body = {key: value for key, value in event.items() if key != "event_sha256"}
        require(event["schema"] == "research_guidance_event.v1" and event["sequence"] == len(out) + 1 and
                event["previous"] == previous and event["event_sha256"] == digest(encoded(body)), "telemetry chain damaged")
        out.append(event)
        previous = event["event_sha256"]
    return out


def append_event(path, kind, actor, packet_ref, packet, **fields):
    require(kind in {"reader_output", "task_assembled", "output_citations"} and
            isinstance(actor, str) and 0 < len(actor.strip()) <= 160, "invalid observation identity")
    validate_packet(packet)
    require(json.loads(read_bound(packet_ref)) == packet, "packet reference mismatch")
    with journal_lock(path):
        prior = events(path)
        body = {"schema": "research_guidance_event.v1", "sequence": len(prior) + 1,
                "previous": prior[-1]["event_sha256"] if prior else None,
                "at": datetime.now(timezone.utc).isoformat(), "kind": kind, "actor": actor,
                "actor_identity": "caller_declared", "packet": packet_ref,
                "line": packet["line"], "phase": packet["phase"], **fields}
        event = {**body, "event_sha256": digest(encoded(body))}
        payload = encoded(event) + b"\n"
        size = Path(path).stat().st_size if Path(path).exists() else 0
        require(size + len(payload) <= MAX_JOURNAL_BYTES, "journal limit reached")
        with Path(path).open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    return event


def observe_citations(packet, artifacts):
    observed = []
    for artifact in artifacts:
        lines = read_bound(artifact).decode("utf-8-sig").splitlines()
        for item in packet["items"]:
            token = citation(item)
            matches = [i for i, text in enumerate(lines, 1) if token in text]
            if matches:
                observed.append({"id": item["id"], "citation": token, "artifact": artifact, "lines": matches})
    return observed


def record_use(journal, actor, packet_ref, artifacts):
    packet = validate_packet(json.loads(read_bound(packet_ref)))
    require(any(event["packet"] == packet_ref and event["kind"] in {"reader_output", "task_assembled"}
                for event in events(journal)), "no observed packet delivery in this run")
    matches = observe_citations(packet, artifacts)
    return append_event(journal, "output_citations", actor, packet_ref, packet,
                        artifacts=artifacts, citations=matches, semantic_use="unknown")


def report(journal):
    rows = events(journal)
    observations = []
    counts = {kind: 0 for kind in ("reader_output", "task_assembled", "output_citations")}
    for event in rows:
        counts[event["kind"]] += 1
        observations.append({key: value for key, value in event.items()
                             if key not in {"schema", "previous", "event_sha256", "actor_identity"}})
    return {"schema": "research_guidance_report.v1", "journal": str(Path(journal).resolve()),
            "observations": observations, "counts": counts, "coverage": "instrumented_entry_only",
            "literal_citations": sum(len(event.get("citations", [])) for event in rows),
            "model_read": "unknown", "understanding": "unknown", "compliance": "unknown", "limitation": LIMITATION}


def persist_packet(directory, packet):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = encoded(packet) + b"\n"
    path = directory / (digest(data) + ".json")
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        require(path.read_bytes() == data, "existing guidance snapshot changed")
    return reference(path, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reader = commands.add_parser("read", help="return a short core plus phase-specific details and record the exact output")
    reader.add_argument("--phase", choices=PHASES, required=True)
    reader.add_argument("--line", required=True)
    reader.add_argument("--stage", choices=("information", "engineering", "economic"))
    reader.add_argument("--detail", choices=CATALOG, action="append", default=[])
    reader.add_argument("--max-chars", type=int, default=20000)
    use = commands.add_parser("record-use", help="observe literal rule citations in an existing artifact, not semantic compliance")
    use.add_argument("--packet", required=True)
    use.add_argument("--artifact", required=True, action="append")
    commands.add_parser("report", help="show only locally observed events; absent observation is unknown")
    for sub in (reader, use):
        sub.add_argument("--actor", required=True)
    for sub in commands.choices.values():
        sub.add_argument("--run-dir", required=True, help="original PLAN/batch directory; no separate research ledger")
    args = parser.parse_args()
    directory = Path(args.run_dir).resolve() / "guidance"
    journal = directory / "EVENTS.jsonl"
    try:
        if args.command == "read":
            packet = build_packet(Path(__file__).resolve().parent.parent, args.line, args.phase,
                                  args.stage, args.detail, args.max_chars)
            packet_ref = persist_packet(directory / "packets", packet)
            output = render(packet)
            event = append_event(journal, "reader_output", args.actor, packet_ref, packet,
                                 items=inventory(packet), output_chars=len(output), output_sha256=digest(output.encode("utf-8")))
            result = {"packet": packet_ref, "event": event["sequence"], "instructions": output, "limitation": LIMITATION}
        elif args.command == "record-use":
            result = record_use(journal, args.actor, reference(args.packet), [reference(p) for p in args.artifact])
        else:
            result = report(journal)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
