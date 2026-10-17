#!/usr/bin/env python3
"""Bounded, source-referenced state navigation. A digest is not an authority or execution receipt."""
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
    if pointer == "":
        return True, obj
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("JSON pointer must start with /")
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
    if type(limit) is not int or limit < 1:
        raise ValueError("excerpt chars must be a positive integer")
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
    if type(budget) is not int or not 1000 <= budget <= 120000:
        raise ValueError("max_chars must be between 1000 and 120000")
    if not isinstance(state, dict):
        raise ValueError("state must be an object")
    sources = [{"path": str(state_path), "sha256": hashlib.sha256(raw).hexdigest()}]
    lines = [f"# {config['line']} 本线状态摘要",
             f"生成：{datetime.now(timezone.utc).isoformat()}　来源：{state_path}　SHA256={hashlib.sha256(raw).hexdigest()}"
             f"　原件 {len(raw):,} 字节",
             "摘要只为定位；裁决、授权与预算以原件为准。下面没有的内容请按引用打开原件，不要重读全量历史。", ""]

    def add(block):
        text = "\n".join(block)
        if sum(len(x) + 1 for x in lines) + len(text) + 1 > budget:
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
        block = [f"## 匹配到的关闭记录（{len(keys)} 项；只覆盖本配置匹配项，关闭范围以原件为准）"]
        for key in keys:
            item = state[key]
            cells = [clip(compact(item.get(f, "")), 120, f"{key}.{f}") if isinstance(item, dict) else compact(item)
                     for f in closed.get("fields", ["id", "status"])]
            row = f"- {key}：" + " | ".join(cells)
            block.append(clip(row, closed.get("chars_per_row", 220), key))
        if not add(block + [""]):
            raise ValueError("max_chars too small for closed records; narrow config or increase budget, never silently omit")

    reflections = config.get("reflections")
    if reflections:
        latest = reflections.get("latest", 3)
        if type(latest) is not int or not 1 <= latest <= 10:
            raise ValueError("latest reflections must be between 1 and 10")
        files = sorted(root.glob(reflections["glob"]), key=natural_key)[-latest:]
        block = ["## 最近反思"]
        for path in reversed(files):
            data = path.read_bytes()
            text = data.decode("utf-8-sig").strip()
            sources.append({"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()})
            block += [f"### {path.relative_to(root)}（SHA256={hashlib.sha256(data).hexdigest()}）",
                      clip(text, reflections.get("chars", 900), str(path.relative_to(root)))]
        if not add(block + [""]):
            raise ValueError("max_chars too small for reflections; narrow config or increase budget")
    text = "\n".join(lines) + "\n"
    return text, {"schema": "research_line_digest_receipt.v1", "line": config["line"],
                  "state": {"path": str(state_path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
                  "sources": sources, "digest_chars": len(text), "omitted": [],
                  "coverage": "configured fields and matching records only; missing fields are unknown"}


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
        receipt_path = out.with_suffix(".receipt.json")
        if out.exists() or receipt_path.exists():
            raise ValueError("digest output already exists; use a new snapshot path")
        with out.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        config_path = Path(args.config).resolve()
        receipt["config"] = {"path": str(config_path), "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest()}
        receipt["digest"] = {"path": str(out.resolve()), "sha256": hashlib.sha256(out.read_bytes()).hexdigest()}
        with receipt_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        receipt["artifact_usage"] = {"files": 2, "logical_bytes": out.stat().st_size + receipt_path.stat().st_size,
                                     "rewritten_bytes": 0}
        print(json.dumps(receipt, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "error", "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
