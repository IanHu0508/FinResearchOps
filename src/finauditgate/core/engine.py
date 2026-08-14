"""Deep implementation behind the two-method FinAuditGate Interface."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import (
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    localcontext,
)
from datetime import date
import hashlib
import json
from pathlib import Path
import re

from finauditgate.ports.model import (
    CalculationCandidate,
    CandidateModel,
    EvidenceCandidate,
    ModelCandidate,
)
from finauditgate.contracts import (
    AuditOutcome,
    AuditTask,
    Decision,
    FrozenDocumentPackage,
    REPLAY_SCHEMA_VERSION,
    RUN_SCHEMA_VERSION,
    ReplayReport,
    RunRef,
)
from finauditgate.core.artifacts import (
    canonical_json_bytes,
    sha256_hex,
    write_once,
)
from finauditgate.core.m2 import (
    M2_DOCUMENT_SHA256,
    M2_POLICY,
    M2_POLICY_BYTES,
    M2_POLICY_SHA256,
    M2_REPLAY_SCHEMA_VERSION,
    M2ValidationFailure,
    evaluate_accepted_candidate,
    failed_artifacts,
)


_RUN_ID = re.compile(r"[0-9a-f]{64}")
_PROPOSAL_SNAPSHOT_SCHEMA_VERSION = "finauditgate.model-proposal-snapshot/v1"
_MAX_PROPOSAL_SNAPSHOT_DEPTH = 32
_MAX_PROPOSAL_SNAPSHOT_NODES = 256
_MAX_PROPOSAL_CONTAINER_ITEMS = 64
_MAX_PROPOSAL_INTEGER_BITS = 4096
_MAX_PROPOSAL_TEXT_LENGTH = 4_096
_MAX_PROPOSAL_BYTES_LENGTH = 4_096
_MAX_M2_JSON_ARTIFACT_BYTES = 16_777_216
_MAX_M2_JSON_NESTING = 128
_PROPOSAL_FAILURE_REASONS = frozenset(
    {
        "BYTES_LIMIT",
        "CONTAINER_LIMIT",
        "CYCLIC_VALUE",
        "DEPTH_LIMIT",
        "INTEGER_LIMIT",
        "NODE_LIMIT",
        "SNAPSHOT_FAILED",
        "TEXT_ENCODING_INVALID",
        "TEXT_LIMIT",
        "UNSUPPORTED_TYPE",
    }
)
_ARTIFACT_FILES = {
    "identity": "identity.json",
    "task": "task.json",
    "candidate": "candidate.json",
    "policy": "policy.json",
    "ledger": "ledger.json",
    "formula": "formula.json",
    "outcome": "outcome.json",
}
_M2_ARTIFACT_FILES = {
    **_ARTIFACT_FILES,
    "attempts": "attempts.json",
}
_M1_CALCULATION_POLICY = {
    "schema_version": "finauditgate.calculation-policy/v1",
    "operation": "growth_rate_percent",
    "output_unit": "PERCENT",
    "quantize": "0.01",
    "precision": 28,
    "rounding": "ROUND_HALF_EVEN",
    "emin": -999999,
    "emax": 999999,
    "capitals": 1,
    "clamp": 0,
    "validation_profile": "synthetic-northstar-revenue-growth/v3",
    "source_id": "synthetic-northstar-revenue-v1",
    "document_sha256": (
        "ddcad525e94d0ca8ce18155b115bfeb6d70bf032e875c1429dea53112c4c303d"
    ),
    "task_question": "What was FY2025 revenue growth versus FY2024?",
    "input_metric": "revenue",
    "current_period": "FY2025",
    "comparison_period": "FY2024",
    "input_unit": "USD_MILLION",
    "accepted_risk_class": "LOW",
}
_M1_CALCULATION_POLICY_BYTES = canonical_json_bytes(_M1_CALCULATION_POLICY)
_M1_CALCULATION_POLICY_SHA256 = sha256_hex(_M1_CALCULATION_POLICY_BYTES)

# Append-only replay compatibility registry. A later default policy may be
# added, but a recognized historical policy and its executor semantics must not
# be removed while its RunRefs remain supported.
_SUPPORTED_CALCULATION_POLICIES = {
    _M1_CALCULATION_POLICY_SHA256: (
        _M1_CALCULATION_POLICY,
        _M1_CALCULATION_POLICY_BYTES,
    ),
    M2_POLICY_SHA256: (M2_POLICY, M2_POLICY_BYTES),
}
_CURRENT_CALCULATION_POLICY_SHA256 = _M1_CALCULATION_POLICY_SHA256


class FinAuditGate:
    """Auditable financial evidence gate with a two-method Interface."""

    def __init__(
        self,
        *,
        artifact_root: Path,
        model: CandidateModel | None = None,
    ) -> None:
        self._artifact_root = Path(artifact_root)
        self._model = model

    def run(self, task: AuditTask) -> AuditOutcome:
        """Validate one candidate, calculate deterministically, and persist it."""

        if self._model is None:
            raise RuntimeError("run() requires a candidate model Adapter")

        if type(task) is not AuditTask:
            raise TypeError("task must be an AuditTask")
        try:
            document = task.document.document_bytes
            document_sha256 = sha256_hex(document)
            task_payload = _task_payload(task, document_sha256)
            task_bytes = canonical_json_bytes(task_payload)
            normalized_task_payload = json.loads(task_bytes)
            if not isinstance(normalized_task_payload, dict):
                raise TypeError("canonical task must be an object")
            adapter_task = _task_from_payload(
                normalized_task_payload,
                document,
            )
            if (
                canonical_json_bytes(
                    _task_payload(adapter_task, document_sha256)
                )
                != task_bytes
            ):
                raise ValueError("task is not canonically representable")
        except (AttributeError, TypeError, ValueError) as exc:
            raise TypeError("task could not be normalized") from exc

        if (
            document_sha256 == M2_DOCUMENT_SHA256
            or adapter_task.document.source_id == M2_POLICY["source_id"]
        ):
            return self._run_m2(
                adapter_task=adapter_task,
                document=document,
                document_sha256=document_sha256,
                task_bytes=task_bytes,
                normalized_task_payload=normalized_task_payload,
            )

        try:
            candidate = self._model.propose(adapter_task)
            candidate_payload = _candidate_payload(candidate)
            candidate_bytes = canonical_json_bytes(candidate_payload)
            normalized_candidate_payload = json.loads(candidate_bytes)
            if not isinstance(normalized_candidate_payload, dict):
                raise TypeError("canonical candidate must be an object")
            candidate = _candidate_from_payload(normalized_candidate_payload)
            if canonical_json_bytes(_candidate_payload(candidate)) != candidate_bytes:
                raise ValueError("candidate is not canonically representable")
        except Exception as exc:
            raise RuntimeError(
                "candidate model Adapter failed before a run could be committed"
            ) from exc

        # Discard the Adapter-visible object. Even a hostile Adapter using
        # object.__setattr__ cannot change the canonical task executed below.
        task = _task_from_payload(normalized_task_payload, document)
        policy_sha256 = _CURRENT_CALCULATION_POLICY_SHA256
        try:
            policy, policy_bytes = _SUPPORTED_CALCULATION_POLICIES[
                policy_sha256
            ]
        except KeyError as exc:
            raise RuntimeError(
                "current calculation policy is not registered"
            ) from exc
        task_sha256 = sha256_hex(task_bytes)
        candidate_sha256 = sha256_hex(candidate_bytes)
        identity_payload = {
            "schema_version": "finauditgate.run-identity/v1",
            "task_sha256": task_sha256,
            "candidate_sha256": candidate_sha256,
            "calculation_policy_sha256": policy_sha256,
        }
        identity_bytes = canonical_json_bytes(identity_payload)
        run_id = sha256_hex(identity_bytes)
        run_ref = RunRef(run_id=run_id)

        try:
            ledger_payload = _verified_ledger(
                document,
                candidate.evidence,
            )
            formula_payload = _execute_calculation(
                ledger_payload,
                candidate.calculation,
                task,
                policy,
                policy_sha256,
            )
            decision = Decision.ACCEPT
            answer = formula_payload["result"]
            answer_unit = formula_payload["output_unit"]
            reason_codes: tuple[str, ...] = ()
        except (
            ValueError,
            TypeError,
            KeyError,
            DecimalException,
            UnicodeDecodeError,
        ):
            ledger_payload = _rejected_ledger()
            formula_payload = _not_executed_formula()
            decision = Decision.HUMAN_REVIEW
            answer = None
            answer_unit = None
            reason_codes = ("CANDIDATE_VALIDATION_FAILED",)
        outcome_payload = {
            "schema_version": RUN_SCHEMA_VERSION,
            "task_id": task.task_id,
            "decision": decision.value,
            "answer": answer,
            "answer_unit": answer_unit,
            "reason_codes": list(reason_codes),
            "run_ref": {"run_id": run_id},
            "document_sha256": document_sha256,
        }

        artifact_payloads = {
            "identity": identity_bytes,
            "task": task_bytes,
            "candidate": candidate_bytes,
            "policy": policy_bytes,
            "ledger": canonical_json_bytes(ledger_payload),
            "formula": canonical_json_bytes(formula_payload),
            "outcome": canonical_json_bytes(outcome_payload),
        }
        run_directory = self._artifact_root / "runs" / run_id
        write_once(
            self._artifact_root / "blobs" / "sha256" / document_sha256,
            document,
        )
        for artifact_name, payload in artifact_payloads.items():
            write_once(run_directory / _ARTIFACT_FILES[artifact_name], payload)

        manifest_payload = {
            "schema_version": "finauditgate.manifest/v1",
            "run_id": run_id,
            "document_sha256": document_sha256,
            "calculation_policy_sha256": policy_sha256,
            "artifacts": {
                name: {
                    "filename": _ARTIFACT_FILES[name],
                    "sha256": sha256_hex(payload),
                }
                for name, payload in sorted(artifact_payloads.items())
            },
        }
        write_once(
            run_directory / "manifest.json",
            canonical_json_bytes(manifest_payload),
        )

        return AuditOutcome(
            schema_version=RUN_SCHEMA_VERSION,
            task_id=task.task_id,
            decision=decision,
            answer=answer,
            answer_unit=answer_unit,
            run_ref=run_ref,
            document_sha256=document_sha256,
            reason_codes=reason_codes,
        )

    def _run_m2(
        self,
        *,
        adapter_task: AuditTask,
        document: bytes,
        document_sha256: str,
        task_bytes: bytes,
        normalized_task_payload: dict[str, object],
    ) -> AuditOutcome:
        """Execute the content-bound M2 profile behind the public run seam."""

        task = _task_from_payload(normalized_task_payload, document)
        attempt_entries: list[dict[str, object]] = []
        candidate_payload: dict[str, object] | None = None
        candidate_bytes: bytes | None = None
        candidate_sha256: str | None = None
        for attempt_index in range(2):
            proposal_snapshot: dict[str, object] | None = None
            proposal_sha256: str | None = None
            try:
                proposed = self._model.propose(
                    adapter_task,
                    attempt_index=attempt_index,
                )
            except LookupError as exc:
                if (
                    attempt_index == 1
                    and len(attempt_entries) == 1
                    and attempt_entries[0]["disposition"]
                    == Decision.RETRY.value
                ):
                    attempt_reasons = ("CANDIDATE_PROPOSAL_MISSING",)
                else:
                    raise RuntimeError(
                        "candidate model Adapter failed before a run could be "
                        "committed"
                    ) from exc
            except Exception as exc:
                raise RuntimeError(
                    "candidate model Adapter failed before a run could be "
                    "committed"
                ) from exc
            else:
                attempt_reasons = ()
                proposal_snapshot = _proposal_snapshot(proposed)
                proposal_sha256 = sha256_hex(
                    canonical_json_bytes(proposal_snapshot)
                )
                try:
                    candidate_payload, candidate = (
                        _candidate_from_proposal_snapshot(proposal_snapshot)
                    )
                    candidate_bytes = canonical_json_bytes(candidate_payload)
                except Exception:
                    candidate_payload = None
                    candidate_bytes = None
                    candidate_sha256 = None
                    attempt_reasons = ("CANDIDATE_SHAPE_INVALID",)

            if attempt_reasons:
                attempt_entries.append(
                    {
                        "attempt_index": attempt_index,
                        "proposal": proposal_snapshot,
                        "proposal_sha256": proposal_sha256,
                        "candidate": None,
                        "candidate_sha256": None,
                        "disposition": Decision.RETRY.value,
                        "reason_codes": list(attempt_reasons),
                    }
                )
                if attempt_index == 0:
                    continue
                decision = Decision.RETRY
                reason_codes = attempt_reasons + (
                    "RETRY_BUDGET_EXHAUSTED",
                )
                answer = None
                answer_unit = None
                ledger_payload, formula_payload = failed_artifacts(
                    decision,
                    reason_codes,
                )
                break

            assert candidate_payload is not None
            assert candidate_bytes is not None
            candidate_sha256 = sha256_hex(candidate_bytes)
            try:
                ledger_payload, formula_payload = evaluate_accepted_candidate(
                    document,
                    candidate,
                    task,
                )
            except M2ValidationFailure as failure:
                attempt_entries.append(
                    {
                        "attempt_index": attempt_index,
                        "proposal": proposal_snapshot,
                        "proposal_sha256": proposal_sha256,
                        "candidate": candidate_payload,
                        "candidate_sha256": candidate_sha256,
                        "disposition": failure.decision.value,
                        "reason_codes": list(failure.reason_codes),
                    }
                )
                if (
                    failure.decision is Decision.RETRY
                    and attempt_index == 0
                ):
                    continue
                decision = failure.decision
                reason_codes = failure.reason_codes
                if decision is Decision.RETRY:
                    reason_codes += ("RETRY_BUDGET_EXHAUSTED",)
                answer = None
                answer_unit = None
                ledger_payload, formula_payload = failed_artifacts(
                    decision,
                    reason_codes,
                )
                break
            attempt_entries.append(
                {
                    "attempt_index": attempt_index,
                    "proposal": proposal_snapshot,
                    "proposal_sha256": proposal_sha256,
                    "candidate": candidate_payload,
                    "candidate_sha256": candidate_sha256,
                    "disposition": Decision.ACCEPT.value,
                    "reason_codes": [],
                }
            )
            decision = Decision.ACCEPT
            answer = formula_payload["result"]
            answer_unit = formula_payload["output_unit"]
            reason_codes: tuple[str, ...] = ()
            break
        else:
            raise RuntimeError("M2 retry loop ended without a decision")

        if candidate_bytes is None:
            candidate_bytes = canonical_json_bytes(None)
            candidate_sha256 = sha256_hex(candidate_bytes)

        attempts_payload = {
            "schema_version": "finauditgate.attempts/v2",
            "retry_budget": 1,
            "attempts": attempt_entries,
            "final_decision": decision.value,
            "final_reason_codes": list(reason_codes),
        }
        attempts_bytes = canonical_json_bytes(attempts_payload)
        identity_payload = {
            "schema_version": "finauditgate.run-identity/v2",
            "task_sha256": sha256_hex(task_bytes),
            "candidate_sha256": candidate_sha256,
            "attempts_sha256": sha256_hex(attempts_bytes),
            "calculation_policy_sha256": M2_POLICY_SHA256,
        }
        identity_bytes = canonical_json_bytes(identity_payload)
        run_id = sha256_hex(identity_bytes)
        run_ref = RunRef(run_id=run_id)
        outcome_payload = {
            "schema_version": RUN_SCHEMA_VERSION,
            "task_id": task.task_id,
            "decision": decision.value,
            "answer": answer,
            "answer_unit": answer_unit,
            "reason_codes": list(reason_codes),
            "run_ref": {"run_id": run_id},
            "document_sha256": document_sha256,
        }
        artifact_payloads = {
            "identity": identity_bytes,
            "task": task_bytes,
            "candidate": candidate_bytes,
            "attempts": attempts_bytes,
            "policy": M2_POLICY_BYTES,
            "ledger": canonical_json_bytes(ledger_payload),
            "formula": canonical_json_bytes(formula_payload),
            "outcome": canonical_json_bytes(outcome_payload),
        }
        run_directory = self._artifact_root / "runs" / run_id
        write_once(
            self._artifact_root / "blobs" / "sha256" / document_sha256,
            document,
        )
        for artifact_name, payload in artifact_payloads.items():
            write_once(
                run_directory / _M2_ARTIFACT_FILES[artifact_name],
                payload,
            )
        manifest_payload = {
            "schema_version": "finauditgate.manifest/v2",
            "run_id": run_id,
            "document_sha256": document_sha256,
            "calculation_policy_sha256": M2_POLICY_SHA256,
            "artifacts": {
                name: {
                    "filename": _M2_ARTIFACT_FILES[name],
                    "sha256": sha256_hex(payload),
                }
                for name, payload in sorted(artifact_payloads.items())
            },
        }
        write_once(
            run_directory / "manifest.json",
            canonical_json_bytes(manifest_payload),
        )
        return AuditOutcome(
            schema_version=RUN_SCHEMA_VERSION,
            task_id=task.task_id,
            decision=decision,
            answer=answer,
            answer_unit=answer_unit,
            run_ref=run_ref,
            document_sha256=document_sha256,
            reason_codes=reason_codes,
        )

    def replay(self, run_ref: RunRef) -> ReplayReport:
        """Verify and recalculate a stored run without model or network access."""

        if type(run_ref) is not RunRef:
            raise TypeError("run_ref must be a RunRef")
        run_id = run_ref.run_id
        run_ref = RunRef(run_id=run_id)

        try:
            manifest_bytes = _read_bounded_json_bytes(
                self._artifact_root / "runs" / run_id / "manifest.json"
            )
            manifest = (
                None
                if manifest_bytes is None
                or _m2_json_payload_exceeds_limits(manifest_bytes)
                else json.loads(manifest_bytes)
            )
        except (OSError, RecursionError, ValueError, TypeError):
            manifest = None
        run_directory = self._artifact_root / "runs" / run_id
        is_m2_run = (
            isinstance(manifest, dict)
            and manifest.get("schema_version")
            == "finauditgate.manifest/v2"
        ) or _has_m2_artifact_identity(run_directory, run_id)
        if is_m2_run:
            report = self._replay_m2(run_ref)
            if report.schema_version == M2_REPLAY_SCHEMA_VERSION:
                return report
            return ReplayReport(
                schema_version=M2_REPLAY_SCHEMA_VERSION,
                run_ref=report.run_ref,
                consistent=report.consistent,
                decision=report.decision,
                answer=report.answer,
                answer_unit=report.answer_unit,
                verified_artifact_count=report.verified_artifact_count,
                reason=report.reason,
            )

        try:
            run_directory = self._artifact_root / "runs" / run_id
            manifest_bytes = _read_bounded_json_bytes(
                run_directory / "manifest.json"
            )
            if manifest_bytes is None:
                return _failed_replay(run_ref, "MALFORMED_MANIFEST")
            manifest = json.loads(manifest_bytes)
            if not isinstance(manifest, dict):
                return _failed_replay(run_ref, "MALFORMED_MANIFEST")
            if set(manifest) != {
                "schema_version",
                "run_id",
                "document_sha256",
                "calculation_policy_sha256",
                "artifacts",
            }:
                return _failed_replay(run_ref, "MALFORMED_MANIFEST")
            if manifest.get("schema_version") != "finauditgate.manifest/v1":
                return _failed_replay(run_ref, "UNSUPPORTED_MANIFEST_SCHEMA")
            if manifest.get("run_id") != run_id:
                return _failed_replay(run_ref, "RUN_ID_MISMATCH")
            policy_sha256 = manifest.get("calculation_policy_sha256")
            if not isinstance(policy_sha256, str) or not _RUN_ID.fullmatch(
                policy_sha256
            ):
                return _failed_replay(run_ref, "POLICY_IDENTITY_MISMATCH")
            try:
                policy, policy_bytes = _SUPPORTED_CALCULATION_POLICIES[
                    policy_sha256
                ]
            except KeyError:
                return _failed_replay(run_ref, "POLICY_IDENTITY_MISMATCH")

            artifacts = manifest["artifacts"]
            if not isinstance(artifacts, dict):
                return _failed_replay(run_ref, "MALFORMED_MANIFEST")
            if set(artifacts) != set(_ARTIFACT_FILES):
                return _failed_replay(run_ref, "ARTIFACT_SET_MISMATCH")
            for entry in artifacts.values():
                if not isinstance(entry, dict) or set(entry) != {
                    "filename",
                    "sha256",
                }:
                    return _failed_replay(run_ref, "MALFORMED_MANIFEST")
                if not isinstance(entry["filename"], str) or not isinstance(
                    entry["sha256"], str
                ):
                    return _failed_replay(run_ref, "MALFORMED_MANIFEST")
                if not _RUN_ID.fullmatch(entry["sha256"]):
                    return _failed_replay(run_ref, "MALFORMED_MANIFEST")

            artifact_payloads: dict[str, bytes] = {}
            for name, expected_filename in _ARTIFACT_FILES.items():
                entry = artifacts[name]
                if entry.get("filename") != expected_filename:
                    return _failed_replay(run_ref, "ARTIFACT_PATH_MISMATCH")
                artifact_path = run_directory / expected_filename
                if not artifact_path.is_file():
                    return _failed_replay(run_ref, "MISSING_ARTIFACT")
                payload = _read_bounded_json_bytes(artifact_path)
                if payload is None:
                    return _failed_replay(
                        run_ref,
                        "ARTIFACT_STRUCTURE_INVALID",
                    )
                if sha256_hex(payload) != entry.get("sha256"):
                    return _failed_replay(run_ref, "ARTIFACT_HASH_MISMATCH")
                if _m2_json_payload_exceeds_limits(payload):
                    return _failed_replay(
                        run_ref,
                        "ARTIFACT_STRUCTURE_INVALID",
                    )
                artifact_payloads[name] = payload

            expected_manifest = {
                "schema_version": "finauditgate.manifest/v1",
                "run_id": run_id,
                "document_sha256": manifest["document_sha256"],
                "calculation_policy_sha256": policy_sha256,
                "artifacts": {
                    name: {
                        "filename": _ARTIFACT_FILES[name],
                        "sha256": sha256_hex(payload),
                    }
                    for name, payload in sorted(artifact_payloads.items())
                },
            }
            if manifest != expected_manifest:
                return _failed_replay(run_ref, "MANIFEST_CONTENT_MISMATCH")
            if manifest_bytes != canonical_json_bytes(expected_manifest):
                return _failed_replay(run_ref, "NON_CANONICAL_MANIFEST")

            document_sha256 = manifest["document_sha256"]
            if not _RUN_ID.fullmatch(document_sha256):
                return _failed_replay(run_ref, "INVALID_DOCUMENT_HASH")
            document = (
                self._artifact_root / "blobs" / "sha256" / document_sha256
            ).read_bytes()
            if sha256_hex(document) != document_sha256:
                return _failed_replay(run_ref, "DOCUMENT_HASH_MISMATCH")

            task_payload = json.loads(artifact_payloads["task"])
            candidate_payload = json.loads(artifact_payloads["candidate"])
            policy_payload = json.loads(artifact_payloads["policy"])
            identity_payload = json.loads(artifact_payloads["identity"])
            stored_ledger = json.loads(artifact_payloads["ledger"])
            stored_formula = json.loads(artifact_payloads["formula"])
            outcome_payload = json.loads(artifact_payloads["outcome"])
            if task_payload.get("schema_version") != "finauditgate.task/v1":
                return _failed_replay(run_ref, "UNSUPPORTED_TASK_SCHEMA")
            if task_payload.get("document_sha256") != document_sha256:
                return _failed_replay(run_ref, "TASK_DOCUMENT_MISMATCH")
            if (
                policy_payload != policy
                or artifact_payloads["policy"] != policy_bytes
            ):
                return _failed_replay(run_ref, "POLICY_ARTIFACT_MISMATCH")
            stored_task = _task_from_payload(task_payload, document)
            if (
                canonical_json_bytes(_task_payload(stored_task, document_sha256))
                != artifact_payloads["task"]
            ):
                return _failed_replay(run_ref, "NON_CANONICAL_TASK")

            expected_identity_payload = {
                "schema_version": "finauditgate.run-identity/v1",
                "task_sha256": sha256_hex(artifact_payloads["task"]),
                "candidate_sha256": sha256_hex(
                    artifact_payloads["candidate"]
                ),
                "calculation_policy_sha256": sha256_hex(
                    artifact_payloads["policy"]
                ),
            }
            expected_identity_bytes = canonical_json_bytes(
                expected_identity_payload
            )
            if (
                identity_payload != expected_identity_payload
                or artifact_payloads["identity"] != expected_identity_bytes
            ):
                return _failed_replay(run_ref, "RUN_IDENTITY_ARTIFACT_MISMATCH")
            expected_run_id = sha256_hex(expected_identity_bytes)
            if expected_run_id != run_id:
                return _failed_replay(run_ref, "RUN_IDENTITY_MISMATCH")

            candidate = _candidate_from_payload(candidate_payload)
            if (
                candidate_payload != _candidate_payload(candidate)
                or artifact_payloads["candidate"]
                != canonical_json_bytes(candidate_payload)
            ):
                return _failed_replay(run_ref, "CANDIDATE_SCHEMA_MISMATCH")
            try:
                replayed_ledger = _verified_ledger(
                    document,
                    candidate.evidence,
                )
                replayed_formula = _execute_calculation(
                    replayed_ledger,
                    candidate.calculation,
                    stored_task,
                    policy,
                    policy_sha256,
                )
                replayed_decision = Decision.ACCEPT
                replayed_answer = replayed_formula["result"]
                replayed_unit = replayed_formula["output_unit"]
                replayed_reason_codes: list[str] = []
            except (
                ValueError,
                TypeError,
                KeyError,
                DecimalException,
                UnicodeDecodeError,
            ):
                replayed_ledger = _rejected_ledger()
                replayed_formula = _not_executed_formula()
                replayed_decision = Decision.HUMAN_REVIEW
                replayed_answer = None
                replayed_unit = None
                replayed_reason_codes = ["CANDIDATE_VALIDATION_FAILED"]
            if replayed_ledger != stored_ledger:
                return _failed_replay(run_ref, "LEDGER_REPLAY_MISMATCH")
            if artifact_payloads["ledger"] != canonical_json_bytes(
                replayed_ledger
            ):
                return _failed_replay(run_ref, "NON_CANONICAL_LEDGER")
            if replayed_formula != stored_formula:
                return _failed_replay(run_ref, "FORMULA_REPLAY_MISMATCH")
            if artifact_payloads["formula"] != canonical_json_bytes(
                replayed_formula
            ):
                return _failed_replay(run_ref, "NON_CANONICAL_FORMULA")
            expected_outcome_payload = {
                "schema_version": RUN_SCHEMA_VERSION,
                "task_id": stored_task.task_id,
                "decision": replayed_decision.value,
                "answer": replayed_answer,
                "answer_unit": replayed_unit,
                "reason_codes": replayed_reason_codes,
                "run_ref": {"run_id": run_id},
                "document_sha256": document_sha256,
            }
            if outcome_payload != expected_outcome_payload:
                return _failed_replay(run_ref, "OUTCOME_METADATA_MISMATCH")
            if (
                artifact_payloads["outcome"]
                != canonical_json_bytes(expected_outcome_payload)
            ):
                return _failed_replay(run_ref, "NON_CANONICAL_OUTCOME")

            return ReplayReport(
                schema_version=REPLAY_SCHEMA_VERSION,
                run_ref=run_ref,
                consistent=True,
                decision=replayed_decision,
                answer=replayed_answer,
                answer_unit=replayed_unit,
                verified_artifact_count=len(_ARTIFACT_FILES) + 1,
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            DecimalException,
            RecursionError,
            UnicodeError,
        ):
            return _failed_replay(run_ref, "REPLAY_VALIDATION_FAILED")

    def _replay_m2(self, run_ref: RunRef) -> ReplayReport:
        """Replay all bound M2 proposal snapshots without an Adapter."""

        run_id = run_ref.run_id
        try:
            run_directory = self._artifact_root / "runs" / run_id
            manifest_bytes = _read_bounded_json_bytes(
                run_directory / "manifest.json"
            )
            if (
                manifest_bytes is None
                or _m2_json_payload_exceeds_limits(manifest_bytes)
            ):
                return _failed_replay(run_ref, "MALFORMED_MANIFEST")
            manifest = json.loads(manifest_bytes)
            if not isinstance(manifest, dict) or set(manifest) != {
                "schema_version",
                "run_id",
                "document_sha256",
                "calculation_policy_sha256",
                "artifacts",
            }:
                return _failed_replay(run_ref, "MALFORMED_MANIFEST")
            if manifest["schema_version"] != "finauditgate.manifest/v2":
                return _failed_replay(run_ref, "UNSUPPORTED_MANIFEST_SCHEMA")
            if manifest["run_id"] != run_id:
                return _failed_replay(run_ref, "RUN_ID_MISMATCH")
            if manifest["calculation_policy_sha256"] != M2_POLICY_SHA256:
                return _failed_replay(run_ref, "POLICY_IDENTITY_MISMATCH")
            artifacts = manifest["artifacts"]
            if not isinstance(artifacts, dict) or set(artifacts) != set(
                _M2_ARTIFACT_FILES
            ):
                return _failed_replay(run_ref, "ARTIFACT_SET_MISMATCH")

            artifact_payloads: dict[str, bytes] = {}
            for name, filename in _M2_ARTIFACT_FILES.items():
                entry = artifacts[name]
                if not isinstance(entry, dict) or set(entry) != {
                    "filename",
                    "sha256",
                }:
                    return _failed_replay(run_ref, "MALFORMED_MANIFEST")
                if entry["filename"] != filename:
                    return _failed_replay(run_ref, "ARTIFACT_PATH_MISMATCH")
                artifact_path = run_directory / filename
                if not artifact_path.is_file():
                    return _failed_replay(run_ref, "MISSING_ARTIFACT")
                payload = _read_bounded_json_bytes(artifact_path)
                if payload is None:
                    return _failed_replay(
                        run_ref,
                        "ARTIFACT_STRUCTURE_INVALID",
                    )
                if sha256_hex(payload) != entry["sha256"]:
                    return _failed_replay(run_ref, "ARTIFACT_HASH_MISMATCH")
                if _m2_json_payload_exceeds_limits(payload):
                    return _failed_replay(
                        run_ref,
                        "ARTIFACT_STRUCTURE_INVALID",
                    )
                artifact_payloads[name] = payload
            expected_manifest = {
                "schema_version": "finauditgate.manifest/v2",
                "run_id": run_id,
                "document_sha256": manifest["document_sha256"],
                "calculation_policy_sha256": M2_POLICY_SHA256,
                "artifacts": {
                    name: {
                        "filename": _M2_ARTIFACT_FILES[name],
                        "sha256": sha256_hex(payload),
                    }
                    for name, payload in sorted(artifact_payloads.items())
                },
            }
            if manifest != expected_manifest:
                return _failed_replay(run_ref, "MANIFEST_CONTENT_MISMATCH")
            if manifest_bytes != canonical_json_bytes(expected_manifest):
                return _failed_replay(run_ref, "NON_CANONICAL_MANIFEST")

            document_sha256 = manifest["document_sha256"]
            if (
                not isinstance(document_sha256, str)
                or not _RUN_ID.fullmatch(document_sha256)
            ):
                return _failed_replay(run_ref, "INVALID_DOCUMENT_HASH")
            document_path = (
                self._artifact_root / "blobs" / "sha256" / document_sha256
            )
            if not document_path.is_file():
                return _failed_replay(run_ref, "MISSING_ARTIFACT")
            document = document_path.read_bytes()
            if sha256_hex(document) != document_sha256:
                return _failed_replay(run_ref, "DOCUMENT_HASH_MISMATCH")
            if artifact_payloads["policy"] != M2_POLICY_BYTES:
                return _failed_replay(run_ref, "POLICY_ARTIFACT_MISMATCH")

            task_payload = json.loads(artifact_payloads["task"])
            stored_task = _task_from_payload(task_payload, document)
            if (
                canonical_json_bytes(_task_payload(stored_task, document_sha256))
                != artifact_payloads["task"]
            ):
                return _failed_replay(run_ref, "NON_CANONICAL_TASK")
            candidate_payload = json.loads(artifact_payloads["candidate"])
            if candidate_payload is not None:
                candidate = _candidate_from_payload(candidate_payload)
                if (
                    canonical_json_bytes(_candidate_payload(candidate))
                    != artifact_payloads["candidate"]
                ):
                    return _failed_replay(
                        run_ref,
                        "CANDIDATE_SCHEMA_MISMATCH",
                    )
            attempts_payload = json.loads(artifact_payloads["attempts"])
            if not isinstance(attempts_payload, dict) or set(
                attempts_payload
            ) != {
                "schema_version",
                "retry_budget",
                "attempts",
                "final_decision",
                "final_reason_codes",
            }:
                return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
            raw_attempts = attempts_payload["attempts"]
            if (
                attempts_payload["schema_version"]
                != "finauditgate.attempts/v2"
                or attempts_payload["retry_budget"] != 1
                or not isinstance(raw_attempts, list)
                or len(raw_attempts) not in {1, 2}
            ):
                return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
            expected_attempt_entries: list[dict[str, object]] = []
            replayed_ledger: dict[str, object] | None = None
            replayed_formula: dict[str, object] | None = None
            last_candidate_payload: dict[str, object] | None = None
            for attempt_index, raw_attempt in enumerate(raw_attempts):
                if not isinstance(raw_attempt, dict) or set(raw_attempt) != {
                    "attempt_index",
                    "proposal",
                    "proposal_sha256",
                    "candidate",
                    "candidate_sha256",
                    "disposition",
                    "reason_codes",
                }:
                    return _failed_replay(
                        run_ref,
                        "ATTEMPTS_ARTIFACT_MISMATCH",
                    )
                raw_proposal_snapshot = raw_attempt["proposal"]
                proposal_candidate_payload: dict[str, object] | None = None
                proposal_candidate: ModelCandidate | None = None
                proposal_shape_invalid = False
                if raw_proposal_snapshot is None:
                    if raw_attempt["proposal_sha256"] is not None:
                        return _failed_replay(
                            run_ref,
                            "ATTEMPTS_ARTIFACT_MISMATCH",
                        )
                else:
                    if not isinstance(raw_proposal_snapshot, dict):
                        return _failed_replay(
                            run_ref,
                            "ATTEMPTS_ARTIFACT_MISMATCH",
                        )
                    raw_proposal_bytes = canonical_json_bytes(
                        raw_proposal_snapshot
                    )
                    if raw_attempt["proposal_sha256"] != sha256_hex(
                        raw_proposal_bytes
                    ):
                        return _failed_replay(
                            run_ref,
                            "ATTEMPTS_ARTIFACT_MISMATCH",
                        )
                    try:
                        (
                            proposal_candidate_payload,
                            proposal_candidate,
                        ) = _candidate_from_proposal_snapshot(
                            raw_proposal_snapshot
                        )
                    except (
                        AttributeError,
                        KeyError,
                        OverflowError,
                        RecursionError,
                        TypeError,
                        ValueError,
                    ):
                        proposal_shape_invalid = True
                raw_candidate_payload = raw_attempt["candidate"]
                if raw_candidate_payload is None:
                    raw_reason_codes = raw_attempt["reason_codes"]
                    if raw_reason_codes == ["CANDIDATE_SHAPE_INVALID"]:
                        if (
                            raw_proposal_snapshot is None
                            or not proposal_shape_invalid
                        ):
                            return _failed_replay(
                                run_ref,
                                "ATTEMPTS_ARTIFACT_MISMATCH",
                            )
                        attempt_reasons = ("CANDIDATE_SHAPE_INVALID",)
                        last_candidate_payload = None
                    elif (
                        raw_reason_codes == ["CANDIDATE_PROPOSAL_MISSING"]
                        and attempt_index == 1
                    ):
                        if raw_proposal_snapshot is not None:
                            return _failed_replay(
                                run_ref,
                                "ATTEMPTS_ARTIFACT_MISMATCH",
                            )
                        attempt_reasons = ("CANDIDATE_PROPOSAL_MISSING",)
                    else:
                        return _failed_replay(
                            run_ref,
                            "ATTEMPTS_ARTIFACT_MISMATCH",
                        )
                    disposition = Decision.RETRY
                    expected_attempt_entries.append(
                        {
                            "attempt_index": attempt_index,
                            "proposal": raw_proposal_snapshot,
                            "proposal_sha256": raw_attempt["proposal_sha256"],
                            "candidate": None,
                            "candidate_sha256": None,
                            "disposition": disposition.value,
                            "reason_codes": list(attempt_reasons),
                        }
                    )
                else:
                    if not isinstance(raw_candidate_payload, dict):
                        return _failed_replay(
                            run_ref,
                            "ATTEMPTS_ARTIFACT_MISMATCH",
                        )
                    if (
                        raw_proposal_snapshot is None
                        or proposal_shape_invalid
                        or proposal_candidate_payload
                        != raw_candidate_payload
                        or proposal_candidate is None
                    ):
                        return _failed_replay(
                            run_ref,
                            "ATTEMPTS_ARTIFACT_MISMATCH",
                        )
                    last_candidate_payload = raw_candidate_payload
                    attempt_candidate_bytes = canonical_json_bytes(
                        raw_candidate_payload
                    )
                    attempt_candidate = proposal_candidate
                    if (
                        canonical_json_bytes(
                            _candidate_payload(attempt_candidate)
                        )
                        != attempt_candidate_bytes
                    ):
                        return _failed_replay(
                            run_ref,
                            "ATTEMPTS_ARTIFACT_MISMATCH",
                        )
                    try:
                        attempt_ledger, attempt_formula = (
                            evaluate_accepted_candidate(
                                document,
                                attempt_candidate,
                                stored_task,
                            )
                        )
                    except M2ValidationFailure as failure:
                        disposition = failure.decision
                        attempt_reasons = failure.reason_codes
                    else:
                        disposition = Decision.ACCEPT
                        attempt_reasons = ()
                        replayed_ledger = attempt_ledger
                        replayed_formula = attempt_formula
                    expected_attempt_entries.append(
                        {
                            "attempt_index": attempt_index,
                            "proposal": raw_proposal_snapshot,
                            "proposal_sha256": raw_attempt["proposal_sha256"],
                            "candidate": raw_candidate_payload,
                            "candidate_sha256": sha256_hex(
                                attempt_candidate_bytes
                            ),
                            "disposition": disposition.value,
                            "reason_codes": list(attempt_reasons),
                        }
                    )
                if disposition is Decision.ACCEPT and attempt_index != len(
                    raw_attempts
                ) - 1:
                    return _failed_replay(
                        run_ref,
                        "ATTEMPTS_ARTIFACT_MISMATCH",
                    )
                if (
                    disposition is Decision.RETRY
                    and attempt_index == 0
                    and len(raw_attempts) == 1
                ):
                    return _failed_replay(
                        run_ref,
                        "ATTEMPTS_ARTIFACT_MISMATCH",
                    )
                if (
                    attempt_index == 0
                    and len(raw_attempts) == 2
                    and disposition is not Decision.RETRY
                ):
                    return _failed_replay(
                        run_ref,
                        "ATTEMPTS_ARTIFACT_MISMATCH",
                    )
            final_decision = disposition
            if final_decision is Decision.ACCEPT:
                final_reason_codes: tuple[str, ...] = ()
                if replayed_ledger is None or replayed_formula is None:
                    return _failed_replay(
                        run_ref,
                        "ATTEMPTS_ARTIFACT_MISMATCH",
                    )
                replayed_answer = replayed_formula["result"]
                replayed_answer_unit = replayed_formula["output_unit"]
            else:
                final_reason_codes = attempt_reasons
                if final_decision is Decision.RETRY:
                    final_reason_codes += ("RETRY_BUDGET_EXHAUSTED",)
                replayed_ledger, replayed_formula = failed_artifacts(
                    final_decision,
                    final_reason_codes,
                )
                replayed_answer = None
                replayed_answer_unit = None
            if last_candidate_payload != candidate_payload:
                return _failed_replay(run_ref, "CANDIDATE_SCHEMA_MISMATCH")
            expected_attempts = {
                "schema_version": "finauditgate.attempts/v2",
                "retry_budget": 1,
                "attempts": expected_attempt_entries,
                "final_decision": final_decision.value,
                "final_reason_codes": list(final_reason_codes),
            }
            if (
                attempts_payload != expected_attempts
                or artifact_payloads["attempts"]
                != canonical_json_bytes(expected_attempts)
            ):
                return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
            identity_payload = json.loads(artifact_payloads["identity"])
            expected_identity = {
                "schema_version": "finauditgate.run-identity/v2",
                "task_sha256": sha256_hex(artifact_payloads["task"]),
                "candidate_sha256": sha256_hex(artifact_payloads["candidate"]),
                "attempts_sha256": sha256_hex(
                    artifact_payloads["attempts"]
                ),
                "calculation_policy_sha256": M2_POLICY_SHA256,
            }
            expected_identity_bytes = canonical_json_bytes(expected_identity)
            if (
                identity_payload != expected_identity
                or artifact_payloads["identity"] != expected_identity_bytes
            ):
                return _failed_replay(
                    run_ref,
                    "RUN_IDENTITY_ARTIFACT_MISMATCH",
                )
            if sha256_hex(expected_identity_bytes) != run_id:
                return _failed_replay(run_ref, "RUN_IDENTITY_MISMATCH")

            if artifact_payloads["ledger"] != canonical_json_bytes(
                replayed_ledger
            ):
                return _failed_replay(run_ref, "LEDGER_REPLAY_MISMATCH")
            if artifact_payloads["formula"] != canonical_json_bytes(
                replayed_formula
            ):
                return _failed_replay(run_ref, "FORMULA_REPLAY_MISMATCH")
            expected_outcome = {
                "schema_version": RUN_SCHEMA_VERSION,
                "task_id": stored_task.task_id,
                "decision": final_decision.value,
                "answer": replayed_answer,
                "answer_unit": replayed_answer_unit,
                "reason_codes": list(final_reason_codes),
                "run_ref": {"run_id": run_id},
                "document_sha256": document_sha256,
            }
            if artifact_payloads["outcome"] != canonical_json_bytes(
                expected_outcome
            ):
                return _failed_replay(run_ref, "OUTCOME_METADATA_MISMATCH")
            return ReplayReport(
                schema_version=M2_REPLAY_SCHEMA_VERSION,
                run_ref=run_ref,
                consistent=True,
                decision=final_decision,
                answer=replayed_answer,
                answer_unit=replayed_answer_unit,
                verified_artifact_count=len(_M2_ARTIFACT_FILES) + 1,
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            DecimalException,
            OverflowError,
            RecursionError,
            UnicodeError,
        ):
            return _failed_replay(run_ref, "REPLAY_VALIDATION_FAILED")


def _task_payload(task: AuditTask, document_sha256: str) -> dict[str, object]:
    return {
        "schema_version": "finauditgate.task/v1",
        "task_id": task.task_id,
        "question": task.question,
        "cutoff": task.cutoff.isoformat(),
        "source_id": task.document.source_id,
        "document_name": task.document.document_name,
        "document_sha256": document_sha256,
        "declared_published_at": (
            task.document.declared_published_at.isoformat()
        ),
        "answer_contract": task.answer_contract,
        "risk_class": task.risk_class,
        "mode": task.mode,
    }


def _task_from_payload(
    payload: dict[str, object],
    document: bytes,
) -> AuditTask:
    expected_keys = {
        "schema_version",
        "task_id",
        "question",
        "cutoff",
        "source_id",
        "document_name",
        "document_sha256",
        "declared_published_at",
        "answer_contract",
        "risk_class",
        "mode",
    }
    if set(payload) != expected_keys:
        raise ValueError("task payload fields do not match the schema")
    if payload.get("schema_version") != "finauditgate.task/v1":
        raise ValueError("unsupported task schema")
    string_fields = expected_keys - {"schema_version"}
    if any(type(payload[field]) is not str for field in string_fields):
        raise TypeError("task fields must be strings")
    return AuditTask(
        task_id=payload["task_id"],
        question=payload["question"],
        cutoff=date.fromisoformat(payload["cutoff"]),
        document=FrozenDocumentPackage(
            source_id=payload["source_id"],
            document_name=payload["document_name"],
            document_bytes=document,
            declared_published_at=date.fromisoformat(
                payload["declared_published_at"]
            ),
        ),
        answer_contract=payload["answer_contract"],
        risk_class=payload["risk_class"],
        mode=payload["mode"],
    )


@dataclass(slots=True)
class _ProposalSnapshotState:
    active_ids: set[int]
    nodes: int = 0


@dataclass(slots=True)
class _ProposalReplayState:
    nodes: int = 0


def _proposal_snapshot(proposal: object) -> dict[str, object]:
    """Serialize an untrusted proposal into a bounded canonical envelope."""

    state = _ProposalSnapshotState(active_ids=set())
    try:
        value_snapshot = _snapshot_proposal_value(proposal, state, depth=0)
    except (
        AttributeError,
        LookupError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
    ) as exc:
        value_snapshot = _proposal_failure_snapshot(
            "SNAPSHOT_FAILED",
            {
                "exception": _snapshot_exception_tag(exc),
                "value_type": _proposal_type_tag(proposal),
            },
        )
    return {
        "schema_version": _PROPOSAL_SNAPSHOT_SCHEMA_VERSION,
        "value": value_snapshot,
    }


def _snapshot_proposal_value(
    value: object,
    state: _ProposalSnapshotState,
    *,
    depth: int,
) -> dict[str, object]:
    value_type = type(value)
    state.nodes += 1
    if state.nodes > _MAX_PROPOSAL_SNAPSHOT_NODES:
        return _proposal_failure_snapshot(
            "NODE_LIMIT",
            {
                "nodes": state.nodes,
                "value_type": _proposal_type_tag(value),
            },
        )
    if depth > _MAX_PROPOSAL_SNAPSHOT_DEPTH:
        return _proposal_failure_snapshot(
            "DEPTH_LIMIT",
            {"depth": depth, "value_type": _proposal_type_tag(value)},
        )
    if value is None:
        return {"kind": "none"}
    if value_type is bool:
        return {"kind": "bool", "value": value}
    if value_type is int:
        bit_length = value.bit_length()
        if bit_length > _MAX_PROPOSAL_INTEGER_BITS:
            return _proposal_failure_snapshot(
                "INTEGER_LIMIT",
                {
                    "bit_length": bit_length,
                    "magnitude_sha256": _oversized_integer_fingerprint(
                        value
                    ),
                    "negative": value < 0,
                },
            )
        return {"kind": "int", "value": value}
    if value_type is str:
        if len(value) > _MAX_PROPOSAL_TEXT_LENGTH:
            return _proposal_failure_snapshot(
                "TEXT_LIMIT",
                {
                    "length": len(value),
                    "value_sha256": _text_fingerprint(value),
                },
            )
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return _proposal_failure_snapshot(
                "TEXT_ENCODING_INVALID",
                {
                    "length": len(value),
                    "surrogatepass_sha256": sha256_hex(
                        value.encode("utf-8", errors="surrogatepass")
                    ),
                },
            )
        return {"kind": "str", "value": value}
    if value_type is float:
        return {"kind": "float", "hex": value.hex()}
    if value_type is bytes:
        if len(value) > _MAX_PROPOSAL_BYTES_LENGTH:
            return _proposal_failure_snapshot(
                "BYTES_LIMIT",
                {
                    "length": len(value),
                    "value_sha256": sha256_hex(value),
                },
            )
        return {"kind": "bytes", "hex": value.hex()}

    value_id = id(value)
    if value_id in state.active_ids:
        return _proposal_failure_snapshot(
            "CYCLIC_VALUE",
            {"depth": depth, "value_type": _proposal_type_tag(value)},
        )
    state.active_ids.add(value_id)
    try:
        if value_type is ModelCandidate:
            return {
                "kind": "model_candidate",
                "evidence": _snapshot_proposal_value(
                    value.evidence,
                    state,
                    depth=depth + 1,
                ),
                "calculation": _snapshot_proposal_value(
                    value.calculation,
                    state,
                    depth=depth + 1,
                ),
            }
        if value_type is EvidenceCandidate:
            return {
                "kind": "evidence_candidate",
                "evidence_id": _snapshot_proposal_value(
                    value.evidence_id, state, depth=depth + 1
                ),
                "byte_start": _snapshot_proposal_value(
                    value.byte_start, state, depth=depth + 1
                ),
                "byte_end": _snapshot_proposal_value(
                    value.byte_end, state, depth=depth + 1
                ),
                "metric": _snapshot_proposal_value(
                    value.metric, state, depth=depth + 1
                ),
                "period": _snapshot_proposal_value(
                    value.period, state, depth=depth + 1
                ),
                "value": _snapshot_proposal_value(
                    value.value, state, depth=depth + 1
                ),
                "unit": _snapshot_proposal_value(
                    value.unit, state, depth=depth + 1
                ),
                "metric_basis": _snapshot_proposal_value(
                    value.metric_basis, state, depth=depth + 1
                ),
                "currency": _snapshot_proposal_value(
                    value.currency, state, depth=depth + 1
                ),
                "scale": _snapshot_proposal_value(
                    value.scale, state, depth=depth + 1
                ),
                "sign": _snapshot_proposal_value(
                    value.sign, state, depth=depth + 1
                ),
            }
        if value_type is CalculationCandidate:
            return {
                "kind": "calculation_candidate",
                "operation": _snapshot_proposal_value(
                    value.operation, state, depth=depth + 1
                ),
                "operand_ids": _snapshot_proposal_value(
                    value.operand_ids, state, depth=depth + 1
                ),
                "output_unit": _snapshot_proposal_value(
                    value.output_unit, state, depth=depth + 1
                ),
                "quantize": _snapshot_proposal_value(
                    value.quantize, state, depth=depth + 1
                ),
            }
        if value_type in {tuple, list}:
            if len(value) > _MAX_PROPOSAL_CONTAINER_ITEMS:
                return _proposal_failure_snapshot(
                    "CONTAINER_LIMIT",
                    {
                        "length": len(value),
                        "value_type": _proposal_type_tag(value),
                    },
                )
            return {
                "kind": "tuple" if value_type is tuple else "list",
                "items": [
                    _snapshot_proposal_value(
                        item,
                        state,
                        depth=depth + 1,
                    )
                    for item in value
                ],
            }
        if value_type is dict:
            if len(value) > _MAX_PROPOSAL_CONTAINER_ITEMS:
                return _proposal_failure_snapshot(
                    "CONTAINER_LIMIT",
                    {"length": len(value), "value_type": "dict"},
                )
            if not all(type(key) is str for key in value):
                return _proposal_failure_snapshot(
                    "UNSUPPORTED_TYPE",
                    {
                        "key_types": sorted(
                            _proposal_type_tag(key) for key in value
                        ),
                        "value_type": "dict",
                    },
                )
            oversized_keys = [
                key
                for key in value
                if len(key) > _MAX_PROPOSAL_TEXT_LENGTH
            ]
            if oversized_keys:
                return _proposal_failure_snapshot(
                    "TEXT_LIMIT",
                    {
                        "key_fingerprints": sorted(
                            _text_fingerprint(key)
                            for key in oversized_keys
                        ),
                        "value_type": "dict_key",
                    },
                )
            invalid_keys = []
            for key in value:
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError:
                    invalid_keys.append(
                        sha256_hex(
                            key.encode("utf-8", errors="surrogatepass")
                        )
                    )
            if invalid_keys:
                return _proposal_failure_snapshot(
                    "TEXT_ENCODING_INVALID",
                    {
                        "key_fingerprints": sorted(invalid_keys),
                        "value_type": "dict_key",
                    },
                )
            return {
                "kind": "dict",
                "items": [
                    {
                        "key": key,
                        "value": _snapshot_proposal_value(
                            value[key],
                            state,
                            depth=depth + 1,
                        ),
                    }
                    for key in sorted(value)
                ],
            }
        return _proposal_failure_snapshot(
            "UNSUPPORTED_TYPE",
            {"value_type": _proposal_type_tag(value)},
        )
    finally:
        state.active_ids.remove(value_id)


def _proposal_failure_snapshot(
    reason: str,
    details: dict[str, object],
) -> dict[str, object]:
    fingerprint = sha256_hex(
        canonical_json_bytes({"reason": reason, "details": details})
    )
    return {
        "kind": "unsupported",
        "reason": reason,
        "fingerprint": fingerprint,
    }


def _text_fingerprint(value: str) -> str:
    digest = hashlib.sha256()
    for start in range(0, len(value), 1_024):
        digest.update(
            value[start : start + 1_024].encode(
                "utf-8",
                errors="surrogatepass",
            )
        )
    return digest.hexdigest()


def _oversized_integer_fingerprint(value: int) -> str:
    digest = hashlib.sha256()
    digest.update(b"negative" if value < 0 else b"nonnegative")
    magnitude = abs(value)
    byte_length = (magnitude.bit_length() + 7) // 8
    digest.update(magnitude.to_bytes(byte_length, byteorder="big"))
    return digest.hexdigest()


def _proposal_type_tag(value: object) -> str:
    value_type = type(value)
    if value is None:
        return "none"
    if value_type is bool:
        return "bool"
    if value_type is int:
        return "int"
    if value_type is str:
        return "str"
    if value_type is float:
        return "float"
    if value_type is bytes:
        return "bytes"
    if value_type is ModelCandidate:
        return "model_candidate"
    if value_type is EvidenceCandidate:
        return "evidence_candidate"
    if value_type is CalculationCandidate:
        return "calculation_candidate"
    if value_type is tuple:
        return "tuple"
    if value_type is list:
        return "list"
    if value_type is dict:
        return "dict"
    return "unsupported"


def _snapshot_exception_tag(exc: BaseException) -> str:
    exception_type = type(exc)
    if exception_type is AttributeError:
        return "attribute_error"
    if exception_type is KeyError:
        return "key_error"
    if exception_type is IndexError:
        return "index_error"
    if exception_type is OverflowError:
        return "overflow_error"
    if exception_type is RecursionError:
        return "recursion_error"
    if exception_type is TypeError:
        return "type_error"
    if exception_type is ValueError:
        return "value_error"
    return "lookup_error"


def _proposal_value_from_snapshot(
    snapshot: object,
    state: _ProposalReplayState | None = None,
    *,
    depth: int = 0,
) -> object:
    if state is None:
        state = _ProposalReplayState()
    state.nodes += 1
    if (
        state.nodes > _MAX_PROPOSAL_SNAPSHOT_NODES
        or depth > _MAX_PROPOSAL_SNAPSHOT_DEPTH
    ):
        raise ValueError("proposal snapshot exceeds structural limits")
    if not isinstance(snapshot, dict) or type(snapshot.get("kind")) is not str:
        raise ValueError("proposal snapshot value is malformed")
    kind = snapshot["kind"]
    if kind == "none" and set(snapshot) == {"kind"}:
        return None
    if kind == "bool" and set(snapshot) == {"kind", "value"}:
        if type(snapshot["value"]) is not bool:
            raise TypeError("proposal bool snapshot is malformed")
        return snapshot["value"]
    if kind == "int" and set(snapshot) == {"kind", "value"}:
        if (
            type(snapshot["value"]) is not int
            or snapshot["value"].bit_length()
            > _MAX_PROPOSAL_INTEGER_BITS
        ):
            raise TypeError("proposal int snapshot is malformed")
        return snapshot["value"]
    if kind == "str" and set(snapshot) == {"kind", "value"}:
        if (
            type(snapshot["value"]) is not str
            or len(snapshot["value"]) > _MAX_PROPOSAL_TEXT_LENGTH
        ):
            raise TypeError("proposal string snapshot is malformed")
        return snapshot["value"]
    if kind == "float" and set(snapshot) == {"kind", "hex"}:
        if type(snapshot["hex"]) is not str or len(snapshot["hex"]) > 32:
            raise TypeError("proposal float snapshot is malformed")
        return float.fromhex(snapshot["hex"])
    if kind == "bytes" and set(snapshot) == {"kind", "hex"}:
        if (
            type(snapshot["hex"]) is not str
            or len(snapshot["hex"]) > _MAX_PROPOSAL_BYTES_LENGTH * 2
        ):
            raise TypeError("proposal bytes snapshot is malformed")
        return bytes.fromhex(snapshot["hex"])
    if kind in {"tuple", "list"} and set(snapshot) == {"kind", "items"}:
        items = snapshot["items"]
        if (
            not isinstance(items, list)
            or len(items) > _MAX_PROPOSAL_CONTAINER_ITEMS
        ):
            raise TypeError("proposal sequence snapshot is malformed")
        values = [
            _proposal_value_from_snapshot(
                item,
                state,
                depth=depth + 1,
            )
            for item in items
        ]
        return tuple(values) if kind == "tuple" else values
    if kind == "dict" and set(snapshot) == {"kind", "items"}:
        items = snapshot["items"]
        if (
            not isinstance(items, list)
            or len(items) > _MAX_PROPOSAL_CONTAINER_ITEMS
        ):
            raise TypeError("proposal mapping snapshot is malformed")
        result: dict[str, object] = {}
        previous_key: str | None = None
        for item in items:
            if (
                not isinstance(item, dict)
                or set(item) != {"key", "value"}
                or type(item["key"]) is not str
                or item["key"] in result
                or (previous_key is not None and item["key"] <= previous_key)
            ):
                raise ValueError("proposal mapping entry is malformed")
            result[item["key"]] = _proposal_value_from_snapshot(
                item["value"],
                state,
                depth=depth + 1,
            )
            previous_key = item["key"]
        return result
    if kind == "model_candidate" and set(snapshot) == {
        "kind",
        "evidence",
        "calculation",
    }:
        return ModelCandidate(
            evidence=_proposal_value_from_snapshot(
                snapshot["evidence"], state, depth=depth + 1
            ),
            calculation=_proposal_value_from_snapshot(
                snapshot["calculation"], state, depth=depth + 1
            ),
        )
    if kind == "evidence_candidate" and set(snapshot) == {
        "kind",
        "evidence_id",
        "byte_start",
        "byte_end",
        "metric",
        "period",
        "value",
        "unit",
        "metric_basis",
        "currency",
        "scale",
        "sign",
    }:
        return EvidenceCandidate(
            evidence_id=_proposal_value_from_snapshot(
                snapshot["evidence_id"], state, depth=depth + 1
            ),
            byte_start=_proposal_value_from_snapshot(
                snapshot["byte_start"], state, depth=depth + 1
            ),
            byte_end=_proposal_value_from_snapshot(
                snapshot["byte_end"], state, depth=depth + 1
            ),
            metric=_proposal_value_from_snapshot(
                snapshot["metric"], state, depth=depth + 1
            ),
            period=_proposal_value_from_snapshot(
                snapshot["period"], state, depth=depth + 1
            ),
            value=_proposal_value_from_snapshot(
                snapshot["value"], state, depth=depth + 1
            ),
            unit=_proposal_value_from_snapshot(
                snapshot["unit"], state, depth=depth + 1
            ),
            metric_basis=_proposal_value_from_snapshot(
                snapshot["metric_basis"], state, depth=depth + 1
            ),
            currency=_proposal_value_from_snapshot(
                snapshot["currency"], state, depth=depth + 1
            ),
            scale=_proposal_value_from_snapshot(
                snapshot["scale"], state, depth=depth + 1
            ),
            sign=_proposal_value_from_snapshot(
                snapshot["sign"], state, depth=depth + 1
            ),
        )
    if kind == "calculation_candidate" and set(snapshot) == {
        "kind",
        "operation",
        "operand_ids",
        "output_unit",
        "quantize",
    }:
        return CalculationCandidate(
            operation=_proposal_value_from_snapshot(
                snapshot["operation"], state, depth=depth + 1
            ),
            operand_ids=_proposal_value_from_snapshot(
                snapshot["operand_ids"], state, depth=depth + 1
            ),
            output_unit=_proposal_value_from_snapshot(
                snapshot["output_unit"], state, depth=depth + 1
            ),
            quantize=_proposal_value_from_snapshot(
                snapshot["quantize"], state, depth=depth + 1
            ),
        )
    if kind == "unsupported" and set(snapshot) == {
        "kind",
        "reason",
        "fingerprint",
    }:
        if (
            snapshot["reason"] not in _PROPOSAL_FAILURE_REASONS
            or type(snapshot["fingerprint"]) is not str
            or not _RUN_ID.fullmatch(snapshot["fingerprint"])
        ):
            raise ValueError("unsupported proposal reason is malformed")
        raise ValueError("proposal contains an unsupported value")
    raise ValueError("proposal snapshot value is malformed")


def _candidate_from_proposal_snapshot(
    snapshot: dict[str, object],
) -> tuple[dict[str, object], ModelCandidate]:
    if (
        set(snapshot) != {"schema_version", "value"}
        or snapshot["schema_version"] != _PROPOSAL_SNAPSHOT_SCHEMA_VERSION
    ):
        raise ValueError("unsupported proposal snapshot schema")
    proposal = _proposal_value_from_snapshot(snapshot["value"])
    candidate_payload = _candidate_payload(proposal)
    candidate_bytes = canonical_json_bytes(candidate_payload)
    normalized_candidate_payload = json.loads(candidate_bytes)
    if not isinstance(normalized_candidate_payload, dict):
        raise TypeError("canonical candidate must be an object")
    candidate = _candidate_from_payload(normalized_candidate_payload)
    if canonical_json_bytes(_candidate_payload(candidate)) != candidate_bytes:
        raise ValueError("candidate is not canonically representable")
    return normalized_candidate_payload, candidate


def _candidate_payload(candidate: ModelCandidate) -> dict[str, object]:
    evidence_items = tuple(candidate.evidence)
    uses_m2_semantics = any(
        value is not None
        for item in evidence_items
        for value in (
            item.metric_basis,
            item.currency,
            item.scale,
            item.sign,
        )
    )
    evidence_payloads = []
    for item in evidence_items:
        evidence_payload = {
            "evidence_id": item.evidence_id,
            "byte_start": item.byte_start,
            "byte_end": item.byte_end,
            "metric": item.metric,
            "period": item.period,
            "value": item.value,
            "unit": item.unit,
        }
        semantic_values = {
            "metric_basis": item.metric_basis,
            "currency": item.currency,
            "scale": item.scale,
            "sign": item.sign,
        }
        if uses_m2_semantics:
            evidence_payload.update(semantic_values)
        evidence_payloads.append(evidence_payload)
    raw_operand_ids = candidate.calculation.operand_ids
    serialized_operand_ids = (
        list(raw_operand_ids)
        if type(raw_operand_ids) is tuple
        else raw_operand_ids
    )
    return {
        "schema_version": (
            "finauditgate.candidate/v2"
            if uses_m2_semantics
            else "finauditgate.candidate/v1"
        ),
        "evidence": evidence_payloads,
        "calculation": {
            **asdict(candidate.calculation),
            "operand_ids": serialized_operand_ids,
        },
    }


def _candidate_from_payload(payload: dict[str, object]) -> ModelCandidate:
    schema_version = payload.get("schema_version")
    if schema_version not in {
        "finauditgate.candidate/v1",
        "finauditgate.candidate/v2",
    }:
        raise ValueError("unsupported candidate schema")
    if set(payload) != {"schema_version", "evidence", "calculation"}:
        raise ValueError("candidate payload fields do not match the schema")
    raw_evidence = payload["evidence"]
    raw_calculation = payload["calculation"]
    if not isinstance(raw_evidence, list) or not isinstance(
        raw_calculation, dict
    ):
        raise TypeError("invalid candidate payload")
    expected_evidence_fields = {
        "evidence_id",
        "byte_start",
        "byte_end",
        "metric",
        "period",
        "value",
        "unit",
    }
    if schema_version == "finauditgate.candidate/v2":
        expected_evidence_fields |= {
            "metric_basis",
            "currency",
            "scale",
            "sign",
        }
    if any(
        not isinstance(item, dict) or set(item) != expected_evidence_fields
        for item in raw_evidence
    ):
        raise ValueError("evidence candidate fields do not match the schema")
    if set(raw_calculation) != {
        "operation",
        "operand_ids",
        "output_unit",
        "quantize",
    }:
        raise ValueError("calculation candidate fields do not match the schema")
    raw_operand_ids = raw_calculation["operand_ids"]
    operand_ids = (
        tuple(raw_operand_ids)
        if isinstance(raw_operand_ids, list)
        else raw_operand_ids
    )
    return ModelCandidate(
        evidence=tuple(EvidenceCandidate(**item) for item in raw_evidence),
        calculation=CalculationCandidate(
            operation=raw_calculation["operation"],
            operand_ids=operand_ids,
            output_unit=raw_calculation["output_unit"],
            quantize=raw_calculation["quantize"],
        ),
    )


def _verified_ledger(
    document: bytes,
    candidates: tuple[EvidenceCandidate, ...],
) -> dict[str, object]:
    if len(candidates) != 2:
        raise ValueError("growth calculation requires exactly two evidence items")
    nodes: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for candidate in candidates:
        if any(
            value is not None
            for value in (
                candidate.metric_basis,
                candidate.currency,
                candidate.scale,
                candidate.sign,
            )
        ):
            raise ValueError(
                "M1 candidate shape does not include M2 semantic fields"
            )
        if (
            not isinstance(candidate.evidence_id, str)
            or not candidate.evidence_id.strip()
            or candidate.evidence_id in seen_ids
        ):
            raise ValueError("evidence ids must be non-empty and unique")
        seen_ids.add(candidate.evidence_id)
        if (
            type(candidate.byte_start) is not int
            or type(candidate.byte_end) is not int
        ):
            raise TypeError("evidence byte locators must be integers")
        if not (0 <= candidate.byte_start < candidate.byte_end <= len(document)):
            raise ValueError("evidence locator is outside the frozen document")
        span = document[candidate.byte_start : candidate.byte_end]
        record = _parse_record(span.decode("utf-8"))
        expected = {
            "metric": candidate.metric,
            "period": candidate.period,
            "value": candidate.value,
            "unit": candidate.unit,
        }
        if record != expected:
            raise ValueError("candidate semantics do not match frozen bytes")
        value = Decimal(record["value"])
        if not value.is_finite():
            raise ValueError("evidence value must be finite")
        nodes.append(
            {
                "evidence_id": candidate.evidence_id,
                **record,
                "locator": {
                    "byte_start": candidate.byte_start,
                    "byte_end": candidate.byte_end,
                    "span_sha256": sha256_hex(span),
                },
                "verification": "VERIFIED_FROM_FROZEN_BYTES",
            }
        )
    return {
        "schema_version": "finauditgate.ledger/v1",
        "nodes": nodes,
    }


def _parse_record(record: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for field in record.split(";"):
        key, separator, value = field.partition("=")
        if separator != "=" or not key or not value or key in parsed:
            raise ValueError("invalid synthetic evidence record")
        parsed[key] = value
    if set(parsed) != {"metric", "period", "value", "unit"}:
        raise ValueError("synthetic evidence record has unexpected fields")
    return parsed


def _execute_calculation(
    ledger: dict[str, object],
    candidate: CalculationCandidate,
    task: AuditTask,
    policy: dict[str, object],
    policy_sha256: str,
) -> dict[str, object]:
    if candidate.operation != policy["operation"]:
        raise ValueError("calculation operation is not allowlisted")
    if candidate.output_unit != policy["output_unit"]:
        raise ValueError("growth calculation output unit must be PERCENT")
    if candidate.quantize != policy["quantize"]:
        raise ValueError("growth calculation quantize must be 0.01")
    if len(candidate.operand_ids) != 2:
        raise ValueError("growth calculation requires two operands")
    if any(
        not isinstance(operand_id, str) or not operand_id.strip()
        for operand_id in candidate.operand_ids
    ):
        raise ValueError("calculation operand ids must be non-empty strings")
    if candidate.operand_ids[0] == candidate.operand_ids[1]:
        raise ValueError("growth calculation operands must be distinct")
    nodes = {node["evidence_id"]: node for node in ledger["nodes"]}
    current = nodes[candidate.operand_ids[0]]
    prior = nodes[candidate.operand_ids[1]]
    if task.mode != "SYNTHETIC_DEV":
        raise ValueError("current validation profile is synthetic-only")
    if task.document.declared_published_at > task.cutoff:
        raise ValueError("document was published after the task cutoff")
    if task.answer_contract != "PERCENTAGE_CHANGE":
        raise ValueError("task answer contract is not supported")
    if task.question != policy["task_question"]:
        raise ValueError("task question is outside the validation profile")
    if task.risk_class != policy["accepted_risk_class"]:
        raise ValueError("task risk class requires a later gate profile")
    if task.document.source_id != policy["source_id"]:
        raise ValueError("document source is outside the validation profile")
    if (
        sha256_hex(task.document.document_bytes)
        != policy["document_sha256"]
    ):
        raise ValueError("document content is outside the validation profile")
    if (
        current["metric"] != policy["input_metric"]
        or prior["metric"] != policy["input_metric"]
        or current["unit"] != policy["input_unit"]
        or prior["unit"] != policy["input_unit"]
    ):
        raise ValueError("calculation operands have incompatible semantics")
    if (
        current["period"] != policy["current_period"]
        or prior["period"] != policy["comparison_period"]
    ):
        raise ValueError("calculation operands are outside the period profile")
    current_value = Decimal(current["value"])
    prior_value = Decimal(prior["value"])
    if prior_value == 0:
        raise ValueError("growth calculation prior value is zero")
    quantum = Decimal(candidate.quantize)
    if not quantum.is_finite() or quantum <= 0:
        raise ValueError("quantize must be a positive finite Decimal")
    calculation_context = Context(
        prec=policy["precision"],
        rounding=ROUND_HALF_EVEN,
        Emin=policy["emin"],
        Emax=policy["emax"],
        capitals=policy["capitals"],
        clamp=policy["clamp"],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
    with localcontext(calculation_context):
        result = (
            (current_value - prior_value) / prior_value * Decimal("100")
        ).quantize(quantum, rounding=ROUND_HALF_EVEN)
    return {
        "schema_version": "finauditgate.formula/v1",
        "operation": candidate.operation,
        "operand_ids": list(candidate.operand_ids),
        "input_unit": current["unit"],
        "output_unit": candidate.output_unit,
        "quantize": candidate.quantize,
        "rounding": policy["rounding"],
        "calculation_policy_sha256": policy_sha256,
        "result": format(result, "f"),
    }


def _rejected_ledger() -> dict[str, object]:
    return {
        "schema_version": "finauditgate.ledger/v1",
        "nodes": [],
        "status": "REJECTED",
        "reason": "CANDIDATE_VALIDATION_FAILED",
    }


def _not_executed_formula() -> dict[str, object]:
    return {
        "schema_version": "finauditgate.formula/v1",
        "status": "NOT_EXECUTED",
        "reason": "CANDIDATE_VALIDATION_FAILED",
    }


def _read_json(path: Path) -> dict[str, object]:
    raw_payload = _read_bounded_json_bytes(path)
    if raw_payload is None:
        raise ValueError("artifact exceeds JSON byte limit")
    payload = json.loads(raw_payload)
    if not isinstance(payload, dict):
        raise TypeError("artifact must be a JSON object")
    return payload


def _read_bounded_json_bytes(path: Path) -> bytes | None:
    with path.open("rb") as artifact:
        payload = artifact.read(_MAX_M2_JSON_ARTIFACT_BYTES + 1)
    if len(payload) > _MAX_M2_JSON_ARTIFACT_BYTES:
        return None
    return payload


def _m2_json_payload_exceeds_limits(payload: bytes) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in payload:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in {0x5B, 0x7B}:
            depth += 1
            if depth > _MAX_M2_JSON_NESTING:
                return True
        elif byte in {0x5D, 0x7D}:
            depth -= 1
    return False


def _has_m2_artifact_identity(run_directory: Path, run_id: str) -> bool:
    """Recognize a damaged M2 run without trusting one manifest field."""

    try:
        identity_bytes = _read_bounded_json_bytes(
            run_directory / "identity.json"
        )
        if (
            identity_bytes is None
            or _m2_json_payload_exceeds_limits(identity_bytes)
        ):
            return False
        identity = json.loads(identity_bytes)
        return (
            isinstance(identity, dict)
            and identity.get("schema_version")
            == "finauditgate.run-identity/v2"
            and canonical_json_bytes(identity) == identity_bytes
            and sha256_hex(identity_bytes) == run_id
        )
    except (
        OSError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        return False


def _failed_replay(run_ref: RunRef, reason: str) -> ReplayReport:
    return ReplayReport(
        schema_version=REPLAY_SCHEMA_VERSION,
        run_ref=run_ref,
        consistent=False,
        decision=None,
        answer=None,
        answer_unit=None,
        verified_artifact_count=0,
        reason=reason,
    )
