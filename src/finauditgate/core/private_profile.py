"""Deterministic executor for frozen natural-disclosure private profiles.

A private profile is the reviewed answer key for one question on one frozen
document: the exact byte spans, their hashes and values, the normalized
semantics, and the one allowlisted formula.  The model's proposal is accepted
only when it lands exactly on that key.  Two negative profile shapes exist so
that the reviewed failure classes can be expressed too: a document published
after the task cutoff, and a question with no admissible evidence at all.
"""

from __future__ import annotations

from decimal import (
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    localcontext,
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
    if task.mode != "PRIVATE_DEV":
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
        if (
            type(evidence.byte_start) is not int
            or type(evidence.byte_end) is not int
            or evidence.byte_start != frozen["byte_start"]
            or evidence.byte_end != frozen["byte_end"]
            or not 0 <= evidence.byte_start < evidence.byte_end <= len(document)
        ):
            raise ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_LOCATOR_INVALID",
            )
        span = document[evidence.byte_start : evidence.byte_end]
        if sha256_hex(span) != frozen["span_sha256"]:
            raise ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_SPAN_HASH_MISMATCH",
            )
        if evidence.value != frozen["value"]:
            raise ValidationFailure(
                Decision.RETRY,
                "CLAIMED_VALUE_MISMATCH",
            )
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
                    "span_sha256": frozen["span_sha256"],
                },
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
    if comparison_value == 0:
        raise ValidationFailure(Decision.ABSTAIN, "FORMULA_DOMAIN_ERROR")
    context_policy = frozen_calculation["decimal_context"]
    context = Context(
        prec=context_policy["precision"],
        rounding=ROUND_HALF_EVEN,
        Emin=context_policy["emin"],
        Emax=context_policy["emax"],
        capitals=context_policy["capitals"],
        clamp=context_policy["clamp"],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
    with localcontext(context):
        result = (
            (current_value - comparison_value)
            / comparison_value
            * Decimal("100")
        ).quantize(
            Decimal(frozen_calculation["quantize"]),
            rounding=ROUND_HALF_EVEN,
        )

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
