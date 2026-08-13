"""Deep implementation behind the two-method FinAuditGate Interface."""

from __future__ import annotations

from dataclasses import asdict
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


_RUN_ID = re.compile(r"[0-9a-f]{64}")
_ARTIFACT_FILES = {
    "identity": "identity.json",
    "task": "task.json",
    "candidate": "candidate.json",
    "policy": "policy.json",
    "ledger": "ledger.json",
    "formula": "formula.json",
    "outcome": "outcome.json",
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

    def replay(self, run_ref: RunRef) -> ReplayReport:
        """Verify and recalculate a stored run without model or network access."""

        if type(run_ref) is not RunRef:
            raise TypeError("run_ref must be a RunRef")
        run_id = run_ref.run_id
        run_ref = RunRef(run_id=run_id)

        try:
            run_directory = self._artifact_root / "runs" / run_id
            manifest_bytes = (run_directory / "manifest.json").read_bytes()
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
                payload = artifact_path.read_bytes()
                if sha256_hex(payload) != entry.get("sha256"):
                    return _failed_replay(run_ref, "ARTIFACT_HASH_MISMATCH")
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


def _candidate_payload(candidate: ModelCandidate) -> dict[str, object]:
    return {
        "schema_version": "finauditgate.candidate/v1",
        "evidence": [asdict(item) for item in candidate.evidence],
        "calculation": {
            **asdict(candidate.calculation),
            "operand_ids": list(candidate.calculation.operand_ids),
        },
    }


def _candidate_from_payload(payload: dict[str, object]) -> ModelCandidate:
    if payload.get("schema_version") != "finauditgate.candidate/v1":
        raise ValueError("unsupported candidate schema")
    raw_evidence = payload["evidence"]
    raw_calculation = payload["calculation"]
    if not isinstance(raw_evidence, list) or not isinstance(
        raw_calculation, dict
    ):
        raise TypeError("invalid candidate payload")
    return ModelCandidate(
        evidence=tuple(EvidenceCandidate(**item) for item in raw_evidence),
        calculation=CalculationCandidate(
            operation=raw_calculation["operation"],
            operand_ids=tuple(raw_calculation["operand_ids"]),
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
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise TypeError("artifact must be a JSON object")
    return payload


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
