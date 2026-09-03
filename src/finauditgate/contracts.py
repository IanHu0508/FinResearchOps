"""Public contracts for the FinAuditGate Module."""

from dataclasses import dataclass
from datetime import date
from enum import Enum
import re
from typing import Literal


RUN_SCHEMA_VERSION = "finauditgate.run/v1"
REPLAY_SCHEMA_VERSION = "finauditgate.replay/v5"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class Decision(str, Enum):
    """The only hard decisions returned by the gate."""

    ACCEPT = "ACCEPT"
    RETRY = "RETRY"
    ABSTAIN = "ABSTAIN"
    HUMAN_REVIEW = "HUMAN_REVIEW"


@dataclass(frozen=True, slots=True)
class FrozenDocumentPackage:
    """Immutable submitted bytes plus declared, not verified, metadata."""

    source_id: str
    document_name: str
    document_bytes: bytes
    declared_published_at: date

    def __post_init__(self) -> None:
        for field_name in ("source_id", "document_name"):
            value = getattr(self, field_name)
            if type(value) is not str:
                raise TypeError(f"{field_name} must be a string")
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if type(self.document_bytes) is not bytes:
            raise TypeError("document_bytes must be immutable bytes")
        if not self.document_bytes:
            raise ValueError("document_bytes must not be empty")
        if type(self.declared_published_at) is not date:
            raise TypeError("declared_published_at must be a date")


# The two modes decided against a reviewed validation profile on a real filing,
# as opposed to the public synthetic fixture.  They run through identical
# checks; the mode is what keeps development runs and post-freeze transfer runs
# from ever being pooled, in the artifacts and in the profile each one accepts.
REVIEWED_PROFILE_MODES = ("PRIVATE_DEV", "POST_FREEZE_EVAL")


@dataclass(frozen=True, slots=True)
class AuditTask:
    """One question against one immutable submitted document package."""

    task_id: str
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
        for field_name in ("task_id", "question"):
            value = getattr(self, field_name)
            if type(value) is not str:
                raise TypeError(f"{field_name} must be a string")
        if not self.task_id.strip():
            raise ValueError("task_id must not be empty")
        if not self.question.strip():
            raise ValueError("question must not be empty")
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
class RunRef:
    """Stable reference to an append-only run artifact set."""

    run_id: str

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not _SHA256_HEX.fullmatch(
            self.run_id
        ):
            raise ValueError(
                "run_id must be a lowercase SHA-256 hex digest"
            )


@dataclass(frozen=True, slots=True)
class AuditOutcome:
    """Result returned by :meth:`FinAuditGate.run`."""

    schema_version: str
    task_id: str
    decision: Decision
    answer: str | None
    answer_unit: str | None
    run_ref: RunRef
    document_sha256: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """Offline integrity and deterministic-calculation replay result."""

    schema_version: str
    run_ref: RunRef
    consistent: bool
    decision: Decision | None
    answer: str | None
    answer_unit: str | None
    verified_artifact_count: int
    reason: str | None = None
