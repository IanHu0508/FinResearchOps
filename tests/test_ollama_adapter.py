import base64
from dataclasses import replace
from datetime import date
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import error as urllib_error

from finauditgate import AuditTask, Decision, FinAuditGate, FrozenDocumentPackage
from finauditgate.adapters import ollama_route
from finauditgate.adapters.ollama import OllamaAdapterError, OllamaModelAdapter
from finauditgate.adapters.ollama_route import (
    MAX_RESPONSE_BYTES,
    MODEL_DIGEST,
    MODEL_ID,
    chat_request,
)
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.core.model_trace import TRACE_SCHEMA_VERSION, verify_raw_model_trace
from finauditgate.ports.model import ModelExecution


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "synthetic"
    / "aurora_revenue_growth_m2.txt"
)
MODEL_ROUTE_MANIFEST_PATH = (
    Path(__file__).parents[1]
    / "manifests"
    / "examples"
    / "qwen3_4b_ollama_route.json"
)
PRIOR = (
    "metric=Net sales;basis=Reported;period=Year ended 2024-12-31;"
    "value=125.00;currency=US dollar;unit=Monetary;scale=Millions;"
    "sign=Positive"
)
CURRENT = (
    "metric=Total revenue;basis=IFRS reported;period=FY 2025;"
    "value=150.00;currency=USD;unit=Currency amount;scale=Million;"
    "sign=As presented"
)


class _JSONResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_JSONResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            return self._payload
        return self._payload[:amount]


class _RawResponse(_JSONResponse):
    def __init__(self, payload: bytes) -> None:
        self._payload = payload


class _IncompleteReadResponse(_RawResponse):
    def read(self, amount: int = -1) -> bytes:
        del amount
        raise IncompleteRead(self._payload, expected=200)

    def close(self) -> None:
        return None


def _task(document: bytes) -> AuditTask:
    return AuditTask(
        task_id="local-adapter-valid-tool-call",
        question="What was Aurora Devices FY2025 revenue growth versus FY2024?",
        cutoff=date(2026, 3, 1),
        document=FrozenDocumentPackage(
            source_id="synthetic-aurora-revenue-growth-v2",
            document_name="aurora_revenue_growth_m2.txt",
            document_bytes=document,
            declared_published_at=date(2026, 2, 15),
        ),
    )


def _private_trace_root(temporary_directory: str) -> Path:
    workspace = Path(temporary_directory)
    (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
    (workspace / "private").mkdir()
    return workspace / "private" / "model-traces"


def _chat_payload(
    *,
    done_reason: str = "stop",
    operation: str = "growth_rate_percent",
    response_model: str = MODEL_ID,
) -> dict[str, object]:
    return {
        "model": response_model,
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "propose_financial_candidate",
                        "arguments": {
                            "evidence": [
                                {
                                    "evidence_id": "comparison",
                                    "exact_span": PRIOR,
                                    "metric": "revenue",
                                    "metric_basis": "REPORTED",
                                    "period": "FY2024",
                                    "value": "125.00",
                                    "currency": "USD",
                                    "unit": "MONETARY",
                                    "scale": "MILLION",
                                    "sign": "POSITIVE",
                                },
                                {
                                    "evidence_id": "current",
                                    "exact_span": CURRENT,
                                    "metric": "revenue",
                                    "metric_basis": "REPORTED",
                                    "period": "FY2025",
                                    "value": "150.00",
                                    "currency": "USD",
                                    "unit": "MONETARY",
                                    "scale": "MILLION",
                                    "sign": "POSITIVE",
                                },
                            ],
                            "calculation": {
                                "operation": operation,
                                "operand_ids": ["current", "comparison"],
                                "output_unit": "PERCENT",
                                "quantize": "0.01",
                            },
                        },
                    }
                }
            ],
        },
        "done": True,
        "done_reason": done_reason,
        "total_duration": 2_000_000,
        "load_duration": 1_000_000,
        "prompt_eval_count": 500,
        "eval_count": 100,
    }


def _responses(
    *,
    done_reason: str = "stop",
    operation: str = "growth_rate_percent",
    response_model: str = MODEL_ID,
    version: str = "0.33.1",
    tag_digest: str = MODEL_DIGEST,
    chat: object | None = None,
    models: list[object] | None = None,
) -> tuple[object, ...]:
    """One attempt on the wire: /api/version, /api/tags, /api/chat."""

    if models is None:
        models = [
            {
                "name": MODEL_ID,
                "model": MODEL_ID,
                "size": 2_620_788_260,
                "digest": tag_digest,
                "details": {
                    "format": "gguf",
                    "family": "qwen3",
                    "parameter_size": "4.0B",
                    "quantization_level": "Q4_K_M",
                    "context_length": 40_960,
                },
                "capabilities": ["completion", "tools", "thinking"],
            }
        ]
    return (
        _JSONResponse({"version": version}),
        _JSONResponse({"models": models}),
        chat
        if chat is not None
        else _JSONResponse(
            _chat_payload(
                done_reason=done_reason,
                operation=operation,
                response_model=response_model,
            )
        ),
    )


def _rewrite_trace(
    trace_root: Path,
    execution: ModelExecution,
    mutate,
) -> ModelExecution:
    receipt = execution.trace_receipt
    payload = json.loads((trace_root / receipt.raw_trace_ref).read_bytes())
    mutate(payload)
    payload["request_sha256"] = sha256_hex(canonical_json_bytes(payload["request"]))
    trace_bytes = canonical_json_bytes(payload)
    trace_sha256 = sha256_hex(trace_bytes)
    raw_trace_ref = f"model-calls/sha256/{trace_sha256}.json"
    (trace_root / raw_trace_ref).write_bytes(trace_bytes)
    return ModelExecution(
        proposal=execution.proposal,
        trace_receipt=replace(
            receipt,
            request_sha256=payload["request_sha256"],
            raw_trace_sha256=trace_sha256,
            raw_trace_ref=raw_trace_ref,
        ),
        failure_code=execution.failure_code,
    )


class OllamaModelAdapterTest(unittest.TestCase):
    def test_missing_private_trace_root_fails_before_any_request(self) -> None:
        document = FIXTURE_PATH.read_bytes()

        with patch(
            "finauditgate.adapters.ollama._open_no_redirect",
        ) as open_no_redirect, self.assertRaisesRegex(
            OllamaAdapterError,
            "MODEL_TRACE_ROOT_REQUIRED",
        ):
            OllamaModelAdapter(trace_root=None).propose(_task(document))

        open_no_redirect.assert_not_called()

    def test_schema_valid_tool_call_becomes_an_untrusted_candidate(self) -> None:
        document = FIXTURE_PATH.read_bytes()

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_responses(),
            ):
                execution = OllamaModelAdapter(
                    trace_root=_private_trace_root(temporary_directory),
                ).propose(_task(document))
        candidate = execution.proposal
        assert candidate is not None

        self.assertIsNone(execution.failure_code)
        self.assertEqual(2, len(candidate.evidence))
        self.assertEqual("comparison", candidate.evidence[0].evidence_id)
        self.assertEqual(document.index(PRIOR.encode()), candidate.evidence[0].byte_start)
        self.assertEqual("current", candidate.evidence[1].evidence_id)
        self.assertEqual(
            ("current", "comparison"),
            candidate.calculation.operand_ids,
        )

    def test_valid_call_writes_one_content_addressed_trace(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = _task(document)
        responses = _responses()
        chat_bytes = responses[2].read()

        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_root = _private_trace_root(temporary_directory)
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=responses,
            ):
                execution = OllamaModelAdapter(trace_root=trace_root).propose(task)
            trace_paths = list(trace_root.glob("model-calls/sha256/*.json"))
            trace = json.loads(trace_paths[0].read_bytes())
            verified, failure = verify_raw_model_trace(
                trace_root.resolve(),
                execution.trace_receipt,
                task=task,
                attempt_index=0,
                expected_proposal=execution.proposal,
                expected_failure_code=None,
            )

        self.assertEqual(1, len(trace_paths))
        self.assertEqual(execution.trace_receipt.raw_trace_sha256, trace_paths[0].stem)
        self.assertEqual(TRACE_SCHEMA_VERSION, trace["schema_version"])
        self.assertEqual("ollama", trace["provider"])
        self.assertEqual("0.33.1", trace["runtime_version"])
        self.assertEqual(MODEL_ID, trace["model_id"])
        self.assertEqual(MODEL_DIGEST, trace["observed_model_digest"])
        self.assertEqual("CANDIDATE_PARSED", trace["parse_status"])
        self.assertEqual(chat_request(task, 0), trace["request"])
        self.assertEqual(
            chat_bytes,
            base64.b64decode(trace["raw_response_base64"], validate=True),
        )
        self.assertEqual(
            {
                "total_duration": 2_000_000,
                "load_duration": 1_000_000,
                "prompt_eval_count": 500,
                "prompt_eval_duration": None,
                "eval_count": 100,
                "eval_duration": None,
            },
            trace["provider_metrics"],
        )
        self.assertIsNotNone(verified)
        self.assertIsNone(failure)
        with self.assertRaisesRegex(ValueError, "rejected trace must contain"):
            replace(execution.trace_receipt, parse_status="CANDIDATE_REJECTED")

    def test_shared_route_run_enters_the_gate_and_replays(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_root = _private_trace_root(temporary_directory)
            artifact_root = trace_root.parent / "artifacts" / "core"
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_responses(),
            ):
                outcome = FinAuditGate(
                    artifact_root=artifact_root,
                    model=OllamaModelAdapter(trace_root=trace_root),
                    model_trace_root=trace_root,
                ).run(_task(document))
            replay = FinAuditGate(
                artifact_root=artifact_root,
                model_trace_root=trace_root,
            ).replay(outcome.run_ref)
            manifest = json.loads(
                (artifact_root / "runs" / outcome.run_ref.run_id / "manifest.json").read_bytes()
            )

        self.assertIs(Decision.ACCEPT, outcome.decision)
        self.assertEqual("20.00", outcome.answer)
        self.assertEqual("finauditgate.run/v1", outcome.schema_version)
        self.assertIn("model_trace", manifest["artifacts"])
        self.assertTrue(replay.consistent, replay.reason)
        self.assertEqual("finauditgate.replay/v5", replay.schema_version)
        self.assertEqual(10, replay.verified_artifact_count)

    def test_rejected_tool_response_returns_a_bound_failure_receipt(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = _task(document)
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_root = _private_trace_root(temporary_directory)
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_responses(operation="percentage_change"),
            ):
                execution = OllamaModelAdapter(trace_root=trace_root).propose(task)
            trace_written = (
                trace_root / execution.trace_receipt.raw_trace_ref
            ).is_file()
            verified, failure = verify_raw_model_trace(
                trace_root.resolve(),
                execution.trace_receipt,
                task=task,
                attempt_index=0,
                expected_proposal=None,
                expected_failure_code="TOOL_ARGUMENT_NOT_ALLOWLISTED",
            )

        self.assertIsNone(execution.proposal)
        self.assertEqual("TOOL_ARGUMENT_NOT_ALLOWLISTED", execution.failure_code)
        self.assertEqual("CANDIDATE_REJECTED", execution.trace_receipt.parse_status)
        self.assertTrue(trace_written)
        self.assertIsNotNone(verified)
        self.assertIsNone(failure)

    def test_length_terminated_tool_call_fails_closed(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_responses(done_reason="length"),
            ):
                execution = OllamaModelAdapter(
                    trace_root=_private_trace_root(temporary_directory),
                ).propose(_task(document))

        self.assertIsNone(execution.proposal)
        self.assertEqual("OLLAMA_RESPONSE_INCOMPLETE", execution.failure_code)
        self.assertEqual("length", execution.trace_receipt.termination_reason)

    def test_chat_model_mismatch_is_rejected_and_verifies_offline(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = _task(document)
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_root = _private_trace_root(temporary_directory)
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_responses(response_model="unexpected-local-model"),
            ):
                execution = OllamaModelAdapter(trace_root=trace_root).propose(task)
            verified, failure = verify_raw_model_trace(
                trace_root.resolve(),
                execution.trace_receipt,
                task=task,
                attempt_index=0,
                expected_proposal=None,
                expected_failure_code="OLLAMA_RESPONSE_INCOMPLETE",
            )

        self.assertIsNone(execution.proposal)
        self.assertEqual("OLLAMA_RESPONSE_INCOMPLETE", execution.failure_code)
        self.assertIsNotNone(verified)
        self.assertIsNone(failure)

    def test_frozen_model_tag_is_required_before_any_chat(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        attacks = {
            "digest-mismatch": (_responses(tag_digest="f" * 64), "MODEL_IDENTITY_MISMATCH"),
            "tag-missing": (_responses(models=[]), "MODEL_IDENTITY_MISSING"),
        }
        for name, (responses, expected_code) in attacks.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary_directory:
                trace_root = _private_trace_root(temporary_directory)
                with patch(
                    "finauditgate.adapters.ollama._open_no_redirect",
                    side_effect=responses,
                ) as open_no_redirect, self.assertRaisesRegex(
                    OllamaAdapterError,
                    expected_code,
                ):
                    OllamaModelAdapter(trace_root=trace_root).propose(_task(document))
                self.assertEqual(2, open_no_redirect.call_count)
                self.assertEqual([], list(trace_root.glob("model-calls/sha256/*.json")))

    def test_runtime_version_is_recorded_not_gated(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_root = _private_trace_root(temporary_directory)
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_responses(version="9.9.9"),
            ):
                execution = OllamaModelAdapter(trace_root=trace_root).propose(_task(document))
            trace = json.loads(
                (trace_root / execution.trace_receipt.raw_trace_ref).read_bytes()
            )

        self.assertIsNotNone(execution.proposal)
        self.assertEqual("9.9.9", trace["runtime_version"])
        self.assertNotEqual(ollama_route.SMOKE_TESTED_RUNTIME_VERSION, "9.9.9")

    def test_negative_exchanges_preserve_bounded_raw_bytes_and_verify_offline(
        self,
    ) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = _task(document)
        chat_payload = _chat_payload()
        nonfinite = dict(chat_payload)
        nonfinite["total_duration"] = float("nan")
        invalid_metrics = dict(chat_payload)
        invalid_metrics["total_duration"] = 1.5
        oversized = b"x" * (MAX_RESPONSE_BYTES + 10)
        http_error_body = b'{"error":"temporarily unavailable"}'
        attacks = {
            "invalid-json": (
                _RawResponse(b"not-json\x00"),
                b"not-json\x00",
                "OLLAMA_RESPONSE_NOT_JSON",
                200,
                True,
            ),
            "non-object-json": (
                _RawResponse(b'["not","an","object"]'),
                b'["not","an","object"]',
                "OLLAMA_RESPONSE_SHAPE_INVALID",
                200,
                True,
            ),
            "utf-16": (
                _RawResponse(json.dumps(chat_payload).encode("utf-16")),
                json.dumps(chat_payload).encode("utf-16"),
                "OLLAMA_RESPONSE_NOT_UTF8",
                200,
                True,
            ),
            "non-finite": (
                _RawResponse(json.dumps(nonfinite).encode("utf-8")),
                json.dumps(nonfinite).encode("utf-8"),
                "OLLAMA_RESPONSE_NOT_JSON",
                200,
                True,
            ),
            "invalid-metrics": (
                _RawResponse(json.dumps(invalid_metrics).encode("utf-8")),
                json.dumps(invalid_metrics).encode("utf-8"),
                "OLLAMA_RESPONSE_METRICS_INVALID",
                200,
                True,
            ),
            "oversized": (
                _RawResponse(oversized),
                oversized[: MAX_RESPONSE_BYTES + 1],
                "OLLAMA_RESPONSE_TOO_LARGE",
                200,
                False,
            ),
            "incomplete": (
                _IncompleteReadResponse(b'{"model":"qwen3'),
                b'{"model":"qwen3',
                "OLLAMA_RESPONSE_INCOMPLETE",
                200,
                False,
            ),
            "http-error": (
                urllib_error.HTTPError(
                    "http://127.0.0.1:11434/api/chat",
                    503,
                    "Service Unavailable",
                    {},
                    BytesIO(http_error_body),
                ),
                http_error_body,
                "OLLAMA_REQUEST_FAILED",
                503,
                True,
            ),
        }

        for name, (
            response,
            expected_capture,
            expected_failure,
            expected_status,
            capture_complete,
        ) in attacks.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary_directory:
                trace_root = _private_trace_root(temporary_directory)
                with patch(
                    "finauditgate.adapters.ollama._open_no_redirect",
                    side_effect=_responses(chat=response),
                ):
                    execution = OllamaModelAdapter(trace_root=trace_root).propose(task)
                trace_paths = list(trace_root.glob("model-calls/sha256/*.json"))
                trace = json.loads(trace_paths[0].read_bytes())
                verified, failure = verify_raw_model_trace(
                    trace_root.resolve(),
                    execution.trace_receipt,
                    task=task,
                    attempt_index=0,
                    expected_proposal=None,
                    expected_failure_code=expected_failure,
                )

            self.assertIsNone(execution.proposal)
            self.assertEqual(expected_failure, execution.failure_code)
            self.assertEqual(1, len(trace_paths))
            self.assertEqual(
                expected_capture,
                base64.b64decode(trace["raw_response_base64"], validate=True),
            )
            self.assertEqual(sha256_hex(expected_capture), trace["response_sha256"])
            self.assertEqual(expected_status, trace["http_status"])
            self.assertIs(capture_complete, trace["response_capture_complete"])
            self.assertIsNotNone(verified, failure)
            self.assertIsNone(failure)

    def test_trace_tamper_and_receipt_mismatch_fail_closed(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = _task(document)
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_root = _private_trace_root(temporary_directory)
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_responses(chat=_RawResponse(b"not-json")),
            ):
                execution = OllamaModelAdapter(trace_root=trace_root).propose(task)
            receipt = execution.trace_receipt
            resolved_trace_root = trace_root.resolve()
            trace_path = resolved_trace_root / receipt.raw_trace_ref
            original_trace = trace_path.read_bytes()
            trace_path.write_bytes(original_trace + b"\n")
            _, tamper_failure = verify_raw_model_trace(
                resolved_trace_root,
                receipt,
                task=task,
                attempt_index=0,
                expected_proposal=None,
                expected_failure_code="OLLAMA_RESPONSE_NOT_JSON",
            )
            trace_path.write_bytes(original_trace)
            _, receipt_failure = verify_raw_model_trace(
                resolved_trace_root,
                replace(receipt, failure_code="OLLAMA_RESPONSE_SHAPE_INVALID"),
                task=task,
                attempt_index=0,
                expected_proposal=None,
                expected_failure_code="OLLAMA_RESPONSE_SHAPE_INVALID",
            )

        self.assertEqual("MODEL_TRACE_HASH_MISMATCH", tamper_failure)
        self.assertEqual("MODEL_TRACE_RECEIPT_MISMATCH", receipt_failure)

    def test_request_that_deviates_from_the_frozen_route_fails_offline(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = _task(document)

        def prompt(payload: dict[str, object]) -> None:
            payload["request"]["messages"][0]["content"] = "attacker system prompt"

        def tool(payload: dict[str, object]) -> None:
            payload["request"]["tools"][0]["attacker_extension"] = True

        def generation(payload: dict[str, object]) -> None:
            payload["request"]["options"] = {
                "num_ctx": 128,
                "num_predict": 7,
                "temperature": 99,
                "seed": 123,
            }

        def model(payload: dict[str, object]) -> None:
            payload["request"]["model"] = "attacker-model"

        attacks = {"prompt": prompt, "tool": tool, "generation": generation, "model": model}
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_root = _private_trace_root(temporary_directory)
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_responses(),
            ):
                execution = OllamaModelAdapter(trace_root=trace_root).propose(task)
            for name, mutate in attacks.items():
                with self.subTest(name=name):
                    rewritten = _rewrite_trace(trace_root, execution, mutate)
                    verified, failure = verify_raw_model_trace(
                        trace_root.resolve(),
                        rewritten.trace_receipt,
                        task=task,
                        attempt_index=0,
                        expected_proposal=rewritten.proposal,
                        expected_failure_code=None,
                    )
                    self.assertIsNone(verified)
                    self.assertEqual("MODEL_TRACE_REQUEST_MISMATCH", failure)

                    class RewrittenModel:
                        def propose(
                            self,
                            requested_task: AuditTask,
                            attempt_index: int = 0,
                        ) -> ModelExecution:
                            del requested_task, attempt_index
                            return rewritten

                    artifact_root = trace_root.parent / "artifacts" / name
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "MODEL_TRACE_REQUEST_MISMATCH",
                    ):
                        FinAuditGate(
                            artifact_root=artifact_root,
                            model=RewrittenModel(),
                            model_trace_root=trace_root,
                        ).run(task)
                    self.assertFalse((artifact_root / "runs").exists())

    def test_protocol_rejection_run_is_bounded_and_replays_offline(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        rejected_attempt = _responses(chat=_RawResponse(b"not-json"))
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_root = _private_trace_root(temporary_directory)
            artifact_root = trace_root.parent / "artifacts" / "core"
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=rejected_attempt + _responses(chat=_RawResponse(b"not-json")),
            ) as open_no_redirect:
                outcome = FinAuditGate(
                    artifact_root=artifact_root,
                    model=OllamaModelAdapter(trace_root=trace_root),
                    model_trace_root=trace_root,
                ).run(_task(document))
            replay = FinAuditGate(
                artifact_root=artifact_root,
                model_trace_root=trace_root,
            ).replay(outcome.run_ref)
            trace_count = len(list(trace_root.glob("model-calls/sha256/*.json")))

        self.assertIs(Decision.RETRY, outcome.decision)
        self.assertEqual(
            ("MODEL_CANDIDATE_REJECTED", "RETRY_BUDGET_EXHAUSTED"),
            outcome.reason_codes,
        )
        self.assertEqual(6, open_no_redirect.call_count)
        self.assertEqual(2, trace_count)
        self.assertTrue(replay.consistent, replay.reason)

    def test_every_attempt_rechecks_the_installed_tag(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            adapter = OllamaModelAdapter(
                trace_root=_private_trace_root(temporary_directory),
            )
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_responses() + _responses(),
            ) as open_no_redirect:
                adapter.propose(_task(document), attempt_index=0)
                adapter.propose(_task(document), attempt_index=1)

        self.assertEqual(6, open_no_redirect.call_count)

    def test_loopback_redirect_cannot_reach_a_second_origin(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        target_hits: list[str] = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                target_hits.append(self.path)
                body = json.dumps({"version": "0.33.1"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return None

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever)
        target_thread.start()

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{target.server_port}/captured",
                )
                self.end_headers()

            def log_message(self, *args: object) -> None:
                return None

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever)
        redirect_thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                with self.assertRaisesRegex(
                    OllamaAdapterError,
                    "OLLAMA_REDIRECT_FORBIDDEN",
                ):
                    OllamaModelAdapter(
                        endpoint=f"http://127.0.0.1:{redirect.server_port}",
                        timeout_seconds=2,
                        trace_root=_private_trace_root(temporary_directory),
                    ).propose(_task(document))
        finally:
            redirect.shutdown()
            target.shutdown()
            redirect.server_close()
            target.server_close()
            redirect_thread.join(timeout=2)
            target_thread.join(timeout=2)

        self.assertEqual([], target_hits)

    def test_proxy_environment_cannot_intercept_loopback_requests(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        direct_hits: list[str] = []
        direct_responses = list(_responses())

        class DirectHandler(BaseHTTPRequestHandler):
            def _respond(self) -> None:
                direct_hits.append(self.path)
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length:
                    self.rfile.read(content_length)
                body = direct_responses.pop(0).read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _respond
            do_POST = _respond

            def log_message(self, *args: object) -> None:
                return None

        proxy_hits: list[str] = []

        class ProxyHandler(BaseHTTPRequestHandler):
            def _reject(self) -> None:
                proxy_hits.append(self.path)
                self.send_response(502)
                self.end_headers()

            do_GET = _reject
            do_POST = _reject

            def log_message(self, *args: object) -> None:
                return None

        direct = ThreadingHTTPServer(("127.0.0.1", 0), DirectHandler)
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        direct_thread = threading.Thread(target=direct.serve_forever)
        proxy_thread = threading.Thread(target=proxy.serve_forever)
        direct_thread.start()
        proxy_thread.start()
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        proxy_environment = {
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "all_proxy": proxy_url,
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "ALL_PROXY": proxy_url,
            "NO_PROXY": "",
            "no_proxy": "",
        }
        try:
            with tempfile.TemporaryDirectory() as temporary_directory, patch.dict(
                os.environ,
                proxy_environment,
                clear=False,
            ):
                execution = OllamaModelAdapter(
                    endpoint=f"http://127.0.0.1:{direct.server_port}",
                    timeout_seconds=2,
                    trace_root=_private_trace_root(temporary_directory),
                ).propose(_task(document))
        finally:
            direct.shutdown()
            proxy.shutdown()
            direct.server_close()
            proxy.server_close()
            direct_thread.join(timeout=2)
            proxy_thread.join(timeout=2)

        self.assertIsNone(execution.failure_code)
        self.assertIsNotNone(execution.proposal)
        self.assertEqual([], proxy_hits)
        self.assertEqual(["/api/version", "/api/tags", "/api/chat"], direct_hits)

    def test_raw_trace_root_rejects_public_relative_and_symlink_paths(self) -> None:
        repo_root = Path(__file__).parents[1]
        with self.assertRaisesRegex(ValueError, "PRIVATE_STORAGE_REQUIRED"):
            OllamaModelAdapter(trace_root=repo_root / "raw-traces")
        with self.assertRaisesRegex(ValueError, "PRIVATE_STORAGE_REQUIRED"):
            OllamaModelAdapter(trace_root=Path("relative-traces"))

        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
            private = workspace / "private"
            private.mkdir()
            outside = workspace / "outside"
            outside.mkdir()
            (private / "escaped").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "PRIVATE_STORAGE_REQUIRED"):
                OllamaModelAdapter(trace_root=private / "escaped" / "traces")

    def test_public_model_route_manifest_matches_the_adapter(self) -> None:
        manifest_bytes = MODEL_ROUTE_MANIFEST_PATH.read_bytes()
        manifest = json.loads(manifest_bytes)

        self.assertEqual(
            ollama_route.SMOKE_TESTED_RUNTIME_VERSION,
            manifest["runtime"]["smoke_tested_version"],
        )
        self.assertEqual(MODEL_ID, manifest["model"]["id"])
        self.assertEqual(MODEL_DIGEST, manifest["model"]["manifest_digest"])
        self.assertEqual(ollama_route.PROMPT_SHA256, manifest["adapter"]["prompt_sha256"])
        self.assertEqual(
            ollama_route.TOOL_SCHEMA_SHA256,
            manifest["adapter"]["tool_schema_sha256"],
        )
        self.assertEqual(
            ollama_route.GENERATION_CONFIG_SHA256,
            manifest["adapter"]["generation_config_sha256"],
        )
        self.assertEqual(TRACE_SCHEMA_VERSION, manifest["adapter"]["trace_schema"])
        self.assertEqual(
            {
                "generation_context_tokens": ollama_route.GENERATION_CONTEXT,
                "maximum_generated_tokens": ollama_route.GENERATION_BUDGET,
                "maximum_document_bytes": ollama_route.MAX_DOCUMENT_BYTES,
                "maximum_request_bytes": ollama_route.MAX_REQUEST_BYTES,
                "maximum_response_bytes": ollama_route.MAX_RESPONSE_BYTES,
                "maximum_trace_bytes": ollama_route.MAX_TRACE_BYTES,
                "temperature": 0,
                "seed": 0,
            },
            manifest["budgets"],
        )
        self.assertNotIn(b"/Users/", manifest_bytes)
        self.assertNotIn(b"raw_response", manifest_bytes)


if __name__ == "__main__":
    unittest.main()
