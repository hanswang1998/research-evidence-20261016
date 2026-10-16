#!/usr/bin/env python3
"""Bounded per-line state digest for the start of each batch. Standard library only.

In the three ML3 cases of 2026-10-16 each batch took 33-48 minutes, 64-76% of the
43-118 tool calls were reads or searches, and 340-650 thousand characters were
read back, while the account job itself took about 1.5 minutes. ML3 STATE.json
alone holds 182 keys and 333 thousand characters, mostly closed tasks.

The digest gives the Agent one bounded file: the fields that decide the next
action, the list of closed expressions (so they are not reopened under a new
name), and the latest reflections. Every excerpt cites the source path and
SHA-256; full originals stay available and are opened only when needed.

Config (research_line_digest.v1), paths relative to --root:
{
  "schema": "research_line_digest.v1", "line": "ml3",
  "state": "analysis/yjj_ml3_research_20261012/STATE.json",
  "fields": [{"pointer": "/status"}, {"pointer": "/rsi/completed_cycles", "chars": 200}],
  "closed": {"key_pattern": "^b\\\\d+(_preserved)?_closed_task$", "fields": ["id", "status", "purpose"],
             "chars_per_row": 220},
  "reflections": {"glob": "analysis/yjj_ml3_research_20261012/batch*/REFLECTION.md", "latest": 3, "chars": 900},
  "max_chars": 20000
}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "research_line_digest.v1"
DEFAULT_FIELD_CHARS = 800


def pointer_get(obj, pointer):
    """RFC 6901 JSON pointer lookup; returns (found, value)."""
    if pointer in ("", "/"):
        return True, obj
    for raw in pointer.lstrip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(obj, dict) and token in obj:
            obj = obj[token]
        elif isinstance(obj, list) and token.isdigit() and int(token) < len(obj):
            obj = obj[int(token)]
        else:
            return False, None
    return True, obj


def compact(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def clip(text, limit, where):
    if len(text) <= limit:
        return text
    return text[:limit] + f" …[截断：{where} 共 {len(text)} 字符，按需读取原件]"


def natural_key(path):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(path))]


def build(config, root):
    if config.get("schema") != SCHEMA:
        raise ValueError("digest config schema mismatch")
    root = Path(root).resolve()
    state_path = (root / config["state"]).resolve()
    raw = state_path.read_bytes()
    state = json.loads(raw.decode("utf-8-sig"))
    budget = config.get("max_chars", 20000)
    lines = [f"# {config['line']} 本线状态摘要",
             f"生成：{datetime.now(timezone.utc).isoformat()}　来源：{state_path}　SHA256={hashlib.sha256(raw).hexdigest()}"
             f"　原件 {len(raw):,} 字节",
             "摘要只为定位；裁决、授权与预算以原件为准。下面没有的内容请按引用打开原件，不要重读全量历史。", ""]
    omitted = []

    def add(block):
        text = "\n".join(block)
        if sum(len(x) + 1 for x in lines) + len(text) > budget:
            return False
        lines.extend(block)
        return True

    block = ["## 关键字段"]
    for field in config.get("fields", []):
        found, value = pointer_get(state, field["pointer"])
        shown = clip(compact(value), field.get("chars", DEFAULT_FIELD_CHARS), field["pointer"]) if found else "（原件无此字段）"
        block.append(f"- `{field['pointer']}`：{shown}")
    if not add(block + [""]):
        raise ValueError("max_chars too small for the key fields")

    closed = config.get("closed")
    if closed:
        pattern = re.compile(closed["key_pattern"])
        keys = sorted((k for k in state if pattern.search(k)), key=natural_key)
        block = [f"## 已关闭表达（{len(keys)} 项；不要换名、换阈值或换窗口重开）"]
        for key in keys:
            item = state[key]
            cells = [clip(compact(item.get(f, "")), 120, f"{key}.{f}") if isinstance(item, dict) else compact(item)
                     for f in closed.get("fields", ["id", "status"])]
            row = f"- {key}：" + " | ".join(cells)
            block.append(clip(row, closed.get("chars_per_row", 220), key))
        if not add(block + [""]):
            omitted.append("closed")

    reflections = config.get("reflections")
    if reflections:
        files = sorted(root.glob(reflections["glob"]), key=natural_key)[-reflections.get("latest", 3):]
        block = ["## 最近反思"]
        for path in reversed(files):
            data = path.read_bytes()
            text = data.decode("utf-8-sig").strip()
            block += [f"### {path.relative_to(root)}（SHA256={hashlib.sha256(data).hexdigest()[:16]}…）",
                      clip(text, reflections.get("chars", 900), str(path.relative_to(root)))]
        if not add(block + [""]):
            omitted.append("reflections")
    if omitted:
        lines.append(f"（超出 {budget} 字符预算未纳入：{', '.join(omitted)}；请缩短字段或提高 max_chars）")
    text = "\n".join(lines) + "\n"
    return text, {"schema": "research_line_digest_receipt.v1", "line": config["line"],
                  "state": {"path": str(state_path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
                  "digest_chars": len(text), "omitted": omitted}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", required=True, help="project root that config paths are relative to")
    parser.add_argument("--out", required=True, help="digest Markdown; a .receipt.json is written beside it")
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        text, receipt = build(config, args.root)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")
        receipt["digest"] = {"path": str(out.resolve()), "sha256": hashlib.sha256(out.read_bytes()).hexdigest()}
        out.with_suffix(".receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                                                    encoding="utf-8", newline="\n")
        print(json.dumps(receipt, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "error", "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
