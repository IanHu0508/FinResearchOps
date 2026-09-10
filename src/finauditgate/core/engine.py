"""Deep implementation behind the two-method FinAuditGate Interface.

One run is: normalize the task, ask the model Adapter for at most two
proposals, validate each proposal against the frozen profile with deterministic
Decimal arithmetic, and persist every attempt as append-once artifacts whose
hashes are bound into the run identity.  Replay re-reads those artifacts,
repeats the validation without any Adapter, and reports whether the stored
decision still follows from the stored bytes.
"""

from __future__ import annotations

from finauditgate.cashflow import CashflowOutcome, CashflowTask
from finauditgate.research import FundamentalEvidenceTask

from datetime import date
from decimal import DecimalException
import json
from pathlib import Path
import re

from finauditgate.contracts import (
    REVIEWED_PROFILE_MODES,
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
from finauditgate.core.model_trace import (
    receipt_from_payload,
    receipt_payload,
    verify_raw_model_trace,
)
from finauditgate.core.private_profile import evaluate_private_candidate
from finauditgate.core.profiles import PrivateDevValidationProfile
from finauditgate.core.proposal_snapshot import (
    candidate_from_payload,
    candidate_from_proposal_snapshot,
    candidate_payload,
    proposal_snapshot,
)
from finauditgate.core.synthetic_profile import (
    SYNTHETIC_POLICY,
    SYNTHETIC_POLICY_BYTES,
    SYNTHETIC_POLICY_SHA256,
    SYNTHETIC_REGISTRIES_SHA256,
    ValidationFailure,
    evaluate_profiled_candidate,
    failed_artifacts,
)
from finauditgate.ports.model import CandidateModel, ModelExecution
from finauditgate.private_storage import (
    PrivateStorageError,
    PrivateWorkspaceAnchor,
    require_private_storage_root,
    resolve_private_workspace_anchor,
)


TASK_SCHEMA_VERSION = "finauditgate.task/v1"
ATTEMPTS_SCHEMA_VERSION = "finauditgate.attempts/v2"
IDENTITY_SCHEMA_VERSION = "finauditgate.run-identity/v5"
MANIFEST_SCHEMA_VERSION = "finauditgate.manifest/v2"
TRACE_SUMMARY_SCHEMA_VERSION = "finauditgate.model-trace-summary/v3"
RETRY_BUDGET = 1

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_MAX_JSON_ARTIFACT_BYTES = 16_777_216
_MAX_JSON_NESTING = 128
_ARTIFACT_FILES = {
    "identity": "identity.json",
    "task": "task.json",
    "candidate": "candidate.json",
    "attempts": "attempts.json",
    "policy": "policy.json",
    "ledger": "ledger.json",
    "formula": "formula.json",
    "outcome": "outcome.json",
}
_TRACED_ARTIFACT_FILES = {
    **_ARTIFACT_FILES,
    "model_trace": "model-trace.json",
}
_ATTEMPT_FIELDS = {
    "attempt_index",
    "proposal",
    "proposal_sha256",
    "candidate",
    "candidate_sha256",
    "disposition",
    "reason_codes",
}


class FinAuditGate:
    """Auditable financial evidence gate with a two-method Interface."""

    def __init__(
        self,
        *,
        artifact_root: Path,
        model: CandidateModel | None = None,
        model_trace_root: Path | None = None,
        private_dev_profile: PrivateDevValidationProfile | None = None,
        private_workspace_anchor: PrivateWorkspaceAnchor | None = None,
        investigator=None,
    ) -> None:
        self._artifact_root = Path(artifact_root)
        self._model = model
        self._investigator = investigator
        if (
            private_workspace_anchor is not None
            and type(private_workspace_anchor) is not PrivateWorkspaceAnchor
        ):
            raise TypeError(
                "private_workspace_anchor must be a PrivateWorkspaceAnchor"
            )
        self._private_workspace_anchor = private_workspace_anchor
        if (
            private_dev_profile is not None
            and type(private_dev_profile) is not PrivateDevValidationProfile
        ):
            raise TypeError(
                "private_dev_profile must be a PrivateDevValidationProfile"
            )
        self._private_dev_profile = private_dev_profile
        profile_anchor = (
            None
            if private_dev_profile is None
            else private_dev_profile.private_workspace_anchor
        )
        if profile_anchor is not None:
            anchor = self._ensure_private_artifact_root()
            if profile_anchor != anchor:
                raise PrivateStorageError(
                    "PRIVATE_STORAGE_REQUIRED:private-validation-profile:"
                    "WORKSPACE_ANCHOR_MISMATCH"
                )
        if model_trace_root is None:
            self._model_trace_root = None
        else:
            anchor = self._ensure_private_artifact_root()
            self._model_trace_root = require_private_storage_root(
                Path(model_trace_root),
                purpose="model-trace-root",
                anchor=anchor,
            )

    def _ensure_private_artifact_root(self) -> PrivateWorkspaceAnchor:
        anchor = self._private_workspace_anchor
        if anchor is None:
            anchor = resolve_private_workspace_anchor(
                self._artifact_root,
                purpose="artifact-root",
            )
            self._private_workspace_anchor = anchor
        self._artifact_root = require_private_storage_root(
            self._artifact_root,
            purpose="artifact-root",
            anchor=anchor,
        )
        return anchor

    # ------------------------------------------------------------------ run

    def run(self, task: AuditTask | CashflowTask | FundamentalEvidenceTask) -> AuditOutcome | CashflowOutcome:
        """Run the task's financial checks and persist a replayable outcome."""

        from finauditgate.research import SecurityMarketTask, ReviewNativeJudgment
        if type(task) is ReviewNativeJudgment:
            from finauditgate.core.judgment import run
            self._ensure_private_artifact_root()
            return run(task,self._artifact_root)
        if type(task) is SecurityMarketTask:
            from finauditgate.core.security_market import run
            self._ensure_private_artifact_root()
            return run(task, self._artifact_root)
        if type(task) is FundamentalEvidenceTask:
            from finauditgate.core.research_evidence import run
            self._ensure_private_artifact_root()
            return run(task, self._artifact_root)
        if type(task) is CashflowTask:
            from finauditgate.core.cashflow import run
            self._ensure_private_artifact_root()
            return run(task, self._artifact_root, self._investigator)
        if self._model is None:
            raise RuntimeError("run() requires a candidate model Adapter")
        if type(task) is not AuditTask:
            raise TypeError("task must be an AuditTask")
        if task.mode in REVIEWED_PROFILE_MODES:
            anchor = self._ensure_private_artifact_root()
            if (
                self._private_dev_profile is not None
                and self._private_dev_profile.private_workspace_anchor is not None
                and self._private_dev_profile.private_workspace_anchor != anchor
            ):
                raise PrivateStorageError(
                    "PRIVATE_STORAGE_REQUIRED:private-validation-profile:"
                    "WORKSPACE_ANCHOR_MISMATCH"
                )
        try:
            document = task.document.document_bytes
            document_sha256 = sha256_hex(document)
            task_bytes = canonical_json_bytes(_task_payload(task, document_sha256))
            normalized_task_payload = json.loads(task_bytes)
            if not isinstance(normalized_task_payload, dict):
                raise TypeError("canonical task must be an object")
            task = _task_from_payload(normalized_task_payload, document)
            if (
                canonical_json_bytes(_task_payload(task, document_sha256))
                != task_bytes
            ):
                raise ValueError("task is not canonically representable")
        except (AttributeError, TypeError, ValueError) as exc:
            raise TypeError("task could not be normalized") from exc

        profile: PrivateDevValidationProfile | None = None
        if task.mode in REVIEWED_PROFILE_MODES:
            if self._private_dev_profile is None:
                raise RuntimeError("REVIEWED_VALIDATION_PROFILE_REQUIRED")
            profile = self._private_dev_profile
        if profile is None:
            policy = SYNTHETIC_POLICY
            policy_bytes = SYNTHETIC_POLICY_BYTES
            policy_sha256 = SYNTHETIC_POLICY_SHA256
            registries_sha256 = SYNTHETIC_REGISTRIES_SHA256
        else:
            policy = profile.policy
            policy_bytes = profile.policy_bytes
            policy_sha256 = profile.sha256
            registries_sha256 = profile.registries_sha256

        attempt_entries: list[dict[str, object]] = []
        trace_receipts: dict[int, dict[str, object]] = {}
        candidate_bytes: bytes | None = None
        for attempt_index in range(RETRY_BUDGET + 1):
            snapshot: dict[str, object] | None = None
            snapshot_sha256: str | None = None
            attempt_candidate_payload: dict[str, object] | None = None
            attempt_candidate_bytes: bytes | None = None
            candidate = None
            try:
                proposed = self._model.propose(task, attempt_index=attempt_index)
            except LookupError as exc:
                if (
                    attempt_index == 1
                    and len(attempt_entries) == 1
                    and attempt_entries[0]["disposition"] == Decision.RETRY.value
                ):
                    attempt_reasons: tuple[str, ...] = (
                        "CANDIDATE_PROPOSAL_MISSING",
                    )
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
                if type(proposed) is ModelExecution:
                    receipt = proposed.trace_receipt
                    if (
                        receipt.task_id != task.task_id
                        or receipt.attempt_index != attempt_index
                    ):
                        raise RuntimeError(
                            "model trace does not belong to this task attempt"
                        )
                    _, trace_failure = verify_raw_model_trace(
                        self._model_trace_root,
                        receipt,
                        task=task,
                        attempt_index=attempt_index,
                        expected_proposal=proposed.proposal,
                        expected_failure_code=proposed.failure_code,
                    )
                    if trace_failure is not None:
                        raise RuntimeError(
                            "model trace failed before a run could be committed: "
                            + trace_failure
                        )
                    trace_receipts[attempt_index] = receipt_payload(receipt)
                    if proposed.failure_code is not None:
                        attempt_reasons = ("MODEL_CANDIDATE_REJECTED",)
                        proposed = None
                    else:
                        proposed = proposed.proposal
                elif profile is not None:
                    raise RuntimeError("REVIEWED_MODEL_TRACE_REQUIRED")
                if not attempt_reasons:
                    snapshot = proposal_snapshot(proposed)
                    snapshot_sha256 = sha256_hex(canonical_json_bytes(snapshot))
                    try:
                        attempt_candidate_payload, candidate = (
                            candidate_from_proposal_snapshot(snapshot)
                        )
                        attempt_candidate_bytes = canonical_json_bytes(
                            attempt_candidate_payload
                        )
                    except Exception:
                        attempt_candidate_payload = None
                        attempt_candidate_bytes = None
                        attempt_reasons = ("CANDIDATE_SHAPE_INVALID",)

            if attempt_reasons:
                candidate_bytes = None
                attempt_entries.append(
                    _attempt_entry(
                        attempt_index,
                        snapshot,
                        snapshot_sha256,
                        None,
                        Decision.RETRY,
                        attempt_reasons,
                    )
                )
                if attempt_index == 0:
                    continue
                decision = Decision.RETRY
                reason_codes = attempt_reasons + ("RETRY_BUDGET_EXHAUSTED",)
                answer = None
                answer_unit = None
                ledger_payload, formula_payload = failed_artifacts(
                    decision,
                    reason_codes,
                )
                break

            assert attempt_candidate_payload is not None
            assert attempt_candidate_bytes is not None
            candidate_bytes = attempt_candidate_bytes
            try:
                if profile is None:
                    ledger_payload, formula_payload = evaluate_profiled_candidate(
                        document,
                        candidate,
                        task,
                        policy=policy,
                        policy_sha256=policy_sha256,
                        registries_sha256=registries_sha256,
                    )
                else:
                    ledger_payload, formula_payload = evaluate_private_candidate(
                        document,
                        candidate,
                        task,
                        policy=policy,
                        policy_sha256=policy_sha256,
                        semantics_sha256=registries_sha256,
                    )
            except ValidationFailure as failure:
                attempt_entries.append(
                    _attempt_entry(
                        attempt_index,
                        snapshot,
                        snapshot_sha256,
                        attempt_candidate_payload,
                        failure.decision,
                        failure.reason_codes,
                    )
                )
                if failure.decision is Decision.RETRY and attempt_index == 0:
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
                _attempt_entry(
                    attempt_index,
                    snapshot,
                    snapshot_sha256,
                    attempt_candidate_payload,
                    Decision.ACCEPT,
                    (),
                )
            )
            decision = Decision.ACCEPT
            answer = formula_payload["result"]
            answer_unit = formula_payload["output_unit"]
            reason_codes = ()
            break
        else:
            raise RuntimeError("retry loop ended without a decision")

        if candidate_bytes is None:
            candidate_bytes = canonical_json_bytes(None)
        attempts_bytes = canonical_json_bytes(
            {
                "schema_version": ATTEMPTS_SCHEMA_VERSION,
                "retry_budget": RETRY_BUDGET,
                "attempts": attempt_entries,
                "final_decision": decision.value,
                "final_reason_codes": list(reason_codes),
            }
        )
        model_trace_bytes: bytes | None = None
        if trace_receipts:
            if set(trace_receipts) != {
                entry["attempt_index"] for entry in attempt_entries
            }:
                raise RuntimeError("every model attempt must have a trace receipt")
            model_trace_bytes = canonical_json_bytes(
                {
                    "schema_version": TRACE_SUMMARY_SCHEMA_VERSION,
                    "attempts": [
                        {
                            "attempt_index": entry["attempt_index"],
                            "receipt": trace_receipts[entry["attempt_index"]],
                            "proposal_sha256": entry["proposal_sha256"],
                            "disposition": entry["disposition"],
                            "reason_codes": entry["reason_codes"],
                            "terminal": index == len(attempt_entries) - 1,
                        }
                        for index, entry in enumerate(attempt_entries)
                    ],
                    "final_decision": decision.value,
                    "final_reason_codes": list(reason_codes),
                }
            )
        identity_bytes = canonical_json_bytes(
            _identity_payload(
                task_bytes=task_bytes,
                candidate_bytes=candidate_bytes,
                attempts_bytes=attempts_bytes,
                model_trace_bytes=model_trace_bytes,
                policy_sha256=policy_sha256,
            )
        )
        run_id = sha256_hex(identity_bytes)
        run_ref = RunRef(run_id=run_id)
        artifact_payloads = {
            "identity": identity_bytes,
            "task": task_bytes,
            "candidate": candidate_bytes,
            "attempts": attempts_bytes,
            "policy": policy_bytes,
            "ledger": canonical_json_bytes(ledger_payload),
            "formula": canonical_json_bytes(formula_payload),
            "outcome": canonical_json_bytes(
                _outcome_payload(
                    task.task_id,
                    decision,
                    answer,
                    answer_unit,
                    reason_codes,
                    run_id,
                    document_sha256,
                )
            ),
        }
        artifact_files = _ARTIFACT_FILES
        if model_trace_bytes is not None:
            artifact_payloads["model_trace"] = model_trace_bytes
            artifact_files = _TRACED_ARTIFACT_FILES
        run_directory = self._artifact_root / "runs" / run_id
        write_once(
            self._artifact_root / "blobs" / "sha256" / document_sha256,
            document,
        )
        for name, payload in artifact_payloads.items():
            write_once(run_directory / artifact_files[name], payload)
        write_once(
            run_directory / "manifest.json",
            canonical_json_bytes(
                _manifest_payload(
                    run_id,
                    document_sha256,
                    policy_sha256,
                    artifact_files,
                    artifact_payloads,
                )
            ),
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

    # --------------------------------------------------------------- replay

    def replay(self, run_ref: RunRef) -> ReplayReport:
        """Verify and recalculate a stored run without model or network access."""

        if type(run_ref) is not RunRef:
            raise TypeError("run_ref must be a RunRef")
        run_id = run_ref.run_id
        run_ref = RunRef(run_id=run_id)
        run_directory = self._artifact_root / "runs" / run_id
        if (run_directory / "judgment.json").is_file():
            from finauditgate.core.judgment import replay
            self._ensure_private_artifact_root()
            return replay(self._artifact_root,run_ref)
        if (run_directory / "security-market.json").is_file():
            from finauditgate.core.security_market import replay
            self._ensure_private_artifact_root()
            return replay(self._artifact_root, run_ref)
        if (run_directory / "research-evidence.json").is_file():
            from finauditgate.core.research_evidence import replay
            self._ensure_private_artifact_root()
            return replay(self._artifact_root, run_ref)
        if (run_directory / "cashflow.json").is_file():
            from finauditgate.core.cashflow import replay
            self._ensure_private_artifact_root()
            return replay(self._artifact_root, run_ref)
        try:
            manifest_bytes = _read_bounded_json_bytes(run_directory / "manifest.json")
            if manifest_bytes is None or _json_payload_exceeds_limits(manifest_bytes):
                return _failed_replay(run_ref, "MALFORMED_MANIFEST")
            manifest = json.loads(manifest_bytes)
            if not isinstance(manifest, dict):
                return _failed_replay(run_ref, "MALFORMED_MANIFEST")
            if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
                return _failed_replay(run_ref, "UNSUPPORTED_MANIFEST_SCHEMA")
            if set(manifest) != {
                "schema_version",
                "run_id",
                "document_sha256",
                "calculation_policy_sha256",
                "artifacts",
            }:
                return _failed_replay(run_ref, "MALFORMED_MANIFEST")
            if manifest["run_id"] != run_id:
                return _failed_replay(run_ref, "RUN_ID_MISMATCH")
            policy_sha256 = manifest["calculation_policy_sha256"]
            if not isinstance(policy_sha256, str) or not _SHA256_HEX.fullmatch(
                policy_sha256
            ):
                return _failed_replay(run_ref, "POLICY_IDENTITY_MISMATCH")
            artifacts = manifest["artifacts"]
            if not isinstance(artifacts, dict):
                return _failed_replay(run_ref, "MALFORMED_MANIFEST")
            if set(artifacts) == set(_TRACED_ARTIFACT_FILES):
                artifact_files = _TRACED_ARTIFACT_FILES
            elif set(artifacts) == set(_ARTIFACT_FILES):
                artifact_files = _ARTIFACT_FILES
            else:
                return _failed_replay(run_ref, "ARTIFACT_SET_MISMATCH")
            is_traced_run = "model_trace" in artifact_files

            artifact_payloads: dict[str, bytes] = {}
            for name, filename in artifact_files.items():
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
                    return _failed_replay(run_ref, "ARTIFACT_STRUCTURE_INVALID")
                if sha256_hex(payload) != entry["sha256"]:
                    return _failed_replay(run_ref, "ARTIFACT_HASH_MISMATCH")
                if _json_payload_exceeds_limits(payload):
                    return _failed_replay(run_ref, "ARTIFACT_STRUCTURE_INVALID")
                artifact_payloads[name] = payload
            expected_manifest = _manifest_payload(
                run_id,
                manifest["document_sha256"],
                policy_sha256,
                artifact_files,
                artifact_payloads,
            )
            if manifest != expected_manifest:
                return _failed_replay(run_ref, "MANIFEST_CONTENT_MISMATCH")
            if manifest_bytes != canonical_json_bytes(expected_manifest):
                return _failed_replay(run_ref, "NON_CANONICAL_MANIFEST")

            document_sha256 = manifest["document_sha256"]
            if not isinstance(document_sha256, str) or not _SHA256_HEX.fullmatch(
                document_sha256
            ):
                return _failed_replay(run_ref, "INVALID_DOCUMENT_HASH")
            document_path = self._artifact_root / "blobs" / "sha256" / document_sha256
            if not document_path.is_file():
                return _failed_replay(run_ref, "MISSING_ARTIFACT")
            document = document_path.read_bytes()
            if sha256_hex(document) != document_sha256:
                return _failed_replay(run_ref, "DOCUMENT_HASH_MISMATCH")

            policy_bytes = artifact_payloads["policy"]
            if sha256_hex(policy_bytes) != policy_sha256:
                return _failed_replay(run_ref, "POLICY_ARTIFACT_MISMATCH")
            if policy_bytes == SYNTHETIC_POLICY_BYTES:
                policy = SYNTHETIC_POLICY
                registries_sha256 = SYNTHETIC_REGISTRIES_SHA256
                is_private_profile = False
            else:
                if not is_traced_run:
                    return _failed_replay(run_ref, "POLICY_ARTIFACT_MISMATCH")
                try:
                    private_profile = PrivateDevValidationProfile.from_bytes(
                        policy_bytes
                    )
                except (TypeError, ValueError):
                    return _failed_replay(run_ref, "POLICY_ARTIFACT_MISMATCH")
                if private_profile.sha256 != policy_sha256:
                    return _failed_replay(run_ref, "POLICY_ARTIFACT_MISMATCH")
                policy = private_profile.policy
                registries_sha256 = private_profile.registries_sha256
                is_private_profile = True

            task_payload = json.loads(artifact_payloads["task"])
            stored_task = _task_from_payload(task_payload, document)
            if (
                canonical_json_bytes(_task_payload(stored_task, document_sha256))
                != artifact_payloads["task"]
            ):
                return _failed_replay(run_ref, "NON_CANONICAL_TASK")
            stored_candidate_payload = json.loads(artifact_payloads["candidate"])
            if stored_candidate_payload is not None:
                stored_candidate = candidate_from_payload(stored_candidate_payload)
                if (
                    canonical_json_bytes(candidate_payload(stored_candidate))
                    != artifact_payloads["candidate"]
                ):
                    return _failed_replay(run_ref, "CANDIDATE_SCHEMA_MISMATCH")

            attempts_payload = json.loads(artifact_payloads["attempts"])
            if not isinstance(attempts_payload, dict) or set(attempts_payload) != {
                "schema_version",
                "retry_budget",
                "attempts",
                "final_decision",
                "final_reason_codes",
            }:
                return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
            raw_attempts = attempts_payload["attempts"]
            if (
                attempts_payload["schema_version"] != ATTEMPTS_SCHEMA_VERSION
                or attempts_payload["retry_budget"] != RETRY_BUDGET
                or not isinstance(raw_attempts, list)
                or len(raw_attempts) not in {1, 2}
            ):
                return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")

            expected_entries: list[dict[str, object]] = []
            replayed_ledger: dict[str, object] | None = None
            replayed_formula: dict[str, object] | None = None
            last_candidate_payload: dict[str, object] | None = None
            for attempt_index, raw_attempt in enumerate(raw_attempts):
                if not isinstance(raw_attempt, dict) or set(raw_attempt) != _ATTEMPT_FIELDS:
                    return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                raw_snapshot = raw_attempt["proposal"]
                snapshot_candidate_payload: dict[str, object] | None = None
                snapshot_candidate = None
                snapshot_invalid = False
                if raw_snapshot is None:
                    if raw_attempt["proposal_sha256"] is not None:
                        return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                else:
                    if not isinstance(raw_snapshot, dict):
                        return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                    if raw_attempt["proposal_sha256"] != sha256_hex(
                        canonical_json_bytes(raw_snapshot)
                    ):
                        return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                    try:
                        snapshot_candidate_payload, snapshot_candidate = (
                            candidate_from_proposal_snapshot(raw_snapshot)
                        )
                    except (
                        AttributeError,
                        KeyError,
                        OverflowError,
                        RecursionError,
                        TypeError,
                        ValueError,
                    ):
                        snapshot_invalid = True
                raw_candidate_payload = raw_attempt["candidate"]
                if raw_candidate_payload is None:
                    raw_reason_codes = raw_attempt["reason_codes"]
                    if raw_reason_codes == ["CANDIDATE_SHAPE_INVALID"]:
                        if raw_snapshot is None or not snapshot_invalid:
                            return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                        attempt_reasons: tuple[str, ...] = ("CANDIDATE_SHAPE_INVALID",)
                        last_candidate_payload = None
                    elif (
                        raw_reason_codes == ["CANDIDATE_PROPOSAL_MISSING"]
                        and attempt_index == 1
                    ):
                        if raw_snapshot is not None:
                            return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                        attempt_reasons = ("CANDIDATE_PROPOSAL_MISSING",)
                        last_candidate_payload = None
                    elif raw_reason_codes == ["MODEL_CANDIDATE_REJECTED"]:
                        if (
                            not is_traced_run
                            or raw_snapshot is not None
                            or raw_attempt["proposal_sha256"] is not None
                        ):
                            return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                        attempt_reasons = ("MODEL_CANDIDATE_REJECTED",)
                        last_candidate_payload = None
                    else:
                        return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                    disposition = Decision.RETRY
                    expected_entries.append(
                        _attempt_entry(
                            attempt_index,
                            raw_snapshot,
                            raw_attempt["proposal_sha256"],
                            None,
                            disposition,
                            attempt_reasons,
                        )
                    )
                else:
                    if (
                        not isinstance(raw_candidate_payload, dict)
                        or raw_snapshot is None
                        or snapshot_invalid
                        or snapshot_candidate_payload != raw_candidate_payload
                        or snapshot_candidate is None
                    ):
                        return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                    last_candidate_payload = raw_candidate_payload
                    if (
                        canonical_json_bytes(candidate_payload(snapshot_candidate))
                        != canonical_json_bytes(raw_candidate_payload)
                    ):
                        return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                    try:
                        if is_private_profile:
                            attempt_ledger, attempt_formula = (
                                evaluate_private_candidate(
                                    document,
                                    snapshot_candidate,
                                    stored_task,
                                    policy=policy,
                                    policy_sha256=policy_sha256,
                                    semantics_sha256=registries_sha256,
                                )
                            )
                        else:
                            attempt_ledger, attempt_formula = (
                                evaluate_profiled_candidate(
                                    document,
                                    snapshot_candidate,
                                    stored_task,
                                    policy=policy,
                                    policy_sha256=policy_sha256,
                                    registries_sha256=registries_sha256,
                                )
                            )
                    except ValidationFailure as failure:
                        disposition = failure.decision
                        attempt_reasons = failure.reason_codes
                    else:
                        disposition = Decision.ACCEPT
                        attempt_reasons = ()
                        replayed_ledger = attempt_ledger
                        replayed_formula = attempt_formula
                    expected_entries.append(
                        _attempt_entry(
                            attempt_index,
                            raw_snapshot,
                            raw_attempt["proposal_sha256"],
                            raw_candidate_payload,
                            disposition,
                            attempt_reasons,
                        )
                    )
                if disposition is Decision.ACCEPT and attempt_index != len(raw_attempts) - 1:
                    return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                if (
                    disposition is Decision.RETRY
                    and attempt_index == 0
                    and len(raw_attempts) == 1
                ):
                    return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
                if (
                    attempt_index == 0
                    and len(raw_attempts) == 2
                    and disposition is not Decision.RETRY
                ):
                    return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
            final_decision = disposition
            if final_decision is Decision.ACCEPT:
                final_reason_codes: tuple[str, ...] = ()
                if replayed_ledger is None or replayed_formula is None:
                    return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
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
            if last_candidate_payload != stored_candidate_payload:
                return _failed_replay(run_ref, "CANDIDATE_SCHEMA_MISMATCH")
            expected_attempts_bytes = canonical_json_bytes(
                {
                    "schema_version": ATTEMPTS_SCHEMA_VERSION,
                    "retry_budget": RETRY_BUDGET,
                    "attempts": expected_entries,
                    "final_decision": final_decision.value,
                    "final_reason_codes": list(final_reason_codes),
                }
            )
            if artifact_payloads["attempts"] != expected_attempts_bytes:
                return _failed_replay(run_ref, "ATTEMPTS_ARTIFACT_MISMATCH")
            if is_traced_run:
                trace_failure = _verify_model_trace_summary(
                    artifact_payloads["model_trace"],
                    expected_entries,
                    stored_task,
                    final_decision,
                    final_reason_codes,
                    self._model_trace_root,
                )
                if trace_failure is not None:
                    return _failed_replay(run_ref, trace_failure)
            expected_identity_bytes = canonical_json_bytes(
                _identity_payload(
                    task_bytes=artifact_payloads["task"],
                    candidate_bytes=artifact_payloads["candidate"],
                    attempts_bytes=artifact_payloads["attempts"],
                    model_trace_bytes=(
                        artifact_payloads["model_trace"] if is_traced_run else None
                    ),
                    policy_sha256=policy_sha256,
                )
            )
            if artifact_payloads["identity"] != expected_identity_bytes:
                return _failed_replay(run_ref, "RUN_IDENTITY_ARTIFACT_MISMATCH")
            if sha256_hex(expected_identity_bytes) != run_id:
                return _failed_replay(run_ref, "RUN_IDENTITY_MISMATCH")
            if artifact_payloads["ledger"] != canonical_json_bytes(replayed_ledger):
                return _failed_replay(run_ref, "LEDGER_REPLAY_MISMATCH")
            if artifact_payloads["formula"] != canonical_json_bytes(replayed_formula):
                return _failed_replay(run_ref, "FORMULA_REPLAY_MISMATCH")
            expected_outcome_bytes = canonical_json_bytes(
                _outcome_payload(
                    stored_task.task_id,
                    final_decision,
                    replayed_answer,
                    replayed_answer_unit,
                    final_reason_codes,
                    run_id,
                    document_sha256,
                )
            )
            if artifact_payloads["outcome"] != expected_outcome_bytes:
                return _failed_replay(run_ref, "OUTCOME_METADATA_MISMATCH")
            return ReplayReport(
                schema_version=REPLAY_SCHEMA_VERSION,
                run_ref=run_ref,
                consistent=True,
                decision=final_decision,
                answer=replayed_answer,
                answer_unit=replayed_answer_unit,
                verified_artifact_count=len(artifact_files) + 1,
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


# ------------------------------------------------------------- helpers


def _attempt_entry(
    attempt_index: int,
    snapshot: dict[str, object] | None,
    snapshot_sha256: str | None,
    candidate: dict[str, object] | None,
    disposition: Decision,
    reason_codes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "attempt_index": attempt_index,
        "proposal": snapshot,
        "proposal_sha256": snapshot_sha256,
        "candidate": candidate,
        "candidate_sha256": (
            None
            if candidate is None
            else sha256_hex(canonical_json_bytes(candidate))
        ),
        "disposition": disposition.value,
        "reason_codes": list(reason_codes),
    }


def _identity_payload(
    *,
    task_bytes: bytes,
    candidate_bytes: bytes,
    attempts_bytes: bytes,
    model_trace_bytes: bytes | None,
    policy_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "task_sha256": sha256_hex(task_bytes),
        "candidate_sha256": sha256_hex(candidate_bytes),
        "attempts_sha256": sha256_hex(attempts_bytes),
        "model_trace_sha256": (
            None if model_trace_bytes is None else sha256_hex(model_trace_bytes)
        ),
        "calculation_policy_sha256": policy_sha256,
    }


def _outcome_payload(
    task_id: str,
    decision: Decision,
    answer: str | None,
    answer_unit: str | None,
    reason_codes: tuple[str, ...],
    run_id: str,
    document_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "task_id": task_id,
        "decision": decision.value,
        "answer": answer,
        "answer_unit": answer_unit,
        "reason_codes": list(reason_codes),
        "run_ref": {"run_id": run_id},
        "document_sha256": document_sha256,
    }


def _manifest_payload(
    run_id: str,
    document_sha256: object,
    policy_sha256: str,
    artifact_files: dict[str, str],
    artifact_payloads: dict[str, bytes],
) -> dict[str, object]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "document_sha256": document_sha256,
        "calculation_policy_sha256": policy_sha256,
        "artifacts": {
            name: {
                "filename": artifact_files[name],
                "sha256": sha256_hex(payload),
            }
            for name, payload in sorted(artifact_payloads.items())
        },
    }


def _task_payload(task: AuditTask, document_sha256: str) -> dict[str, object]:
    return {
        "schema_version": TASK_SCHEMA_VERSION,
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
    if payload.get("schema_version") != TASK_SCHEMA_VERSION:
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


def _read_bounded_json_bytes(path: Path) -> bytes | None:
    with path.open("rb") as artifact:
        payload = artifact.read(_MAX_JSON_ARTIFACT_BYTES + 1)
    if len(payload) > _MAX_JSON_ARTIFACT_BYTES:
        return None
    return payload


def _json_payload_exceeds_limits(payload: bytes) -> bool:
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
            if depth > _MAX_JSON_NESTING:
                return True
        elif byte in {0x5D, 0x7D}:
            depth -= 1
    return False


def _verify_model_trace_summary(
    summary_bytes: bytes,
    expected_attempts: list[dict[str, object]],
    task: AuditTask,
    final_decision: Decision,
    final_reason_codes: tuple[str, ...],
    trace_root: Path | None,
) -> str | None:
    try:
        summary = json.loads(summary_bytes)
        if canonical_json_bytes(summary) != summary_bytes:
            return "MODEL_TRACE_SUMMARY_INVALID"
    except (RecursionError, TypeError, UnicodeError, ValueError):
        return "MODEL_TRACE_SUMMARY_INVALID"
    if (
        type(summary) is not dict
        or set(summary)
        != {"schema_version", "attempts", "final_decision", "final_reason_codes"}
        or summary["schema_version"] != TRACE_SUMMARY_SCHEMA_VERSION
    ):
        return "MODEL_TRACE_SUMMARY_INVALID"
    raw_attempts = summary["attempts"]
    if (
        type(raw_attempts) is not list
        or len(raw_attempts) != len(expected_attempts)
        or summary["final_decision"] != final_decision.value
        or summary["final_reason_codes"] != list(final_reason_codes)
    ):
        return "MODEL_TRACE_SUMMARY_INVALID"
    for index, (raw_attempt, expected_attempt) in enumerate(
        zip(raw_attempts, expected_attempts, strict=True)
    ):
        if type(raw_attempt) is not dict or set(raw_attempt) != {
            "attempt_index",
            "receipt",
            "proposal_sha256",
            "disposition",
            "reason_codes",
            "terminal",
        }:
            return "MODEL_TRACE_SUMMARY_INVALID"
        if (
            raw_attempt["attempt_index"] != expected_attempt["attempt_index"]
            or raw_attempt["proposal_sha256"] != expected_attempt["proposal_sha256"]
            or raw_attempt["disposition"] != expected_attempt["disposition"]
            or raw_attempt["reason_codes"] != expected_attempt["reason_codes"]
            or raw_attempt["terminal"] is not (index == len(expected_attempts) - 1)
        ):
            return "MODEL_TRACE_SUMMARY_INVALID"
        try:
            receipt = receipt_from_payload(raw_attempt["receipt"])
        except (TypeError, ValueError):
            return "MODEL_TRACE_RECEIPT_INVALID"
        if (
            receipt.task_id != task.task_id
            or receipt.attempt_index != expected_attempt["attempt_index"]
        ):
            return "MODEL_TRACE_CROSS_RUN_MISMATCH"
        if expected_attempt["reason_codes"] == ["MODEL_CANDIDATE_REJECTED"]:
            if receipt.parse_status != "CANDIDATE_REJECTED":
                return "MODEL_TRACE_SUMMARY_INVALID"
            expected_proposal = None
            expected_failure_code = receipt.failure_code
        elif receipt.parse_status != "CANDIDATE_PARSED":
            return "MODEL_TRACE_SUMMARY_INVALID"
        else:
            try:
                _, expected_proposal = candidate_from_proposal_snapshot(
                    expected_attempt["proposal"]
                )
            except (TypeError, ValueError):
                return "MODEL_TRACE_SUMMARY_INVALID"
            expected_failure_code = None
        _, raw_failure = verify_raw_model_trace(
            trace_root,
            raw_attempt["receipt"],
            task=task,
            attempt_index=expected_attempt["attempt_index"],
            expected_proposal=expected_proposal,
            expected_failure_code=expected_failure_code,
        )
        if raw_failure is not None:
            return raw_failure
    return None


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
