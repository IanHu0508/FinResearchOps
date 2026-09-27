"""Acquire once before research, or reuse an already frozen acquisition."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from .thesis_protocol import validate_sources


SCHEMA = "finresearchops.source-acquisition/v1"


def _digest(value):
    return sha256_hex(canonical_json_bytes(value))


def _read(path, maximum=2_000_000):
    raw = path.read_bytes()
    if len(raw) > maximum:
        raise ValueError("THESIS_ACQUISITION_FILE_TOO_LARGE")
    return json.loads(raw)


def _check_merge(base, merged):
    request = {"symbol": base["symbol"], "as_of": base["as_of"]}
    validate_sources(base, request)
    validate_sources(merged, request)
    if (set(base) != set(merged)
            or any(merged[k] != base[k] for k in base if k != "sources")
            or merged["sources"][:len(base["sources"])] != base["sources"]):
        raise ValueError("THESIS_ACQUISITION_BASE_CHANGED")


def prepare_sources(base_bundle, output_root, *, resume_from=None, collector=None):
    """Return the merged bundle and receipt path; never collect on resume."""
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    supplied = deepcopy(base_bundle)
    if resume_from is not None:
        parent = Path(resume_from)
        receipt_path = parent / "source-acquisition.json"
        if not receipt_path.is_file():
            raise ValueError("THESIS_RESUME_ACQUISITION_REQUIRED")
        receipt_raw = receipt_path.read_bytes()
        if len(receipt_raw) > 2_000_000:
            raise ValueError("THESIS_ACQUISITION_FILE_TOO_LARGE")
        receipt = json.loads(receipt_raw)
        if receipt.get("schema_version") != SCHEMA:
            raise ValueError("THESIS_ACQUISITION_VERSION_INVALID")
        base, merged = _read(parent / "source-base.json"), _read(parent / "source-bundle.json")
        _check_merge(base, merged)
        if (_digest(base) != receipt["base_bundle_sha256"]
                or _digest(merged) != receipt["merged_bundle_sha256"]
                or _digest(supplied) not in {_digest(base), _digest(merged)}):
            raise ValueError("THESIS_RESUME_ACQUISITION_MISMATCH")
        request_path = parent / "request.json"
        if request_path.exists() and _read(request_path)["sources"] != merged:
            raise ValueError("THESIS_RESUME_ACQUISITION_MISMATCH")
        receipt = {**receipt, "mode": "REUSED", "parent_execution": str(parent),
            "parent_receipt_sha256": sha256_hex(receipt_raw), "network_requests_this_execution": 0,
            "resumed_at": datetime.now(timezone.utc).isoformat()}
    else:
        base = supplied
        if collector is None:
            from .public_research_sources import collect_sources
            collector = collect_sources
        offered = deepcopy(base)
        result = collector(offered, root / "source-responses")
        if offered != base:
            raise ValueError("THESIS_ACQUISITION_BASE_CHANGED")
        if (not isinstance(result, dict) or not isinstance(result.get("rows"), list)
                or not isinstance(result.get("channels"), dict)
                or set(result["channels"]) != {"news", "social"}
                or not isinstance(result.get("requests"), list)):
            raise ValueError("THESIS_ACQUISITION_RESULT_INVALID")
        merged = {**deepcopy(base), "sources": [*deepcopy(base["sources"]), *deepcopy(result["rows"]) ]}
        _check_merge(base, merged)
        receipt = {"schema_version": SCHEMA, "mode": "FETCHED",
            "status": "COMPLETED" if all(result["channels"][k].get("count", 0) > 0 for k in ("news", "social")) else "PARTIAL",
            "base_bundle_sha256": _digest(base), "merged_bundle_sha256": _digest(merged),
            "base_source_count": len(base["sources"]),
            "added_source_ids": [r["id"] for r in result["rows"]],
            "collection": {k: deepcopy(v) for k, v in result.items() if k != "rows"},
            "network_requests_this_execution": len(result["requests"]),
            "created_at": datetime.now(timezone.utc).isoformat()}
    receipt.update(base_bundle_file="source-base.json", frozen_bundle_file="source-bundle.json")
    write_once(root / "source-base.json", canonical_json_bytes(base))
    write_once(root / "source-bundle.json", canonical_json_bytes(merged))
    path = root / "source-acquisition.json"
    write_once(path, canonical_json_bytes(receipt))
    return merged, path


def model_resume_root(resume_from, request, merged):
    """An acquisition-only interruption may start modeling, without re-fetching."""
    if resume_from is None:
        return None
    root = Path(resume_from)
    if (root / "runtime-receipt.json").exists():
        return root
    # A copied acquisition may inherit paid calls from an earlier execution.
    # Without its runtime checkpoint it cannot become a fresh model chain.
    if _read(root / "source-acquisition.json").get("mode") != "FETCHED":
        raise ValueError("THESIS_RESUME_RUNTIME_REQUIRED")
    saved = _read(root / "request.json") if (root / "request.json").exists() else None
    if saved is not None and saved != {"request": request, "sources": merged}:
        raise ValueError("THESIS_RESUME_INPUT_MISMATCH")
    traces = root / "model-traces"
    if ((traces.exists() and any(traces.iterdir())) or (root / "main-result.json").exists()):
        raise ValueError("THESIS_RESUME_RUNTIME_REQUIRED")
    return None
