"""Append-only paired-run and manual-QA records for private evaluation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
from typing import Protocol

from finauditgate.contracts import AuditTask
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.private_storage import (
    PrivateWorkspaceAnchor,
    require_private_storage_root,
    resolve_private_workspace_anchor,
)


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_MAX_PAIRED_RECORD_BYTES = 1_048_576
_MAX_PAIRED_OUTPUT_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class PairedArmResult:
    """Actual immutable output bytes and normalized outcome from one arm."""

    arm_id: str
    output_bytes: bytes
    config_sha256: str
    answer: str | None
    decision: str
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.arm_id) is not str or not self.arm_id:
            raise ValueError("arm_id must be a non-empty string")
        if (
            type(self.output_bytes) is not bytes
            or not self.output_bytes
            or len(self.output_bytes) > _MAX_PAIRED_OUTPUT_BYTES
        ):
            raise ValueError("output_bytes must be bounded immutable bytes")
        if (
            type(self.config_sha256) is not str
            or not _SHA256_HEX.fullmatch(self.config_sha256)
        ):
            raise ValueError("config_sha256 must be a SHA-256 digest")
        if self.answer is not None and type(self.answer) is not str:
            raise TypeError("answer must be a string or None")
        if type(self.decision) is not str or not self.decision:
            raise ValueError("decision must be a non-empty string")
        if type(self.reason_codes) is not tuple or any(
            type(code) is not str or not code for code in self.reason_codes
        ):
            raise ValueError("reason_codes must be non-empty strings")


class PairedArm(Protocol):
    """One independently configured side of a paired evaluation."""

    def run(self, task: AuditTask) -> PairedArmResult: ...


@dataclass(frozen=True, slots=True)
class PairedExecutionRef:
    pair_id: str

    def __post_init__(self) -> None:
        if type(self.pair_id) is not str or not _SHA256_HEX.fullmatch(
            self.pair_id
        ):
            raise ValueError("pair_id must be a SHA-256 digest")

    @property
    def relative_path(self) -> str:
        return f"paired/executions/sha256/{self.pair_id}.json"


@dataclass(frozen=True, slots=True)
class PairedQARef:
    qa_id: str

    def __post_init__(self) -> None:
        if type(self.qa_id) is not str or not _SHA256_HEX.fullmatch(self.qa_id):
            raise ValueError("qa_id must be a SHA-256 digest")

    @property
    def relative_path(self) -> str:
        return f"paired/human-qa/sha256/{self.qa_id}.json"


class PairedRunner:
    """Run both arms, then preserve human QA as a separate immutable fact."""

    def __init__(
        self,
        *,
        private_root: Path,
        baseline: PairedArm,
        gated: PairedArm,
        private_workspace_anchor: PrivateWorkspaceAnchor | None = None,
    ) -> None:
        self._anchor = (
            private_workspace_anchor
            or resolve_private_workspace_anchor(
                private_root,
                purpose="paired-evaluation-root",
            )
        )
        self._root = require_private_storage_root(
            private_root,
            purpose="paired-evaluation-root",
            anchor=self._anchor,
        )
        if not callable(getattr(baseline, "run", None)):
            raise TypeError("baseline must implement run(task)")
        if not callable(getattr(gated, "run", None)):
            raise TypeError("gated must implement run(task)")
        self._baseline = baseline
        self._gated = gated

    def run(self, task: AuditTask) -> PairedExecutionRef:
        """Execute both arms without inferring either answer's correctness."""

        if type(task) is not AuditTask:
            raise TypeError("task must be an AuditTask")
        baseline = self._baseline.run(task)
        gated = self._gated.run(task)
        if type(baseline) is not PairedArmResult or baseline.arm_id != "baseline":
            raise ValueError("baseline arm returned an invalid result")
        if type(gated) is not PairedArmResult or gated.arm_id != "gated":
            raise ValueError("gated arm returned an invalid result")
        baseline_payload = self._persist_arm_output(baseline)
        gated_payload = self._persist_arm_output(gated)
        body = {
            "schema_version": "finresearchops.paired-execution/v2",
            "task_identity": _task_identity(task),
            "baseline": baseline_payload,
            "gated": gated_payload,
            "qa_status": "AWAITING_HUMAN_QA",
        }
        pair_id = sha256_hex(canonical_json_bytes(body))
        payload = {**body, "pair_id": pair_id}
        pair_ref = PairedExecutionRef(pair_id)
        path = self._root / pair_ref.relative_path
        require_private_storage_root(
            path,
            purpose="paired-execution",
            anchor=self._anchor,
        )
        write_once(path, canonical_json_bytes(payload))
        return pair_ref

    def record_human_qa(
        self,
        pair_ref: PairedExecutionRef,
        *,
        baseline_correct: bool,
        gated_correct: bool,
        reviewer: str,
        rationale: str,
    ) -> PairedQARef:
        """Append one human assessment; never replace an earlier assessment."""

        if type(pair_ref) is not PairedExecutionRef:
            raise TypeError("pair_ref must be a PairedExecutionRef")
        if type(baseline_correct) is not bool or type(gated_correct) is not bool:
            raise TypeError("correctness assessments must be booleans")
        if type(reviewer) is not str or not reviewer.strip():
            raise ValueError("reviewer must be a non-empty string")
        if type(rationale) is not str or not rationale.strip():
            raise ValueError("rationale must be a non-empty string")
        self._read_verified_pair(pair_ref)
        body = {
            "schema_version": "finresearchops.paired-human-qa/v1",
            "pair_id": pair_ref.pair_id,
            "baseline_correct": baseline_correct,
            "gated_correct": gated_correct,
            "classification": _classification(
                baseline_correct,
                gated_correct,
            ),
            "reviewer": reviewer,
            "rationale": rationale,
        }
        qa_id = sha256_hex(canonical_json_bytes(body))
        payload = {**body, "qa_id": qa_id}
        qa_ref = PairedQARef(qa_id)
        path = self._root / qa_ref.relative_path
        require_private_storage_root(
            path,
            purpose="paired-human-qa",
            anchor=self._anchor,
        )
        write_once(path, canonical_json_bytes(payload))
        return qa_ref

    def _read_verified_pair(
        self,
        pair_ref: PairedExecutionRef,
    ) -> dict[str, object]:
        path = self._root / pair_ref.relative_path
        require_private_storage_root(
            path,
            purpose="paired-execution",
            anchor=self._anchor,
        )
        try:
            with path.open("rb") as pair_file:
                payload_bytes = pair_file.read(_MAX_PAIRED_RECORD_BYTES + 1)
            if not payload_bytes or len(payload_bytes) > _MAX_PAIRED_RECORD_BYTES:
                raise ValueError
            payload = json.loads(payload_bytes)
            if type(payload) is not dict or set(payload) != {
                "schema_version",
                "pair_id",
                "task_identity",
                "baseline",
                "gated",
                "qa_status",
            }:
                raise ValueError
            body = {key: value for key, value in payload.items() if key != "pair_id"}
            if (
                payload["schema_version"]
                != "finresearchops.paired-execution/v2"
                or payload["qa_status"] != "AWAITING_HUMAN_QA"
                or payload["pair_id"] != pair_ref.pair_id
                or sha256_hex(canonical_json_bytes(body)) != pair_ref.pair_id
                or canonical_json_bytes(payload) != payload_bytes
            ):
                raise ValueError
            self._verify_arm_output(payload["baseline"], "baseline")
            self._verify_arm_output(payload["gated"], "gated")
            return payload
        except (OSError, RecursionError, UnicodeError, ValueError) as exc:
            raise RuntimeError(
                "PAIRED_EXECUTION_INTEGRITY_FAILURE"
            ) from exc

    def _persist_arm_output(
        self,
        result: PairedArmResult,
    ) -> dict[str, object]:
        output_sha256 = sha256_hex(result.output_bytes)
        output_ref = (
            f"paired/arm-outputs/{result.arm_id}/sha256/"
            f"{output_sha256}.bin"
        )
        output_path = self._root / output_ref
        require_private_storage_root(
            output_path,
            purpose="paired-arm-output",
            anchor=self._anchor,
        )
        write_once(output_path, result.output_bytes)
        return {
            "arm_id": result.arm_id,
            "output_ref": output_ref,
            "output_sha256": output_sha256,
            "config_sha256": result.config_sha256,
            "answer": result.answer,
            "decision": result.decision,
            "reason_codes": list(result.reason_codes),
        }

    def _verify_arm_output(self, value: object, arm_id: str) -> None:
        expected_fields = {
            "arm_id",
            "output_ref",
            "output_sha256",
            "config_sha256",
            "answer",
            "decision",
            "reason_codes",
        }
        if (
            type(value) is not dict
            or set(value) != expected_fields
            or value["arm_id"] != arm_id
            or type(value["output_sha256"]) is not str
            or not _SHA256_HEX.fullmatch(value["output_sha256"])
            or type(value["config_sha256"]) is not str
            or not _SHA256_HEX.fullmatch(value["config_sha256"])
            or not _safe_relative_ref(value["output_ref"])
            or value["output_ref"]
            != (
                f"paired/arm-outputs/{arm_id}/sha256/"
                f"{value['output_sha256']}.bin"
            )
        ):
            raise ValueError
        output_path = self._root / value["output_ref"]
        require_private_storage_root(
            output_path,
            purpose="paired-arm-output",
            anchor=self._anchor,
        )
        with output_path.open("rb") as output_file:
            output_bytes = output_file.read(_MAX_PAIRED_OUTPUT_BYTES + 1)
        if (
            not output_bytes
            or len(output_bytes) > _MAX_PAIRED_OUTPUT_BYTES
            or sha256_hex(output_bytes) != value["output_sha256"]
        ):
            raise ValueError


def _task_identity(task: AuditTask) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "question_sha256": sha256_hex(task.question.encode("utf-8")),
        "document_sha256": sha256_hex(task.document.document_bytes),
        "source_id": task.document.source_id,
        "document_name": task.document.document_name,
        "declared_published_at": task.document.declared_published_at.isoformat(),
        "cutoff": task.cutoff.isoformat(),
        "mode": task.mode,
        "risk_class": task.risk_class,
        "answer_contract": task.answer_contract,
    }


def _classification(baseline_correct: bool, gated_correct: bool) -> str:
    if not baseline_correct and gated_correct:
        return "WRONG_TO_RIGHT"
    if baseline_correct and not gated_correct:
        return "RIGHT_TO_WRONG"
    if baseline_correct:
        return "RIGHT_TO_RIGHT"
    return "WRONG_TO_WRONG"


def _safe_relative_ref(value: object) -> bool:
    if type(value) is not str or not value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and value == str(path)
