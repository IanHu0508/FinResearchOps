from dataclasses import replace
from datetime import date
from decimal import localcontext
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from finauditgate import AuditTask, Decision, FinAuditGate, FrozenDocumentPackage
from finauditgate.adapters.scripted import (
    CalculationCandidate,
    EvidenceCandidate,
    ScriptedCandidate,
    ScriptedModelAdapter,
)


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "synthetic"
    / "aurora_revenue_growth_m2.txt"
)
QUESTION = "What was Aurora Devices FY2025 revenue growth versus FY2024?"


def evidence_for(
    document: bytes,
    evidence_id: str,
    record: bytes,
) -> EvidenceCandidate:
    fields = dict(
        field.split("=", 1)
        for field in record.decode("utf-8").split(";")
    )
    start = document.index(record)
    return EvidenceCandidate(
        evidence_id=evidence_id,
        byte_start=start,
        byte_end=start + len(record),
        metric=fields["metric"],
        period=fields["period"],
        value=fields["value"],
        unit=fields["unit"],
        metric_basis=fields["basis"],
        currency=fields["currency"],
        scale=fields["scale"],
        sign=fields["sign"],
    )


def standard_task(document: bytes, task_id: str) -> AuditTask:
    return AuditTask(
        task_id=task_id,
        question=QUESTION,
        cutoff=date(2026, 3, 1),
        document=FrozenDocumentPackage(
            source_id="synthetic-aurora-revenue-growth-v2",
            document_name="aurora_revenue_growth_m2.txt",
            document_bytes=document,
            declared_published_at=date(2026, 2, 15),
        ),
    )


def standard_candidate(document: bytes) -> ScriptedCandidate:
    prior = (
        b"metric=Net sales;basis=Reported;period=Year ended 2024-12-31;"
        b"value=125.00;currency=US dollar;unit=Monetary;scale=Millions;"
        b"sign=Positive"
    )
    current = (
        b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
        b"value=150.00;currency=USD;unit=Currency amount;scale=Million;"
        b"sign=As presented"
    )
    return ScriptedCandidate(
        evidence=(
            evidence_for(document, "revenue_prior", prior),
            evidence_for(document, "revenue_current", current),
        ),
        calculation=CalculationCandidate(
            operation="growth_rate_percent",
            operand_ids=("revenue_current", "revenue_prior"),
            output_unit="PERCENT",
            quantize="0.01",
        ),
    )


def candidate_with_current_record(
    document: bytes,
    current_record: bytes,
) -> ScriptedCandidate:
    valid = standard_candidate(document)
    return replace(
        valid,
        evidence=(
            valid.evidence[0],
            evidence_for(document, "revenue_current", current_record),
        ),
    )


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _rewrite_artifact(run_directory: Path, name: str, payload: bytes) -> None:
    """Overwrite one artifact and keep the manifest hash in step with it."""

    (run_directory / name).write_bytes(payload)
    manifest_path = run_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"][name.removesuffix(".json")]["sha256"] = hashlib.sha256(
        payload
    ).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))


class CoreGateTest(unittest.TestCase):
    def test_the_growth_arithmetic_is_pinned_field_by_field(self) -> None:
        """Pin the one allowlisted calculation against silent change.

        The formula artifact is the audit record of how an answer was reached,
        so any change to it -- a different rounding, a different lineage, a
        renamed field -- has to be a deliberate edit to this expectation, not a
        side effect of refactoring the arithmetic.
        """

        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "synthetic-aurora-revenue-growth-v2")
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_root = Path(temporary_directory) / "core"
            outcome = FinAuditGate(
                artifact_root=artifact_root,
                model=ScriptedModelAdapter(
                    {task.task_id: standard_candidate(document)}
                ),
            ).run(task)
            formula = json.loads(
                (
                    artifact_root / "runs" / outcome.run_ref.run_id / "formula.json"
                ).read_bytes()
            )

        self.assertIs(Decision.ACCEPT, outcome.decision)
        self.assertEqual("20.00", outcome.answer)
        self.assertEqual("PERCENT", outcome.answer_unit)
        self.assertEqual(
            {
                "schema_version": "finauditgate.formula/v2",
                "operation": "growth_rate_percent",
                "operand_ids": ["revenue_current", "revenue_prior"],
                "operand_lineage": [
                    {
                        "role": "CURRENT",
                        "evidence_id": "revenue_current",
                        "period": "FY2025",
                        "value": "150.00",
                    },
                    {
                        "role": "COMPARISON",
                        "evidence_id": "revenue_prior",
                        "period": "FY2024",
                        "value": "125.00",
                    },
                ],
                # the answer's unit is recorded apart from the inputs' unit, so
                # an operation whose result is not a percentage already fits
                "input_currency": "USD",
                "input_unit": "MONETARY",
                "input_scale": "MILLION",
                "output_unit": "PERCENT",
                "quantize": "0.01",
                "rounding": "ROUND_HALF_EVEN",
                "result": "20.00",
                "calculation_policy_sha256": formula["calculation_policy_sha256"],
            },
            formula,
        )

    def test_registered_aliases_form_an_accepted_replayable_lineage(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "valid-aliases")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: standard_candidate(document)}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.ACCEPT, outcome.decision)
        self.assertEqual("20.00", outcome.answer)
        self.assertEqual("PERCENT", outcome.answer_unit)
        self.assertEqual((), outcome.reason_codes)
        self.assertEqual("finauditgate.run/v1", outcome.schema_version)
        self.assertTrue(replay.consistent)
        self.assertEqual("finauditgate.replay/v5", replay.schema_version)
        self.assertEqual(Decision.ACCEPT, replay.decision)
        self.assertEqual(outcome.answer, replay.answer)
        self.assertEqual(outcome.answer_unit, replay.answer_unit)
        self.assertEqual(9, replay.verified_artifact_count)

    def test_identical_run_is_idempotent_and_append_only(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "idempotent")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            gate = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: standard_candidate(document)}),
            )
            first = gate.run(task)
            first_snapshot = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            second = gate.run(task)
            second_snapshot = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(first.run_ref, second.run_ref)
            self.assertEqual(first_snapshot, second_snapshot)

            outcome_path = root / "runs" / first.run_ref.run_id / "outcome.json"
            outcome_path.write_bytes(b"forged")
            with self.assertRaisesRegex(RuntimeError, "append-only artifact conflict"):
                gate.run(task)
            self.assertEqual(b"forged", outcome_path.read_bytes())

    def test_replay_ignores_process_decimal_precision(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "decimal-context")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: standard_candidate(document)}),
            ).run(task)
            with localcontext() as process_context:
                process_context.prec = 2
                report = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertTrue(report.consistent)
        self.assertEqual(Decision.ACCEPT, report.decision)
        self.assertEqual("20.00", report.answer)

    def test_recoverable_first_attempt_can_accept_on_the_only_retry(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "accept-on-retry")
        valid = standard_candidate(document)
        invalid = replace(
            valid,
            evidence=(valid.evidence[0], replace(valid.evidence[1], value="999.00")),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: (invalid, valid)}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.ACCEPT, outcome.decision)
        self.assertEqual("20.00", outcome.answer)
        self.assertEqual((), outcome.reason_codes)
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.ACCEPT, replay.decision)

    def test_exhausted_recoverable_problem_stops_after_two_attempts(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "retry-budget-exhausted")
        valid = standard_candidate(document)
        first_invalid = replace(
            valid,
            evidence=(valid.evidence[0], replace(valid.evidence[1], value="999.00")),
        )
        second_invalid = replace(
            valid,
            evidence=(valid.evidence[0], replace(valid.evidence[1], value="888.00")),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {task.task_id: (first_invalid, second_invalid, valid)}
                ),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.RETRY, outcome.decision)
        self.assertIsNone(outcome.answer)
        self.assertEqual(
            ("CLAIMED_VALUE_MISMATCH", "RETRY_BUDGET_EXHAUSTED"),
            outcome.reason_codes,
        )
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.RETRY, replay.decision)
        self.assertIsNone(replay.answer)

    def test_formula_outside_allowlist_abstains_without_retrying(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "formula-not-allowlisted")
        valid = standard_candidate(document)
        unsupported = replace(
            valid,
            calculation=replace(valid.calculation, operation="arbitrary_python"),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: (unsupported, valid)}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.ABSTAIN, outcome.decision)
        self.assertEqual(("FORMULA_NOT_ALLOWLISTED",), outcome.reason_codes)
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.ABSTAIN, replay.decision)

    def test_financial_semantic_ambiguity_and_conflict_require_review(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        base_task = standard_task(document, "semantic-gate")
        valid = standard_candidate(document)
        current_records = {
            "metric_conflict": (
                b"metric=Operating income;basis=IFRS reported;period=FY 2025;"
                b"value=150.00;currency=USD;unit=Currency amount;"
                b"scale=Million;sign=As presented",
                "METRIC_CONFLICT",
            ),
            "metric_ambiguous": (
                b"metric=Sales;basis=IFRS reported;period=FY 2025;"
                b"value=150.00;currency=USD;unit=Currency amount;"
                b"scale=Million;sign=As presented",
                "METRIC_AMBIGUOUS",
            ),
            "metric_basis_conflict": (
                b"metric=Total revenue;basis=Adjusted;period=FY 2025;"
                b"value=150.00;currency=USD;unit=Currency amount;"
                b"scale=Million;sign=As presented",
                "METRIC_BASIS_CONFLICT",
            ),
            "period_ambiguous": (
                b"metric=Total revenue;basis=IFRS reported;"
                b"period=Annual period;value=150.00;currency=USD;"
                b"unit=Currency amount;scale=Million;sign=As presented",
                "FISCAL_PERIOD_AMBIGUOUS",
            ),
            "currency_conflict": (
                b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
                b"value=150.00;currency=EUR;unit=Currency amount;"
                b"scale=Million;sign=As presented",
                "CURRENCY_CONFLICT",
            ),
            "currency_ambiguous": (
                b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
                b"value=150.00;currency=Dollar;unit=Currency amount;"
                b"scale=Million;sign=As presented",
                "CURRENCY_AMBIGUOUS",
            ),
            "unit_conflict": (
                b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
                b"value=150.00;currency=USD;unit=Shares;scale=Million;"
                b"sign=As presented",
                "UNIT_CONFLICT",
            ),
            "unit_ambiguous": (
                b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
                b"value=150.00;currency=USD;unit=Financial measure;"
                b"scale=Million;sign=As presented",
                "UNIT_AMBIGUOUS",
            ),
            "scale_conflict": (
                b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
                b"value=150.00;currency=USD;unit=Currency amount;"
                b"scale=Billions;sign=As presented",
                "SCALE_CONFLICT",
            ),
            "scale_ambiguous": (
                b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
                b"value=150.00;currency=USD;unit=Currency amount;"
                b"scale=Reported scale;sign=As presented",
                "SCALE_AMBIGUOUS",
            ),
            "sign_conflict": (
                b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
                b"value=150.00;currency=USD;unit=Currency amount;"
                b"scale=Million;sign=Negative",
                "SIGN_CONFLICT",
            ),
            "sign_ambiguous": (
                b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
                b"value=150.00;currency=USD;unit=Currency amount;"
                b"scale=Million;sign=Reported sign",
                "SIGN_AMBIGUOUS",
            ),
        }
        cases = {
            name: (base_task, candidate_with_current_record(document, record), reason)
            for name, (record, reason) in current_records.items()
        }
        cases["period_conflict"] = (
            base_task,
            replace(
                valid,
                calculation=replace(
                    valid.calculation,
                    operand_ids=("revenue_prior", "revenue_current"),
                ),
            ),
            "FISCAL_PERIOD_CONFLICT",
        )
        cases["post_cutoff"] = (
            replace(base_task, cutoff=date(2026, 2, 14)),
            valid,
            "POST_CUTOFF_DOCUMENT",
        )
        cases["question"] = (
            replace(base_task, question="What was profit growth?"),
            valid,
            "QUESTION_PROFILE_CONFLICT",
        )
        cases["source_id"] = (
            replace(
                base_task,
                document=replace(base_task.document, source_id="unrecognized-source"),
            ),
            valid,
            "SOURCE_PROFILE_CONFLICT",
        )
        cases["material_risk"] = (
            replace(base_task, risk_class="MATERIAL"),
            valid,
            "RISK_CLASS_REQUIRES_HUMAN_REVIEW",
        )
        cases["rounding_quantum"] = (
            base_task,
            replace(valid, calculation=replace(valid.calculation, quantize="1E+2")),
            "QUANTIZATION_CONFLICT",
        )

        for case_name, (task, candidate, expected_reason) in cases.items():
            with self.subTest(case_name=case_name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    outcome = FinAuditGate(
                        artifact_root=root,
                        model=ScriptedModelAdapter({task.task_id: (candidate, valid)}),
                    ).run(task)
                    replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

                self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
                self.assertIsNone(outcome.answer)
                self.assertEqual((expected_reason,), outcome.reason_codes)
                self.assertTrue(replay.consistent)
                self.assertEqual(Decision.HUMAN_REVIEW, replay.decision)

    def test_recoverable_candidate_classes_share_the_bounded_retry_stop(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "recoverable-classes")
        valid = standard_candidate(document)
        cases = {
            "missing_evidence": (
                replace(valid, evidence=(valid.evidence[0],)),
                "MISSING_EVIDENCE",
            ),
            "invalid_locator": (
                replace(
                    valid,
                    evidence=(valid.evidence[0], replace(valid.evidence[1], byte_start=-1)),
                ),
                "EVIDENCE_LOCATOR_INVALID",
            ),
            "candidate_shape": (
                replace(
                    valid,
                    evidence=(valid.evidence[0], replace(valid.evidence[1], currency=None)),
                ),
                "CANDIDATE_SHAPE_INVALID",
            ),
            "claimed_semantics": (
                replace(
                    valid,
                    evidence=(valid.evidence[0], replace(valid.evidence[1], metric="Profit")),
                ),
                "CLAIMED_EVIDENCE_MISMATCH",
            ),
            "operand_lineage": (
                replace(
                    valid,
                    calculation=replace(
                        valid.calculation,
                        operand_ids=("revenue_current", "missing"),
                    ),
                ),
                "OPERAND_LINEAGE_INVALID",
            ),
            "duplicate_operands": (
                replace(
                    valid,
                    calculation=replace(
                        valid.calculation,
                        operand_ids=("revenue_current", "revenue_current"),
                    ),
                ),
                "OPERAND_LINEAGE_INVALID",
            ),
            "non_string_lineage_ids": (
                ScriptedCandidate(
                    evidence=(
                        replace(valid.evidence[0], evidence_id=1),
                        replace(valid.evidence[1], evidence_id=2),
                    ),
                    calculation=replace(valid.calculation, operand_ids=(2, 1)),
                ),
                "CANDIDATE_SHAPE_INVALID",
            ),
        }

        for case_name, (candidate, expected_reason) in cases.items():
            with self.subTest(case_name=case_name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    outcome = FinAuditGate(
                        artifact_root=root,
                        model=ScriptedModelAdapter(
                            {task.task_id: (candidate, candidate, valid)}
                        ),
                    ).run(task)
                    replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

                self.assertEqual(Decision.RETRY, outcome.decision)
                self.assertEqual(
                    (expected_reason, "RETRY_BUDGET_EXHAUSTED"),
                    outcome.reason_codes,
                )
                self.assertTrue(replay.consistent)
                self.assertEqual(Decision.RETRY, replay.decision)

    def test_attempts_artifact_is_required_and_bound_to_run_identity(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "attempts-integrity")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: standard_candidate(document)}),
            ).run(task)
            (root / "runs" / outcome.run_ref.run_id / "attempts.json").unlink()
            missing = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertFalse(missing.consistent)
        self.assertEqual("finauditgate.replay/v5", missing.schema_version)
        self.assertIsNone(missing.decision)
        self.assertEqual("MISSING_ARTIFACT", missing.reason)

        valid = standard_candidate(document)
        invalid = replace(
            valid,
            evidence=(valid.evidence[0], replace(valid.evidence[1], value="999.00")),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: (invalid, valid)}),
            ).run(task)
            run_directory = root / "runs" / outcome.run_ref.run_id
            attempts = json.loads((run_directory / "attempts.json").read_bytes())
            forged_candidate = attempts["attempts"][0]["candidate"]
            forged_candidate["evidence"][1]["value"] = "888.00"
            attempts["attempts"][0]["candidate_sha256"] = hashlib.sha256(
                _canonical(forged_candidate)
            ).hexdigest()
            forged_proposal = attempts["attempts"][0]["proposal"]
            forged_proposal["value"]["evidence"]["items"][1]["value"]["value"] = "888.00"
            attempts["attempts"][0]["proposal_sha256"] = hashlib.sha256(
                _canonical(forged_proposal)
            ).hexdigest()
            _rewrite_artifact(run_directory, "attempts.json", _canonical(attempts))
            forged = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertFalse(forged.consistent)
        self.assertIsNone(forged.decision)
        self.assertEqual("RUN_IDENTITY_ARTIFACT_MISMATCH", forged.reason)

    def test_registered_source_with_different_content_requires_review(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        altered_document = document + b"synthetic-tamper=true\n"
        task = standard_task(altered_document, "source-profile-conflict")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {task.task_id: standard_candidate(altered_document)}
                ),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
        self.assertEqual(("SOURCE_PROFILE_CONFLICT",), outcome.reason_codes)
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.HUMAN_REVIEW, replay.decision)

    def test_private_mode_requires_a_frozen_profile(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = replace(standard_task(document, "private-without-profile"), mode="PRIVATE_DEV")
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "finaudit-gate" / ".git").mkdir(parents=True)
            (workspace / "private").mkdir()
            with self.assertRaisesRegex(RuntimeError, "REVIEWED_VALIDATION_PROFILE_REQUIRED"):
                FinAuditGate(
                    artifact_root=workspace / "private" / "artifacts",
                    model=ScriptedModelAdapter({task.task_id: standard_candidate(document)}),
                ).run(task)

    def test_tampered_replay_artifacts_fail_closed(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        deeply_nested = b"[" * 10_000 + b"null" + b"]" * 10_000

        def deep_attempts(run_directory: Path) -> None:
            _rewrite_artifact(run_directory, "attempts.json", deeply_nested)

        def deep_manifest_and_identity(run_directory: Path) -> None:
            (run_directory / "manifest.json").write_bytes(deeply_nested)
            (run_directory / "identity.json").write_bytes(deeply_nested)

        def oversized_attempts(run_directory: Path) -> None:
            _rewrite_artifact(run_directory, "attempts.json", b"0" * (16_777_216 + 1))

        def malformed_identity(run_directory: Path) -> None:
            (run_directory / "manifest.json").write_bytes(deeply_nested)
            (run_directory / "identity.json").write_bytes(
                b'{"schema_version":"finauditgate.run-identity/v5","value":"\\ud800"}'
            )

        def forged_outcome(run_directory: Path) -> None:
            outcome_payload = json.loads((run_directory / "outcome.json").read_bytes())
            outcome_payload["task_id"] = "forged-task"
            outcome_payload["document_sha256"] = "0" * 64
            _rewrite_artifact(run_directory, "outcome.json", _canonical(outcome_payload))

        def unsupported_task_schema(run_directory: Path) -> None:
            task_payload = json.loads((run_directory / "task.json").read_bytes())
            task_payload["schema_version"] = "finauditgate.task/v999"
            _rewrite_artifact(run_directory, "task.json", _canonical(task_payload))

        def malformed_manifest(run_directory: Path) -> None:
            manifest_path = run_directory / "manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            manifest["artifacts"]["task"] = []
            manifest_path.write_bytes(_canonical(manifest))

        def missing_ledger(run_directory: Path) -> None:
            (run_directory / "ledger.json").unlink()

        cases = {
            "deep-attempts": (deep_attempts, "ARTIFACT_STRUCTURE_INVALID"),
            "deep-manifest-and-identity": (deep_manifest_and_identity, "MALFORMED_MANIFEST"),
            "oversized-attempts": (oversized_attempts, "ARTIFACT_STRUCTURE_INVALID"),
            "malformed-identity": (malformed_identity, "MALFORMED_MANIFEST"),
            "forged-outcome": (forged_outcome, "OUTCOME_METADATA_MISMATCH"),
            "unsupported-task-schema": (unsupported_task_schema, "REPLAY_VALIDATION_FAILED"),
            "malformed-manifest": (malformed_manifest, "MALFORMED_MANIFEST"),
            "missing-ledger": (missing_ledger, "MISSING_ARTIFACT"),
        }
        for name, (tamper, expected_reason) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                task = standard_task(document, f"tamper-{name}")
                outcome = FinAuditGate(
                    artifact_root=root,
                    model=ScriptedModelAdapter(
                        {task.task_id: standard_candidate(document)}
                    ),
                ).run(task)
                tamper(root / "runs" / outcome.run_ref.run_id)
                replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

            self.assertFalse(replay.consistent)
            self.assertIsNone(replay.decision)
            self.assertEqual(expected_reason, replay.reason)

    def test_overflowing_float_snapshot_tamper_fails_closed(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "overflowing-float-tamper")
        valid = standard_candidate(document)
        malformed = ScriptedCandidate(evidence=1.0, calculation=valid.calculation)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: (malformed, valid)}),
            ).run(task)
            run_directory = root / "runs" / outcome.run_ref.run_id
            attempts = json.loads((run_directory / "attempts.json").read_bytes())
            proposal = attempts["attempts"][0]["proposal"]
            proposal["value"]["evidence"]["hex"] = "0x1p+999999999"
            attempts["attempts"][0]["proposal_sha256"] = hashlib.sha256(
                _canonical(proposal)
            ).hexdigest()
            _rewrite_artifact(run_directory, "attempts.json", _canonical(attempts))
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertFalse(replay.consistent)
        self.assertIsNone(replay.decision)
        self.assertEqual("RUN_IDENTITY_ARTIFACT_MISMATCH", replay.reason)

    def test_decimal_domain_failures_are_decisions_not_exceptions(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "decimal-domain")
        valid = standard_candidate(document)
        invalid_value_records = {
            "non_numeric": (
                b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
                b"value=not-a-number;currency=USD;unit=Currency amount;"
                b"scale=Million;sign=As presented"
            ),
            "infinity": (
                b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
                b"value=Infinity;currency=USD;unit=Currency amount;"
                b"scale=Million;sign=As presented"
            ),
        }
        for case_name, record in invalid_value_records.items():
            with self.subTest(case_name=case_name):
                invalid = candidate_with_current_record(document, record)
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    outcome = FinAuditGate(
                        artifact_root=root,
                        model=ScriptedModelAdapter({task.task_id: (invalid, invalid, valid)}),
                    ).run(task)
                    replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

                self.assertEqual(Decision.RETRY, outcome.decision)
                self.assertEqual(
                    ("EVIDENCE_VALUE_INVALID", "RETRY_BUDGET_EXHAUSTED"),
                    outcome.reason_codes,
                )
                self.assertTrue(replay.consistent)
                self.assertEqual(Decision.RETRY, replay.decision)

        zero_prior_record = (
            b"metric=Net sales;basis=Reported;period=Year ended 2024-12-31;"
            b"value=0;currency=US dollar;unit=Monetary;scale=Millions;"
            b"sign=Positive"
        )
        zero_prior = replace(
            valid,
            evidence=(evidence_for(document, "revenue_prior", zero_prior_record), valid.evidence[1]),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: (zero_prior, valid)}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.ABSTAIN, outcome.decision)
        self.assertEqual(("FORMULA_DOMAIN_ERROR",), outcome.reason_codes)
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.ABSTAIN, replay.decision)

    def test_mixed_optional_semantic_shape_consumes_the_only_retry(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "mixed-optional-shape")
        valid = standard_candidate(document)
        mixed = replace(
            valid,
            evidence=(
                replace(
                    valid.evidence[0],
                    metric_basis=None,
                    currency=None,
                    scale=None,
                    sign=None,
                ),
                valid.evidence[1],
            ),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: (mixed, valid)}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.ACCEPT, outcome.decision)
        self.assertEqual("20.00", outcome.answer)
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.ACCEPT, replay.decision)

    def test_missing_retry_proposal_is_preserved_as_exhausted_retry(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "missing-retry-proposal")
        valid = standard_candidate(document)
        invalid = replace(
            valid,
            evidence=(valid.evidence[0], replace(valid.evidence[1], value="999.00")),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: invalid}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.RETRY, outcome.decision)
        self.assertEqual(
            ("CANDIDATE_PROPOSAL_MISSING", "RETRY_BUDGET_EXHAUSTED"),
            outcome.reason_codes,
        )
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.RETRY, replay.decision)

    def test_malformed_calculation_fields_are_recoverable_shape_errors(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "malformed-calculation-shape")
        valid = standard_candidate(document)
        cases = {
            "operation": replace(valid.calculation, operation=None),
            "output_unit": replace(valid.calculation, output_unit=None),
            "quantize": replace(valid.calculation, quantize=None),
        }

        for field_name, calculation in cases.items():
            with self.subTest(field_name=field_name):
                malformed = replace(valid, calculation=calculation)
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    outcome = FinAuditGate(
                        artifact_root=root,
                        model=ScriptedModelAdapter(
                            {task.task_id: (malformed, malformed, valid)}
                        ),
                    ).run(task)
                    replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

                self.assertEqual(Decision.RETRY, outcome.decision)
                self.assertEqual(
                    ("CANDIDATE_SHAPE_INVALID", "RETRY_BUDGET_EXHAUSTED"),
                    outcome.reason_codes,
                )
                self.assertTrue(replay.consistent)
                self.assertEqual(Decision.RETRY, replay.decision)

    def test_malformed_operand_shape_consumes_the_only_retry(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "malformed-operand-shape")
        valid = standard_candidate(document)
        malformed = replace(valid, calculation=replace(valid.calculation, operand_ids=None))

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: (malformed, valid)}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)
            attempts = json.loads(
                (root / "runs" / outcome.run_ref.run_id / "attempts.json").read_bytes()
            )

        self.assertEqual(Decision.ACCEPT, outcome.decision)
        self.assertEqual("20.00", outcome.answer)
        self.assertEqual(["CANDIDATE_SHAPE_INVALID"], attempts["attempts"][0]["reason_codes"])
        self.assertTrue(replay.consistent)

    def test_top_level_malformed_candidate_is_preserved_and_retried(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "top-level-malformed-candidate")
        valid = standard_candidate(document)
        malformed = ScriptedCandidate(evidence=None, calculation=valid.calculation)

        class CountingAdapter:
            def __init__(self) -> None:
                self.calls: list[int] = []

            def propose(self, adapter_task: AuditTask, attempt_index: int = 0) -> ScriptedCandidate:
                self.calls.append(attempt_index)
                return (malformed, valid)[attempt_index]

        adapter = CountingAdapter()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(artifact_root=root, model=adapter).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)
            attempts = json.loads(
                (root / "runs" / outcome.run_ref.run_id / "attempts.json").read_bytes()
            )

        self.assertEqual([0, 1], adapter.calls)
        self.assertEqual(Decision.ACCEPT, outcome.decision)
        malformed_attempt = attempts["attempts"][0]
        self.assertEqual(0, malformed_attempt["attempt_index"])
        self.assertIsInstance(malformed_attempt["proposal"], dict)
        self.assertEqual(
            hashlib.sha256(_canonical(malformed_attempt["proposal"])).hexdigest(),
            malformed_attempt["proposal_sha256"],
        )
        self.assertIsNone(malformed_attempt["candidate"])
        self.assertIsNone(malformed_attempt["candidate_sha256"])
        self.assertEqual(Decision.RETRY.value, malformed_attempt["disposition"])
        self.assertEqual(["CANDIDATE_SHAPE_INVALID"], malformed_attempt["reason_codes"])
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.ACCEPT, replay.decision)

    def test_distinct_malformed_proposals_have_distinct_replayable_identities(
        self,
    ) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "distinct-malformed-proposals")
        valid = standard_candidate(document)
        shared_prefix = "p" * 128
        shared_suffix = "s" * 128
        malformed_proposals = (
            ScriptedCandidate(evidence=None, calculation=valid.calculation),
            ScriptedCandidate(evidence=42, calculation=valid.calculation),
            ScriptedCandidate(
                evidence=shared_prefix + "a" * 4_744 + shared_suffix,
                calculation=valid.calculation,
            ),
            ScriptedCandidate(
                evidence=shared_prefix + "b" * 4_744 + shared_suffix,
                calculation=valid.calculation,
            ),
        )
        outcomes = []
        replays = []

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index, malformed in enumerate(malformed_proposals):
                artifact_root = root / str(index)
                outcome = FinAuditGate(
                    artifact_root=artifact_root,
                    model=ScriptedModelAdapter({task.task_id: (malformed, valid)}),
                ).run(task)
                outcomes.append(outcome)
                replays.append(FinAuditGate(artifact_root=artifact_root).replay(outcome.run_ref))

        self.assertEqual(len(outcomes), len({outcome.run_ref for outcome in outcomes}))
        self.assertTrue(all(replay.consistent for replay in replays))
        self.assertEqual({Decision.ACCEPT}, {replay.decision for replay in replays})

    def test_snapshot_limits_still_record_shape_error_and_retry(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        valid = standard_candidate(document)
        deeply_nested: object = None
        for _ in range(1500):
            deeply_nested = [deeply_nested]

        class EquivocatingStr(str):
            def __eq__(self, other: object) -> bool:
                return True

            __hash__ = str.__hash__

        malformed_proposals = {
            "oversized_integer": ScriptedCandidate(
                evidence=10**5000,
                calculation=valid.calculation,
            ),
            "deeply_nested": ScriptedCandidate(
                evidence=deeply_nested,
                calculation=valid.calculation,
            ),
            "unpaired_surrogate": ScriptedCandidate(
                evidence="\ud800",
                calculation=valid.calculation,
            ),
            "uninitialized_candidate": object.__new__(ScriptedCandidate),
            "string_subclass": ScriptedCandidate(
                evidence=(
                    replace(valid.evidence[0], metric=EquivocatingStr("profit")),
                    valid.evidence[1],
                ),
                calculation=valid.calculation,
            ),
        }

        for case_name, malformed in malformed_proposals.items():
            with self.subTest(case_name=case_name):
                task = standard_task(document, f"snapshot-limit-{case_name}")
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    outcome = FinAuditGate(
                        artifact_root=root,
                        model=ScriptedModelAdapter({task.task_id: (malformed, valid)}),
                    ).run(task)
                    replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)
                    attempts = json.loads(
                        (root / "runs" / outcome.run_ref.run_id / "attempts.json").read_bytes()
                    )

                self.assertEqual(Decision.ACCEPT, outcome.decision)
                self.assertEqual(
                    ["CANDIDATE_SHAPE_INVALID"],
                    attempts["attempts"][0]["reason_codes"],
                )
                self.assertIsInstance(attempts["attempts"][0]["proposal"], dict)
                self.assertTrue(replay.consistent)
                self.assertEqual(Decision.ACCEPT, replay.decision)

    def test_two_top_level_malformed_candidates_exhaust_retry_replayably(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "two-top-level-malformed")
        valid = standard_candidate(document)
        malformed = ScriptedCandidate(evidence=None, calculation=valid.calculation)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: (malformed, malformed)}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)
            candidate_payload = json.loads(
                (root / "runs" / outcome.run_ref.run_id / "candidate.json").read_bytes()
            )

        self.assertEqual(Decision.RETRY, outcome.decision)
        self.assertEqual(
            ("CANDIDATE_SHAPE_INVALID", "RETRY_BUDGET_EXHAUSTED"),
            outcome.reason_codes,
        )
        self.assertIsNone(candidate_payload)
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.RETRY, replay.decision)

    def test_valid_retry_then_malformed_candidate_replays_consistently(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "valid-retry-then-malformed")
        valid = standard_candidate(document)
        claimed_value_mismatch = replace(
            valid,
            evidence=(valid.evidence[0], replace(valid.evidence[1], value="999.00")),
        )
        malformed = ScriptedCandidate(evidence=None, calculation=valid.calculation)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: (claimed_value_mismatch, malformed)}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)
            candidate_payload = json.loads(
                (root / "runs" / outcome.run_ref.run_id / "candidate.json").read_bytes()
            )

        self.assertEqual(Decision.RETRY, outcome.decision)
        self.assertEqual(
            ("CANDIDATE_SHAPE_INVALID", "RETRY_BUDGET_EXHAUSTED"),
            outcome.reason_codes,
        )
        self.assertIsNone(candidate_payload)
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.RETRY, replay.decision)

    def test_candidate_normalization_runtime_error_consumes_retry(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "normalization-runtime-error")
        valid = standard_candidate(document)

        class ExplodingEvidence:
            def __iter__(self) -> object:
                raise RuntimeError("malformed iterable")

        malformed = ScriptedCandidate(evidence=ExplodingEvidence(), calculation=valid.calculation)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: (malformed, valid)}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.ACCEPT, outcome.decision)
        self.assertEqual("20.00", outcome.answer)
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.ACCEPT, replay.decision)

    def test_model_cannot_mutate_the_hash_bound_policy(self) -> None:
        from finauditgate.core import synthetic_profile

        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "policy-mutation-attempt")
        valid = standard_candidate(document)
        unsupported = replace(
            valid,
            calculation=replace(valid.calculation, operation="arbitrary_python"),
        )

        class PolicyMutatingModel:
            def propose(self, proposed_task: AuditTask, attempt_index: int = 0) -> ScriptedCandidate:
                try:
                    synthetic_profile.SYNTHETIC_POLICY["operation"] = "arbitrary_python"
                except TypeError:
                    pass
                return unsupported

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(artifact_root=root, model=PolicyMutatingModel()).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.ABSTAIN, outcome.decision)
        self.assertEqual(("FORMULA_NOT_ALLOWLISTED",), outcome.reason_codes)
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.ABSTAIN, replay.decision)

    def test_replay_is_consistent_in_a_fresh_python_process(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "fresh-process-replay")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: standard_candidate(document)}),
            ).run(task)
            source_root = Path(__file__).parents[1] / "src"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        "from finauditgate import FinAuditGate, RunRef; "
                        f"report = FinAuditGate(artifact_root=Path({str(root)!r}))"
                        f".replay(RunRef({outcome.run_ref.run_id!r})); "
                        "print(report.schema_version, report.consistent, "
                        "report.decision.value, report.answer, "
                        "report.verified_artifact_count)"
                    ),
                ],
                env={
                    "PATH": str(Path(sys.executable).parent),
                    "PYTHONPATH": str(source_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(
            "finauditgate.replay/v5 True ACCEPT 20.00 9",
            completed.stdout.strip(),
        )


if __name__ == "__main__":
    unittest.main()
