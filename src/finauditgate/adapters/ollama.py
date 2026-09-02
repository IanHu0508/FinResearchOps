"""Loopback-only Ollama Adapter for the one frozen local Qwen route.

The Adapter does three things per attempt: confirm the frozen model tag is
installed, send the frozen request, and save the exact bytes that came back as
one content-addressed private trace.  It then hands the deterministic core an
untrusted proposal (or a failure code) plus the trace receipt.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from http.client import IncompleteRead
import json
from pathlib import Path
import time
from urllib import error, parse, request

from finauditgate.adapters.ollama_route import (
    GENERATION_CONFIG_SHA256,
    MAX_DOCUMENT_BYTES,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_TRACE_BYTES,
    MODEL_DIGEST,
    MODEL_ID,
    PROMPT_SHA256,
    PROVIDER,
    TOOL_SCHEMA_SHA256,
    chat_request,
)
from finauditgate.adapters.ollama_trace import (
    InspectedResponse,
    OllamaTraceCodecError,
    decode_strict_json_object,
    inspect_captured_response,
    normalized_model_digest,
)
from finauditgate.contracts import AuditTask
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.core.model_trace import TRACE_SCHEMA_VERSION
from finauditgate.ports.model import ModelExecution, ModelTraceReceipt
from finauditgate.private_storage import (
    PrivateWorkspaceAnchor,
    require_private_storage_root,
)


class OllamaAdapterError(RuntimeError):
    """A fail-closed local-runtime, protocol, or candidate error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _NoRedirectHandler(request.HTTPRedirectHandler):
    """Reject every redirect before urllib can contact the next origin."""

    def redirect_request(
        self,
        req: request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> request.Request | None:
        raise OllamaAdapterError("OLLAMA_REDIRECT_FORBIDDEN")


def _open_no_redirect(
    http_request: request.Request,
    *,
    timeout: float,
) -> object:
    # An explicit empty ProxyHandler keeps environment proxies away from
    # loopback; the redirect handler refuses to contact any second origin.
    return request.build_opener(
        request.ProxyHandler({}),
        _NoRedirectHandler(),
    ).open(
        http_request,
        timeout=timeout,
    )


@dataclass(frozen=True, slots=True)
class CapturedExchange:
    """Exactly what was sent and what came back, before any parsing."""

    request_bytes: bytes
    response_bytes: bytes
    http_status: int
    response_capture_complete: bool
    elapsed_ns: int


def write_model_trace(
    trace_root: Path,
    *,
    task: AuditTask,
    attempt_index: int,
    exchange: CapturedExchange,
    inspected: InspectedResponse,
    runtime_version: str,
    observed_model_digest: str | None,
    transport_origin: str,
) -> ModelTraceReceipt:
    """Persist one exchange as a content-addressed trace; return its receipt."""

    request_sha256 = sha256_hex(exchange.request_bytes)
    response_sha256 = sha256_hex(exchange.response_bytes)
    parse_status = (
        "CANDIDATE_PARSED" if inspected.failure_code is None else "CANDIDATE_REJECTED"
    )
    mirror = {
        "provider": PROVIDER,
        "transport_origin": transport_origin,
        "runtime_version": runtime_version,
        "model_id": MODEL_ID,
        "observed_model_digest": normalized_model_digest(observed_model_digest),
        "prompt_sha256": PROMPT_SHA256,
        "tool_schema_sha256": TOOL_SCHEMA_SHA256,
        "generation_config_sha256": GENERATION_CONFIG_SHA256,
        "task_id": task.task_id,
        "attempt_index": attempt_index,
        "parse_status": parse_status,
        "failure_code": inspected.failure_code,
        "termination_reason": inspected.termination_reason,
        "response_derived_proposal_sha256": inspected.proposal_sha256,
        "request_sha256": request_sha256,
        "response_sha256": response_sha256,
    }
    trace_payload = {
        "schema_version": TRACE_SCHEMA_VERSION,
        **mirror,
        "request": json.loads(exchange.request_bytes),
        "raw_response_encoding": "BASE64",
        "raw_response_base64": base64.b64encode(
            exchange.response_bytes
        ).decode("ascii"),
        "http_status": exchange.http_status,
        "response_capture_complete": exchange.response_capture_complete,
        "elapsed_ns": exchange.elapsed_ns,
        "provider_metrics": inspected.provider_metrics,
    }
    trace_bytes = canonical_json_bytes(trace_payload)
    if len(trace_bytes) > MAX_TRACE_BYTES:
        raise OllamaAdapterError("MODEL_TRACE_BUDGET_EXCEEDED")
    trace_sha256 = sha256_hex(trace_bytes)
    raw_trace_ref = f"model-calls/sha256/{trace_sha256}.json"
    write_once(trace_root / raw_trace_ref, trace_bytes)
    return ModelTraceReceipt(
        **mirror,
        raw_trace_sha256=trace_sha256,
        raw_trace_ref=raw_trace_ref,
    )


class OllamaModelAdapter:
    """Generate untrusted candidates through the one frozen local Qwen route."""

    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:11434",
        timeout_seconds: float = 120.0,
        trace_root: Path | None = None,
        private_workspace_anchor: PrivateWorkspaceAnchor | None = None,
    ) -> None:
        if type(endpoint) is not str:
            raise TypeError("endpoint must be a string")
        parsed = parse.urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("endpoint must be an unauthenticated loopback HTTP origin")
        if type(timeout_seconds) not in {int, float}:
            raise TypeError("timeout_seconds must be a number")
        if not 0 < float(timeout_seconds) <= 300:
            raise ValueError("timeout_seconds must be in (0, 300]")
        self._endpoint = endpoint.rstrip("/")
        self._timeout_seconds = float(timeout_seconds)
        if trace_root is None:
            raise OllamaAdapterError("MODEL_TRACE_ROOT_REQUIRED")
        if not isinstance(trace_root, Path):
            raise TypeError("trace_root must be a pathlib.Path")
        self._trace_root = require_private_storage_root(
            trace_root,
            purpose="model-trace-root",
            anchor=private_workspace_anchor,
        )

    def propose(
        self,
        task: AuditTask,
        attempt_index: int = 0,
    ) -> ModelExecution:
        if type(task) is not AuditTask:
            raise TypeError("task must be an AuditTask")
        if type(attempt_index) is not int or attempt_index not in {0, 1}:
            raise ValueError("attempt_index must be 0 or 1")
        document = task.document.document_bytes
        if len(document) > MAX_DOCUMENT_BYTES:
            raise OllamaAdapterError("DOCUMENT_BUDGET_EXCEEDED")
        try:
            document.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OllamaAdapterError("DOCUMENT_NOT_UTF8") from exc

        runtime_version = self._observe_runtime_version()
        observed_model_digest = self._require_frozen_model_tag()
        exchange = self._capture_http_exchange(
            "/api/chat",
            chat_request(task, attempt_index),
        )
        inspected = inspect_captured_response(
            exchange.response_bytes,
            http_status=exchange.http_status,
            capture_complete=exchange.response_capture_complete,
            document=document,
            expected_model_id=MODEL_ID,
        )
        receipt = write_model_trace(
            self._trace_root,
            task=task,
            attempt_index=attempt_index,
            exchange=exchange,
            inspected=inspected,
            runtime_version=runtime_version,
            observed_model_digest=observed_model_digest,
            transport_origin=self._endpoint,
        )
        return ModelExecution(
            proposal=inspected.candidate,
            trace_receipt=receipt,
            failure_code=inspected.failure_code,
        )

    def _observe_runtime_version(self) -> str:
        version = self._request_json("/api/version").get("version")
        if type(version) is not str or not version:
            raise OllamaAdapterError("OLLAMA_RESPONSE_SHAPE_INVALID")
        return version

    def _require_frozen_model_tag(self) -> str:
        """The installed tag must carry the frozen digest before any chat."""

        models = self._request_json("/api/tags").get("models")
        if type(models) is not list:
            raise OllamaAdapterError("MODEL_REGISTRY_SHAPE_INVALID")
        matching = [
            model
            for model in models
            if isinstance(model, dict) and model.get("name") == MODEL_ID
        ]
        if len(matching) != 1:
            raise OllamaAdapterError("MODEL_IDENTITY_MISSING")
        digest = normalized_model_digest(matching[0].get("digest"))
        if digest != MODEL_DIGEST:
            raise OllamaAdapterError("MODEL_IDENTITY_MISMATCH")
        return digest

    def _request_json(self, path: str) -> dict[str, object]:
        exchange = self._capture_http_exchange(path, None)
        if exchange.http_status >= 400:
            raise OllamaAdapterError("OLLAMA_REQUEST_FAILED")
        if not exchange.response_capture_complete:
            raise OllamaAdapterError("OLLAMA_RESPONSE_INCOMPLETE")
        try:
            return decode_strict_json_object(exchange.response_bytes).payload
        except OllamaTraceCodecError as exc:
            raise OllamaAdapterError(exc.code) from exc

    def _capture_http_exchange(
        self,
        path: str,
        payload: dict[str, object] | None,
    ) -> CapturedExchange:
        data = None
        method = "GET"
        if payload is not None:
            data = canonical_json_bytes(payload)
            if len(data) > MAX_REQUEST_BYTES:
                raise OllamaAdapterError("OLLAMA_REQUEST_TOO_LARGE")
            method = "POST"
        http_request = request.Request(
            self._endpoint + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        started_ns = time.monotonic_ns()
        try:
            with _open_no_redirect(
                http_request,
                timeout=self._timeout_seconds,
            ) as response:
                final_url = (
                    response.geturl()
                    if hasattr(response, "geturl")
                    else http_request.full_url
                )
                if final_url != http_request.full_url:
                    raise OllamaAdapterError("OLLAMA_REDIRECT_FORBIDDEN")
                http_status = getattr(response, "status", 200)
                raw, complete = _bounded_read(response)
        except error.HTTPError as exc:
            if exc.geturl() != http_request.full_url:
                raise OllamaAdapterError("OLLAMA_REDIRECT_FORBIDDEN") from exc
            http_status = exc.code
            try:
                raw, complete = _bounded_read(exc)
            except OSError as read_exc:
                raise OllamaAdapterError("OLLAMA_REQUEST_FAILED") from read_exc
        except (error.URLError, OSError, TimeoutError) as exc:
            raise OllamaAdapterError("OLLAMA_REQUEST_FAILED") from exc
        if type(http_status) is not int or not 100 <= http_status <= 599:
            raise OllamaAdapterError("OLLAMA_RESPONSE_SHAPE_INVALID")
        return CapturedExchange(
            request_bytes=b"" if data is None else data,
            response_bytes=raw,
            http_status=http_status,
            response_capture_complete=complete,
            elapsed_ns=time.monotonic_ns() - started_ns,
        )


def _bounded_read(response: object) -> tuple[bytes, bool]:
    """Read at most the response budget plus one byte; keep partial bodies."""

    try:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except IncompleteRead as exc:
        if type(exc.partial) is not bytes:
            raise OllamaAdapterError("OLLAMA_REQUEST_FAILED") from exc
        return exc.partial[: MAX_RESPONSE_BYTES + 1], False
    if type(raw) is not bytes:
        raise OllamaAdapterError("OLLAMA_RESPONSE_SHAPE_INVALID")
    return raw, len(raw) <= MAX_RESPONSE_BYTES
