"""Deep FinResearchOps Implementation behind ``handle`` and ``read_case``."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from typing import Callable

from finauditgate import (
    AuditTask,
    Decision,
    FinAuditGate,
    FrozenDocumentPackage,
    ReplayReport,
    RunRef,
)
from finauditgate.application.contracts import (
    ApplicationError,
    ApplicationOperation,
    ApplicationOutcome,
    CalculationRecord,
    CaseStatus,
    CaseView,
    ChangePacketView,
    CreateCase,
    ExportChangePacket,
    ReplayRecordView,
    ReplayRun,
    ReviewAction,
    ReviewRecordView,
    RunAnalysis,
    SourceRefView,
    SubmitReview,
    VerifiedFact,
    WorkpaperView,
    _validate_case_ref,
)
from finauditgate.contracts import REVIEWED_PROFILE_MODES, RUN_SCHEMA_VERSION
from finauditgate.core.artifacts import (
    canonical_json_bytes,
    sha256_hex,
    write_once,
)
from finauditgate.core.engine import MANIFEST_SCHEMA_VERSION
from finauditgate.core.profiles import PrivateDevValidationProfile
from finauditgate.ports.model import CandidateModel
from finauditgate.core.operations import ANSWER_CONTRACT_VALUES
from finauditgate.cashflow import CashflowCaseView, InvestigateCashflow
from finauditgate.research import ResearchCaseView, TradingBaselineView, NativeResearchView, ThesisCaseView
from finauditgate.private_storage import (
    PrivateStorageError,
    PrivateWorkspaceAnchor,
    require_private_storage_root,
    resolve_private_workspace_anchor,
)


WORKPAPER_SCHEMA_VERSION = "finresearchops.workpaper/v3"
REPLAY_RECORD_SCHEMA_VERSION = "finresearchops.replay-record/v3"
_VERIFIED_LEDGER_LABELS = {
    "VERIFIED_FROM_FROZEN_BYTES",
    "VERIFIED_AGAINST_FROZEN_PRIVATE_ALLOWLIST",
}


@dataclass(frozen=True, slots=True)
class _CaseTransactionInspection:
    """Pure inspection result for the journal-head visibility boundary."""

    pending: dict[str, object] | None
    pending_material_published: bool
    pending_event_published: bool
    unsealed_committed: dict[str, object] | None


class FinResearchOps:
    """Case, Review, export, and replay coordination behind two methods."""

    def __init__(
        self,
        *,
        artifact_root: Path,
        model: CandidateModel | None = None,
        model_trace_root: Path | None = None,
        private_dev_profile: PrivateDevValidationProfile | None = None,
        private_workspace_anchor: PrivateWorkspaceAnchor | None = None,
        retry_budget: int = 1,
        clock: Callable[[], datetime] | None = None,
        investigator=None,
        researcher=None,
    ) -> None:
        if type(retry_budget) is not int or retry_budget != 1:
            raise ValueError("M2 retry_budget must be exactly 1")
        self._private_workspace_anchor = private_workspace_anchor
        self._root = Path(artifact_root)
        if private_workspace_anchor is not None:
            self._root = require_private_storage_root(
                self._root,
                purpose="artifact-root",
                anchor=private_workspace_anchor,
            )
        self._application_root = self._root / "application"
        self._core_root = self._root / "core"
        self._gate = FinAuditGate(
            artifact_root=self._core_root,
            model=model,
            model_trace_root=model_trace_root,
            private_dev_profile=private_dev_profile,
            private_workspace_anchor=private_workspace_anchor,
            investigator=investigator,
        )
        self._offline_gate = FinAuditGate(
            artifact_root=self._core_root,
            model_trace_root=model_trace_root,
            private_workspace_anchor=private_workspace_anchor,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._researcher = researcher

    def handle(self, command: object) -> ApplicationOutcome | CashflowCaseView | ResearchCaseView | TradingBaselineView | NativeResearchView | ThesisCaseView | ReplayReport:
        """Execute one exact, closed, versioned command type."""

        try:
            from finauditgate.research import ResearchSecurity, RunTradingBaseline, RunAuditedNativeResearch, ResearchThesis
            if type(command) is ResearchThesis:
                from finauditgate.application.thesis_case import run
                return run(self, command)
            if type(command) is RunAuditedNativeResearch:
                from finauditgate.application.native_case import run
                return run(self, command)
            if type(command) is RunTradingBaseline:
                from finauditgate.application.baseline_case import run
                return run(self, command)
            if type(command) is ResearchSecurity:
                from finauditgate.application.research_case import run
                return run(self, command)
            if type(command) is InvestigateCashflow:
                from finauditgate.application.cashflow_case import save
                self._require_private_artifact_root()
                outcome = self._gate.run(command.task)
                case_ref = save(self._application_root, outcome)
                return self.read_case(case_ref)
            if type(command) is CreateCase:
                return self._create_case(command)
            if type(command) is RunAnalysis:
                with self._case_lock(command.case_ref):
                    self._seal_committed_case_transaction(command.case_ref)
                    return self._run_analysis(command)
            if type(command) is SubmitReview:
                with self._case_lock(command.case_ref):
                    self._seal_committed_case_transaction(command.case_ref)
                    return self._submit_review(command)
            if type(command) is ExportChangePacket:
                with self._case_lock(command.case_ref):
                    self._seal_committed_case_transaction(command.case_ref)
                    return self._export_change_packet(command)
            if type(command) is ReplayRun:
                if (self._core_root / "runs" / command.run_ref.run_id / "research-evidence.json").is_file():
                    return self._offline_gate.replay(command.run_ref)
                if (self._core_root / "runs" / command.run_ref.run_id / "cashflow.json").is_file():
                    return self._offline_gate.replay(command.run_ref)
                return self._replay_run(command)
            raise ApplicationError("UNSUPPORTED_COMMAND")
        except ApplicationError:
            raise
        except RuntimeError as exc:
            raise ApplicationError("APPLICATION_ARTIFACT_CONFLICT") from exc
        except OSError as exc:
            raise ApplicationError("APPLICATION_STORAGE_FAILED") from exc

    def read_case(self, case_ref: str) -> CaseView | CashflowCaseView | ResearchCaseView | TradingBaselineView | NativeResearchView | ThesisCaseView:
        """Reopen and integrity-check one Case from append-only artifacts."""

        try:
            try:
                _validate_case_ref(case_ref)
            except (ValueError, TypeError):
                raise ApplicationError("CASE_REF_INVALID")
            if (self._application_root / "thesis-cases" / case_ref / "case.json").is_file():
                from finauditgate.application.thesis_case import load
                self._require_private_artifact_root()
                return load(self, case_ref)
            if (self._application_root / "baseline-cases" / case_ref / "case.json").is_file():
                from finauditgate.application.baseline_case import load
                self._require_private_artifact_root()
                return load(self, case_ref)
            if (self._application_root / "native-audited-cases" / case_ref / "case.json").is_file():
                from finauditgate.application.native_case import load
                self._require_private_artifact_root()
                return load(self, case_ref)
            if (self._application_root / "research-cases" / case_ref / "case.json").is_file():
                from finauditgate.application.research_case import load
                self._require_private_artifact_root()
                return load(self, case_ref)
            if (self._application_root / "cashflow-cases" / case_ref / "case.json").is_file():
                from finauditgate.application.cashflow_case import load
                self._require_private_artifact_root()
                return load(self._application_root, self._offline_gate, self._core_root, case_ref)
            with self._case_lock(case_ref):
                return self._load_case(case_ref)
        except ApplicationError:
            raise
        except RuntimeError as exc:
            raise ApplicationError("APPLICATION_ARTIFACT_CONFLICT") from exc
        except OSError as exc:
            raise ApplicationError("APPLICATION_STORAGE_FAILED") from exc

    def _create_case(self, command: CreateCase) -> ApplicationOutcome:
        if command.mode in REVIEWED_PROFILE_MODES:
            self._require_private_artifact_root()
        document_sha256 = sha256_hex(command.document.document_bytes)
        identity = {
            "schema_version": "finresearchops.case-identity/v1",
            "question": command.question,
            "cutoff": command.cutoff.isoformat(),
            "source_ref": {
                "source_id": command.document.source_id,
                "document_name": command.document.document_name,
                "document_sha256": document_sha256,
                "declared_published_at": (
                    command.document.declared_published_at.isoformat()
                ),
            },
            "answer_contract": command.answer_contract,
            "risk_class": command.risk_class,
            "mode": command.mode,
        }
        case_ref = f"case-{sha256_hex(canonical_json_bytes(identity))}"
        payload = {
            "schema_version": "finresearchops.case-record/v1",
            "case_ref": case_ref,
            **{key: value for key, value in identity.items() if key != "schema_version"},
        }
        with self._case_lock(case_ref, allow_missing=True):
            case_path = self._case_directory(case_ref) / "case.json"
            case_already_exists = case_path.is_file()
            write_once(
                self._application_root / "blobs" / "sha256" / document_sha256,
                command.document.document_bytes,
            )
            if not case_already_exists:
                write_once(
                    self._case_head_path(case_ref),
                    canonical_json_bytes(
                        self._case_head_payload(case_ref, 0, None)
                    ),
                )
            write_once(
                case_path,
                canonical_json_bytes(payload),
            )
            view = self._load_case(case_ref)
            return ApplicationOutcome(
                operation=ApplicationOperation.CREATE_CASE,
                case_ref=case_ref,
                status=view.status,
            )

    def _run_analysis(self, command: RunAnalysis) -> ApplicationOutcome:
        inspection = self._recover_case_transitions(command.case_ref)
        command_identity = {
            "schema_version": RunAnalysis.schema_version,
            "case_ref": command.case_ref,
        }
        if inspection.pending is not None:
            if inspection.pending["command_identity"] != command_identity:
                raise ApplicationError("CASE_TRANSACTION_PENDING_CONFLICT")
            self._validate_pending_case_transaction(inspection.pending)
            self._apply_case_transaction(inspection.pending)
            material = inspection.pending["material_payload"]
            run_ref = self._run_ref_from_payload(material["run_ref"])
            decision = Decision(material["machine_decision"])
            return ApplicationOutcome(
                operation=ApplicationOperation.RUN_ANALYSIS,
                case_ref=command.case_ref,
                status=CaseStatus.AWAITING_REVIEW,
                run_ref=run_ref,
                workpaper_ref=inspection.pending["material_ref"],
                machine_decision=decision,
                reason_codes=tuple(material["gate_reason_codes"]),
            )
        view = self._load_case(command.case_ref)
        if view.status not in {CaseStatus.CREATED, CaseStatus.RETURNED}:
            raise ApplicationError("ANALYSIS_NOT_ALLOWED_IN_CURRENT_STATE")
        case_payload = self._read_case_payload(command.case_ref)
        document_sha256 = case_payload["source_ref"]["document_sha256"]
        document = self._read_blob(document_sha256)
        analysis_number = len(view.run_refs) + 1
        source_id = case_payload["source_ref"]["source_id"]
        task_id = (
            source_id
            if analysis_number == 1
            else f"{source_id}:analysis-{analysis_number}"
        )
        task = AuditTask(
            task_id=task_id,
            question=case_payload["question"],
            cutoff=date.fromisoformat(case_payload["cutoff"]),
            document=self._document_from_case(case_payload, document),
            answer_contract=case_payload["answer_contract"],
            risk_class=case_payload["risk_class"],
            mode=case_payload["mode"],
        )
        try:
            outcome = self._gate.run(task)
            replay = self._offline_gate.replay(outcome.run_ref)
        except Exception as exc:
            raise ApplicationError("ANALYSIS_EXECUTION_FAILED") from exc
        if (
            not replay.consistent
            or replay.decision is not outcome.decision
            or replay.answer != outcome.answer
            or replay.answer_unit != outcome.answer_unit
        ):
            raise ApplicationError("PRIMARY_RUN_REPLAY_MISMATCH")

        run_id = outcome.run_ref.run_id
        run_prefix = f"runs/{run_id}/"
        (
            attempts_artifact_ref,
            trace_summary_ref,
            trace_summary_status,
        ) = self._verified_run_trace_summary(outcome.run_ref)
        workpaper_body = {
            "schema_version": WORKPAPER_SCHEMA_VERSION,
            "case_ref": command.case_ref,
            "run_ref": {"run_id": run_id},
            "machine_decision": outcome.decision.value,
            "answer": outcome.answer,
            "answer_unit": outcome.answer_unit,
            "document_sha256": outcome.document_sha256,
            "evidence_ledger_ref": run_prefix + "ledger.json",
            "formula_ref": run_prefix + "formula.json",
            "candidate_artifact_ref": run_prefix + "candidate.json",
            "attempts_artifact_ref": attempts_artifact_ref,
            "trace_summary_ref": trace_summary_ref,
            "trace_summary_status": trace_summary_status,
            "gate_reason_codes": list(outcome.reason_codes),
        }
        workpaper_ref = (
            "workpaper-" + sha256_hex(canonical_json_bytes(workpaper_body))
        )
        workpaper_payload = {
            **workpaper_body,
            "workpaper_ref": workpaper_ref,
        }
        committed = self._commit_case_transition(
            command.case_ref,
            command_identity=command_identity,
            material_kind="workpaper",
            material_ref=workpaper_ref,
            material_payload=workpaper_payload,
            event_body={
                "schema_version": "finresearchops.case-event.analysis/v1",
                "run_ref": {"run_id": run_id},
                "workpaper_ref": workpaper_ref,
            },
        )
        if committed["material_ref"] != workpaper_ref:
            raise ApplicationError("CASE_TRANSACTION_COMMAND_INVALID")
        return ApplicationOutcome(
            operation=ApplicationOperation.RUN_ANALYSIS,
            case_ref=command.case_ref,
            status=CaseStatus.AWAITING_REVIEW,
            run_ref=outcome.run_ref,
            workpaper_ref=workpaper_ref,
            machine_decision=outcome.decision,
            reason_codes=outcome.reason_codes,
        )

    def _submit_review(self, command: SubmitReview) -> ApplicationOutcome:
        inspection = self._recover_case_transitions(command.case_ref)
        command_identity = {
            "schema_version": SubmitReview.schema_version,
            "case_ref": command.case_ref,
            "run_ref": {"run_id": command.run_ref.run_id},
            "action": command.action.value,
            "reason": command.reason.strip(),
        }
        if inspection.pending is not None:
            if inspection.pending["command_identity"] != command_identity:
                raise ApplicationError("CASE_TRANSACTION_PENDING_CONFLICT")
            self._validate_pending_case_transaction(inspection.pending)
            self._apply_case_transaction(inspection.pending)
            material = inspection.pending["material_payload"]
            resumed_view = self._load_case(command.case_ref)
            return ApplicationOutcome(
                operation=ApplicationOperation.SUBMIT_REVIEW,
                case_ref=command.case_ref,
                status=CaseStatus(material["resulting_case_status"]),
                run_ref=command.run_ref,
                workpaper_ref=material["workpaper_ref"],
                review_ref=inspection.pending["material_ref"],
                machine_decision=resumed_view.latest_machine_decision,
                reason_codes=resumed_view.latest_gate_reason_codes,
            )
        view = self._load_case(command.case_ref)
        if view.status is not CaseStatus.AWAITING_REVIEW:
            raise ApplicationError("REVIEW_NOT_ALLOWED_IN_CURRENT_STATE")
        if not view.run_refs or command.run_ref != view.run_refs[-1]:
            raise ApplicationError("REVIEW_RUN_IS_NOT_CURRENT")
        workpaper = view.workpapers[-1]
        if (
            command.action is ReviewAction.APPROVE
            and workpaper.machine_decision is not Decision.ACCEPT
        ):
            raise ApplicationError("MACHINE_DECISION_NOT_APPROVABLE")
        resulting_status = {
            ReviewAction.APPROVE: CaseStatus.APPROVED,
            ReviewAction.RETURN: CaseStatus.RETURNED,
            ReviewAction.REJECT: CaseStatus.REJECTED,
        }[command.action]
        recorded_at = self._recorded_at()
        body = {
            "schema_version": "finresearchops.review-record/v1",
            "case_ref": command.case_ref,
            "run_ref": {"run_id": command.run_ref.run_id},
            "workpaper_ref": workpaper.workpaper_ref,
            "action": command.action.value,
            "reason": command.reason.strip(),
            "recorded_at": recorded_at,
            "previous_case_status": CaseStatus.AWAITING_REVIEW.value,
            "resulting_case_status": resulting_status.value,
        }
        review_ref = "review-" + sha256_hex(canonical_json_bytes(body))
        payload = {**body, "review_ref": review_ref}
        committed = self._commit_case_transition(
            command.case_ref,
            command_identity=command_identity,
            material_kind="review",
            material_ref=review_ref,
            material_payload=payload,
            event_body={
                "schema_version": "finresearchops.case-event.review/v1",
                "review_ref": review_ref,
            },
        )
        review_ref = committed["material_ref"]
        committed_review = committed["material_payload"]
        resulting_status = CaseStatus(
            committed_review["resulting_case_status"]
        )
        return ApplicationOutcome(
            operation=ApplicationOperation.SUBMIT_REVIEW,
            case_ref=command.case_ref,
            status=resulting_status,
            run_ref=command.run_ref,
            workpaper_ref=workpaper.workpaper_ref,
            review_ref=review_ref,
            machine_decision=workpaper.machine_decision,
            reason_codes=workpaper.gate_reason_codes,
        )

    def _export_change_packet(
        self,
        command: ExportChangePacket,
    ) -> ApplicationOutcome:
        view = self._load_case(command.case_ref)
        if view.status is not CaseStatus.APPROVED:
            raise ApplicationError("CASE_NOT_ELIGIBLE_FOR_EXPORT")
        if not view.run_refs or command.run_ref != view.run_refs[-1]:
            raise ApplicationError("EXPORT_RUN_IS_NOT_CURRENT")
        workpaper = view.workpapers[-1]
        review = view.reviews[-1]
        if (
            workpaper.machine_decision is not Decision.ACCEPT
            or review.action is not ReviewAction.APPROVE
            or review.run_ref != command.run_ref
            or review.workpaper_ref != workpaper.workpaper_ref
        ):
            raise ApplicationError("EXPORT_LINEAGE_NOT_ELIGIBLE")
        replay = self._offline_gate.replay(command.run_ref)
        if replay.consistent is not True or replay.decision is not Decision.ACCEPT:
            raise ApplicationError("EXPORT_REPLAY_NOT_TRUSTED")
        ledger = self._read_core_object(
            command.run_ref,
            workpaper.evidence_ledger_ref,
            "ledger.json",
        )
        formula = self._read_core_object(
            command.run_ref,
            workpaper.formula_ref,
            "formula.json",
        )
        facts = self._facts_from_ledger(ledger)
        calculation = self._calculation_from_formula(formula)
        body = {
            "schema_version": "finresearchops.research-change-packet/v1",
            "case_ref": command.case_ref,
            "run_ref": {"run_id": command.run_ref.run_id},
            "review_ref": review.review_ref,
            "workpaper_ref": workpaper.workpaper_ref,
            "proposal_only": True,
            "verified_facts": [self._fact_payload(fact) for fact in facts],
            "calculations": [self._calculation_payload(calculation)],
            "impact_statement": (
                "Proposal only: "
                f"{view.question} Answer: {replay.answer} "
                f"{replay.answer_unit}."
            ),
            "unresolved_items": [],
        }
        packet_ref = "packet-" + sha256_hex(canonical_json_bytes(body))
        payload = {**body, "packet_ref": packet_ref}
        write_once(
            self._case_directory(command.case_ref)
            / "packets"
            / f"{packet_ref}.json",
            canonical_json_bytes(payload),
        )
        return ApplicationOutcome(
            operation=ApplicationOperation.EXPORT_CHANGE_PACKET,
            case_ref=command.case_ref,
            status=view.status,
            run_ref=command.run_ref,
            workpaper_ref=workpaper.workpaper_ref,
            review_ref=review.review_ref,
            packet_ref=packet_ref,
            machine_decision=workpaper.machine_decision,
        )

    def _replay_run(self, command: ReplayRun) -> ApplicationOutcome:
        case_ref = self._find_case_for_run(command.run_ref)
        with self._case_lock(case_ref):
            self._seal_committed_case_transaction(case_ref)
            view = self._load_case(case_ref)
            if command.run_ref not in view.run_refs:
                raise ApplicationError("RUN_NOT_OWNED_BY_A_CASE")
            report = self._offline_gate.replay(command.run_ref)
            if not report.consistent:
                raise ApplicationError("REPLAY_INTEGRITY_FAILED")
            self._verified_run_trace_summary(command.run_ref)
            body = {
                "schema_version": REPLAY_RECORD_SCHEMA_VERSION,
                "case_ref": case_ref,
                "run_ref": {"run_id": command.run_ref.run_id},
                "report": self._replay_report_payload(report),
            }
            replay_ref = "replay-" + sha256_hex(canonical_json_bytes(body))
            payload = {**body, "replay_ref": replay_ref}
            write_once(
                self._case_directory(case_ref)
                / "replays"
                / f"{replay_ref}.json",
                canonical_json_bytes(payload),
            )
            return ApplicationOutcome(
                operation=ApplicationOperation.REPLAY_RUN,
                case_ref=case_ref,
                status=view.status,
                run_ref=command.run_ref,
                replay_ref=replay_ref,
                machine_decision=report.decision,
            )

    def _load_case(
        self,
        case_ref: str,
        *,
        validate_pending: bool = True,
    ) -> CaseView:
        case_payload = self._read_case_payload(case_ref)
        source = case_payload["source_ref"]
        self._read_blob(source["document_sha256"])
        inspection = self._recover_case_transitions(case_ref)
        journal_head = self._read_case_head(case_ref)
        status = CaseStatus.CREATED
        workpapers: list[WorkpaperView] = []
        reviews: list[ReviewRecordView] = []
        run_refs: list[RunRef] = []

        event_directory = self._case_directory(case_ref) / "events"
        all_event_paths = sorted(event_directory.glob("*.json"))
        committed_event_count = journal_head["event_count"]
        expected_event_names = {
            f"{sequence:08d}.json"
            for sequence in range(1, committed_event_count + 1)
        }
        if inspection.pending_event_published:
            assert inspection.pending is not None
            expected_event_names.add(
                f"{inspection.pending['sequence']:08d}.json"
            )
        if {path.name for path in all_event_paths} != expected_event_names:
            raise ApplicationError("CASE_EVENT_MEMBERSHIP_MISMATCH")
        event_paths = [
            event_directory / f"{sequence:08d}.json"
            for sequence in range(1, committed_event_count + 1)
        ]
        previous_event_sha256: str | None = None
        for expected_sequence, path in enumerate(event_paths, start=1):
            if path.name != f"{expected_sequence:08d}.json":
                raise ApplicationError("CASE_EVENT_FILENAME_INVALID")
            event = self._read_canonical_object(path)
            if event.get("sequence") != expected_sequence:
                raise ApplicationError("CASE_EVENT_SEQUENCE_INVALID")
            if event.get("case_ref") != case_ref:
                raise ApplicationError("CASE_EVENT_CASE_MISMATCH")
            if event.get("previous_event_sha256") != previous_event_sha256:
                raise ApplicationError("CASE_EVENT_CHAIN_INVALID")
            if event.get("schema_version") == (
                "finresearchops.case-event.analysis/v1"
            ):
                if status not in {CaseStatus.CREATED, CaseStatus.RETURNED}:
                    raise ApplicationError("CASE_TRANSITION_INVALID")
                if set(event) != {
                    "schema_version",
                    "sequence",
                    "case_ref",
                    "previous_event_sha256",
                    "run_ref",
                    "workpaper_ref",
                }:
                    raise ApplicationError("CASE_EVENT_SHAPE_INVALID")
                run_ref = self._run_ref_from_payload(event["run_ref"])
                workpaper = self._read_workpaper(
                    case_ref,
                    run_ref,
                    event["workpaper_ref"],
                )
                self._assert_workpaper_matches_case_task(
                    case_ref,
                    run_ref,
                    len(run_refs) + 1,
                )
                run_refs.append(run_ref)
                workpapers.append(workpaper)
                if workpaper.document_sha256 != source["document_sha256"]:
                    raise ApplicationError("WORKPAPER_DOCUMENT_MISMATCH")
                status = CaseStatus.AWAITING_REVIEW
            elif event.get("schema_version") == (
                "finresearchops.case-event.review/v1"
            ):
                if status is not CaseStatus.AWAITING_REVIEW:
                    raise ApplicationError("CASE_TRANSITION_INVALID")
                if set(event) != {
                    "schema_version",
                    "sequence",
                    "case_ref",
                    "previous_event_sha256",
                    "review_ref",
                }:
                    raise ApplicationError("CASE_EVENT_SHAPE_INVALID")
                if not run_refs or not workpapers:
                    raise ApplicationError("REVIEW_WITHOUT_ANALYSIS")
                review = self._read_review(
                    case_ref,
                    run_refs[-1],
                    workpapers[-1],
                    event["review_ref"],
                )
                reviews.append(review)
                status = review.resulting_case_status
            else:
                raise ApplicationError("CASE_EVENT_SCHEMA_UNSUPPORTED")
            previous_event_sha256 = sha256_hex(canonical_json_bytes(event))

        expected_head = self._case_head_payload(
            case_ref,
            committed_event_count,
            previous_event_sha256,
        )
        if journal_head != expected_head:
            raise ApplicationError("CASE_JOURNAL_HEAD_MISMATCH")

        pending_workpaper_refs: set[str] = set()
        pending_review_refs: set[str] = set()
        if inspection.pending_material_published:
            assert inspection.pending is not None
            if inspection.pending["material_kind"] == "workpaper":
                pending_workpaper_refs.add(inspection.pending["material_ref"])
            else:
                pending_review_refs.add(inspection.pending["material_ref"])
        self._assert_case_artifact_membership(
            case_ref,
            {
                workpaper.workpaper_ref for workpaper in workpapers
            }
            | pending_workpaper_refs,
            {review.review_ref for review in reviews} | pending_review_refs,
        )

        packets = self._read_packets(
            case_ref,
            case_payload["question"],
            status,
            tuple(run_refs),
            tuple(workpapers),
            tuple(reviews),
        )
        replays = self._read_replays(case_ref, tuple(run_refs))

        view = CaseView(
            case_ref=case_ref,
            question=case_payload["question"],
            cutoff=date.fromisoformat(case_payload["cutoff"]),
            source_ref=SourceRefView(
                source_id=source["source_id"],
                document_name=source["document_name"],
                document_sha256=source["document_sha256"],
                declared_published_at=date.fromisoformat(
                    source["declared_published_at"]
                ),
            ),
            status=status,
            run_refs=tuple(run_refs),
            workpapers=tuple(workpapers),
            reviews=tuple(reviews),
            packets=packets,
            replays=replays,
            latest_machine_decision=(
                workpapers[-1].machine_decision if workpapers else None
            ),
            latest_gate_reason_codes=(
                workpapers[-1].gate_reason_codes if workpapers else ()
            ),
        )
        if validate_pending and inspection.pending is not None:
            self._validate_pending_case_transaction(
                inspection.pending,
                committed_view=view,
            )
        return view

    def _read_case_payload(self, case_ref: str) -> dict[str, object]:
        try:
            _validate_case_ref(case_ref)
        except (TypeError, ValueError) as exc:
            raise ApplicationError("CASE_REF_INVALID") from exc
        path = self._case_directory(case_ref) / "case.json"
        if not path.is_file():
            raise ApplicationError("CASE_NOT_FOUND")
        payload = self._read_canonical_object(path)
        expected_fields = {
            "schema_version",
            "case_ref",
            "question",
            "cutoff",
            "source_ref",
            "answer_contract",
            "risk_class",
            "mode",
        }
        if set(payload) != expected_fields:
            raise ApplicationError("CASE_RECORD_SHAPE_INVALID")
        if (
            payload["schema_version"] != "finresearchops.case-record/v1"
            or payload["case_ref"] != case_ref
            or not isinstance(payload["source_ref"], dict)
            or set(payload["source_ref"])
            != {
                "source_id",
                "document_name",
                "document_sha256",
                "declared_published_at",
            }
        ):
            raise ApplicationError("CASE_RECORD_INVALID")
        source = payload["source_ref"]
        try:
            if (
                type(payload["question"]) is not str
                or not payload["question"].strip()
                or type(payload["cutoff"]) is not str
                or type(source["source_id"]) is not str
                or not source["source_id"].strip()
                or type(source["document_name"]) is not str
                or not source["document_name"].strip()
                or type(source["document_sha256"]) is not str
                or len(source["document_sha256"]) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in source["document_sha256"]
                )
                or type(source["declared_published_at"]) is not str
            ):
                raise ValueError
            date.fromisoformat(payload["cutoff"])
            date.fromisoformat(source["declared_published_at"])
            if payload["answer_contract"] not in ANSWER_CONTRACT_VALUES:
                raise ValueError
            if payload["risk_class"] not in {"LOW", "MEDIUM", "MATERIAL"}:
                raise ValueError
            if payload["mode"] not in {
                "SYNTHETIC_DEV",
                "PRIVATE_DEV",
                "POST_FREEZE_EVAL",
            }:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApplicationError("CASE_RECORD_VALUE_INVALID") from exc
        identity = {
            "schema_version": "finresearchops.case-identity/v1",
            "question": payload["question"],
            "cutoff": payload["cutoff"],
            "source_ref": payload["source_ref"],
            "answer_contract": payload["answer_contract"],
            "risk_class": payload["risk_class"],
            "mode": payload["mode"],
        }
        expected_ref = f"case-{sha256_hex(canonical_json_bytes(identity))}"
        if case_ref != expected_ref:
            raise ApplicationError("CASE_IDENTITY_MISMATCH")
        if payload["mode"] in REVIEWED_PROFILE_MODES:
            self._require_private_artifact_root()
        return payload

    def _require_private_artifact_root(self) -> None:
        try:
            anchor = self._private_workspace_anchor
            if anchor is None:
                anchor = resolve_private_workspace_anchor(
                    self._root,
                    purpose="artifact-root",
                )
                self._private_workspace_anchor = anchor
            resolved = require_private_storage_root(
                self._root,
                purpose="artifact-root",
                anchor=anchor,
            )
        except PrivateStorageError as exc:
            raise ApplicationError("PRIVATE_STORAGE_REQUIRED") from exc
        if resolved != self._root:
            self._root = resolved
            self._application_root = self._root / "application"
            self._core_root = self._root / "core"

    def _read_workpaper(
        self,
        case_ref: str,
        run_ref: RunRef,
        workpaper_ref: object,
    ) -> WorkpaperView:
        if type(workpaper_ref) is not str:
            raise ApplicationError("WORKPAPER_REF_INVALID")
        payload = self._read_canonical_object(
            self._case_directory(case_ref)
            / "workpapers"
            / f"{workpaper_ref}.json"
        )
        return self._workpaper_from_payload(
            case_ref,
            run_ref,
            workpaper_ref,
            payload,
        )

    def _workpaper_from_payload(
        self,
        case_ref: str,
        run_ref: RunRef,
        workpaper_ref: object,
        payload: dict[str, object],
    ) -> WorkpaperView:
        expected_workpaper_fields = {
            "schema_version",
            "case_ref",
            "run_ref",
            "machine_decision",
            "answer",
            "answer_unit",
            "document_sha256",
            "evidence_ledger_ref",
            "formula_ref",
            "candidate_artifact_ref",
            "attempts_artifact_ref",
            "trace_summary_ref",
            "trace_summary_status",
            "gate_reason_codes",
            "workpaper_ref",
        }
        if (
            payload.get("schema_version") != WORKPAPER_SCHEMA_VERSION
            or set(payload) != expected_workpaper_fields
        ):
            raise ApplicationError("WORKPAPER_SHAPE_INVALID")
        body = {key: value for key, value in payload.items() if key != "workpaper_ref"}
        expected_ref = "workpaper-" + sha256_hex(canonical_json_bytes(body))
        if (
            payload["case_ref"] != case_ref
            or payload["run_ref"] != {"run_id": run_ref.run_id}
            or workpaper_ref != expected_ref
            or payload["workpaper_ref"] != workpaper_ref
            or payload["evidence_ledger_ref"]
            != f"runs/{run_ref.run_id}/ledger.json"
            or payload["formula_ref"]
            != f"runs/{run_ref.run_id}/formula.json"
            or payload["candidate_artifact_ref"]
            != f"runs/{run_ref.run_id}/candidate.json"
            or payload["attempts_artifact_ref"]
            != f"runs/{run_ref.run_id}/attempts.json"
        ):
            raise ApplicationError("WORKPAPER_IDENTITY_MISMATCH")
        replay = self._offline_gate.replay(run_ref)
        (
            expected_attempts_ref,
            expected_trace_ref,
            expected_trace_status,
        ) = self._verified_run_trace_summary(run_ref)
        if (
            payload["attempts_artifact_ref"] != expected_attempts_ref
            or payload["trace_summary_ref"] != expected_trace_ref
            or payload["trace_summary_status"] != expected_trace_status
        ):
            raise ApplicationError("WORKPAPER_TRACE_SUMMARY_INVALID")
        try:
            decision = Decision(payload["machine_decision"])
        except (TypeError, ValueError) as exc:
            raise ApplicationError("WORKPAPER_DECISION_INVALID") from exc
        if (
            not replay.consistent
            or replay.decision is not decision
            or replay.answer != payload["answer"]
            or replay.answer_unit != payload["answer_unit"]
        ):
            raise ApplicationError("WORKPAPER_CORE_MISMATCH")
        reason_codes = payload["gate_reason_codes"]
        if (
            not isinstance(reason_codes, list)
            or any(type(code) is not str or not code for code in reason_codes)
            or len(reason_codes) != len(set(reason_codes))
        ):
            raise ApplicationError("WORKPAPER_REASON_CODES_INVALID")
        outcome_payload = self._read_core_object(
            run_ref,
            f"runs/{run_ref.run_id}/outcome.json",
            "outcome.json",
        )
        expected_outcome_fields = {
            "schema_version",
            "task_id",
            "decision",
            "answer",
            "answer_unit",
            "reason_codes",
            "run_ref",
            "document_sha256",
        }
        if (
            set(outcome_payload) != expected_outcome_fields
            or outcome_payload["schema_version"] != RUN_SCHEMA_VERSION
            or type(outcome_payload["task_id"]) is not str
            or not outcome_payload["task_id"]
            or outcome_payload["decision"] != decision.value
            or outcome_payload["answer"] != payload["answer"]
            or outcome_payload["answer_unit"] != payload["answer_unit"]
            or outcome_payload["reason_codes"] != reason_codes
            or outcome_payload["run_ref"] != {"run_id": run_ref.run_id}
            or outcome_payload["document_sha256"]
            != payload["document_sha256"]
        ):
            raise ApplicationError("WORKPAPER_CORE_MISMATCH")
        return WorkpaperView(
            workpaper_ref=workpaper_ref,
            run_ref=run_ref,
            machine_decision=decision,
            answer=payload["answer"],
            answer_unit=payload["answer_unit"],
            document_sha256=payload["document_sha256"],
            evidence_ledger_ref=payload["evidence_ledger_ref"],
            formula_ref=payload["formula_ref"],
            candidate_artifact_ref=payload["candidate_artifact_ref"],
            attempts_artifact_ref=payload["attempts_artifact_ref"],
            trace_summary_ref=payload["trace_summary_ref"],
            trace_summary_status=payload["trace_summary_status"],
            gate_reason_codes=tuple(reason_codes),
        )

    def _verified_run_trace_summary(
        self,
        run_ref: RunRef,
    ) -> tuple[str, str, str]:
        """Return (attempts ref, trace-summary ref, status) from the manifest."""

        run_directory = self._core_root / "runs" / run_ref.run_id
        manifest = self._read_canonical_object(run_directory / "manifest.json")
        if (
            set(manifest)
            != {
                "schema_version",
                "run_id",
                "document_sha256",
                "calculation_policy_sha256",
                "artifacts",
            }
            or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION
            or manifest["run_id"] != run_ref.run_id
        ):
            raise ApplicationError("CORE_MANIFEST_INVALID")
        artifacts = manifest["artifacts"]
        if type(artifacts) is not dict:
            raise ApplicationError("CORE_MANIFEST_INVALID")
        attempts_ref = self._verified_manifest_artifact_ref(
            run_ref,
            artifacts,
            "attempts",
            "attempts.json",
        )
        if "model_trace" in artifacts:
            trace_ref = self._verified_manifest_artifact_ref(
                run_ref,
                artifacts,
                "model_trace",
                "model-trace.json",
            )
            return attempts_ref, trace_ref, "MODEL_TRACE_BOUND"
        model_trace_path = run_directory / "model-trace.json"
        if model_trace_path.is_symlink() or model_trace_path.exists():
            raise ApplicationError("CORE_ARTIFACT_SET_MISMATCH")
        return attempts_ref, attempts_ref, "ATTEMPTS_RECORDED"

    def _verified_manifest_artifact_ref(
        self,
        run_ref: RunRef,
        artifacts: dict[str, object],
        artifact_name: str,
        filename: str,
    ) -> str:
        entry = artifacts.get(artifact_name)
        if (
            type(entry) is not dict
            or set(entry) != {"filename", "sha256"}
            or entry["filename"] != filename
            or type(entry["sha256"]) is not str
        ):
            raise ApplicationError("CORE_ARTIFACT_SET_MISMATCH")
        artifact_path = self._core_root / "runs" / run_ref.run_id / filename
        if artifact_path.is_symlink():
            raise ApplicationError("CORE_ARTIFACT_PATH_INVALID")
        try:
            artifact_bytes = artifact_path.read_bytes()
        except OSError as exc:
            raise ApplicationError("CORE_ARTIFACT_READ_FAILED") from exc
        if sha256_hex(artifact_bytes) != entry["sha256"]:
            raise ApplicationError("CORE_ARTIFACT_HASH_MISMATCH")
        return f"runs/{run_ref.run_id}/{filename}"

    def _read_review(
        self,
        case_ref: str,
        run_ref: RunRef,
        workpaper: WorkpaperView,
        review_ref: object,
    ) -> ReviewRecordView:
        if type(review_ref) is not str:
            raise ApplicationError("REVIEW_REF_INVALID")
        payload = self._read_canonical_object(
            self._case_directory(case_ref)
            / "reviews"
            / f"{review_ref}.json"
        )
        return self._review_from_payload(
            case_ref,
            run_ref,
            workpaper,
            review_ref,
            payload,
        )

    def _review_from_payload(
        self,
        case_ref: str,
        run_ref: RunRef,
        workpaper: WorkpaperView,
        review_ref: object,
        payload: dict[str, object],
    ) -> ReviewRecordView:
        if set(payload) != {
            "schema_version",
            "case_ref",
            "run_ref",
            "workpaper_ref",
            "action",
            "reason",
            "recorded_at",
            "previous_case_status",
            "resulting_case_status",
            "review_ref",
        }:
            raise ApplicationError("REVIEW_RECORD_SHAPE_INVALID")
        body = {key: value for key, value in payload.items() if key != "review_ref"}
        expected_ref = "review-" + sha256_hex(canonical_json_bytes(body))
        try:
            action = ReviewAction(payload["action"])
            previous = CaseStatus(payload["previous_case_status"])
            resulting = CaseStatus(payload["resulting_case_status"])
            recorded_at = datetime.fromisoformat(
                payload["recorded_at"].replace("Z", "+00:00")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ApplicationError("REVIEW_RECORD_VALUE_INVALID") from exc
        expected_result = {
            ReviewAction.APPROVE: CaseStatus.APPROVED,
            ReviewAction.RETURN: CaseStatus.RETURNED,
            ReviewAction.REJECT: CaseStatus.REJECTED,
        }[action]
        if (
            payload["schema_version"] != "finresearchops.review-record/v1"
            or payload["case_ref"] != case_ref
            or payload["run_ref"] != {"run_id": run_ref.run_id}
            or payload["workpaper_ref"] != workpaper.workpaper_ref
            or payload["review_ref"] != review_ref
            or review_ref != expected_ref
            or previous is not CaseStatus.AWAITING_REVIEW
            or resulting is not expected_result
            or recorded_at.tzinfo is None
            or type(payload["reason"]) is not str
            or not payload["reason"].strip()
        ):
            raise ApplicationError("REVIEW_RECORD_IDENTITY_INVALID")
        if (
            action is ReviewAction.APPROVE
            and workpaper.machine_decision is not Decision.ACCEPT
        ):
            raise ApplicationError("REVIEW_BYPASSES_MACHINE_GATE")
        return ReviewRecordView(
            review_ref=review_ref,
            run_ref=run_ref,
            workpaper_ref=workpaper.workpaper_ref,
            action=action,
            reason=payload["reason"],
            recorded_at=recorded_at,
            previous_case_status=previous,
            resulting_case_status=resulting,
        )

    def _read_packets(
        self,
        case_ref: str,
        question: str,
        status: CaseStatus,
        run_refs: tuple[RunRef, ...],
        workpapers: tuple[WorkpaperView, ...],
        reviews: tuple[ReviewRecordView, ...],
    ) -> tuple[ChangePacketView, ...]:
        packet_directory = self._case_directory(case_ref) / "packets"
        packets: list[ChangePacketView] = []
        for path in sorted(packet_directory.glob("*.json")):
            payload = self._read_canonical_object(path)
            if set(payload) != {
                "schema_version",
                "case_ref",
                "run_ref",
                "review_ref",
                "workpaper_ref",
                "proposal_only",
                "verified_facts",
                "calculations",
                "impact_statement",
                "unresolved_items",
                "packet_ref",
            }:
                raise ApplicationError("PACKET_SHAPE_INVALID")
            body = {
                key: value for key, value in payload.items() if key != "packet_ref"
            }
            expected_ref = "packet-" + sha256_hex(canonical_json_bytes(body))
            packet_ref = payload["packet_ref"]
            if (
                type(packet_ref) is not str
                or path.name != f"{packet_ref}.json"
                or packet_ref != expected_ref
                or payload["schema_version"]
                != "finresearchops.research-change-packet/v1"
                or payload["case_ref"] != case_ref
                or payload["proposal_only"] is not True
                or status is not CaseStatus.APPROVED
                or not run_refs
                or not workpapers
                or not reviews
                or payload["run_ref"] != {"run_id": run_refs[-1].run_id}
                or payload["review_ref"] != reviews[-1].review_ref
                or payload["workpaper_ref"] != workpapers[-1].workpaper_ref
                or reviews[-1].action is not ReviewAction.APPROVE
                or workpapers[-1].machine_decision is not Decision.ACCEPT
            ):
                raise ApplicationError("PACKET_IDENTITY_OR_ELIGIBILITY_INVALID")
            facts_payload = payload["verified_facts"]
            calculations_payload = payload["calculations"]
            if not isinstance(facts_payload, list) or not isinstance(
                calculations_payload, list
            ):
                raise ApplicationError("PACKET_CONTENT_INVALID")
            facts = tuple(self._fact_from_payload(item) for item in facts_payload)
            calculations = tuple(
                self._calculation_from_payload(item)
                for item in calculations_payload
            )
            if (
                type(payload["impact_statement"]) is not str
                or not payload["impact_statement"]
                or not isinstance(payload["unresolved_items"], list)
                or any(
                    type(item) is not str or not item
                    for item in payload["unresolved_items"]
                )
            ):
                raise ApplicationError("PACKET_CONTENT_INVALID")
            replay = self._offline_gate.replay(run_refs[-1])
            if not replay.consistent or replay.decision is not Decision.ACCEPT:
                raise ApplicationError("PACKET_CORE_REPLAY_INVALID")
            ledger = self._read_core_object(
                run_refs[-1],
                workpapers[-1].evidence_ledger_ref,
                "ledger.json",
            )
            formula = self._read_core_object(
                run_refs[-1],
                workpapers[-1].formula_ref,
                "formula.json",
            )
            expected_facts = self._facts_from_ledger(ledger)
            expected_calculation = self._calculation_from_formula(formula)
            expected_impact = (
                f"Proposal only: {question} Answer: {replay.answer} "
                f"{replay.answer_unit}."
            )
            if (
                facts != expected_facts
                or calculations != (expected_calculation,)
                or payload["impact_statement"] != expected_impact
                or payload["unresolved_items"] != []
            ):
                raise ApplicationError("PACKET_DERIVATION_MISMATCH")
            packets.append(
                ChangePacketView(
                    packet_ref=packet_ref,
                    run_ref=run_refs[-1],
                    review_ref=reviews[-1].review_ref,
                    workpaper_ref=workpapers[-1].workpaper_ref,
                    proposal_only=True,
                    verified_facts=facts,
                    calculations=calculations,
                    impact_statement=payload["impact_statement"],
                    unresolved_items=tuple(payload["unresolved_items"]),
                )
            )
        if len(packets) > 1:
            raise ApplicationError("MULTIPLE_PACKETS_FOR_CASE")
        return tuple(packets)

    def _read_replays(
        self,
        case_ref: str,
        run_refs: tuple[RunRef, ...],
    ) -> tuple[ReplayRecordView, ...]:
        replay_directory = self._case_directory(case_ref) / "replays"
        records: list[ReplayRecordView] = []
        for path in sorted(replay_directory.glob("*.json")):
            payload = self._read_canonical_object(path)
            if set(payload) != {
                "schema_version",
                "case_ref",
                "run_ref",
                "report",
                "replay_ref",
            }:
                raise ApplicationError("REPLAY_RECORD_SHAPE_INVALID")
            body = {
                key: value for key, value in payload.items() if key != "replay_ref"
            }
            expected_ref = "replay-" + sha256_hex(canonical_json_bytes(body))
            replay_ref = payload["replay_ref"]
            run_ref = self._run_ref_from_payload(payload["run_ref"])
            if (
                type(replay_ref) is not str
                or path.name != f"{replay_ref}.json"
                or replay_ref != expected_ref
                or payload["schema_version"] != REPLAY_RECORD_SCHEMA_VERSION
                or payload["case_ref"] != case_ref
                or run_ref not in run_refs
            ):
                raise ApplicationError("REPLAY_RECORD_IDENTITY_INVALID")
            report = self._replay_report_from_payload(payload["report"])
            live_report = self._offline_gate.replay(run_ref)
            self._verified_run_trace_summary(run_ref)
            if report != live_report or not report.consistent:
                raise ApplicationError("REPLAY_RECORD_CORE_MISMATCH")
            records.append(ReplayRecordView(replay_ref=replay_ref, report=report))
        return tuple(records)

    def _read_blob(self, document_sha256: object) -> bytes:
        if type(document_sha256) is not str:
            raise ApplicationError("DOCUMENT_IDENTITY_INVALID")
        try:
            document = (
                self._application_root
                / "blobs"
                / "sha256"
                / document_sha256
            ).read_bytes()
        except FileNotFoundError as exc:
            raise ApplicationError("DOCUMENT_BLOB_MISSING") from exc
        if sha256_hex(document) != document_sha256:
            raise ApplicationError("DOCUMENT_HASH_MISMATCH")
        return document

    def _read_core_object(
        self,
        run_ref: RunRef,
        artifact_ref: str,
        expected_filename: str,
    ) -> dict[str, object]:
        expected_ref = f"runs/{run_ref.run_id}/{expected_filename}"
        if artifact_ref != expected_ref:
            raise ApplicationError("CORE_ARTIFACT_REF_INVALID")
        return self._read_canonical_object(self._core_root / artifact_ref)

    @staticmethod
    def _facts_from_ledger(
        ledger: dict[str, object],
    ) -> tuple[VerifiedFact, ...]:
        nodes = ledger.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ApplicationError("VERIFIED_LEDGER_REQUIRED_FOR_EXPORT")
        facts: list[VerifiedFact] = []
        for node in nodes:
            if not isinstance(node, dict):
                raise ApplicationError("LEDGER_NODE_INVALID")
            locator = node.get("locator")
            if (
                node.get("verification") not in _VERIFIED_LEDGER_LABELS
                or not isinstance(locator, dict)
                or type(locator.get("span_sha256")) is not str
            ):
                raise ApplicationError("LEDGER_NODE_NOT_VERIFIED")
            if ledger.get("schema_version") == "finauditgate.ledger/v2":
                semantics = node.get("normalized_semantics")
                if not isinstance(semantics, dict):
                    raise ApplicationError("LEDGER_NODE_INVALID")
                try:
                    fact = VerifiedFact(
                        evidence_id=node["evidence_id"],
                        metric=semantics["metric"],
                        metric_basis=semantics["metric_basis"],
                        period=semantics["fiscal_period"],
                        value=node["value"],
                        currency=semantics["currency"],
                        unit=semantics["unit"],
                        scale=semantics["scale"],
                        sign=semantics["sign"],
                        span_sha256=locator["span_sha256"],
                    )
                except (KeyError, TypeError) as exc:
                    raise ApplicationError("LEDGER_NODE_INVALID") from exc
            else:
                raise ApplicationError("LEDGER_SCHEMA_UNSUPPORTED")
            facts.append(fact)
        return tuple(facts)

    @staticmethod
    def _calculation_from_formula(
        formula: dict[str, object],
    ) -> CalculationRecord:
        if formula.get("schema_version") != "finauditgate.formula/v2":
            raise ApplicationError("FORMULA_SCHEMA_UNSUPPORTED")
        operand_ids = formula.get("operand_ids")
        if (
            not isinstance(operand_ids, list)
            or not operand_ids
            or any(type(item) is not str or not item for item in operand_ids)
        ):
            raise ApplicationError("FORMULA_LINEAGE_INVALID")
        try:
            return CalculationRecord(
                operation=formula["operation"],
                operand_ids=tuple(operand_ids),
                result=formula["result"],
                output_unit=formula["output_unit"],
                calculation_policy_sha256=formula[
                    "calculation_policy_sha256"
                ],
            )
        except (KeyError, TypeError) as exc:
            raise ApplicationError("FORMULA_CONTENT_INVALID") from exc

    @staticmethod
    def _fact_payload(fact: VerifiedFact) -> dict[str, object]:
        return {
            "evidence_id": fact.evidence_id,
            "metric": fact.metric,
            "metric_basis": fact.metric_basis,
            "period": fact.period,
            "value": fact.value,
            "currency": fact.currency,
            "unit": fact.unit,
            "scale": fact.scale,
            "sign": fact.sign,
            "span_sha256": fact.span_sha256,
        }

    @staticmethod
    def _fact_from_payload(payload: object) -> VerifiedFact:
        if not isinstance(payload, dict) or set(payload) != {
            "evidence_id",
            "metric",
            "metric_basis",
            "period",
            "value",
            "currency",
            "unit",
            "scale",
            "sign",
            "span_sha256",
        }:
            raise ApplicationError("PACKET_FACT_INVALID")
        try:
            return VerifiedFact(**payload)
        except (TypeError, ValueError) as exc:
            raise ApplicationError("PACKET_FACT_INVALID") from exc

    @staticmethod
    def _calculation_payload(
        calculation: CalculationRecord,
    ) -> dict[str, object]:
        return {
            "operation": calculation.operation,
            "operand_ids": list(calculation.operand_ids),
            "result": calculation.result,
            "output_unit": calculation.output_unit,
            "calculation_policy_sha256": (
                calculation.calculation_policy_sha256
            ),
        }

    @staticmethod
    def _calculation_from_payload(payload: object) -> CalculationRecord:
        if not isinstance(payload, dict) or set(payload) != {
            "operation",
            "operand_ids",
            "result",
            "output_unit",
            "calculation_policy_sha256",
        }:
            raise ApplicationError("PACKET_CALCULATION_INVALID")
        operand_ids = payload["operand_ids"]
        if not isinstance(operand_ids, list):
            raise ApplicationError("PACKET_CALCULATION_INVALID")
        try:
            return CalculationRecord(
                operation=payload["operation"],
                operand_ids=tuple(operand_ids),
                result=payload["result"],
                output_unit=payload["output_unit"],
                calculation_policy_sha256=payload[
                    "calculation_policy_sha256"
                ],
            )
        except (TypeError, ValueError) as exc:
            raise ApplicationError("PACKET_CALCULATION_INVALID") from exc

    @staticmethod
    def _replay_report_payload(report: ReplayReport) -> dict[str, object]:
        return {
            "schema_version": report.schema_version,
            "run_ref": {"run_id": report.run_ref.run_id},
            "consistent": report.consistent,
            "decision": report.decision.value if report.decision else None,
            "answer": report.answer,
            "answer_unit": report.answer_unit,
            "verified_artifact_count": report.verified_artifact_count,
            "reason": report.reason,
        }

    @staticmethod
    def _replay_report_from_payload(payload: object) -> ReplayReport:
        if not isinstance(payload, dict):
            raise ApplicationError("REPLAY_REPORT_INVALID")
        expected_fields = {
            "schema_version",
            "run_ref",
            "consistent",
            "decision",
            "answer",
            "answer_unit",
            "verified_artifact_count",
            "reason",
        }
        if set(payload) != expected_fields:
            raise ApplicationError("REPLAY_REPORT_INVALID")
        run_ref = FinResearchOps._run_ref_from_payload(payload["run_ref"])
        try:
            decision = (
                Decision(payload["decision"])
                if payload["decision"] is not None
                else None
            )
            return ReplayReport(
                schema_version=payload["schema_version"],
                run_ref=run_ref,
                consistent=payload["consistent"],
                decision=decision,
                answer=payload["answer"],
                answer_unit=payload["answer_unit"],
                verified_artifact_count=payload["verified_artifact_count"],
                reason=payload["reason"],
            )
        except (TypeError, ValueError) as exc:
            raise ApplicationError("REPLAY_REPORT_INVALID") from exc

    def _find_case_for_run(self, run_ref: RunRef) -> str:
        case_root = self._application_root / "cases"
        owners: list[str] = []
        for path in sorted(case_root.glob("case-*/case.json")):
            case_ref = path.parent.name
            with self._case_lock(case_ref):
                view = self._load_case(case_ref)
            if run_ref in view.run_refs:
                owners.append(case_ref)
        if not owners:
            raise ApplicationError("RUN_NOT_OWNED_BY_A_CASE")
        if len(owners) != 1:
            raise ApplicationError("RUN_CASE_OWNERSHIP_AMBIGUOUS")
        return owners[0]

    def _recorded_at(self) -> str:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is None:
            raise ApplicationError("CLOCK_MUST_RETURN_AWARE_DATETIME")
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    def _document_from_case(
        self,
        case_payload: dict[str, object],
        document: bytes,
    ) -> FrozenDocumentPackage:
        source = case_payload["source_ref"]
        return FrozenDocumentPackage(
            source_id=source["source_id"],
            document_name=source["document_name"],
            document_bytes=document,
            declared_published_at=date.fromisoformat(
                source["declared_published_at"]
            ),
        )

    def _commit_case_transition(
        self,
        case_ref: str,
        *,
        command_identity: dict[str, object],
        material_kind: str,
        material_ref: str,
        material_payload: dict[str, object],
        event_body: dict[str, object],
    ) -> dict[str, object]:
        if material_kind not in {"workpaper", "review"}:
            raise ApplicationError("CASE_TRANSACTION_KIND_INVALID")
        inspection = self._recover_case_transitions(case_ref)
        if inspection.unsealed_committed is not None:
            raise ApplicationError("CASE_TRANSACTION_RECEIPT_MISSING")
        if inspection.pending is not None:
            intent = inspection.pending
            if intent["command_identity"] != command_identity:
                raise ApplicationError("CASE_TRANSACTION_PENDING_CONFLICT")
            self._validate_pending_case_transaction(intent)
            self._apply_case_transaction(intent)
            return intent
        current_head = self._read_case_head(case_ref)
        sequence = current_head["event_count"] + 1
        previous_event_sha256 = current_head["tail_event_sha256"]
        event = {
            **event_body,
            "sequence": sequence,
            "case_ref": case_ref,
            "previous_event_sha256": previous_event_sha256,
        }
        event_bytes = canonical_json_bytes(event)
        next_head = self._case_head_payload(
            case_ref,
            sequence,
            sha256_hex(event_bytes),
        )
        intent_body = {
            "schema_version": "finresearchops.case-transaction/v1",
            "case_ref": case_ref,
            "sequence": sequence,
            "previous_head": current_head,
            "next_head": next_head,
            "command_identity": command_identity,
            "material_kind": material_kind,
            "material_ref": material_ref,
            "material_payload": material_payload,
            "event": event,
        }
        transaction_ref = "transaction-" + sha256_hex(
            canonical_json_bytes(intent_body)
        )
        intent = {**intent_body, "transaction_ref": transaction_ref}
        transaction_directory = (
            self._case_directory(case_ref) / "transactions"
        )
        write_once(
            transaction_directory / f"{transaction_ref}.intent.json",
            canonical_json_bytes(intent),
        )
        self._validate_pending_case_transaction(intent)
        self._apply_case_transaction(intent)
        return intent

    def _recover_case_transitions(
        self,
        case_ref: str,
    ) -> _CaseTransactionInspection:
        transaction_directory = (
            self._case_directory(case_ref) / "transactions"
        )
        transaction_paths = sorted(transaction_directory.glob("*.json"))
        intent_paths = tuple(
            path
            for path in transaction_paths
            if path.name.endswith(".intent.json")
        )
        commit_paths = tuple(
            path
            for path in transaction_paths
            if path.name.endswith(".commit.json")
        )
        if len(intent_paths) + len(commit_paths) != len(transaction_paths):
            raise ApplicationError("CASE_TRANSACTION_FILENAME_INVALID")
        intents: dict[str, dict[str, object]] = {}
        for path in intent_paths:
            intent = self._read_case_transaction(path, case_ref)
            transaction_ref = intent["transaction_ref"]
            if transaction_ref in intents:
                raise ApplicationError("CASE_TRANSACTION_IDENTITY_INVALID")
            intents[transaction_ref] = intent
        commit_by_ref = {
            path.name.removesuffix(".commit.json"): path
            for path in commit_paths
        }
        if len(commit_by_ref) != len(commit_paths):
            raise ApplicationError("CASE_TRANSACTION_COMMIT_INVALID")
        if set(commit_by_ref) - set(intents):
            raise ApplicationError("CASE_TRANSACTION_COMMIT_INVALID")
        ordered_intents = sorted(
            intents.items(),
            key=lambda item: item[1]["sequence"],
        )
        sequences = [intent["sequence"] for _, intent in ordered_intents]
        if len(sequences) != len(set(sequences)):
            raise ApplicationError("CASE_TRANSACTION_SEQUENCE_INVALID")
        head = self._read_case_head(case_ref)
        if any(
            sequence < 1 or sequence > head["event_count"] + 1
            for sequence in sequences
        ):
            raise ApplicationError("CASE_TRANSACTION_SEQUENCE_INVALID")
        pending: dict[str, object] | None = None
        pending_material_published = False
        pending_event_published = False
        unsealed_committed: dict[str, object] | None = None
        committed_sequences: set[int] = set()
        for transaction_ref, intent in ordered_intents:
            sequence = intent["sequence"]
            commit_path = commit_by_ref.get(transaction_ref)
            if commit_path is not None:
                commit = self._read_canonical_object(commit_path)
                if commit != {
                    "schema_version": (
                        "finresearchops.case-transaction-commit/v1"
                    ),
                    "case_ref": case_ref,
                    "transaction_ref": transaction_ref,
                    "sequence": intent["sequence"],
                    "next_head_sha256": sha256_hex(
                        canonical_json_bytes(intent["next_head"])
                    ),
                }:
                    raise ApplicationError("CASE_TRANSACTION_COMMIT_INVALID")
                if sequence > head["event_count"]:
                    raise ApplicationError("CASE_TRANSACTION_COMMIT_INVALID")
                self._verify_published_case_transaction(intent)
                committed_sequences.add(sequence)
                continue
            if sequence <= head["event_count"]:
                self._verify_published_case_transaction(intent)
                if (
                    sequence != head["event_count"]
                    or unsealed_committed is not None
                ):
                    raise ApplicationError("CASE_TRANSACTION_COMMIT_INVALID")
                unsealed_committed = intent
                committed_sequences.add(sequence)
                continue
            if unsealed_committed is not None:
                raise ApplicationError("CASE_TRANSACTION_COMMIT_INVALID")
            if pending is not None or sequence != head["event_count"] + 1:
                raise ApplicationError("CASE_TRANSACTION_SEQUENCE_INVALID")
            pending = intent
            material_path, event_path = self._transaction_paths(intent)
            pending_material_published = self._verify_optional_payload(
                material_path,
                canonical_json_bytes(intent["material_payload"]),
            )
            pending_event_published = self._verify_optional_payload(
                event_path,
                canonical_json_bytes(intent["event"]),
            )
            if pending_event_published and not pending_material_published:
                raise ApplicationError("CASE_TRANSACTION_PUBLICATION_INVALID")
        if committed_sequences != set(range(1, head["event_count"] + 1)):
            raise ApplicationError("CASE_TRANSACTION_HISTORY_INCOMPLETE")
        return _CaseTransactionInspection(
            pending=pending,
            pending_material_published=pending_material_published,
            pending_event_published=pending_event_published,
            unsealed_committed=unsealed_committed,
        )

    def _seal_committed_case_transaction(self, case_ref: str) -> None:
        inspection = self._recover_case_transitions(case_ref)
        if inspection.unsealed_committed is not None:
            self._write_case_transaction_commit(
                inspection.unsealed_committed
            )

    @staticmethod
    def _verify_optional_payload(path: Path, expected: bytes) -> bool:
        try:
            actual = path.read_bytes()
        except FileNotFoundError:
            return False
        if actual != expected:
            raise ApplicationError(
                "CASE_TRANSACTION_PUBLISHED_CONTENT_INVALID"
            )
        return True

    def _transaction_paths(
        self,
        intent: dict[str, object],
    ) -> tuple[Path, Path]:
        material_directory = {
            "workpaper": "workpapers",
            "review": "reviews",
        }.get(intent["material_kind"])
        if material_directory is None:
            raise ApplicationError("CASE_TRANSACTION_KIND_INVALID")
        case_directory = self._case_directory(intent["case_ref"])
        return (
            case_directory
            / material_directory
            / f"{intent['material_ref']}.json",
            case_directory / "events" / f"{intent['sequence']:08d}.json",
        )

    def _verify_published_case_transaction(
        self,
        intent: dict[str, object],
    ) -> None:
        material_path, event_path = self._transaction_paths(intent)
        if not self._verify_optional_payload(
            material_path,
            canonical_json_bytes(intent["material_payload"]),
        ) or not self._verify_optional_payload(
            event_path,
            canonical_json_bytes(intent["event"]),
        ):
            raise ApplicationError("CASE_TRANSACTION_COMMIT_INVALID")

    def _validate_pending_case_transaction(
        self,
        intent: dict[str, object],
        *,
        committed_view: CaseView | None = None,
    ) -> None:
        case_ref = intent["case_ref"]
        previous_head = intent["previous_head"]
        if self._read_case_head(case_ref) != previous_head:
            raise ApplicationError("CASE_TRANSACTION_HEAD_CONFLICT")
        material = intent["material_payload"]
        event = intent["event"]
        if intent["material_kind"] == "workpaper":
            if committed_view is None:
                committed_view = self._load_case(
                    case_ref,
                    validate_pending=False,
                )
            if committed_view.status not in {
                CaseStatus.CREATED,
                CaseStatus.RETURNED,
            }:
                raise ApplicationError("CASE_TRANSACTION_DOMAIN_INVALID")
            run_ref = self._run_ref_from_payload(event.get("run_ref"))
            workpaper = self._workpaper_from_payload(
                case_ref,
                run_ref,
                intent["material_ref"],
                material,
            )
            self._assert_workpaper_matches_case_task(
                case_ref,
                run_ref,
                len(committed_view.run_refs) + 1,
            )
            if (
                run_ref in committed_view.run_refs
                or workpaper.document_sha256
                != committed_view.source_ref.document_sha256
            ):
                raise ApplicationError("CASE_TRANSACTION_DOMAIN_INVALID")
            if intent["command_identity"] != {
                "schema_version": RunAnalysis.schema_version,
                "case_ref": case_ref,
            }:
                raise ApplicationError("CASE_TRANSACTION_COMMAND_INVALID")
            return
        if committed_view is None:
            committed_view = self._load_case(
                case_ref,
                validate_pending=False,
            )
        if (
            committed_view.status is not CaseStatus.AWAITING_REVIEW
            or not committed_view.run_refs
            or not committed_view.workpapers
        ):
            raise ApplicationError("CASE_TRANSACTION_DOMAIN_INVALID")
        review = self._review_from_payload(
            case_ref,
            committed_view.run_refs[-1],
            committed_view.workpapers[-1],
            intent["material_ref"],
            material,
        )
        if intent["command_identity"] != {
            "schema_version": SubmitReview.schema_version,
            "case_ref": case_ref,
            "run_ref": {"run_id": review.run_ref.run_id},
            "action": review.action.value,
            "reason": review.reason.strip(),
        }:
            raise ApplicationError("CASE_TRANSACTION_COMMAND_INVALID")

    def _assert_workpaper_matches_case_task(
        self,
        case_ref: str,
        run_ref: RunRef,
        analysis_number: int,
    ) -> None:
        case_payload = self._read_case_payload(case_ref)
        task_path = (
            self._core_root / "runs" / run_ref.run_id / "task.json"
        )
        task_payload = self._read_canonical_object(task_path)
        source = case_payload["source_ref"]
        expected_task_id = (
            source["source_id"]
            if analysis_number == 1
            else f"{source['source_id']}:analysis-{analysis_number}"
        )
        expected = {
            "schema_version": "finauditgate.task/v1",
            "task_id": expected_task_id,
            "question": case_payload["question"],
            "cutoff": case_payload["cutoff"],
            "source_id": source["source_id"],
            "document_name": source["document_name"],
            "document_sha256": source["document_sha256"],
            "declared_published_at": source["declared_published_at"],
            "answer_contract": case_payload["answer_contract"],
            "risk_class": case_payload["risk_class"],
            "mode": case_payload["mode"],
        }
        if task_payload != expected:
            raise ApplicationError("WORKPAPER_CASE_TASK_MISMATCH")

    def _apply_case_transaction(
        self,
        intent: dict[str, object],
    ) -> None:
        case_ref = intent["case_ref"]
        material_kind = intent["material_kind"]
        material_ref = intent["material_ref"]
        material_directory = {
            "workpaper": "workpapers",
            "review": "reviews",
        }.get(material_kind)
        if material_directory is None:
            raise ApplicationError("CASE_TRANSACTION_KIND_INVALID")
        material_path = (
            self._case_directory(case_ref)
            / material_directory
            / f"{material_ref}.json"
        )
        sequence = intent["sequence"]
        event_path = (
            self._case_directory(case_ref)
            / "events"
            / f"{sequence:08d}.json"
        )
        material_bytes = canonical_json_bytes(intent["material_payload"])
        event_bytes = canonical_json_bytes(intent["event"])
        current_head = self._read_case_head(case_ref)
        if current_head == intent["previous_head"]:
            write_once(material_path, material_bytes)
            write_once(event_path, event_bytes)
            self._replace_case_head(
                case_ref,
                sequence,
                intent["next_head"]["tail_event_sha256"],
            )
            current_head = self._read_case_head(case_ref)
        elif current_head == intent["next_head"]:
            try:
                if (
                    material_path.read_bytes() != material_bytes
                    or event_path.read_bytes() != event_bytes
                ):
                    raise ApplicationError(
                        "CASE_TRANSACTION_PUBLISHED_CONTENT_INVALID"
                    )
            except FileNotFoundError as exc:
                raise ApplicationError(
                    "CASE_TRANSACTION_PUBLISHED_CONTENT_MISSING"
                ) from exc
        if current_head != intent["next_head"]:
            raise ApplicationError("CASE_TRANSACTION_HEAD_CONFLICT")
        self._write_case_transaction_commit(intent)

    def _write_case_transaction_commit(
        self,
        intent: dict[str, object],
    ) -> None:
        case_ref = intent["case_ref"]
        sequence = intent["sequence"]
        commit = {
            "schema_version": "finresearchops.case-transaction-commit/v1",
            "case_ref": case_ref,
            "transaction_ref": intent["transaction_ref"],
            "sequence": sequence,
            "next_head_sha256": sha256_hex(
                canonical_json_bytes(intent["next_head"])
            ),
        }
        write_once(
            self._case_directory(case_ref)
            / "transactions"
            / f"{intent['transaction_ref']}.commit.json",
            canonical_json_bytes(commit),
        )

    def _read_case_transaction(
        self,
        path: Path,
        case_ref: str,
    ) -> dict[str, object]:
        payload = self._read_canonical_object(path)
        expected_fields = {
            "schema_version",
            "case_ref",
            "sequence",
            "previous_head",
            "next_head",
            "command_identity",
            "material_kind",
            "material_ref",
            "material_payload",
            "event",
            "transaction_ref",
        }
        if set(payload) != expected_fields:
            raise ApplicationError("CASE_TRANSACTION_SHAPE_INVALID")
        body = {
            key: value
            for key, value in payload.items()
            if key != "transaction_ref"
        }
        expected_ref = "transaction-" + sha256_hex(
            canonical_json_bytes(body)
        )
        if (
            payload["schema_version"]
            != "finresearchops.case-transaction/v1"
            or payload["case_ref"] != case_ref
            or payload["transaction_ref"] != expected_ref
            or path.name != f"{expected_ref}.intent.json"
            or payload["material_kind"] not in {"workpaper", "review"}
            or type(payload["material_ref"]) is not str
            or not isinstance(payload["material_payload"], dict)
            or not isinstance(payload["event"], dict)
            or not isinstance(payload["previous_head"], dict)
            or not isinstance(payload["next_head"], dict)
            or not isinstance(payload["command_identity"], dict)
            or type(payload["sequence"]) is not int
            or payload["sequence"] <= 0
            or payload["event"].get("sequence") != payload["sequence"]
            or payload["event"].get("case_ref") != case_ref
            or payload["previous_head"].get("case_ref") != case_ref
            or payload["next_head"].get("case_ref") != case_ref
            or payload["previous_head"].get("event_count")
            != payload["sequence"] - 1
            or payload["next_head"].get("event_count")
            != payload["sequence"]
            or payload["next_head"].get("tail_event_sha256")
            != sha256_hex(canonical_json_bytes(payload["event"]))
        ):
            raise ApplicationError("CASE_TRANSACTION_IDENTITY_INVALID")
        material_payload = payload["material_payload"]
        event = payload["event"]
        material_ref = payload["material_ref"]
        if payload["material_kind"] == "workpaper":
            expected_ref_field = "workpaper_ref"
            expected_event_schema = "finresearchops.case-event.analysis/v1"
            expected_event_fields = {
                "schema_version",
                "sequence",
                "case_ref",
                "previous_event_sha256",
                "run_ref",
                "workpaper_ref",
            }
        else:
            expected_ref_field = "review_ref"
            expected_event_schema = "finresearchops.case-event.review/v1"
            expected_event_fields = {
                "schema_version",
                "sequence",
                "case_ref",
                "previous_event_sha256",
                "review_ref",
            }
        material_body = {
            key: value
            for key, value in material_payload.items()
            if key != expected_ref_field
        }
        expected_material_ref = (
            payload["material_kind"]
            + "-"
            + sha256_hex(canonical_json_bytes(material_body))
        )
        if (
            material_payload.get("case_ref") != case_ref
            or material_payload.get(expected_ref_field) != material_ref
            or material_ref != expected_material_ref
            or event.get("schema_version") != expected_event_schema
            or set(event) != expected_event_fields
            or event.get(expected_ref_field) != material_ref
            or event.get("previous_event_sha256")
            != payload["previous_head"].get("tail_event_sha256")
            or payload["previous_head"].get("schema_version")
            != "finresearchops.case-journal-head/v1"
            or payload["next_head"].get("schema_version")
            != "finresearchops.case-journal-head/v1"
            or set(payload["previous_head"])
            != {
                "schema_version",
                "case_ref",
                "event_count",
                "tail_event_sha256",
            }
            or set(payload["next_head"])
            != {
                "schema_version",
                "case_ref",
                "event_count",
                "tail_event_sha256",
            }
        ):
            raise ApplicationError("CASE_TRANSACTION_IDENTITY_INVALID")
        if payload["material_kind"] == "workpaper":
            expected_command_identity = {
                "schema_version": RunAnalysis.schema_version,
                "case_ref": case_ref,
            }
        else:
            reason = material_payload.get("reason")
            action = material_payload.get("action")
            run_ref_payload = material_payload.get("run_ref")
            if (
                type(reason) is not str
                or action not in {member.value for member in ReviewAction}
                or not isinstance(run_ref_payload, dict)
                or set(run_ref_payload) != {"run_id"}
            ):
                return payload
            expected_command_identity = {
                "schema_version": SubmitReview.schema_version,
                "case_ref": case_ref,
                "run_ref": run_ref_payload,
                "action": action,
                "reason": reason.strip(),
            }
        if payload["command_identity"] != expected_command_identity:
            raise ApplicationError("CASE_TRANSACTION_COMMAND_INVALID")
        return payload

    def _assert_case_artifact_membership(
        self,
        case_ref: str,
        event_workpaper_refs: set[str],
        event_review_refs: set[str],
    ) -> None:
        stored_workpaper_refs = self._owned_artifact_refs(
            self._case_directory(case_ref) / "workpapers",
            case_ref,
            "workpaper_ref",
        )
        stored_review_refs = self._owned_artifact_refs(
            self._case_directory(case_ref) / "reviews",
            case_ref,
            "review_ref",
        )
        if (
            stored_workpaper_refs != event_workpaper_refs
            or stored_review_refs != event_review_refs
        ):
            raise ApplicationError("CASE_ARTIFACT_MEMBERSHIP_MISMATCH")

    def _owned_artifact_refs(
        self,
        directory: Path,
        case_ref: str,
        ref_field: str,
    ) -> set[str]:
        refs: set[str] = set()
        ref_prefix = ref_field.removesuffix("_ref") + "-"
        for path in sorted(directory.glob("*.json")):
            payload = self._read_canonical_object(path)
            artifact_ref = payload.get(ref_field)
            owner_case_ref = payload.get("case_ref")
            body = {
                key: value for key, value in payload.items() if key != ref_field
            }
            expected_ref = ref_prefix + sha256_hex(canonical_json_bytes(body))
            try:
                _validate_case_ref(owner_case_ref)
            except (TypeError, ValueError) as exc:
                raise ApplicationError(
                    "CASE_ARTIFACT_MEMBERSHIP_INVALID"
                ) from exc
            if (
                type(artifact_ref) is not str
                or path.name != f"{artifact_ref}.json"
                or artifact_ref != expected_ref
                or not (
                    self._case_directory(owner_case_ref) / "case.json"
                ).is_file()
            ):
                raise ApplicationError("CASE_ARTIFACT_MEMBERSHIP_INVALID")
            if owner_case_ref != case_ref:
                raise ApplicationError(
                    "CASE_ARTIFACT_MEMBERSHIP_INVALID"
                )
            if artifact_ref in refs:
                raise ApplicationError("CASE_ARTIFACT_MEMBERSHIP_INVALID")
            refs.add(artifact_ref)
        return refs

    def _read_case_head(self, case_ref: str) -> dict[str, object]:
        payload = self._read_canonical_object(self._case_head_path(case_ref))
        if set(payload) != {
            "schema_version",
            "case_ref",
            "event_count",
            "tail_event_sha256",
        }:
            raise ApplicationError("CASE_JOURNAL_HEAD_INVALID")
        count = payload["event_count"]
        tail = payload["tail_event_sha256"]
        if (
            payload["schema_version"]
            != "finresearchops.case-journal-head/v1"
            or payload["case_ref"] != case_ref
            or type(count) is not int
            or count < 0
            or (count == 0 and tail is not None)
            or (
                count > 0
                and (
                    type(tail) is not str
                    or len(tail) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in tail
                    )
                )
            )
        ):
            raise ApplicationError("CASE_JOURNAL_HEAD_INVALID")
        return payload

    def _replace_case_head(
        self,
        case_ref: str,
        event_count: int,
        tail_event_sha256: str,
    ) -> None:
        path = self._case_head_path(case_ref)
        payload = canonical_json_bytes(
            self._case_head_payload(
                case_ref,
                event_count,
                tail_event_sha256,
            )
        )
        temporary_path = path.with_name(
            f".{path.name}.{event_count}.{sha256_hex(payload)}.tmp"
        )
        write_once(temporary_path, payload)
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    @staticmethod
    def _case_head_payload(
        case_ref: str,
        event_count: int,
        tail_event_sha256: str | None,
    ) -> dict[str, object]:
        return {
            "schema_version": "finresearchops.case-journal-head/v1",
            "case_ref": case_ref,
            "event_count": event_count,
            "tail_event_sha256": tail_event_sha256,
        }

    def _case_head_path(self, case_ref: str) -> Path:
        return self._case_directory(case_ref) / "journal-head.json"

    @contextmanager
    def _case_lock(
        self,
        case_ref: str,
        *,
        shared: bool = False,
        allow_missing: bool = False,
    ) -> Iterator[None]:
        try:
            _validate_case_ref(case_ref)
        except (TypeError, ValueError) as exc:
            raise ApplicationError("CASE_REF_INVALID") from exc
        if not allow_missing and not (
            self._case_directory(case_ref) / "case.json"
        ).is_file():
            raise ApplicationError("CASE_NOT_FOUND")
        lock_path = self._application_root / "locks" / f"{case_ref}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(lock_file.fileno(), operation)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _case_directory(self, case_ref: str) -> Path:
        return self._application_root / "cases" / case_ref

    @staticmethod
    def _run_ref_from_payload(payload: object) -> RunRef:
        if not isinstance(payload, dict) or set(payload) != {"run_id"}:
            raise ApplicationError("RUN_REF_INVALID")
        try:
            return RunRef(run_id=payload["run_id"])
        except (TypeError, ValueError) as exc:
            raise ApplicationError("RUN_REF_INVALID") from exc

    @staticmethod
    def _read_canonical_object(path: Path) -> dict[str, object]:
        try:
            payload_bytes = path.read_bytes()
        except OSError as exc:
            raise ApplicationError("ARTIFACT_READ_FAILED") from exc
        try:
            payload = json.loads(payload_bytes)
        except (TypeError, ValueError) as exc:
            raise ApplicationError("ARTIFACT_JSON_INVALID") from exc
        if not isinstance(payload, dict):
            raise ApplicationError("ARTIFACT_SHAPE_INVALID")
        if payload_bytes != canonical_json_bytes(payload):
            raise ApplicationError("ARTIFACT_NON_CANONICAL")
        return payload
