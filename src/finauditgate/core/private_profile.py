"""Deterministic executor for frozen natural-disclosure private profiles.

A private profile is the reviewed answer key for one question on one frozen
document: the exact byte spans, their hashes and values, the normalized
semantics, and the one allowlisted formula.  The model's proposal is accepted
only when it lands exactly on that key.  Two negative profile shapes exist so
that the reviewed failure classes can be expressed too: a document published
after the task cutoff, and a question with no admissible evidence at all.
"""

from __future__ import annotations

import re

from decimal import Decimal, InvalidOperation

from finauditgate.core.operations import (
    ANSWER_CONTRACTS,
    OperationDomainError,
    evaluate as evaluate_operation,
)
from finauditgate.contracts import AuditTask, Decision
from finauditgate.core.artifacts import sha256_hex
from finauditgate.core.synthetic_profile import ValidationFailure
from finauditgate.ports.model import ModelCandidate


def evaluate_private_candidate(
    document: bytes,
    candidate: ModelCandidate,
    task: AuditTask,
    *,
    policy: dict[str, object],
    policy_sha256: str,
    semantics_sha256: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Match a proposal to reviewed locators/semantics, then calculate."""

    if (
        sha256_hex(document) != policy["document_sha256"]
        or task.document.source_id != policy["source_id"]
        or task.document.document_name != policy["document_name"]
    ):
        raise ValidationFailure(Decision.HUMAN_REVIEW, "SOURCE_PROFILE_CONFLICT")
    if task.question != policy["task_question"]:
        raise ValidationFailure(
            Decision.HUMAN_REVIEW,
            "QUESTION_PROFILE_CONFLICT",
        )
    if (
        task.document.declared_published_at.isoformat()
        != policy["declared_published_at"]
    ):
        raise ValidationFailure(
            Decision.HUMAN_REVIEW,
            "SOURCE_PROFILE_CONFLICT",
        )
    if task.cutoff.isoformat() != policy["task_cutoff"]:
        raise ValidationFailure(
            Decision.HUMAN_REVIEW,
            "CUTOFF_PROFILE_CONFLICT",
        )
    if task.mode != policy["accepted_mode"]:
        raise ValidationFailure(Decision.HUMAN_REVIEW, "MODE_CONFLICT")
    if task.risk_class != policy["accepted_risk_class"]:
        raise ValidationFailure(
            Decision.HUMAN_REVIEW,
            "RISK_CLASS_REQUIRES_HUMAN_REVIEW",
        )
    if task.document.declared_published_at > task.cutoff:
        raise ValidationFailure(
            Decision.HUMAN_REVIEW,
            "POST_CUTOFF_DOCUMENT",
        )
    frozen_items = policy["evidence_allowlist"]
    if not frozen_items:
        # The reviewed answer for this question is "nothing in this document
        # supports it"; no proposal can be admissible.
        raise ValidationFailure(
            Decision.HUMAN_REVIEW,
            "NO_ADMISSIBLE_EVIDENCE",
        )
    if type(candidate) is not ModelCandidate or len(candidate.evidence) != 2:
        raise ValidationFailure(Decision.RETRY, "CANDIDATE_SHAPE_INVALID")

    frozen_by_id = {item["evidence_id"]: item for item in frozen_items}
    seen_ids: set[str] = set()
    nodes: list[dict[str, object]] = []
    semantic_codes = {
        "metric": "METRIC_CONFLICT",
        "metric_basis": "METRIC_BASIS_CONFLICT",
        "fiscal_period": "FISCAL_PERIOD_CONFLICT",
        "currency": "CURRENCY_CONFLICT",
        "unit": "UNIT_CONFLICT",
        "scale": "SCALE_CONFLICT",
        "sign": "SIGN_CONFLICT",
    }
    for evidence in candidate.evidence:
        if (
            type(evidence.evidence_id) is not str
            or evidence.evidence_id in seen_ids
            or evidence.evidence_id not in frozen_by_id
        ):
            raise ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_PROFILE_CONFLICT",
            )
        seen_ids.add(evidence.evidence_id)
        frozen = frozen_by_id[evidence.evidence_id]
        # The reviewed locator is the evidence region (typically one table
        # row).  The model may cite that whole region or the number inside
        # it; anything outside the region is not the reviewed evidence.
        if (
            type(evidence.byte_start) is not int
            or type(evidence.byte_end) is not int
            or not 0 <= evidence.byte_start < evidence.byte_end <= len(document)
            or not (
                frozen["byte_start"]
                <= evidence.byte_start
                < evidence.byte_end
                <= frozen["byte_end"]
            )
        ):
            raise ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_LOCATOR_INVALID",
            )
        reviewed = document[frozen["byte_start"] : frozen["byte_end"]]
        if sha256_hex(reviewed) != frozen["span_sha256"]:
            raise ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_SPAN_HASH_MISMATCH",
            )
        if evidence.value != frozen["value"]:
            raise ValidationFailure(
                Decision.RETRY,
                "CLAIMED_VALUE_MISMATCH",
            )
        span = document[evidence.byte_start : evidence.byte_end]
        if not _span_carries_value(span, frozen["value"]):
            raise ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_LOCATOR_INVALID",
            )
        corroboration = _corroborating_locator(document, frozen)
        semantics = frozen["normalized_semantics"]
        claims = {
            "metric": evidence.metric,
            "metric_basis": evidence.metric_basis,
            "fiscal_period": evidence.period,
            "currency": evidence.currency,
            "unit": evidence.unit,
            "scale": evidence.scale,
            "sign": evidence.sign,
        }
        for name, code in semantic_codes.items():
            if claims[name] != semantics[name]:
                raise ValidationFailure(Decision.HUMAN_REVIEW, code)
        try:
            value = Decimal(evidence.value)
        except InvalidOperation as exc:
            raise ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_VALUE_INVALID",
            ) from exc
        if not value.is_finite():
            raise ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_VALUE_INVALID",
            )
        nodes.append(
            {
                "evidence_id": evidence.evidence_id,
                "role": frozen["role"],
                "raw_semantics": claims,
                "normalized_semantics": semantics,
                "value": evidence.value,
                "locator": {
                    "byte_start": evidence.byte_start,
                    "byte_end": evidence.byte_end,
                    "span_sha256": sha256_hex(span),
                },
                "reviewed_locator": {
                    "byte_start": frozen["byte_start"],
                    "byte_end": frozen["byte_end"],
                    "span_sha256": frozen["span_sha256"],
                },
                "corroboration": corroboration,
                "verification": "VERIFIED_AGAINST_FROZEN_PRIVATE_ALLOWLIST",
            }
        )

    calculation = candidate.calculation
    frozen_calculation = policy["calculation"]
    if calculation.operation != frozen_calculation["operation"]:
        raise ValidationFailure(
            Decision.ABSTAIN,
            "FORMULA_NOT_ALLOWLISTED",
        )
    # The task, the profile and the arithmetic must be asking and answering the
    # same question.  For every run made before a second operation existed this
    # is satisfied by construction.
    if task.answer_contract != ANSWER_CONTRACTS[frozen_calculation["operation"]]:
        raise ValidationFailure(
            Decision.HUMAN_REVIEW,
            "ANSWER_CONTRACT_CONFLICT",
        )
    if calculation.output_unit != frozen_calculation["output_unit"]:
        raise ValidationFailure(
            Decision.HUMAN_REVIEW,
            "OUTPUT_UNIT_CONFLICT",
        )
    if calculation.quantize != frozen_calculation["quantize"]:
        raise ValidationFailure(
            Decision.HUMAN_REVIEW,
            "QUANTIZATION_CONFLICT",
        )
    if list(calculation.operand_ids) != frozen_calculation["operand_ids"]:
        raise ValidationFailure(
            Decision.RETRY,
            "OPERAND_LINEAGE_INVALID",
        )
    by_id = {node["evidence_id"]: node for node in nodes}
    try:
        current = by_id[calculation.operand_ids[0]]
        comparison = by_id[calculation.operand_ids[1]]
    except (IndexError, KeyError) as exc:
        raise ValidationFailure(
            Decision.RETRY,
            "OPERAND_LINEAGE_INVALID",
        ) from exc
    if current["role"] != "CURRENT" or comparison["role"] != "COMPARISON":
        raise ValidationFailure(
            Decision.RETRY,
            "OPERAND_LINEAGE_INVALID",
        )

    current_value = Decimal(current["value"])
    comparison_value = Decimal(comparison["value"])
    context_policy = frozen_calculation["decimal_context"]
    try:
        result = evaluate_operation(
            calculation.operation,
            current=current_value,
            comparison=comparison_value,
            quantize=frozen_calculation["quantize"],
            precision=context_policy["precision"],
            emin=context_policy["emin"],
            emax=context_policy["emax"],
            capitals=context_policy["capitals"],
            clamp=context_policy["clamp"],
        )
    except OperationDomainError as exc:
        raise ValidationFailure(
            Decision.ABSTAIN,
            "FORMULA_DOMAIN_ERROR",
        ) from exc

    ledger = {
        "schema_version": "finauditgate.ledger/v2",
        "semantic_registry_sha256": semantics_sha256,
        "nodes": nodes,
    }
    formula = {
        "schema_version": "finauditgate.formula/v2",
        "operation": calculation.operation,
        "operand_ids": list(calculation.operand_ids),
        "operand_lineage": [
            {
                "role": "CURRENT",
                "evidence_id": current["evidence_id"],
                "period": current["normalized_semantics"]["fiscal_period"],
                "value": current["value"],
            },
            {
                "role": "COMPARISON",
                "evidence_id": comparison["evidence_id"],
                "period": comparison["normalized_semantics"]["fiscal_period"],
                "value": comparison["value"],
            },
        ],
        "input_currency": current["normalized_semantics"]["currency"],
        "input_unit": current["normalized_semantics"]["unit"],
        "input_scale": current["normalized_semantics"]["scale"],
        "output_unit": calculation.output_unit,
        "quantize": calculation.quantize,
        "rounding": context_policy["rounding"],
        "calculation_policy_sha256": policy_sha256,
        "result": format(result, "f"),
    }
    return ledger, formula


def _corroborating_locator(
    document: bytes,
    frozen: dict[str, object],
) -> dict[str, object] | None:
    """Check the reviewed second printing of a figure, if there is one.

    A filing prints the same figure in more than one place -- a total in the
    statement and again in the note that breaks it down.  Where the reviewer
    recorded that second printing, the gate reads it too: the bytes must hash
    as reviewed and must print the same value.  This is checked from the frozen
    document alone, so the model neither knows about it nor can influence it.
    """

    reviewed = frozen["corroboration"]
    if reviewed is None:
        return None
    span = document[reviewed["byte_start"] : reviewed["byte_end"]]
    if sha256_hex(span) != reviewed["span_sha256"]:
        raise ValidationFailure(Decision.HUMAN_REVIEW, "CORROBORATION_CONFLICT")
    if not _span_carries_value(span, frozen["value"]):
        raise ValidationFailure(Decision.HUMAN_REVIEW, "CORROBORATION_CONFLICT")
    return {
        "byte_start": reviewed["byte_start"],
        "byte_end": reviewed["byte_end"],
        "span_sha256": reviewed["span_sha256"],
        "agreement": "SECOND_PRINTING_CARRIES_THE_SAME_VALUE",
    }


def _span_carries_value(span: bytes, value: str) -> bool:
    """True when the cited bytes print the reviewed value.

    Thousands separators and spaces are ignored; a leading minus or
    parentheses are the sign's business, not the locator's.
    """

    try:
        printed = span.decode("utf-8")
    except UnicodeDecodeError:
        return False
    digits = re.sub(r"[,\s]", "", printed)
    return value.lstrip("-") in digits
