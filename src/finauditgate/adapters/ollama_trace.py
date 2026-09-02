"""Pure response codec shared by the online Adapter and the offline verifier.

Given the exact bytes that came back from the daemon, `inspect_captured_response`
derives one of: a candidate proposal, or a stable failure code.  The Adapter
uses it online; `finauditgate.core.model_trace` repeats it from the saved trace,
so a trace can never claim a proposal its own bytes do not produce.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math

from finauditgate.adapters.ollama_contract import (
    CANDIDATE_TOOL_CONTRACT,
    TOOL_NAME,
    ToolContractError,
)
from finauditgate.adapters.ollama_route import MAX_RESPONSE_BYTES
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.ports.model import ModelCandidate


PROVIDER_METRIC_FIELDS = (
    "total_duration",
    "load_duration",
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
)


class OllamaTraceCodecError(ValueError):
    """The raw provider response cannot prove one canonical proposal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DecodedOllamaProposal:
    candidate: ModelCandidate
    proposal_sha256: str


@dataclass(frozen=True, slots=True)
class DecodedStrictJSON:
    """One BOM-free UTF-8 JSON object reconstructed from captured bytes."""

    payload: dict[str, object]
    response_text: str


@dataclass(frozen=True, slots=True)
class InspectedResponse:
    """What one captured exchange proves: a proposal or a failure code."""

    failure_code: str | None
    candidate: ModelCandidate | None
    proposal_sha256: str | None
    provider_metrics: dict[str, int | None] | None
    termination_reason: str


def decode_strict_json_object(response_bytes: bytes) -> DecodedStrictJSON:
    """Reject encoding autodetection and every non-finite JSON number."""

    if type(response_bytes) is not bytes:
        raise TypeError("response_bytes must be immutable bytes")
    try:
        response_text = response_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OllamaTraceCodecError("OLLAMA_RESPONSE_NOT_UTF8") from exc
    if response_text.startswith("\ufeff"):
        raise OllamaTraceCodecError("OLLAMA_RESPONSE_NOT_UTF8")
    try:
        payload = json.loads(
            response_text,
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_float,
        )
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise OllamaTraceCodecError("OLLAMA_RESPONSE_NOT_JSON") from exc
    if type(payload) is not dict:
        raise OllamaTraceCodecError("OLLAMA_RESPONSE_SHAPE_INVALID")
    return DecodedStrictJSON(payload=payload, response_text=response_text)


def normalized_provider_metrics(
    response: dict[str, object],
) -> dict[str, int | None]:
    """Copy only exact, non-negative Ollama duration/count scalars."""

    metrics: dict[str, int | None] = {}
    for field_name in PROVIDER_METRIC_FIELDS:
        value = response.get(field_name)
        if value is not None and (
            type(value) is not int or not 0 <= value <= (2**63 - 1)
        ):
            raise OllamaTraceCodecError("OLLAMA_RESPONSE_METRICS_INVALID")
        metrics[field_name] = value
    return metrics


def normalized_termination_reason(response: dict[str, object]) -> str:
    """Keep only the two chat termination values used by this route."""

    value = response.get("done_reason")
    return value if value in {"stop", "length"} else "MISSING"


def normalized_model_digest(value: object) -> str | None:
    """Return only one trace-safe lowercase SHA-256 provider identity."""

    if type(value) is not str or len(value) != 64:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if any(character not in "0123456789abcdef" for character in value):
        return None
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant rejected: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number rejected")
    return parsed


def decode_ollama_tool_response(
    response: object,
    document: bytes,
    *,
    expected_model_id: str,
) -> DecodedOllamaProposal:
    """Decode the saved response without a model, network, or mutable tag."""

    if type(response) is not dict:
        raise OllamaTraceCodecError("OLLAMA_RESPONSE_SHAPE_INVALID")
    if (
        response.get("model") != expected_model_id
        or response.get("done") is not True
        or response.get("done_reason") != "stop"
    ):
        raise OllamaTraceCodecError("OLLAMA_RESPONSE_INCOMPLETE")
    message = response.get("message")
    if type(message) is not dict:
        raise OllamaTraceCodecError("OLLAMA_MESSAGE_SHAPE_INVALID")
    tool_calls = message.get("tool_calls")
    if type(tool_calls) is not list or len(tool_calls) != 1:
        raise OllamaTraceCodecError("TOOL_CALL_COUNT_INVALID")
    tool_call = tool_calls[0]
    if type(tool_call) is not dict:
        raise OllamaTraceCodecError("TOOL_CALL_SHAPE_INVALID")
    function = tool_call.get("function")
    if type(function) is not dict or function.get("name") != TOOL_NAME:
        raise OllamaTraceCodecError("TOOL_NAME_NOT_ALLOWLISTED")
    arguments = function.get("arguments")
    try:
        candidate = CANDIDATE_TOOL_CONTRACT.decode(arguments, document)
    except ToolContractError as exc:
        raise OllamaTraceCodecError(exc.code) from exc
    return DecodedOllamaProposal(
        candidate=candidate,
        proposal_sha256=_proposal_sha256(arguments),
    )


def candidate_proposal_sha256(
    candidate: ModelCandidate,
    document: bytes,
) -> str:
    """Return the codec identity of a candidate reconstructed from bytes."""

    try:
        arguments = CANDIDATE_TOOL_CONTRACT.encode(candidate, document)
    except ToolContractError as exc:
        raise OllamaTraceCodecError(exc.code) from exc
    return _proposal_sha256(arguments)


def inspect_captured_response(
    response_bytes: bytes,
    *,
    http_status: int,
    capture_complete: bool,
    document: bytes,
    expected_model_id: str,
) -> InspectedResponse:
    """Derive the one proposal-or-failure that these exact bytes support."""

    if http_status >= 400:
        return InspectedResponse("OLLAMA_REQUEST_FAILED", None, None, None, "MISSING")
    if not capture_complete:
        code = (
            "OLLAMA_RESPONSE_TOO_LARGE"
            if len(response_bytes) > MAX_RESPONSE_BYTES
            else "OLLAMA_RESPONSE_INCOMPLETE"
        )
        return InspectedResponse(code, None, None, None, "MISSING")
    try:
        decoded = decode_strict_json_object(response_bytes)
        provider_metrics = normalized_provider_metrics(decoded.payload)
    except OllamaTraceCodecError as exc:
        return InspectedResponse(exc.code, None, None, None, "MISSING")
    termination_reason = normalized_termination_reason(decoded.payload)
    try:
        proposal = decode_ollama_tool_response(
            decoded.payload,
            document,
            expected_model_id=expected_model_id,
        )
    except OllamaTraceCodecError as exc:
        return InspectedResponse(
            exc.code,
            None,
            None,
            provider_metrics,
            termination_reason,
        )
    return InspectedResponse(
        None,
        proposal.candidate,
        proposal.proposal_sha256,
        provider_metrics,
        termination_reason,
    )


def _proposal_sha256(arguments: dict[str, object]) -> str:
    try:
        return sha256_hex(canonical_json_bytes(arguments))
    except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise OllamaTraceCodecError("TOOL_ARGUMENT_SHAPE_INVALID") from exc
