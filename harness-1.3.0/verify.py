"""Read-only verification of the registered Harness 1.3.0 snapshot."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parent
EXPECTED = "51a6c2d4f34b9f00abd8c4fdd1132334fe1cff89ed95c70c9ff524b10cffe937"


def verify():
    archive = ROOT / "v1.3.0.zip"
    if hashlib.sha256(archive.read_bytes()).hexdigest() != EXPECTED:
        raise ValueError("Original archive SHA256 mismatch")
    with zipfile.ZipFile(archive) as z:
        original_manifest = z.read("MANIFEST.json")
        if (ROOT / "MANIFEST.json").read_bytes() != original_manifest:
            raise ValueError("Manifest differs from original archive")
        manifest = json.loads(original_manifest)
        expected_paths = {entry["path"] for entry in manifest["files"]}
        actual_paths = {
            p.relative_to(ROOT / "sources").as_posix()
            for p in (ROOT / "sources").rglob("*") if p.is_file()
        }
        if actual_paths != expected_paths:
            raise ValueError("Expanded source file list differs from manifest")
        for entry in manifest["files"]:
            data = (ROOT / "sources" / entry["path"]).read_bytes()
            if data != z.read("sources/" + entry["path"]):
                raise ValueError("Source differs from original: " + entry["path"])
            if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("Source fingerprint mismatch: " + entry["path"])
    print(json.dumps({"version": manifest["version"], "verified": True,
                      "files": len(expected_paths), "archive_sha256": EXPECTED}, indent=2))


if __name__ == "__main__":
    verify()
