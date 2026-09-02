"""Offline verification of one content-addressed local-model exchange.

A trace is the exact request and response bytes of one model call, saved once
under the private trace root.  Verification reads only those bytes: it checks
that the request is exactly the frozen route for this task and attempt, that
the response bytes hash as recorded, and that the same bytes still produce the
proposal (or the failure) the run claims.  It never calls a model or network.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
from pathlib import Path

from finauditgate.adapters.ollama_route import (
    GENERATION_CONFIG_SHA256,
    MAX_TRACE_BYTES,
    MODEL_ID,
    PROMPT_SHA256,
    PROVIDER,
    TOOL_SCHEMA_SHA256,
    chat_request,
)
from finauditgate.adapters.ollama_trace import (
    OllamaTraceCodecError,
    candidate_proposal_sha256,
    inspect_captured_response,
)
from finauditgate.contracts import AuditTask
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.ports.model import (
    RECEIPT_SCHEMA_VERSION,
    ModelCandidate,
    ModelTraceReceipt,
)


TRACE_SCHEMA_VERSION = "finauditgate.model-call-trace/v6"
TRACE_FIELDS = {
    "schema_version",
    "provider",
    "transport_origin",
    "runtime_version",
    "model_id",
    "observed_model_digest",
    "prompt_sha256",
    "tool_schema_sha256",
    "generation_config_sha256",
    "task_id",
    "attempt_index",
    "parse_status",
    "failure_code",
    "termination_reason",
    "response_derived_proposal_sha256",
    "request_sha256",
    "response_sha256",
    "request",
    "raw_response_encoding",
    "raw_response_base64",
    "http_status",
    "response_capture_complete",
    "elapsed_ns",
    "provider_metrics",
}
RECEIPT_MIRROR_FIELDS = TRACE_FIELDS - {
    "schema_version",
    "request",
    "raw_response_encoding",
    "raw_response_base64",
    "http_status",
    "response_capture_complete",
    "elapsed_ns",
    "provider_metrics",
}
RECEIPT_FIELDS = RECEIPT_MIRROR_FIELDS | {
    "schema_version",
    "raw_trace_sha256",
    "raw_trace_ref",
}


@dataclass(frozen=True, slots=True)
class VerifiedRawModelTrace:
    """A raw exchange whose request, response, and proposal agree."""

    receipt: ModelTraceReceipt
    response_candidate: ModelCandidate | None


def receipt_payload(receipt: ModelTraceReceipt) -> dict[str, object]:
    if type(receipt) is not ModelTraceReceipt:
        raise TypeError("trace receipt must be a ModelTraceReceipt")
    return {name: getattr(receipt, name) for name in RECEIPT_FIELDS}


def receipt_from_payload(payload: object) -> ModelTraceReceipt:
    if type(payload) is not dict or set(payload) != RECEIPT_FIELDS:
        raise ValueError("model trace receipt shape is invalid")
    return ModelTraceReceipt(**payload)


def verify_raw_model_trace(
    trace_root: Path | None,
    receipt_value: ModelTraceReceipt | dict[str, object],
    *,
    task: AuditTask,
    attempt_index: int,
    expected_proposal: ModelCandidate | None,
    expected_failure_code: str | None,
) -> tuple[VerifiedRawModelTrace | None, str | None]:
    """Verify one trace from private bytes only; never call a model/network."""

    if trace_root is None:
        return None, "MODEL_TRACE_ROOT_REQUIRED"
    try:
        receipt = (
            receipt_value
            if type(receipt_value) is ModelTraceReceipt
            else receipt_from_payload(receipt_value)
        )
    except (TypeError, ValueError):
        return None, "MODEL_TRACE_RECEIPT_INVALID"
    if receipt.task_id != task.task_id or receipt.attempt_index != attempt_index:
        return None, "MODEL_TRACE_CROSS_RUN_MISMATCH"

    payload, read_failure = _read_trace(trace_root, receipt)
    if read_failure is not None:
        return None, read_failure
    assert payload is not None
    for field_name in RECEIPT_MIRROR_FIELDS:
        if payload[field_name] != getattr(receipt, field_name):
            return None, "MODEL_TRACE_RECEIPT_MISMATCH"
    if (
        type(payload["request"]) is not dict
        or payload["raw_response_encoding"] != "BASE64"
        or type(payload["raw_response_base64"]) is not str
        or type(payload["http_status"]) is not int
        or not 100 <= payload["http_status"] <= 599
        or type(payload["response_capture_complete"]) is not bool
        or type(payload["elapsed_ns"]) is not int
        or payload["elapsed_ns"] < 0
        or type(payload["provider_metrics"]) not in {dict, type(None)}
    ):
        return None, "MODEL_TRACE_STRUCTURE_INVALID"

    if sha256_hex(canonical_json_bytes(payload["request"])) != receipt.request_sha256:
        return None, "MODEL_TRACE_REQUEST_HASH_MISMATCH"
    if not _request_matches_frozen_route(payload["request"], receipt, task, attempt_index):
        return None, "MODEL_TRACE_REQUEST_MISMATCH"

    try:
        response_bytes = base64.b64decode(
            payload["raw_response_base64"],
            validate=True,
        )
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return None, "MODEL_TRACE_STRUCTURE_INVALID"
    if sha256_hex(response_bytes) != receipt.response_sha256:
        return None, "MODEL_TRACE_RESPONSE_HASH_MISMATCH"

    inspected = inspect_captured_response(
        response_bytes,
        http_status=payload["http_status"],
        capture_complete=payload["response_capture_complete"],
        document=task.document.document_bytes,
        expected_model_id=receipt.model_id,
    )
    if (
        payload["provider_metrics"] != inspected.provider_metrics
        or receipt.termination_reason != inspected.termination_reason
    ):
        return None, "MODEL_TRACE_RESPONSE_MISMATCH"

    if expected_failure_code is None:
        if (
            receipt.parse_status != "CANDIDATE_PARSED"
            or inspected.candidate is None
            or expected_proposal is None
            or receipt.response_derived_proposal_sha256
            != inspected.proposal_sha256
        ):
            return None, "MODEL_TRACE_PROPOSAL_MISMATCH"
        try:
            expected_sha256 = candidate_proposal_sha256(
                expected_proposal,
                task.document.document_bytes,
            )
        except OllamaTraceCodecError:
            return None, "MODEL_TRACE_PROPOSAL_MISMATCH"
        if expected_sha256 != inspected.proposal_sha256:
            return None, "MODEL_TRACE_PROPOSAL_MISMATCH"
        return VerifiedRawModelTrace(receipt, inspected.candidate), None

    if (
        expected_proposal is not None
        or receipt.parse_status != "CANDIDATE_REJECTED"
        or receipt.failure_code != expected_failure_code
        or inspected.failure_code != expected_failure_code
    ):
        return None, "MODEL_TRACE_REJECTION_MISMATCH"
    return VerifiedRawModelTrace(receipt, None), None


def _read_trace(
    trace_root: Path,
    receipt: ModelTraceReceipt,
) -> tuple[dict[str, object] | None, str | None]:
    trace_path = trace_root / receipt.raw_trace_ref
    try:
        resolved_trace = trace_path.resolve(strict=True)
    except OSError:
        return None, "MODEL_TRACE_MISSING"
    if not resolved_trace.is_relative_to(trace_root):
        return None, "MODEL_TRACE_PATH_INVALID"
    current = trace_root
    for part in Path(receipt.raw_trace_ref).parts:
        current = current / part
        if current.is_symlink():
            return None, "MODEL_TRACE_PATH_INVALID"
    try:
        with resolved_trace.open("rb") as trace_file:
            raw_trace = trace_file.read(MAX_TRACE_BYTES + 1)
    except OSError:
        return None, "MODEL_TRACE_MISSING"
    if len(raw_trace) > MAX_TRACE_BYTES:
        return None, "MODEL_TRACE_STRUCTURE_INVALID"
    if sha256_hex(raw_trace) != receipt.raw_trace_sha256:
        return None, "MODEL_TRACE_HASH_MISMATCH"
    try:
        payload = json.loads(raw_trace)
        if canonical_json_bytes(payload) != raw_trace:
            return None, "MODEL_TRACE_STRUCTURE_INVALID"
    except (RecursionError, TypeError, UnicodeError, ValueError):
        return None, "MODEL_TRACE_STRUCTURE_INVALID"
    if (
        type(payload) is not dict
        or set(payload) != TRACE_FIELDS
        or payload["schema_version"] != TRACE_SCHEMA_VERSION
    ):
        return None, "MODEL_TRACE_STRUCTURE_INVALID"
    return payload, None


def _request_matches_frozen_route(
    request: dict[str, object],
    receipt: ModelTraceReceipt,
    task: AuditTask,
    attempt_index: int,
) -> bool:
    if (
        receipt.provider != PROVIDER
        or receipt.model_id != MODEL_ID
        or receipt.prompt_sha256 != PROMPT_SHA256
        or receipt.tool_schema_sha256 != TOOL_SCHEMA_SHA256
        or receipt.generation_config_sha256 != GENERATION_CONFIG_SHA256
    ):
        return False
    try:
        expected = chat_request(task, attempt_index)
    except UnicodeDecodeError:
        return False
    return request == expected
