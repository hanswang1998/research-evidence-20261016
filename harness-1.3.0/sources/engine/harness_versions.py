"""Immutable, bounded Harness source versions. Recording never adopts or executes one."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import difflib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCOPE = "engine/harness_version_scope.json"
STORE = "docs/harness/versions"
MAX_FILE = 2_000_000
MAX_TOTAL = 8_000_000
MAX_FILES = 64
MAX_VERSIONS = 250


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def version_key(version):
    require(isinstance(version, str) and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version),
            "version must be MAJOR.MINOR.PATCH")
    return tuple(map(int, version.split(".")))


def safe_name(name):
    require(isinstance(name, str) and name, "managed path must be nonempty text")
    path = PurePosixPath(name)
    require("\\" not in name and ":" not in name and not path.is_absolute()
            and path.as_posix() == name and not any(part.startswith(".") for part in path.parts)
            and path.suffix in {".md", ".py", ".json"}
            and (name in {"AGENTS.md", "CLAUDE.md"} or path.parts[0] in {"docs", "engine"})
            and not name.startswith(STORE + "/"), "invalid managed path: " + str(name))
    return name


def read_source(root, name):
    path = Path(root) / safe_name(name)
    require(path.resolve().is_relative_to(Path(root).resolve()), "source escapes workspace: " + name)
    before = path.stat()
    require(before.st_size <= MAX_FILE, "source exceeds size limit: " + name)
    raw = path.read_bytes()
    after = path.stat()
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), "source changed while reading: " + name)
    raw.decode("utf-8-sig")
    return raw


def capture(root):
    scope_raw = read_source(root, SCOPE)
    scope = json.loads(scope_raw)
    require(scope["schema"] == "harness_version_scope.v1" and scope["scope"], "invalid version scope")
    paths = [(name, component) for component, names in scope["components"].items() for name in names]
    require(0 < len(paths) <= MAX_FILES and len({p for p, _ in paths}) == len(paths)
            and SCOPE in {p for p, _ in paths}, "invalid or duplicate scope files")
    files, contents = [], {}
    for name, component in sorted(paths):
        raw = scope_raw if name == SCOPE else read_source(root, name)
        contents[name] = raw
        files.append(dict(path=name, component=component, sha256=digest(raw), bytes=len(raw)))
    require(sum(f["bytes"] for f in files) <= MAX_TOTAL, "scope exceeds total size limit")
    return dict(scope=scope["scope"], files=files, content_sha256=digest(encoded(files))), contents


def changes(before, after):
    old = {f["path"]: f for f in before}
    new = {f["path"]: f for f in after}
    result = []
    for name in sorted(old.keys() | new.keys()):
        left, right = old.get(name), new.get(name)
        if left != right:
            kind = "added" if left is None else "removed" if right is None else "modified"
            result.append(dict(path=name, kind=kind, before=left, after=right))
    return result


def read_archive(path):
    path = Path(path)
    require(path.stat().st_size <= MAX_TOTAL + 200_000, "archive exceeds size limit")
    raw = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.infolist()
        require(len(info) <= MAX_FILES + 1 and len({f.filename for f in info}) == len(info)
                and sum(f.file_size for f in info) <= MAX_TOTAL + 200_000, "invalid archive inventory")
        manifest = json.loads(archive.read("MANIFEST.json"))
        require(manifest["schema"] == "harness_version.v1", "unknown version schema")
        version_key(manifest["version"])
        require(path.name == "v" + manifest["version"] + ".zip", "version filename mismatch")
        expected = manifest["record_sha256"]
        body = {k: v for k, v in manifest.items() if k != "record_sha256"}
        require(digest(encoded(body)) == expected, "version record checksum mismatch")
        files = manifest["files"]
        require(0 < len(files) <= MAX_FILES and len({f["path"] for f in files}) == len(files)
                and digest(encoded(files)) == manifest["content_sha256"], "version inventory checksum mismatch")
        require({f.filename for f in info} == {"MANIFEST.json"} | {"sources/" + f["path"] for f in files},
                "archive inventory differs from manifest")
        contents = {}
        for entry in files:
            name = safe_name(entry["path"])
            require(0 <= entry["bytes"] <= MAX_FILE, "invalid source size")
            data = archive.read("sources/" + name)
            require(len(data) == entry["bytes"] and digest(data) == entry["sha256"], "archived source damaged: " + name)
            contents[name] = data
    manifest = dict(manifest, archive=dict(path=str(path.resolve()), sha256=digest(raw), bytes=len(raw)))
    return manifest, contents


def history(root):
    paths = list((Path(root) / STORE).glob("v*.zip"))
    require(len(paths) <= MAX_VERSIONS, "version history exceeds bounded reader; extend scope explicitly")
    versions = [read_archive(path)[0] for path in sorted(paths, key=lambda p: version_key(p.stem[1:]))]
    for index, entry in enumerate(versions):
        previous = versions[index - 1] if index else None
        parent = dict(version=previous["version"], sha256=previous["archive"]["sha256"]) if previous else None
        require(entry["parent"] == parent, "version parent chain mismatch: " + entry["version"])
        require(entry["changes"] == changes(previous["files"] if previous else [], entry["files"]), "version diff mismatch")
    return versions


def store_revision(root):
    """Small metadata comparison under the lock; decompress and hash outside it."""
    result = []
    for path in sorted((Path(root) / STORE).glob("v*.zip")):
        stat = path.stat()
        result.append((path.name, stat.st_size, stat.st_mtime_ns, stat.st_ino))
    return result


@contextmanager
def store_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".record.lock"
    try:
        handle = path.open("x", encoding="utf-8")
    except FileExistsError:
        raise ValueError("version recording in progress or interrupted; inspect lock, do not auto-remove it") from None
    try:
        with handle:
            handle.write(str(os.getpid()))
            handle.flush()
            yield
    finally:
        path.unlink()


def record(root, version, summary, change_type, expected):
    version_key(version)
    require(isinstance(summary, str) and 1 <= len(summary.strip()) <= 2000, "provide a concise change summary")
    require(change_type in {"baseline", "major", "minor", "patch"}, "unknown change type")
    prior_revision = store_revision(root)
    prior = history(root)
    parent = prior[-1] if prior else None
    if parent:
        require(change_type != "baseline", "baseline already recorded")
        major, minor, patch = version_key(parent["version"])
        next_version = {"major": (major + 1, 0, 0), "minor": (major, minor + 1, 0), "patch": (major, minor, patch + 1)}
        require(version_key(version) == next_version[change_type], "version must match the requested increment")
    else:
        require(version == "1.0.0" and change_type == "baseline", "first recorded baseline must be 1.0.0")
    captured, contents = capture(root)
    require(captured["content_sha256"] == expected, "working files changed after observation; review the new diff")
    delta = changes(parent["files"] if parent else [], captured["files"])
    require(delta, "content unchanged; reuse the recorded version")
    body = dict(schema="harness_version.v1", version=version, recorded_at=datetime.now(timezone.utc).isoformat(),
                status="recorded", summary=summary.strip(), change_type=change_type,
                parent=dict(version=parent["version"], sha256=parent["archive"]["sha256"]) if parent else None,
                **captured, changes=delta, adoption="not_established_by_recording")
    manifest = dict(body, record_sha256=digest(encoded(body)))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("MANIFEST.json", encoded(manifest))
        for name, data in contents.items():
            archive.writestr("sources/" + name, data)
    directory = Path(root) / STORE
    with store_lock(directory):
        # Compare the observed parent and source revision immediately before publishing.
        require(store_revision(root) == prior_revision, "version history changed; re-read the parent")
        require(capture(root)[0]["content_sha256"] == expected, "working files changed before publication")
        target = directory / ("v" + version + ".zip")
        require(not target.exists(), "version already exists; never overwrite")
        temporary = directory / (".v" + version + ".pending")
        with temporary.open("xb") as stream:
            stream.write(buffer.getvalue())
        try:
            temporary.rename(target)
        except BaseException:
            temporary.unlink()
            raise
    return read_archive(target)[0]


def status(root=ROOT):
    versions = history(root)
    latest = versions[-1] if versions else None
    try:
        current, _ = capture(root)
        delta = changes(latest["files"] if latest else [], current["files"])
        working = dict(state="changed" if delta else "matches_recorded", **current, changes=delta)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        working = dict(state="unknown", error=str(exc), changes=[])
    return dict(schema="harness_versions_view.v1", observed_at=datetime.now(timezone.utc).isoformat(),
                latest=latest["version"] if latest else None, versions=list(reversed(versions)), working=working,
                adoption_note="登记只固定共用文件。各线实际采用以原 PLAN / BATCH / TASK 的精确绑定为准；单个程序相同不代表整套版本已采用。")


def restore(root, version, output):
    version_key(version)
    manifest, contents = read_archive(Path(root) / STORE / ("v" + version + ".zip"))
    target = Path(output).resolve()
    require(not target.exists() and not target.is_relative_to((Path(root) / STORE).resolve()),
            "restore only to a new review directory outside the version store")
    target.mkdir(parents=True, exist_ok=False)
    for name, raw in contents.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
        require(path.read_bytes() == raw, "restore verification failed: " + name)
    return dict(version=version, output=str(target), restored_files=len(contents),
                content_sha256=manifest["content_sha256"], applied_to_research=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    p = commands.add_parser("record")
    p.add_argument("--version", required=True)
    p.add_argument("--type", choices=["baseline", "major", "minor", "patch"], required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--expect", required=True, help="working.content_sha256 returned by status")
    p = commands.add_parser("verify")
    p.add_argument("--version", required=True)
    p = commands.add_parser("restore")
    p.add_argument("--version", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("diff")
    p.add_argument("--from", dest="before", required=True)
    p.add_argument("--to", default="current")
    p.add_argument("--path", help="show the exact text diff for one managed path")
    args = parser.parse_args()
    try:
        if args.command == "status":
            result = status()
        elif args.command == "record":
            result = record(ROOT, args.version, args.summary, args.type, args.expect)
        elif args.command == "restore":
            result = restore(ROOT, args.version, args.out)
        elif args.command == "verify":
            version_key(args.version)
            versions = history(ROOT)
            require(any(v["version"] == args.version for v in versions), "version not recorded")
            result = dict(version=args.version, verified=True, adoption="not_checked")
        else:
            version_key(args.before)
            before, old = read_archive(ROOT / STORE / ("v" + args.before + ".zip"))
            if args.to == "current":
                after, new = capture(ROOT)
            else:
                version_key(args.to)
                after, new = read_archive(ROOT / STORE / ("v" + args.to + ".zip"))
            result = dict(changes=changes(before["files"], after["files"]))
            if args.path:
                name = safe_name(args.path)
                require(name in old or name in new, "path not part of this comparison")
                result["diff"] = "".join(difflib.unified_diff(old.get(name, b"").decode("utf-8-sig").splitlines(True),
                    new.get(name, b"").decode("utf-8-sig").splitlines(True), fromfile=args.before + "/" + name,
                    tofile=args.to + "/" + name))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print(json.dumps(dict(error=str(exc)), ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
