"""Closed public contracts for the FinResearchOps Application Module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import re
from typing import ClassVar, Literal

from finauditgate import Decision, FrozenDocumentPackage, ReplayReport, RunRef


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class ApplicationOperation(str, Enum):
    CREATE_CASE = "CREATE_CASE"
    RUN_ANALYSIS = "RUN_ANALYSIS"
    SUBMIT_REVIEW = "SUBMIT_REVIEW"
    EXPORT_CHANGE_PACKET = "EXPORT_CHANGE_PACKET"
    REPLAY_RUN = "REPLAY_RUN"


class CaseStatus(str, Enum):
    CREATED = "CREATED"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    APPROVED = "APPROVED"
    RETURNED = "RETURNED"
    REJECTED = "REJECTED"


class ReviewAction(str, Enum):
    APPROVE = "APPROVE"
    RETURN = "RETURN"
    REJECT = "REJECT"


class ApplicationError(RuntimeError):
    """A fail-closed command, state, reference, or integrity failure."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise ValueError("ApplicationError code must be a non-empty string")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CreateCase:
    schema_version: ClassVar[str] = "finresearchops.command.create-case/v1"

    question: str
    cutoff: date
    document: FrozenDocumentPackage
    answer_contract: Literal["PERCENTAGE_CHANGE"] = "PERCENTAGE_CHANGE"
    risk_class: Literal["LOW", "MEDIUM", "MATERIAL"] = "LOW"
    mode: Literal[
        "SYNTHETIC_DEV",
        "PRIVATE_DEV",
        "POST_FREEZE_EVAL",
    ] = "SYNTHETIC_DEV"

    def __post_init__(self) -> None:
        if type(self.question) is not str or not self.question.strip():
            raise ValueError("question must be a non-empty string")
        if type(self.cutoff) is not date:
            raise TypeError("cutoff must be a date")
        if type(self.document) is not FrozenDocumentPackage:
            raise TypeError("document must be a FrozenDocumentPackage")
        if type(self.answer_contract) is not str:
            raise TypeError("answer_contract must be a string")
        if self.answer_contract != "PERCENTAGE_CHANGE":
            raise ValueError("unsupported answer_contract")
        if type(self.risk_class) is not str:
            raise TypeError("risk_class must be a string")
        if self.risk_class not in {"LOW", "MEDIUM", "MATERIAL"}:
            raise ValueError("unsupported risk_class")
        if type(self.mode) is not str:
            raise TypeError("mode must be a string")
        if self.mode not in {
            "SYNTHETIC_DEV",
            "PRIVATE_DEV",
            "POST_FREEZE_EVAL",
        }:
            raise ValueError("unsupported mode")


@dataclass(frozen=True, slots=True)
class RunAnalysis:
    schema_version: ClassVar[str] = "finresearchops.command.run-analysis/v1"

    case_ref: str

    def __post_init__(self) -> None:
        _validate_case_ref(self.case_ref)


@dataclass(frozen=True, slots=True)
class SubmitReview:
    schema_version: ClassVar[str] = "finresearchops.command.submit-review/v1"

    case_ref: str
    run_ref: RunRef
    action: ReviewAction
    reason: str

    def __post_init__(self) -> None:
        _validate_case_ref(self.case_ref)
        if type(self.run_ref) is not RunRef:
            raise TypeError("run_ref must be a RunRef")
        if type(self.action) is not ReviewAction:
            raise TypeError("action must be a ReviewAction")
        if type(self.reason) is not str or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ExportChangePacket:
    schema_version: ClassVar[str] = (
        "finresearchops.command.export-change-packet/v1"
    )

    case_ref: str
    run_ref: RunRef

    def __post_init__(self) -> None:
        _validate_case_ref(self.case_ref)
        if type(self.run_ref) is not RunRef:
            raise TypeError("run_ref must be a RunRef")


@dataclass(frozen=True, slots=True)
class ReplayRun:
    schema_version: ClassVar[str] = "finresearchops.command.replay-run/v1"

    run_ref: RunRef

    def __post_init__(self) -> None:
        if type(self.run_ref) is not RunRef:
            raise TypeError("run_ref must be a RunRef")


@dataclass(frozen=True, slots=True)
class SourceRefView:
    source_id: str
    document_name: str
    document_sha256: str
    declared_published_at: date


@dataclass(frozen=True, slots=True)
class WorkpaperView:
    workpaper_ref: str
    run_ref: RunRef
    machine_decision: Decision
    answer: str | None
    answer_unit: str | None
    document_sha256: str
    evidence_ledger_ref: str
    formula_ref: str
    candidate_artifact_ref: str
    attempts_artifact_ref: str | None
    trace_summary_ref: str
    trace_summary_status: str
    gate_reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewRecordView:
    review_ref: str
    run_ref: RunRef
    workpaper_ref: str
    action: ReviewAction
    reason: str
    recorded_at: datetime
    previous_case_status: CaseStatus
    resulting_case_status: CaseStatus


@dataclass(frozen=True, slots=True)
class VerifiedFact:
    evidence_id: str
    metric: str
    period: str
    value: str
    unit: str
    metric_basis: str | None = None
    currency: str | None = None
    scale: str | None = None
    sign: str | None = None
    span_sha256: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("evidence_id", "metric", "period", "value", "unit"):
            value = getattr(self, field_name)
            if type(value) is not str or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        for field_name in ("metric_basis", "currency", "scale", "sign"):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not str or not value):
                raise ValueError(
                    f"{field_name} must be null or a non-empty string"
                )
        if self.span_sha256 is not None and (
            type(self.span_sha256) is not str
            or not _SHA256_HEX.fullmatch(self.span_sha256)
        ):
            raise ValueError("span_sha256 must be null or a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class CalculationRecord:
    operation: str
    operand_ids: tuple[str, ...]
    result: str
    output_unit: str
    calculation_policy_sha256: str

    def __post_init__(self) -> None:
        for field_name in ("operation", "result", "output_unit"):
            value = getattr(self, field_name)
            if type(value) is not str or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        if (
            type(self.operand_ids) is not tuple
            or not self.operand_ids
            or any(
                type(operand_id) is not str or not operand_id
                for operand_id in self.operand_ids
            )
        ):
            raise ValueError("operand_ids must be non-empty strings")
        if (
            type(self.calculation_policy_sha256) is not str
            or not _SHA256_HEX.fullmatch(self.calculation_policy_sha256)
        ):
            raise ValueError(
                "calculation_policy_sha256 must be a SHA-256 digest"
            )


@dataclass(frozen=True, slots=True)
class ChangePacketView:
    packet_ref: str
    run_ref: RunRef
    review_ref: str
    workpaper_ref: str
    proposal_only: bool
    verified_facts: tuple[VerifiedFact, ...]
    calculations: tuple[CalculationRecord, ...]
    impact_statement: str
    unresolved_items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayRecordView:
    replay_ref: str
    report: ReplayReport


@dataclass(frozen=True, slots=True)
class ApplicationOutcome:
    operation: ApplicationOperation
    case_ref: str
    status: CaseStatus
    run_ref: RunRef | None = None
    workpaper_ref: str | None = None
    review_ref: str | None = None
    packet_ref: str | None = None
    replay_ref: str | None = None
    machine_decision: Decision | None = None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaseView:
    case_ref: str
    question: str
    cutoff: date
    source_ref: SourceRefView
    status: CaseStatus
    run_refs: tuple[RunRef, ...]
    workpapers: tuple[WorkpaperView, ...]
    reviews: tuple[ReviewRecordView, ...]
    packets: tuple[ChangePacketView, ...]
    replays: tuple[ReplayRecordView, ...]
    latest_machine_decision: Decision | None
    latest_gate_reason_codes: tuple[str, ...]


def _validate_case_ref(case_ref: object) -> None:
    if (
        type(case_ref) is not str
        or not case_ref.startswith("case-")
        or not _SHA256_HEX.fullmatch(case_ref.removeprefix("case-"))
    ):
        raise ValueError("case_ref must be a content-addressed Case reference")
