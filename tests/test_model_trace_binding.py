from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import tempfile
from typing import Callable
import unittest

from finauditgate import AuditTask, Decision, FinAuditGate, FrozenDocumentPackage
from finauditgate.adapters.ollama import CapturedExchange, write_model_trace
from finauditgate.adapters.ollama_contract import CANDIDATE_TOOL_CONTRACT
from finauditgate.adapters.ollama_route import MODEL_DIGEST, MODEL_ID, chat_request
from finauditgate.adapters.ollama_trace import inspect_captured_response
from finauditgate.application import (
    CreateCase,
    ExportChangePacket,
    FinResearchOps,
    ReviewAction,
    RunAnalysis,
    SubmitReview,
)
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.core.model_trace import verify_raw_model_trace
from finauditgate.ports.model import (
    CalculationCandidate,
    EvidenceCandidate,
    ModelCandidate,
    ModelExecution,
)


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "synthetic"
    / "aurora_revenue_growth_m2.txt"
)


def _task(document: bytes, task_id: str = "trace-bound") -> AuditTask:
    return AuditTask(
        task_id=task_id,
        question="What was Aurora Devices FY2025 revenue growth versus FY2024?",
        cutoff=date(2026, 3, 1),
        document=FrozenDocumentPackage(
            source_id="synthetic-aurora-revenue-growth-v2",
            document_name="aurora_revenue_growth_m2.txt",
            document_bytes=document,
            declared_published_at=date(2026, 2, 15),
        ),
    )


def _candidate(document: bytes) -> ModelCandidate:
    prior = (
        b"metric=Net sales;basis=Reported;period=Year ended 2024-12-31;"
        b"value=125.00;currency=US dollar;unit=Monetary;scale=Millions;"
        b"sign=Positive"
    )
    current = (
        b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
        b"value=150.00;currency=USD;unit=Currency amount;scale=Million;"
        b"sign=As presented"
    )

    def evidence(
        evidence_id: str,
        record: bytes,
        period: str,
        value: str,
    ) -> EvidenceCandidate:
        start = document.index(record)
        return EvidenceCandidate(
            evidence_id=evidence_id,
            byte_start=start,
            byte_end=start + len(record),
            metric="revenue",
            metric_basis="REPORTED",
            period=period,
            value=value,
            currency="USD",
            unit="MONETARY",
            scale="MILLION",
            sign="POSITIVE",
        )

    return ModelCandidate(
        evidence=(
            evidence("comparison", prior, "FY2024", "125.00"),
            evidence("current", current, "FY2025", "150.00"),
        ),
        calculation=CalculationCandidate(
            operation="growth_rate_percent",
            operand_ids=("current", "comparison"),
            output_unit="PERCENT",
            quantize="0.01",
        ),
    )


def _execution_from_bytes(
    trace_root: Path,
    task: AuditTask,
    attempt_index: int,
    request_bytes: bytes,
    response_bytes: bytes,
) -> ModelExecution:
    inspected = inspect_captured_response(
        response_bytes,
        http_status=200,
        capture_complete=True,
        document=task.document.document_bytes,
        expected_model_id=MODEL_ID,
    )
    receipt = write_model_trace(
        trace_root,
        task=task,
        attempt_index=attempt_index,
        exchange=CapturedExchange(
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            http_status=200,
            response_capture_complete=True,
            elapsed_ns=1,
        ),
        inspected=inspected,
        runtime_version="test-runtime",
        observed_model_digest=MODEL_DIGEST,
        transport_origin="http://127.0.0.1:11434",
    )
    return ModelExecution(
        proposal=inspected.candidate,
        trace_receipt=receipt,
        failure_code=inspected.failure_code,
    )


def _traced_execution(
    trace_root: Path,
    task: AuditTask,
    candidate: ModelCandidate,
    *,
    attempt_index: int = 0,
) -> ModelExecution:
    """Write a trace exactly as the Adapter would for one frozen-route call."""

    arguments = CANDIDATE_TOOL_CONTRACT.encode(
        candidate,
        task.document.document_bytes,
    )
    response_bytes = canonical_json_bytes(
        {
            "model": MODEL_ID,
            "message": {
                "role": "assistant",
                "content": json.dumps(arguments),
            },
            "done": True,
            "done_reason": "stop",
        }
    )
    return _execution_from_bytes(
        trace_root,
        task,
        attempt_index,
        canonical_json_bytes(chat_request(task, attempt_index)),
        response_bytes,
    )


def _rewrite_trace(
    trace_root: Path,
    execution: ModelExecution,
    mutate: Callable[[dict[str, object]], None],
) -> ModelExecution:
    """Rewrite a saved trace and its receipt so only the mutation differs."""

    import base64

    receipt = execution.trace_receipt
    payload = json.loads((trace_root / receipt.raw_trace_ref).read_bytes())
    mutate(payload)
    request_bytes = canonical_json_bytes(payload["request"])
    response_bytes = base64.b64decode(payload["raw_response_base64"])
    payload["request_sha256"] = sha256_hex(request_bytes)
    payload["response_sha256"] = sha256_hex(response_bytes)
    trace_bytes = canonical_json_bytes(payload)
    trace_sha256 = sha256_hex(trace_bytes)
    raw_trace_ref = f"model-calls/sha256/{trace_sha256}.json"
    (trace_root / raw_trace_ref).write_bytes(trace_bytes)
    return ModelExecution(
        proposal=execution.proposal,
        trace_receipt=replace(
            receipt,
            request_sha256=payload["request_sha256"],
            response_sha256=payload["response_sha256"],
            raw_trace_sha256=trace_sha256,
            raw_trace_ref=raw_trace_ref,
        ),
        failure_code=execution.failure_code,
    )


class _OneTraceModel:
    def __init__(self, execution: ModelExecution) -> None:
        self._execution = execution

    def propose(self, task: AuditTask, attempt_index: int = 0) -> ModelExecution:
        if attempt_index != 0:
            raise LookupError("one traced attempt only")
        return self._execution


def _private_workspace(temporary_directory: str) -> Path:
    workspace = Path(temporary_directory)
    (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
    private = workspace / "private"
    private.mkdir()
    return private


WRAPPED_DOCUMENT = (
    b"Revenue          FY2025          FY2024\n"
    b"Revenues\n"
    b"751,766          660,257\n"
)


def _collapsed_response(current: str, comparison: str) -> bytes:
    """One answer whose cited spans are spaced as a model transcribes them."""

    def evidence(evidence_id: str, span: str, period: str, value: str) -> dict:
        return {
            "evidence_id": evidence_id,
            "exact_span": span,
            "metric": "revenue",
            "metric_basis": "REPORTED",
            "period": period,
            "value": value,
            "currency": "RMB",
            "unit": "MONETARY",
            "scale": "MILLION",
            "sign": "POSITIVE",
        }

    return canonical_json_bytes(
        {
            "model": MODEL_ID,
            "message": {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "evidence": [
                            evidence("current", current, "FY2025", "751766"),
                            evidence(
                                "comparison", comparison, "FY2024", "660257"
                            ),
                        ],
                        "calculation": {
                            "operation": "growth_rate_percent",
                            "operand_ids": ["current", "comparison"],
                            "output_unit": "PERCENT",
                            "quantize": "0.01",
                        },
                    }
                ),
            },
            "done": True,
            "done_reason": "stop",
        }
    )


class ModelTraceBindingTest(unittest.TestCase):
    def test_a_whitespace_collapsed_citation_binds_to_its_own_trace(
        self,
    ) -> None:
        """A tolerantly located span must still verify offline.

        The proposal identity has to be the canonical one, rebuilt from the
        document's bytes, or every run that relies on tolerant location would
        fail its own trace check.
        """

        task = _task(WRAPPED_DOCUMENT, task_id="collapsed-citation")
        response_bytes = _collapsed_response(
            "Revenues 751,766", "751,766 660,257"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_root = _private_workspace(temporary_directory) / "traces"
            execution = _execution_from_bytes(
                trace_root,
                task,
                0,
                canonical_json_bytes(chat_request(task, 0)),
                response_bytes,
            )
            verified, failure = verify_raw_model_trace(
                trace_root.resolve(),
                execution.trace_receipt,
                task=task,
                attempt_index=0,
                expected_proposal=execution.proposal,
                expected_failure_code=None,
            )

        self.assertIsNone(execution.failure_code)
        self.assertIsNotNone(execution.proposal)
        current, comparison = execution.proposal.evidence
        # Both citations were located tolerantly: neither is byte-equal to the
        # text the model wrote, so this exercises the new path, not `find`.
        self.assertEqual(
            b"Revenues\n751,766",
            WRAPPED_DOCUMENT[current.byte_start:current.byte_end],
        )
        self.assertEqual(
            b"751,766          660,257",
            WRAPPED_DOCUMENT[comparison.byte_start:comparison.byte_end],
        )
        self.assertIsNone(failure)
        self.assertIsNotNone(verified)

    def test_a_trace_from_other_weights_is_refused_offline(self) -> None:
        """The frozen model must be a replayable fact, not a precondition.

        The daemon tag is mutable, so the digest it reported at run time is the
        only model identity a saved trace carries.  A trace naming any other
        weights must not verify, even though every hash in it is internally
        consistent.
        """

        document = FIXTURE_PATH.read_bytes()
        task = _task(document)
        other_digest = "b" * 64
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _private_workspace(temporary_directory)
            trace_root = private / "model-traces"
            execution = _traced_execution(trace_root, task, _candidate(document))

            def swap_weights(payload: dict[str, object]) -> None:
                payload["observed_model_digest"] = other_digest

            swapped = _rewrite_trace(trace_root, execution, swap_weights)
            swapped = ModelExecution(
                proposal=swapped.proposal,
                trace_receipt=replace(
                    swapped.trace_receipt,
                    observed_model_digest=other_digest,
                ),
                failure_code=swapped.failure_code,
            )
            verified, failure = verify_raw_model_trace(
                trace_root.resolve(),
                swapped.trace_receipt,
                task=task,
                attempt_index=0,
                expected_proposal=swapped.proposal,
                expected_failure_code=None,
            )
            intact, intact_failure = verify_raw_model_trace(
                trace_root.resolve(),
                execution.trace_receipt,
                task=task,
                attempt_index=0,
                expected_proposal=execution.proposal,
                expected_failure_code=None,
            )

        self.assertNotEqual(MODEL_DIGEST, other_digest)
        self.assertEqual("MODEL_TRACE_REQUEST_MISMATCH", failure)
        self.assertIsNone(verified)
        # the untouched trace still verifies, so the refusal is the digest
        self.assertIsNone(intact_failure)
        self.assertIsNotNone(intact)

    def test_raw_response_must_cause_the_same_proposal_before_accept(
        self,
    ) -> None:
        import base64

        document = FIXTURE_PATH.read_bytes()
        task = _task(document, task_id="trace-causality")
        base_candidate = _candidate(document)
        reversed_candidate = replace(
            base_candidate,
            evidence=tuple(reversed(base_candidate.evidence)),
        )
        field_tampered_candidate = replace(
            base_candidate,
            evidence=(
                replace(base_candidate.evidence[0], metric="Revenue"),
                base_candidate.evidence[1],
            ),
        )

        def marker_only(payload: dict[str, object]) -> None:
            payload["raw_response_base64"] = base64.b64encode(
                canonical_json_bytes(
                    {
                        "model": MODEL_ID,
                        "done": True,
                        "done_reason": "stop",
                        "marker": "external-proposal",
                    }
                )
            ).decode("ascii")

        def change_system_prompt(payload: dict[str, object]) -> None:
            payload["request"]["messages"][0]["content"] = "tampered system prompt"

        def change_user_prompt(key: str, value: object) -> Callable:
            def mutate(payload: dict[str, object]) -> None:
                user = payload["request"]["messages"][1]
                prompt = json.loads(user["content"])
                if key == "attempt_index":
                    prompt["attempt_index"] = value
                else:
                    prompt["task"]["task_id"] = value
                user["content"] = json.dumps(
                    prompt,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )

            return mutate

        attacks: dict[str, object] = {
            "marker-only": marker_only,
            "response-proposal-mismatch": reversed_candidate,
            "candidate-field-tamper": field_tampered_candidate,
            "request-config": change_system_prompt,
            "cross-attempt": change_user_prompt("attempt_index", 1),
            "cross-run": change_user_prompt("task_id", "other-run"),
        }
        for attack, mutation in attacks.items():
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temporary_directory:
                private = _private_workspace(temporary_directory)
                trace_root = private / "model-traces"
                execution = _traced_execution(
                    trace_root,
                    task,
                    base_candidate,
                )
                if isinstance(mutation, ModelCandidate):
                    execution = replace(execution, proposal=mutation)
                else:
                    execution = _rewrite_trace(trace_root, execution, mutation)
                artifact_root = private / "artifacts" / "core"
                with self.assertRaisesRegex(
                    RuntimeError,
                    "MODEL_TRACE_(PROPOSAL|REQUEST)_MISMATCH",
                ):
                    FinAuditGate(
                        artifact_root=artifact_root,
                        model=_OneTraceModel(execution),
                        model_trace_root=trace_root,
                    ).run(task)
                self.assertFalse((artifact_root / "runs").exists())

    def test_workpaper_binds_summary_and_packet_never_exports_raw_trace(
        self,
    ) -> None:
        document = FIXTURE_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _private_workspace(temporary_directory)
            artifact_root = private / "product-artifacts"
            trace_root = private / "model-traces"
            task = _task(document, task_id="synthetic-aurora-revenue-growth-v2")
            execution = _traced_execution(
                trace_root,
                task,
                _candidate(document),
            )
            application = FinResearchOps(
                artifact_root=artifact_root,
                model=_OneTraceModel(execution),
                model_trace_root=trace_root,
            )
            created = application.handle(
                CreateCase(
                    question=task.question,
                    cutoff=task.cutoff,
                    document=task.document,
                    mode="SYNTHETIC_DEV",
                )
            )
            analyzed = application.handle(RunAnalysis(case_ref=created.case_ref))
            view = application.read_case(created.case_ref)
            workpaper = view.workpapers[-1]
            reviewed = application.handle(
                SubmitReview(
                    case_ref=created.case_ref,
                    run_ref=analyzed.run_ref,
                    action=ReviewAction.APPROVE,
                    reason="Synthetic contract regression only.",
                )
            )
            exported = application.handle(
                ExportChangePacket(case_ref=created.case_ref, run_ref=analyzed.run_ref)
            )
            packet_bytes = (
                artifact_root
                / "application"
                / "cases"
                / created.case_ref
                / "packets"
                / f"{exported.packet_ref}.json"
            ).read_bytes()

        self.assertEqual(
            f"runs/{analyzed.run_ref.run_id}/model-trace.json",
            workpaper.trace_summary_ref,
        )
        self.assertEqual("MODEL_TRACE_BOUND", workpaper.trace_summary_status)
        self.assertEqual(reviewed.run_ref, analyzed.run_ref)
        self.assertNotIn(b"raw_response_base64", packet_bytes)
        self.assertNotIn(b"model-calls/sha256", packet_bytes)
        self.assertNotIn(str(trace_root).encode("utf-8"), packet_bytes)

    def test_runref_and_replay_bind_a_private_raw_model_trace(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _private_workspace(temporary_directory)
            artifact_root = private / "artifacts" / "core"
            trace_root = private / "model-traces"
            task = _task(document)
            execution = _traced_execution(
                trace_root,
                task,
                _candidate(document),
            )
            outcome = FinAuditGate(
                artifact_root=artifact_root,
                model=_OneTraceModel(execution),
                model_trace_root=trace_root,
            ).run(task)
            run_directory = artifact_root / "runs" / outcome.run_ref.run_id
            manifest = json.loads((run_directory / "manifest.json").read_bytes())
            identity = json.loads((run_directory / "identity.json").read_bytes())
            trace_summary = (run_directory / "model-trace.json").read_bytes()
            replay = FinAuditGate(
                artifact_root=artifact_root,
                model_trace_root=trace_root,
            ).replay(outcome.run_ref)

        self.assertIs(Decision.ACCEPT, outcome.decision)
        self.assertEqual("finauditgate.manifest/v2", manifest["schema_version"])
        self.assertIn("model_trace", manifest["artifacts"])
        self.assertEqual(sha256_hex(trace_summary), identity["model_trace_sha256"])
        self.assertNotIn(b"raw_response_base64", trace_summary)
        self.assertNotIn(str(trace_root).encode("utf-8"), trace_summary)
        self.assertTrue(replay.consistent)
        self.assertEqual("finauditgate.replay/v5", replay.schema_version)
        self.assertEqual(10, replay.verified_artifact_count)

    def test_missing_tampered_and_cross_run_raw_trace_fail_closed(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        for attack in ("missing", "tampered", "cross-run"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temporary_directory:
                private = _private_workspace(temporary_directory)
                artifact_root = private / "artifacts" / "core"
                trace_root = private / "model-traces"
                task = _task(document, task_id=f"trace-{attack}")
                execution = _traced_execution(
                    trace_root,
                    task,
                    _candidate(document),
                )
                outcome = FinAuditGate(
                    artifact_root=artifact_root,
                    model=_OneTraceModel(execution),
                    model_trace_root=trace_root,
                ).run(task)
                raw_path = trace_root / execution.trace_receipt.raw_trace_ref
                if attack == "missing":
                    raw_path.unlink()
                    expected_reason = "MODEL_TRACE_MISSING"
                elif attack == "tampered":
                    raw_path.write_bytes(b"{}")
                    expected_reason = "MODEL_TRACE_HASH_MISMATCH"
                else:
                    other = _traced_execution(
                        trace_root,
                        _task(document, task_id="other-run"),
                        _candidate(document),
                    )
                    raw_path.write_bytes(
                        (trace_root / other.trace_receipt.raw_trace_ref).read_bytes()
                    )
                    expected_reason = "MODEL_TRACE_HASH_MISMATCH"
                replay = FinAuditGate(
                    artifact_root=artifact_root,
                    model_trace_root=trace_root,
                ).replay(outcome.run_ref)

            self.assertFalse(replay.consistent)
            self.assertEqual(expected_reason, replay.reason)


if __name__ == "__main__":
    unittest.main()
