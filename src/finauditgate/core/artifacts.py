"""Canonical serialization and append-only local artifact storage."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_once(path: Path, payload: bytes) -> None:
    """Create an artifact once; an existing different value is a conflict."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        if path.read_bytes() != payload:
            raise RuntimeError(f"append-only artifact conflict at {path.name}")
        return

    try:
        with os.fdopen(descriptor, "wb") as artifact:
            artifact.write(payload)
            artifact.flush()
            os.fsync(artifact.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
