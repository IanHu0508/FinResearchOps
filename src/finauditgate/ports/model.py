"""Candidate-generation contracts at the internal model Seam."""

from dataclasses import dataclass
from typing import Protocol

from finauditgate.contracts import AuditTask


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


class CandidateModel(Protocol):
    """Internal Interface implemented by scripted and local Adapters."""

    def propose(self, task: AuditTask) -> ModelCandidate: ...
