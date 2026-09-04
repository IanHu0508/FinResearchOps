from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finauditgate import AuditTask, Decision, FinAuditGate, FrozenDocumentPackage
from finauditgate.adapters.ollama import OllamaModelAdapter
from finauditgate.adapters.ollama_route import MODEL_DIGEST, MODEL_ID
from finauditgate.application import (
    CreateCase,
    ExportChangePacket,
    FinResearchOps,
    ReplayRun,
    ReviewAction,
    RunAnalysis,
    SubmitReview,
)
from finauditgate.core.artifacts import canonical_json_bytes
from finauditgate.core.model_trace import verify_raw_model_trace
from finauditgate.core.private_profile import evaluate_private_candidate
from finauditgate.core.profiles import PrivateDevValidationProfile
from finauditgate.core.synthetic_profile import ValidationFailure
from finauditgate.ports.model import (
    CalculationCandidate,
    EvidenceCandidate,
    ModelCandidate,
    ModelExecution,
)
from tests.test_model_trace_binding import _OneTraceModel, _traced_execution


NATURAL_DOCUMENT = (
    "Orion Components plc — annual results\n"
    "Revenue by fiscal year (USD millions)\n"
    "FY2024 | Revenue | 125.00\n"
    "FY2025 | Revenue | 150.00\n"
    "Management reported that demand remained stable.\n"
).encode("utf-8")
QUESTION = "What was Orion Components FY2025 revenue growth versus FY2024?"
SOURCE_ID = "private-orion-natural-disclosure-v1"
DOCUMENT_NAME = "orion-natural-disclosure.txt"
PRIOR_SPAN = b"FY2024 | Revenue | 125.00"
CURRENT_SPAN = b"FY2025 | Revenue | 150.00"
# The same figure printed a second time, the way a filing repeats a statement
# total in the note that breaks it down.
CORROBORATED_DOCUMENT = (
    "Orion Components plc — annual results\n"
    "Revenue by fiscal year (USD millions)\n"
    "FY2024 | Revenue | 125.00\n"
    "FY2025 | Revenue | 150.00\n"
    "Note 4 — Revenue by segment (USD millions)\n"
    "FY2025 | Components 90.00 | Services 60.00 | Total 150.00\n"
).encode("utf-8")
CORROBORATING_LINE = b"FY2025 | Components 90.00 | Services 60.00 | Total 150.00"
CONTRADICTING_DOCUMENT = CORROBORATED_DOCUMENT.replace(
    b"Total 150.00", b"Total 151.00"
)


class _JSONResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_JSONResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return self._payload if amount < 0 else self._payload[:amount]


def _route_responses(
    candidate: ModelCandidate | None = None,
    *,
    operation: str | None = None,
) -> tuple[_JSONResponse, ...]:
    """One attempt on the wire: /api/version, /api/tags, /api/chat."""

    tags = _JSONResponse(
        {
            "models": [
                {
                    "name": MODEL_ID,
                    "model": MODEL_ID,
                    "size": 2_620_788_260,
                    "digest": MODEL_DIGEST,
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
        }
    )
    candidate = candidate or _candidate()
    arguments = {
        "evidence": [
            {
                "evidence_id": evidence.evidence_id,
                "exact_span": NATURAL_DOCUMENT[
                    evidence.byte_start:evidence.byte_end
                ].decode("utf-8"),
                "metric": evidence.metric,
                "metric_basis": evidence.metric_basis,
                "period": evidence.period,
                "value": evidence.value,
                "currency": evidence.currency,
                "unit": evidence.unit,
                "scale": evidence.scale,
                "sign": evidence.sign,
            }
            for evidence in candidate.evidence
        ],
        "calculation": {
            "operation": (
                candidate.calculation.operation if operation is None else operation
            ),
            "operand_ids": list(candidate.calculation.operand_ids),
            "output_unit": candidate.calculation.output_unit,
            "quantize": candidate.calculation.quantize,
        },
    }
    chat = _JSONResponse(
        {
            "model": MODEL_ID,
            "message": {
                "role": "assistant",
                "content": json.dumps(arguments),
            },
            "done": True,
            "done_reason": "stop",
            "total_duration": 2_000_000,
            "load_duration": 1_000_000,
            "prompt_eval_count": 500,
            "eval_count": 100,
        }
    )
    return (_JSONResponse({"version": "0.33.1"}), tags, chat)


def _locator(document: bytes, span: bytes) -> tuple[int, int]:
    start = document.index(span)
    return start, start + len(span)


def _profile_payload(
    document: bytes,
    *,
    prior_span_sha256: str | None = None,
    declared_published_at: str = "2026-02-15",
    task_cutoff: str = "2026-03-01",
) -> dict[str, object]:
    prior_start, prior_end = _locator(document, PRIOR_SPAN)
    current_start, current_end = _locator(document, CURRENT_SPAN)
    semantics = {
        "metric": "revenue",
        "metric_basis": "REPORTED",
        "currency": "USD",
        "unit": "MONETARY",
        "scale": "MILLION",
        "sign": "POSITIVE",
    }
    return {
        "schema_version": "finauditgate.private-dev-validation-profile/v3",
        "validation_profile": "private-natural-revenue-growth/v2",
        "source_id": SOURCE_ID,
        "document_name": DOCUMENT_NAME,
        "document_sha256": hashlib.sha256(document).hexdigest(),
        "declared_published_at": declared_published_at,
        "task_cutoff": task_cutoff,
        "task_question": QUESTION,
        "accepted_mode": "PRIVATE_DEV",
        "accepted_risk_class": "LOW",
        "evidence_allowlist": [
            {
                "evidence_id": "comparison",
                "role": "COMPARISON",
                "byte_start": prior_start,
                "byte_end": prior_end,
                "span_sha256": (
                    prior_span_sha256 or hashlib.sha256(PRIOR_SPAN).hexdigest()
                ),
                "value": "125.00",
                "normalized_semantics": {**semantics, "fiscal_period": "FY2024"},
                "corroboration": None,
            },
            {
                "evidence_id": "current",
                "role": "CURRENT",
                "byte_start": current_start,
                "byte_end": current_end,
                "span_sha256": hashlib.sha256(CURRENT_SPAN).hexdigest(),
                "value": "150.00",
                "normalized_semantics": {**semantics, "fiscal_period": "FY2025"},
                "corroboration": None,
            },
        ],
        "calculation": {
            "operation": "growth_rate_percent",
            "operand_ids": ["current", "comparison"],
            "output_unit": "PERCENT",
            "quantize": "0.01",
            "decimal_context": {
                "precision": 28,
                "rounding": "ROUND_HALF_EVEN",
                "emin": -999999,
                "emax": 999999,
                "capitals": 1,
                "clamp": 0,
            },
        },
    }


def _no_evidence_profile_payload(document: bytes) -> dict[str, object]:
    payload = _profile_payload(document)
    payload["validation_profile"] = "private-no-admissible-evidence/v1"
    payload["evidence_allowlist"] = []
    payload["calculation"] = None
    return payload


def _private_task(
    document: bytes = NATURAL_DOCUMENT,
    *,
    published_at: date = date(2026, 2, 15),
    cutoff: date = date(2026, 3, 1),
    mode: str = "PRIVATE_DEV",
) -> AuditTask:
    return AuditTask(
        task_id=SOURCE_ID,
        question=QUESTION,
        cutoff=cutoff,
        document=FrozenDocumentPackage(
            source_id=SOURCE_ID,
            document_name=DOCUMENT_NAME,
            document_bytes=document,
            declared_published_at=published_at,
        ),
        mode=mode,
    )


def _candidate(document: bytes = NATURAL_DOCUMENT) -> ModelCandidate:
    prior_start, prior_end = _locator(document, PRIOR_SPAN)
    current_start, current_end = _locator(document, CURRENT_SPAN)
    return ModelCandidate(
        evidence=(
            EvidenceCandidate(
                evidence_id="comparison",
                byte_start=prior_start,
                byte_end=prior_end,
                metric="revenue",
                metric_basis="REPORTED",
                period="FY2024",
                value="125.00",
                currency="USD",
                unit="MONETARY",
                scale="MILLION",
                sign="POSITIVE",
            ),
            EvidenceCandidate(
                evidence_id="current",
                byte_start=current_start,
                byte_end=current_end,
                metric="revenue",
                metric_basis="REPORTED",
                period="FY2025",
                value="150.00",
                currency="USD",
                unit="MONETARY",
                scale="MILLION",
                sign="POSITIVE",
            ),
        ),
        calculation=CalculationCandidate(
            operation="growth_rate_percent",
            operand_ids=("current", "comparison"),
            output_unit="PERCENT",
            quantize="0.01",
        ),
    )


class _TraceSequenceModel:
    def __init__(self, executions: tuple[ModelExecution, ...]) -> None:
        self._executions = executions

    def propose(self, task: AuditTask, attempt_index: int = 0) -> ModelExecution:
        del task
        try:
            return self._executions[attempt_index]
        except IndexError as exc:
            raise LookupError("no more traced attempts") from exc


def _workspace(temporary_directory: str) -> Path:
    workspace = Path(temporary_directory)
    (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
    private = workspace / "private"
    private.mkdir()
    return private


def _profile(private: Path, payload: dict[str, object]) -> PrivateDevValidationProfile:
    path = private / "profiles" / "natural-profile.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(payload))
    return PrivateDevValidationProfile.from_path(path)


class PrivateDevValidationProfileTest(unittest.TestCase):
    def _corroborated_profile(
        self,
        private: Path,
        document: bytes,
        corroborating: bytes,
    ) -> PrivateDevValidationProfile:
        payload = _profile_payload(document)
        payload["document_sha256"] = hashlib.sha256(document).hexdigest()
        start = document.index(corroborating)
        for item in payload["evidence_allowlist"]:
            if item["role"] != "CURRENT":
                continue
            item["corroboration"] = {
                "byte_start": start,
                "byte_end": start + len(corroborating),
                "span_sha256": hashlib.sha256(corroborating).hexdigest(),
            }
        return _profile(private, payload)

    def test_a_second_printing_of_the_figure_is_checked_too(self) -> None:
        """A total in the statement, and the note that breaks it down.

        The reviewer records where the figure is printed again; the gate reads
        that region from the frozen document and requires it to carry the same
        value. The model is not told about it and cannot influence it.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = _private_task(CORROBORATED_DOCUMENT)
            profile = self._corroborated_profile(
                private, CORROBORATED_DOCUMENT, CORROBORATING_LINE
            )
            trace_root = private / "model-traces"
            execution = _traced_execution(
                trace_root, task, _candidate(CORROBORATED_DOCUMENT)
            )
            outcome = FinAuditGate(
                artifact_root=private / "artifacts" / "core",
                model=_OneTraceModel(execution),
                model_trace_root=trace_root,
                private_dev_profile=profile,
            ).run(task)
            ledger = json.loads(
                (
                    private / "artifacts" / "core" / "runs"
                    / outcome.run_ref.run_id / "ledger.json"
                ).read_bytes()
            )

        self.assertIs(Decision.ACCEPT, outcome.decision)
        current = next(n for n in ledger["nodes"] if n["role"] == "CURRENT")
        self.assertEqual(
            "SECOND_PRINTING_CARRIES_THE_SAME_VALUE",
            current["corroboration"]["agreement"],
        )
        comparison = next(
            n for n in ledger["nodes"] if n["role"] == "COMPARISON"
        )
        self.assertIsNone(comparison["corroboration"])

    def test_a_second_printing_that_disagrees_stops_the_run(self) -> None:
        """The note says 151.00 where the statement says 150.00."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = _private_task(CONTRADICTING_DOCUMENT)
            profile = self._corroborated_profile(
                private,
                CONTRADICTING_DOCUMENT,
                CORROBORATING_LINE.replace(b"Total 150.00", b"Total 151.00"),
            )
            trace_root = private / "model-traces"
            execution = _traced_execution(
                trace_root, task, _candidate(CONTRADICTING_DOCUMENT)
            )
            outcome = FinAuditGate(
                artifact_root=private / "artifacts" / "core",
                model=_OneTraceModel(execution),
                model_trace_root=trace_root,
                private_dev_profile=profile,
            ).run(task)

        self.assertIs(Decision.HUMAN_REVIEW, outcome.decision)
        self.assertEqual(("CORROBORATION_CONFLICT",), outcome.reason_codes)

    def test_a_corroboration_may_not_overlap_the_span_it_corroborates(
        self,
    ) -> None:
        """Citing the same bytes twice would corroborate nothing."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            payload = _profile_payload(NATURAL_DOCUMENT)
            for item in payload["evidence_allowlist"]:
                if item["role"] != "CURRENT":
                    continue
                item["corroboration"] = {
                    "byte_start": item["byte_start"],
                    "byte_end": item["byte_end"],
                    "span_sha256": item["span_sha256"],
                }
            with self.assertRaises(ValueError):
                _profile(private, payload)

    def test_absolute_change_answers_in_the_unit_of_the_figures(self) -> None:
        """The second operation, end to end through the private gate.

        Its answer is not a percentage: subtracting two figures denominated in
        millions leaves millions, so the answer's unit follows the operands and
        the task asks for a different contract.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = replace(_private_task(), answer_contract="ABSOLUTE_CHANGE")
            payload = _profile_payload(NATURAL_DOCUMENT)
            payload["calculation"]["operation"] = "absolute_change"
            payload["calculation"]["output_unit"] = "MONETARY"
            profile = _profile(private, payload)
            candidate = replace(
                _candidate(),
                calculation=CalculationCandidate(
                    operation="absolute_change",
                    operand_ids=("current", "comparison"),
                    output_unit="MONETARY",
                    quantize="0.01",
                ),
            )
            trace_root = private / "model-traces"
            execution = _traced_execution(trace_root, task, candidate)
            outcome = FinAuditGate(
                artifact_root=private / "artifacts" / "core",
                model=_OneTraceModel(execution),
                model_trace_root=trace_root,
                private_dev_profile=profile,
            ).run(task)

        self.assertIs(Decision.ACCEPT, outcome.decision)
        # 150.00 - 125.00, in millions, not 20.00 percent
        self.assertEqual("25.00", outcome.answer)
        self.assertEqual("MONETARY", outcome.answer_unit)

    def test_a_task_and_its_profile_must_ask_the_same_question(self) -> None:
        """A percentage task cannot be answered by a difference, or the reverse."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = _private_task()  # answer_contract PERCENTAGE_CHANGE
            payload = _profile_payload(NATURAL_DOCUMENT)
            payload["calculation"]["operation"] = "absolute_change"
            payload["calculation"]["output_unit"] = "MONETARY"
            profile = _profile(private, payload)
            candidate = replace(
                _candidate(),
                calculation=CalculationCandidate(
                    operation="absolute_change",
                    operand_ids=("current", "comparison"),
                    output_unit="MONETARY",
                    quantize="0.01",
                ),
            )
            trace_root = private / "model-traces"
            execution = _traced_execution(trace_root, task, candidate)
            outcome = FinAuditGate(
                artifact_root=private / "artifacts" / "core",
                model=_OneTraceModel(execution),
                model_trace_root=trace_root,
                private_dev_profile=profile,
            ).run(task)

        self.assertIs(Decision.HUMAN_REVIEW, outcome.decision)
        self.assertEqual(("ANSWER_CONTRACT_CONFLICT",), outcome.reason_codes)

    def test_post_freeze_transfer_runs_get_every_reviewed_guarantee(
        self,
    ) -> None:
        """The transfer split is decided the same way, never more loosely.

        A run on the second filing must require a reviewed profile, require a
        private artifact root and a model trace, and be verified against the
        same gate as development runs.  What differs is only that the mode is
        recorded, so the two splits can never be pooled.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = _private_task(mode="POST_FREEZE_EVAL")
            payload = _profile_payload(NATURAL_DOCUMENT)
            payload["accepted_mode"] = "POST_FREEZE_EVAL"
            profile = _profile(private, payload)
            trace_root = private / "model-traces"
            artifact_root = private / "artifacts" / "core"
            execution = _traced_execution(
                trace_root,
                task,
                _candidate(),
            )
            outcome = FinAuditGate(
                artifact_root=artifact_root,
                model=_OneTraceModel(execution),
                model_trace_root=trace_root,
                private_dev_profile=profile,
            ).run(task)
            replay = FinAuditGate(
                artifact_root=artifact_root,
                model_trace_root=trace_root,
                private_dev_profile=profile,
            ).replay(outcome.run_ref)
            policy = json.loads(
                (
                    artifact_root / "runs" / outcome.run_ref.run_id / "policy.json"
                ).read_bytes()
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "REVIEWED_VALIDATION_PROFILE_REQUIRED",
            ):
                FinAuditGate(
                    artifact_root=private / "artifacts" / "unprofiled",
                    model=_OneTraceModel(execution),
                    model_trace_root=trace_root,
                ).run(task)

        self.assertIs(Decision.ACCEPT, outcome.decision)
        self.assertTrue(replay.consistent)
        # the reviewed profile is the answer key, never the public fixture
        self.assertEqual("POST_FREEZE_EVAL", policy["accepted_mode"])

    def test_a_development_profile_cannot_decide_a_transfer_run(self) -> None:
        """The split is content-addressed: profile and task must agree."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            development_profile = _profile(
                private,
                _profile_payload(NATURAL_DOCUMENT),
            )
            trace_root = private / "model-traces"
            crossed = _private_task(mode="POST_FREEZE_EVAL")
            execution = _traced_execution(trace_root, crossed, _candidate())
            outcome = FinAuditGate(
                artifact_root=private / "artifacts" / "core",
                model=_OneTraceModel(execution),
                model_trace_root=trace_root,
                private_dev_profile=development_profile,
            ).run(crossed)

        self.assertIs(Decision.HUMAN_REVIEW, outcome.decision)
        self.assertEqual(("MODE_CONFLICT",), outcome.reason_codes)

    def test_protocol_rejection_verifies_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = _private_task()
            trace_root = private / "model-traces"
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_route_responses(operation="percentage_change"),
            ):
                execution = OllamaModelAdapter(trace_root=trace_root).propose(task)
            verified, offline_failure = verify_raw_model_trace(
                trace_root.resolve(),
                execution.trace_receipt,
                task=task,
                attempt_index=0,
                expected_proposal=None,
                expected_failure_code="TOOL_ARGUMENT_NOT_ALLOWLISTED",
            )
            trace = json.loads(
                (trace_root / execution.trace_receipt.raw_trace_ref).read_bytes()
            )

        self.assertIsNone(offline_failure, offline_failure)
        self.assertIsNotNone(verified)
        self.assertIsNone(execution.proposal)
        self.assertEqual("TOOL_ARGUMENT_NOT_ALLOWLISTED", execution.failure_code)
        self.assertEqual("CANDIDATE_REJECTED", trace["parse_status"])
        self.assertEqual(MODEL_DIGEST, trace["observed_model_digest"])
        self.assertIsNone(trace["response_derived_proposal_sha256"])

    def test_protocol_rejections_are_bounded_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = _private_task()
            profile = _profile(private, _profile_payload(NATURAL_DOCUMENT))
            trace_root = private / "model-traces"
            artifact_root = private / "artifacts" / "core"
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=(
                    _route_responses(operation="percentage_change")
                    + _route_responses(operation="percentage_change")
                ),
            ) as open_no_redirect:
                outcome = FinAuditGate(
                    artifact_root=artifact_root,
                    model=OllamaModelAdapter(trace_root=trace_root),
                    model_trace_root=trace_root,
                    private_dev_profile=profile,
                ).run(task)
            replay = FinAuditGate(
                artifact_root=artifact_root,
                model_trace_root=trace_root,
            ).replay(outcome.run_ref)
            run_directory = artifact_root / "runs" / outcome.run_ref.run_id
            attempts = json.loads((run_directory / "attempts.json").read_bytes())
            trace_count = len(list(trace_root.glob("model-calls/sha256/*.json")))
            request_count = open_no_redirect.call_count

        self.assertIs(Decision.RETRY, outcome.decision)
        self.assertEqual(
            ("MODEL_CANDIDATE_REJECTED", "RETRY_BUDGET_EXHAUSTED"),
            outcome.reason_codes,
        )
        self.assertEqual(6, request_count)
        self.assertEqual(2, trace_count)
        self.assertEqual(
            [["MODEL_CANDIDATE_REJECTED"], ["MODEL_CANDIDATE_REJECTED"]],
            [attempt["reason_codes"] for attempt in attempts["attempts"]],
        )
        self.assertTrue(replay.consistent, replay.reason)
        self.assertIs(Decision.RETRY, replay.decision)
        self.assertEqual("finauditgate.replay/v5", replay.schema_version)

    def test_profile_binds_source_publication_date_and_task_cutoff(self) -> None:
        base_task = _private_task()
        attacks = {
            "published-at": replace(
                base_task,
                document=replace(
                    base_task.document,
                    declared_published_at=date(2026, 2, 14),
                ),
            ),
            "cutoff": replace(base_task, cutoff=date(2026, 3, 2)),
        }
        for name, task in attacks.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary_directory:
                private = _workspace(temporary_directory)
                profile = _profile(private, _profile_payload(NATURAL_DOCUMENT))
                trace_root = private / "model-traces"
                with patch(
                    "finauditgate.adapters.ollama._open_no_redirect",
                    side_effect=_route_responses(),
                ):
                    outcome = FinAuditGate(
                        artifact_root=private / "artifacts" / "core",
                        model=OllamaModelAdapter(trace_root=trace_root),
                        model_trace_root=trace_root,
                        private_dev_profile=profile,
                    ).run(task)

            self.assertIs(Decision.HUMAN_REVIEW, outcome.decision)
            self.assertIn(
                "SOURCE_PROFILE_CONFLICT"
                if name == "published-at"
                else "CUTOFF_PROFILE_CONFLICT",
                outcome.reason_codes,
            )

    def test_natural_disclosure_reaches_accept_review_export_and_replay(
        self,
    ) -> None:
        self.assertNotIn(b"metric=", NATURAL_DOCUMENT)
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = _private_task()
            profile = _profile(private, _profile_payload(NATURAL_DOCUMENT))
            trace_root = private / "model-traces"
            application = FinResearchOps(
                artifact_root=private / "product-artifacts",
                model=OllamaModelAdapter(trace_root=trace_root),
                model_trace_root=trace_root,
                private_dev_profile=profile,
            )
            created = application.handle(
                CreateCase(
                    question=task.question,
                    cutoff=task.cutoff,
                    document=task.document,
                    mode="PRIVATE_DEV",
                )
            )
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_route_responses(),
            ):
                analyzed = application.handle(RunAnalysis(case_ref=created.case_ref))
            view = application.read_case(created.case_ref)
            replayed = application.handle(ReplayRun(run_ref=analyzed.run_ref))
            application.handle(
                SubmitReview(
                    case_ref=created.case_ref,
                    run_ref=analyzed.run_ref,
                    action=ReviewAction.APPROVE,
                    reason="Reviewed against the frozen private profile.",
                )
            )
            exported = application.handle(
                ExportChangePacket(case_ref=created.case_ref, run_ref=analyzed.run_ref)
            )
            reopened = application.read_case(created.case_ref)
            case_directory = (
                private / "product-artifacts" / "application" / "cases" / created.case_ref
            )
            workpaper_artifact = json.loads(
                (case_directory / "workpapers" / f"{analyzed.workpaper_ref}.json").read_bytes()
            )
            replay_artifact = json.loads(
                (case_directory / "replays" / f"{replayed.replay_ref}.json").read_bytes()
            )
            packet_bytes = (
                case_directory / "packets" / f"{exported.packet_ref}.json"
            ).read_bytes()
            schema_root = Path(__file__).parents[1] / "schemas"
            workpaper_schema = json.loads(
                (schema_root / "workpaper.v3.schema.json").read_bytes()
            )
            replay_record_schema = json.loads(
                (schema_root / "replay-record.v3.schema.json").read_bytes()
            )
            replay_report_schema = json.loads(
                (schema_root / "replay-report.v5.schema.json").read_bytes()
            )

        self.assertIs(Decision.ACCEPT, analyzed.machine_decision)
        self.assertEqual("20.00", view.workpapers[-1].answer)
        self.assertEqual("MODEL_TRACE_BOUND", view.workpapers[-1].trace_summary_status)
        self.assertEqual(
            "finauditgate.replay/v5",
            reopened.replays[-1].report.schema_version,
        )
        self.assertEqual(set(workpaper_schema["properties"]), set(workpaper_artifact))
        self.assertEqual(set(replay_record_schema["properties"]), set(replay_artifact))
        self.assertEqual(
            set(replay_report_schema["properties"]),
            set(replay_artifact["report"]),
        )
        packet = reopened.packets[0]
        self.assertTrue(packet.proposal_only)
        self.assertEqual(2, len(packet.verified_facts))
        self.assertEqual("20.00", packet.calculations[0].result)
        self.assertTrue(all(fact.span_sha256 for fact in packet.verified_facts))
        self.assertNotIn(b"raw_response_base64", packet_bytes)
        self.assertNotIn(b"model-calls/sha256", packet_bytes)

    def test_wrong_locator_span_value_period_metric_and_operand_fail_closed(
        self,
    ) -> None:
        base = _candidate()
        header_start, header_end = _locator(
            NATURAL_DOCUMENT,
            b"Revenue by fiscal year (USD millions)",
        )
        attacks = {
            "locator": (
                replace(
                    base,
                    evidence=(
                        replace(base.evidence[0], byte_start=header_start, byte_end=header_end),
                        base.evidence[1],
                    ),
                ),
                None,
                "EVIDENCE_LOCATOR_INVALID",
            ),
            "span": (base, "f" * 64, "EVIDENCE_SPAN_HASH_MISMATCH"),
            "value": (
                replace(
                    base,
                    evidence=(replace(base.evidence[0], value="124.00"), base.evidence[1]),
                ),
                None,
                "CLAIMED_VALUE_MISMATCH",
            ),
            "period": (
                replace(
                    base,
                    evidence=(replace(base.evidence[0], period="FY2023"), base.evidence[1]),
                ),
                None,
                "FISCAL_PERIOD_CONFLICT",
            ),
            "metric": (
                replace(
                    base,
                    evidence=(
                        replace(base.evidence[0], metric="gross_profit"),
                        base.evidence[1],
                    ),
                ),
                None,
                "METRIC_CONFLICT",
            ),
            "operand": (
                replace(
                    base,
                    calculation=replace(
                        base.calculation,
                        operand_ids=("comparison", "current"),
                    ),
                ),
                None,
                "OPERAND_LINEAGE_INVALID",
            ),
        }
        for name, (candidate, span_hash, expected_reason) in attacks.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary_directory:
                private = _workspace(temporary_directory)
                task = _private_task()
                profile = _profile(
                    private,
                    _profile_payload(NATURAL_DOCUMENT, prior_span_sha256=span_hash),
                )
                trace_root = private / "model-traces"
                with patch(
                    "finauditgate.adapters.ollama._open_no_redirect",
                    side_effect=_route_responses(candidate) + _route_responses(candidate),
                ):
                    outcome = FinAuditGate(
                        artifact_root=private / "artifacts" / "core",
                        model=OllamaModelAdapter(trace_root=trace_root),
                        model_trace_root=trace_root,
                        private_dev_profile=profile,
                    ).run(task)
                attempts = json.loads(
                    (
                        private / "artifacts" / "core" / "runs"
                        / outcome.run_ref.run_id / "attempts.json"
                    ).read_bytes()
                )

            self.assertIsNot(Decision.ACCEPT, outcome.decision)
            self.assertIn(expected_reason, attempts["attempts"][0]["reason_codes"])

    def test_citation_inside_the_reviewed_line_is_accepted(self) -> None:
        """The reviewed span is a region; the model may cite the number in it."""

        profile = PrivateDevValidationProfile.from_bytes(
            canonical_json_bytes(_profile_payload(NATURAL_DOCUMENT))
        )
        evaluate = lambda candidate: evaluate_private_candidate(  # noqa: E731
            NATURAL_DOCUMENT,
            candidate,
            _private_task(),
            policy=profile.policy,
            policy_sha256=profile.sha256,
            semantics_sha256=profile.registries_sha256,
        )
        base = _candidate()
        prior_number = _locator(NATURAL_DOCUMENT, b"125.00")
        current_number = _locator(NATURAL_DOCUMENT, b"150.00")
        narrowed = replace(
            base,
            evidence=(
                replace(
                    base.evidence[0],
                    byte_start=prior_number[0],
                    byte_end=prior_number[1],
                ),
                replace(
                    base.evidence[1],
                    byte_start=current_number[0],
                    byte_end=current_number[1],
                ),
            ),
        )

        ledger, formula = evaluate(narrowed)

        self.assertEqual("20.00", formula["result"])
        self.assertEqual(
            [list(prior_number), list(current_number)],
            [
                [node["locator"]["byte_start"], node["locator"]["byte_end"]]
                for node in ledger["nodes"]
            ],
        )
        for node, span in zip(ledger["nodes"], (b"125.00", b"150.00")):
            self.assertEqual(
                hashlib.sha256(span).hexdigest(),
                node["locator"]["span_sha256"],
            )
        self.assertEqual(
            [hashlib.sha256(PRIOR_SPAN).hexdigest(), hashlib.sha256(CURRENT_SPAN).hexdigest()],
            [node["reviewed_locator"]["span_sha256"] for node in ledger["nodes"]],
        )

        label = _locator(NATURAL_DOCUMENT, b"FY2024 | Revenue")
        label_only = replace(
            base,
            evidence=(
                replace(base.evidence[0], byte_start=label[0], byte_end=label[1]),
                base.evidence[1],
            ),
        )
        with self.assertRaises(ValidationFailure) as rejected:
            evaluate(label_only)
        self.assertEqual(Decision.RETRY, rejected.exception.decision)
        self.assertEqual(
            ("EVIDENCE_LOCATOR_INVALID",),
            rejected.exception.reason_codes,
        )

    def test_post_cutoff_profile_returns_human_review_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = _private_task(published_at=date(2026, 3, 5), cutoff=date(2026, 3, 1))
            profile = _profile(
                private,
                _profile_payload(
                    NATURAL_DOCUMENT,
                    declared_published_at="2026-03-05",
                    task_cutoff="2026-03-01",
                ),
            )
            trace_root = private / "model-traces"
            artifact_root = private / "artifacts" / "core"
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_route_responses() + _route_responses(),
            ):
                outcome = FinAuditGate(
                    artifact_root=artifact_root,
                    model=OllamaModelAdapter(trace_root=trace_root),
                    model_trace_root=trace_root,
                    private_dev_profile=profile,
                ).run(task)
            replay = FinAuditGate(
                artifact_root=artifact_root,
                model_trace_root=trace_root,
            ).replay(outcome.run_ref)

        self.assertIs(Decision.HUMAN_REVIEW, outcome.decision)
        self.assertEqual(("POST_CUTOFF_DOCUMENT",), outcome.reason_codes)
        self.assertTrue(replay.consistent, replay.reason)
        self.assertIs(Decision.HUMAN_REVIEW, replay.decision)

    def test_no_admissible_evidence_profile_never_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = _private_task()
            profile = _profile(private, _no_evidence_profile_payload(NATURAL_DOCUMENT))
            trace_root = private / "model-traces"
            artifact_root = private / "artifacts" / "core"
            with patch(
                "finauditgate.adapters.ollama._open_no_redirect",
                side_effect=_route_responses() + _route_responses(),
            ):
                outcome = FinAuditGate(
                    artifact_root=artifact_root,
                    model=OllamaModelAdapter(trace_root=trace_root),
                    model_trace_root=trace_root,
                    private_dev_profile=profile,
                ).run(task)
            replay = FinAuditGate(
                artifact_root=artifact_root,
                model_trace_root=trace_root,
            ).replay(outcome.run_ref)

        self.assertIs(Decision.HUMAN_REVIEW, outcome.decision)
        self.assertEqual(("NO_ADMISSIBLE_EVIDENCE",), outcome.reason_codes)
        self.assertTrue(replay.consistent, replay.reason)
        self.assertIs(Decision.HUMAN_REVIEW, replay.decision)

    def test_private_mode_without_a_frozen_profile_cannot_accept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = _private_task()
            trace_root = private / "model-traces"
            execution = _traced_execution(
                trace_root,
                task,
                _candidate(),
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "REVIEWED_VALIDATION_PROFILE_REQUIRED",
            ):
                FinAuditGate(
                    artifact_root=private / "artifacts" / "core",
                    model=_TraceSequenceModel((execution,)),
                    model_trace_root=trace_root,
                ).run(task)

    def test_private_mode_requires_a_traced_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            private = _workspace(temporary_directory)
            task = _private_task()
            profile = _profile(private, _profile_payload(NATURAL_DOCUMENT))

            class UntracedModel:
                def propose(self, requested: AuditTask, attempt_index: int = 0):
                    del requested, attempt_index
                    return _candidate()

            artifact_root = private / "artifacts" / "core"
            with self.assertRaisesRegex(RuntimeError, "REVIEWED_MODEL_TRACE_REQUIRED"):
                FinAuditGate(
                    artifact_root=artifact_root,
                    model=UntracedModel(),
                    private_dev_profile=profile,
                ).run(task)
            self.assertFalse((artifact_root / "runs").exists())

    def test_profile_contract_rejects_unknown_fields_and_boolean_integers(
        self,
    ) -> None:
        base = _profile_payload(NATURAL_DOCUMENT)
        unknown = {**base, "unreviewed_rule": True}
        wrong_context = {
            **base,
            "calculation": {
                **base["calculation"],
                "decimal_context": {
                    **base["calculation"]["decimal_context"],
                    "precision": True,
                },
            },
        }
        empty_with_formula = {**base, "evidence_allowlist": []}
        for name, payload in (
            ("unknown", unknown),
            ("boolean", wrong_context),
            ("empty-with-formula", empty_with_formula),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError,
                "PRIVATE_VALIDATION_PROFILE_INVALID",
            ):
                PrivateDevValidationProfile.from_bytes(canonical_json_bytes(payload))

    def test_profile_rejects_incompatible_formula_semantics(self) -> None:
        base = _profile_payload(NATURAL_DOCUMENT)
        incompatible = json.loads(json.dumps(base))
        incompatible["evidence_allowlist"][1]["normalized_semantics"]["currency"] = "EUR"

        with self.assertRaisesRegex(ValueError, "PRIVATE_VALIDATION_PROFILE_INVALID"):
            PrivateDevValidationProfile.from_bytes(canonical_json_bytes(incompatible))


if __name__ == "__main__":
    unittest.main()
