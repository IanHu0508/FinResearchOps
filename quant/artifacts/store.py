"""Private, append-once JSON artifacts. No pickle or implicit model execution."""

import hashlib
import json
from pathlib import Path
import re

from quant.contracts import canonical, require


def _private_root(root):
    root = Path(root).expanduser().resolve()
    matches = [p for p in (root, *root.parents) if p.name == "private"
               and (p.parent / "finaudit-gate" / "pyproject.toml").is_file()]
    require(bool(matches) and root != matches[0], "QUANT_OUTPUT_REQUIRES_SIBLING_PRIVATE_DIRECTORY")
    require(not any((p / ".git").exists() for p in (root, *root.parents)
                    if p == matches[0] or matches[0] in p.parents), "QUANT_OUTPUT_INSIDE_GIT")
    return root


def _name(name):
    require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9-]*\.json", name)
            and name != "manifest.json", "ARTIFACT_FILENAME_INVALID")


def write_run(root, documents, *, data_kind, dataset_id):
    root = _private_root(root)
    require(data_kind in ("SYNTHETIC", "REAL_DATA"), "ARTIFACT_DATA_KIND_INVALID")
    require(isinstance(dataset_id, str) and re.fullmatch(r"[0-9a-f]{64}", dataset_id),
            "ARTIFACT_DATASET_ID_INVALID")
    require(bool(documents), "ARTIFACTS_EMPTY")
    encoded = {}
    for name, document in documents.items():
        _name(name)
        require(isinstance(document, dict) and isinstance(document.get("schema_version"), str)
                and re.fullmatch(r"[a-z0-9.-]+/v[1-9][0-9]*", document["schema_version"]),
                "ARTIFACT_SCHEMA_VERSION_REQUIRED")
        encoded[name] = (canonical(document) + "\n").encode()
    # Validate all documents before making a new run directory. Never overwrite.
    root.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name, content in encoded.items():
        with (root / name).open("xb") as stream:
            stream.write(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    manifest = {"schema_version": "quant.run-manifest/v1", "data_kind": data_kind,
                "dataset_id": dataset_id, "artifacts": hashes,
                "data_audit_status": "NOT_STARTED", "investment_effectiveness": "NOT_STARTED"}
    with (root / "manifest.json").open("x", encoding="utf-8") as stream:
        stream.write(canonical(manifest) + "\n")
    return manifest


def read_run(root):
    root = _private_root(root)
    manifest_path = root / "manifest.json"
    require(not manifest_path.is_symlink() and manifest_path.resolve().parent == root,
            "MANIFEST_PATH_INVALID")
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get("schema_version") == "quant.run-manifest/v1", "RUN_MANIFEST_VERSION_INVALID")
    result = {}
    for name, expected in manifest["artifacts"].items():
        _name(name)
        path = root / name
        require(path.resolve().parent == root and not path.is_symlink(), "ARTIFACT_PATH_INVALID")
        content = path.read_bytes()
        require(hashlib.sha256(content).hexdigest() == expected, "ARTIFACT_CONTENT_MISMATCH")
        result[name] = json.loads(content)
    return manifest, result
