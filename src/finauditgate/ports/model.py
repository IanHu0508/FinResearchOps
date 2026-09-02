"""Candidate-generation contracts at the internal model Seam."""

from dataclasses import dataclass
import re
from typing import Protocol

from finauditgate.contracts import AuditTask


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
RECEIPT_SCHEMA_VERSION = "finauditgate.model-trace-receipt/v3"


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    """Untrusted evidence coordinates and semantics proposed by a model."""

    evidence_id: str
    byte_start: int
    byte_end: int
    metric: str
    period: str
    value: str
    unit: str
    metric_basis: str | None = None
    currency: str | None = None
    scale: str | None = None
    sign: str | None = None


@dataclass(frozen=True, slots=True)
class CalculationCandidate:
    """An untrusted request to execute one allowlisted calculation."""

    operation: str
    operand_ids: tuple[str, ...]
    output_unit: str
    quantize: str


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    """Candidate proposal consumed by deterministic validation."""

    evidence: tuple[EvidenceCandidate, ...]
    calculation: CalculationCandidate


@dataclass(frozen=True, slots=True)
class ModelTraceReceipt:
    """Public-safe identity of one private, content-addressed raw trace.

    The receipt mirrors the metadata of the saved exchange.  It carries hashes
    and codes only; the raw request and response stay in the private trace.
    """

    provider: str
    transport_origin: str
    runtime_version: str
    model_id: str
    observed_model_digest: str | None
    prompt_sha256: str
    tool_schema_sha256: str
    generation_config_sha256: str
    task_id: str
    attempt_index: int
    request_sha256: str
    response_sha256: str
    raw_trace_sha256: str
    raw_trace_ref: str
    parse_status: str
    failure_code: str | None
    termination_reason: str
    response_derived_proposal_sha256: str | None
    schema_version: str = RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RECEIPT_SCHEMA_VERSION:
            raise ValueError("unsupported model trace receipt schema")
        string_fields = (
            "provider",
            "transport_origin",
            "runtime_version",
            "model_id",
            "task_id",
            "parse_status",
            "termination_reason",
        )
        if any(
            type(getattr(self, field_name)) is not str
            or not getattr(self, field_name)
            for field_name in string_fields
        ):
            raise ValueError("trace receipt strings must be non-empty")
        hash_fields = (
            "prompt_sha256",
            "tool_schema_sha256",
            "generation_config_sha256",
            "request_sha256",
            "response_sha256",
            "raw_trace_sha256",
        )
        if any(
            type(getattr(self, field_name)) is not str
            or not _SHA256_HEX.fullmatch(getattr(self, field_name))
            for field_name in hash_fields
        ):
            raise ValueError("trace receipt hashes must be SHA-256 digests")
        for value in (
            self.observed_model_digest,
            self.response_derived_proposal_sha256,
        ):
            if value is not None and (
                type(value) is not str or not _SHA256_HEX.fullmatch(value)
            ):
                raise ValueError("optional trace hashes must be SHA-256 digests")
        if type(self.attempt_index) is not int or self.attempt_index not in {0, 1}:
            raise ValueError("trace receipt attempt_index must be 0 or 1")
        if self.raw_trace_ref != (
            "model-calls/sha256/" + self.raw_trace_sha256 + ".json"
        ):
            raise ValueError("raw_trace_ref must be content-addressed")
        if self.parse_status == "CANDIDATE_PARSED":
            if (
                self.failure_code is not None
                or self.response_derived_proposal_sha256 is None
            ):
                raise ValueError("parsed trace cannot contain a failure code")
        elif self.parse_status == "CANDIDATE_REJECTED":
            if (
                type(self.failure_code) is not str
                or not self.failure_code
                or self.response_derived_proposal_sha256 is not None
            ):
                raise ValueError("rejected trace must contain a failure code")
        else:
            raise ValueError("unsupported trace parse_status")


@dataclass(frozen=True, slots=True)
class ModelExecution:
    """One proposal or fail-closed parse result plus its private trace receipt."""

    proposal: ModelCandidate | None
    trace_receipt: ModelTraceReceipt
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.trace_receipt) is not ModelTraceReceipt:
            raise TypeError("trace_receipt must be a ModelTraceReceipt")
        if self.proposal is None:
            if type(self.failure_code) is not str or not self.failure_code:
                raise ValueError("failed execution requires a failure code")
            if self.failure_code != self.trace_receipt.failure_code:
                raise ValueError("execution and trace failure codes must match")
        elif type(self.proposal) is not ModelCandidate or self.failure_code is not None:
            raise ValueError("parsed execution requires one ModelCandidate")


class CandidateModel(Protocol):
    """Internal Interface implemented by the scripted and local Adapters."""

    def propose(
        self,
        task: AuditTask,
        attempt_index: int = 0,
    ) -> ModelCandidate | ModelExecution: ...
