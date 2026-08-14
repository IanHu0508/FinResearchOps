"""Content-bound M2 financial semantics and deterministic calculation."""

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
import re
from types import MappingProxyType
from typing import Any

from finauditgate.contracts import AuditTask, Decision
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.ports.model import ModelCandidate


M2_DOCUMENT_SHA256 = (
    "b8068de14be8f02595b6a48b4bfb9e61f43676c5259cba9ed2f77e1600b2a269"
)
_M2_POLICY_PAYLOAD = {
    "schema_version": "finauditgate.calculation-policy/v2",
    "validation_profile": "synthetic-aurora-revenue-growth/v1",
    "source_id": "synthetic-aurora-revenue-growth-v2",
    "document_sha256": M2_DOCUMENT_SHA256,
    "task_question": (
        "What was Aurora Devices FY2025 revenue growth versus FY2024?"
    ),
    "accepted_mode": "SYNTHETIC_DEV",
    "accepted_risk_class": "LOW",
    "operation": "growth_rate_percent",
    "output_unit": "PERCENT",
    "quantize": "0.01",
    "precision": 28,
    "rounding": "ROUND_HALF_EVEN",
    "emin": -999999,
    "emax": 999999,
    "capitals": 1,
    "clamp": 0,
    "expected": {
        "metric": "revenue",
        "metric_basis": "REPORTED",
        "current_period": "FY2025",
        "comparison_period": "FY2024",
        "currency": "USD",
        "unit": "MONETARY",
        "scale": "MILLION",
        "sign": "POSITIVE",
    },
    "registries": {
        "schema_version": "finauditgate.semantic-registries/v1",
        "fiscal_period": {
            "FY2023": ["FY2023", "FY 2023"],
            "FY2024": [
                "FY2024",
                "FY 2024",
                "Year ended 2024-12-31",
                "Annual period",
            ],
            "FY2025": [
                "FY2025",
                "FY 2025",
                "Year ended 2025-12-31",
                "Annual period",
            ],
        },
        "metric": {
            "revenue": [
                "Revenue",
                "Total revenue",
                "Net sales",
                "Sales",
            ],
            "sales_volume": ["Unit sales", "Sales"],
            "operating_income": ["Operating income", "Operating profit"],
        },
        "metric_basis": {
            "REPORTED": ["Reported", "IFRS reported", "Reported basis"],
            "ADJUSTED": ["Adjusted", "Non-GAAP", "Reported basis"],
        },
        "currency": {
            "USD": ["USD", "US dollar", "Dollar"],
            "CAD": ["CAD", "Canadian dollar", "Dollar"],
            "EUR": ["EUR", "Euro"],
        },
        "unit": {
            "MONETARY": [
                "Monetary",
                "Currency amount",
                "Financial measure",
            ],
            "COUNT": ["Count", "Shares", "Financial measure"],
        },
        "scale": {
            "MILLION": ["Million", "Millions", "Reported scale"],
            "BILLION": ["Billion", "Billions", "Reported scale"],
        },
        "sign": {
            "POSITIVE": ["Positive", "As presented", "Reported sign"],
            "NEGATIVE": ["Negative", "Parentheses", "Reported sign"],
        },
    },
}
M2_POLICY_BYTES = canonical_json_bytes(_M2_POLICY_PAYLOAD)
M2_POLICY_SHA256 = sha256_hex(M2_POLICY_BYTES)
M2_REGISTRIES_SHA256 = sha256_hex(
    canonical_json_bytes(_M2_POLICY_PAYLOAD["registries"])
)
M2_REPLAY_SCHEMA_VERSION = "finauditgate.replay/v2"


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


M2_POLICY = _deep_freeze(_M2_POLICY_PAYLOAD)


class M2ValidationFailure(ValueError):
    """A deterministic M2 disposition with stable public reason codes."""

    def __init__(self, decision: Decision, *reason_codes: str) -> None:
        super().__init__(", ".join(reason_codes))
        self.decision = decision
        self.reason_codes = tuple(reason_codes)


def failed_artifacts(
    decision: Decision,
    reason_codes: tuple[str, ...],
) -> tuple[dict[str, object], dict[str, object]]:
    """Canonical M2 artifacts for a calculation that was not admissible."""

    return (
        {
            "schema_version": "finauditgate.ledger/v2",
            "nodes": [],
            "status": "REJECTED",
            "decision": decision.value,
            "reason_codes": list(reason_codes),
        },
        {
            "schema_version": "finauditgate.formula/v2",
            "status": "NOT_EXECUTED",
            "decision": decision.value,
            "reason_codes": list(reason_codes),
        },
    )


def evaluate_accepted_candidate(
    document: bytes,
    candidate: ModelCandidate,
    task: AuditTask,
) -> tuple[dict[str, object], dict[str, object]]:
    """Validate the first M2 ACCEPT slice and execute its frozen formula."""

    if sha256_hex(document) != M2_DOCUMENT_SHA256:
        raise M2ValidationFailure(
            Decision.HUMAN_REVIEW,
            "SOURCE_PROFILE_CONFLICT",
        )
    if task.document.source_id != M2_POLICY["source_id"]:
        raise M2ValidationFailure(
            Decision.HUMAN_REVIEW,
            "SOURCE_PROFILE_CONFLICT",
        )
    if task.question != M2_POLICY["task_question"]:
        raise M2ValidationFailure(
            Decision.HUMAN_REVIEW,
            "QUESTION_PROFILE_CONFLICT",
        )
    if task.mode != M2_POLICY["accepted_mode"]:
        raise M2ValidationFailure(
            Decision.HUMAN_REVIEW,
            "MODE_CONFLICT",
        )
    if task.risk_class != M2_POLICY["accepted_risk_class"]:
        raise M2ValidationFailure(
            Decision.HUMAN_REVIEW,
            "RISK_CLASS_REQUIRES_HUMAN_REVIEW",
        )
    if task.document.declared_published_at > task.cutoff:
        raise M2ValidationFailure(
            Decision.HUMAN_REVIEW,
            "POST_CUTOFF_DOCUMENT",
        )
    calculation = candidate.calculation
    if type(calculation.operand_ids) is not tuple or any(
        type(value) is not str or not value.strip()
        for value in (
            calculation.operation,
            calculation.output_unit,
            calculation.quantize,
        )
    ):
        raise M2ValidationFailure(
            Decision.RETRY,
            "CANDIDATE_SHAPE_INVALID",
        )
    if calculation.operation != M2_POLICY["operation"]:
        raise M2ValidationFailure(
            Decision.ABSTAIN,
            "FORMULA_NOT_ALLOWLISTED",
        )
    if len(candidate.evidence) < 2:
        raise M2ValidationFailure(
            Decision.RETRY,
            "MISSING_EVIDENCE",
        )
    if len(candidate.evidence) > 2:
        raise M2ValidationFailure(
            Decision.ABSTAIN,
            "EVIDENCE_SHAPE_NOT_ALLOWLISTED",
        )

    nodes: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for evidence in candidate.evidence:
        if (
            type(evidence.evidence_id) is not str
            or not evidence.evidence_id.strip()
            or evidence.evidence_id in seen_ids
        ):
            raise M2ValidationFailure(
                Decision.RETRY,
                "CANDIDATE_SHAPE_INVALID",
            )
        seen_ids.add(evidence.evidence_id)
        semantic_claims = (
            evidence.metric,
            evidence.metric_basis,
            evidence.period,
            evidence.value,
            evidence.currency,
            evidence.unit,
            evidence.scale,
            evidence.sign,
        )
        if any(
            type(value) is not str or not value.strip()
            for value in semantic_claims
        ):
            raise M2ValidationFailure(
                Decision.RETRY,
                "CANDIDATE_SHAPE_INVALID",
            )
        if (
            type(evidence.byte_start) is not int
            or type(evidence.byte_end) is not int
            or not (
                0
                <= evidence.byte_start
                < evidence.byte_end
                <= len(document)
            )
        ):
            raise M2ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_LOCATOR_INVALID",
            )
        span = document[evidence.byte_start : evidence.byte_end]
        try:
            record = _parse_record(span.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise M2ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_LOCATOR_INVALID",
            ) from exc
        claims = {
            "metric": evidence.metric,
            "basis": evidence.metric_basis,
            "period": evidence.period,
            "value": evidence.value,
            "currency": evidence.currency,
            "unit": evidence.unit,
            "scale": evidence.scale,
            "sign": evidence.sign,
        }
        if record != claims:
            if (
                record.get("value") != claims.get("value")
                and {
                    key: value
                    for key, value in record.items()
                    if key != "value"
                }
                == {
                    key: value
                    for key, value in claims.items()
                    if key != "value"
                }
            ):
                raise M2ValidationFailure(
                    Decision.RETRY,
                    "CLAIMED_VALUE_MISMATCH",
                )
            raise M2ValidationFailure(
                Decision.RETRY,
                "CLAIMED_EVIDENCE_MISMATCH",
            )
        try:
            value = Decimal(record["value"])
        except InvalidOperation as exc:
            raise M2ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_VALUE_INVALID",
            ) from exc
        if not value.is_finite():
            raise M2ValidationFailure(
                Decision.RETRY,
                "EVIDENCE_VALUE_INVALID",
            )
        normalized = {
            "metric": _resolve("metric", record["metric"]),
            "metric_basis": _resolve("metric_basis", record["basis"]),
            "fiscal_period": _resolve("fiscal_period", record["period"]),
            "currency": _resolve("currency", record["currency"]),
            "unit": _resolve("unit", record["unit"]),
            "scale": _resolve("scale", record["scale"]),
            "sign": _resolve("sign", record["sign"]),
        }
        nodes.append(
            {
                "evidence_id": evidence.evidence_id,
                "raw_semantics": record,
                "normalized_semantics": normalized,
                "value": record["value"],
                "locator": {
                    "byte_start": evidence.byte_start,
                    "byte_end": evidence.byte_end,
                    "span_sha256": sha256_hex(span),
                },
                "verification": "VERIFIED_FROM_FROZEN_BYTES",
            }
        )

    if calculation.output_unit != M2_POLICY["output_unit"]:
        raise M2ValidationFailure(
            Decision.HUMAN_REVIEW,
            "OUTPUT_UNIT_CONFLICT",
        )
    if calculation.quantize != M2_POLICY["quantize"]:
        raise M2ValidationFailure(
            Decision.HUMAN_REVIEW,
            "QUANTIZATION_CONFLICT",
        )
    if (
        len(calculation.operand_ids) != 2
        or calculation.operand_ids[0] == calculation.operand_ids[1]
        or any(
            type(item) is not str or not item.strip()
            for item in calculation.operand_ids
        )
    ):
        raise M2ValidationFailure(
            Decision.RETRY,
            "OPERAND_LINEAGE_INVALID",
        )
    by_id = {node["evidence_id"]: node for node in nodes}
    try:
        current = by_id[calculation.operand_ids[0]]
        prior = by_id[calculation.operand_ids[1]]
    except KeyError as exc:
        raise M2ValidationFailure(
            Decision.RETRY,
            "OPERAND_LINEAGE_INVALID",
        ) from exc
    expected = M2_POLICY["expected"]
    conflict_codes = {
        "metric": "METRIC_CONFLICT",
        "metric_basis": "METRIC_BASIS_CONFLICT",
        "currency": "CURRENCY_CONFLICT",
        "unit": "UNIT_CONFLICT",
        "scale": "SCALE_CONFLICT",
        "sign": "SIGN_CONFLICT",
    }
    for node in (current, prior):
        semantics = node["normalized_semantics"]
        for name in (
            "metric",
            "metric_basis",
            "currency",
            "unit",
            "scale",
            "sign",
        ):
            if semantics[name] != expected[name]:
                raise M2ValidationFailure(
                    Decision.HUMAN_REVIEW,
                    conflict_codes[name],
                )
    if (
        current["normalized_semantics"]["fiscal_period"]
        != expected["current_period"]
        or prior["normalized_semantics"]["fiscal_period"]
        != expected["comparison_period"]
    ):
        raise M2ValidationFailure(
            Decision.HUMAN_REVIEW,
            "FISCAL_PERIOD_CONFLICT",
        )

    current_value = Decimal(current["value"])
    prior_value = Decimal(prior["value"])
    if prior_value == 0:
        raise M2ValidationFailure(
            Decision.ABSTAIN,
            "FORMULA_DOMAIN_ERROR",
        )
    context = Context(
        prec=M2_POLICY["precision"],
        rounding=ROUND_HALF_EVEN,
        Emin=M2_POLICY["emin"],
        Emax=M2_POLICY["emax"],
        capitals=M2_POLICY["capitals"],
        clamp=M2_POLICY["clamp"],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
    with localcontext(context):
        result = (
            (current_value - prior_value) / prior_value * Decimal("100")
        ).quantize(Decimal(M2_POLICY["quantize"]), rounding=ROUND_HALF_EVEN)

    ledger = {
        "schema_version": "finauditgate.ledger/v2",
        "semantic_registry_sha256": M2_REGISTRIES_SHA256,
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
                "evidence_id": prior["evidence_id"],
                "period": prior["normalized_semantics"]["fiscal_period"],
                "value": prior["value"],
            },
        ],
        "input_currency": current["normalized_semantics"]["currency"],
        "input_unit": current["normalized_semantics"]["unit"],
        "input_scale": current["normalized_semantics"]["scale"],
        "output_unit": calculation.output_unit,
        "quantize": calculation.quantize,
        "rounding": M2_POLICY["rounding"],
        "calculation_policy_sha256": M2_POLICY_SHA256,
        "result": format(result, "f"),
    }
    return ledger, formula


def _parse_record(record: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for field in record.split(";"):
        key, separator, value = field.partition("=")
        if separator != "=" or not key or not value or key in parsed:
            raise ValueError("invalid M2 synthetic evidence record")
        parsed[key] = value
    if set(parsed) != {
        "metric",
        "basis",
        "period",
        "value",
        "currency",
        "unit",
        "scale",
        "sign",
    }:
        raise ValueError("M2 synthetic evidence record has unexpected fields")
    return parsed


def _resolve(registry_name: str, label: str) -> str:
    normalized_label = _normalize_label(label)
    registry: dict[str, list[str]] = M2_POLICY["registries"][registry_name]
    matches = [
        canonical
        for canonical, aliases in registry.items()
        if normalized_label in {_normalize_label(alias) for alias in aliases}
    ]
    reason_prefix = {
        "fiscal_period": "FISCAL_PERIOD",
        "metric": "METRIC",
        "metric_basis": "METRIC_BASIS",
        "currency": "CURRENCY",
        "unit": "UNIT",
        "scale": "SCALE",
        "sign": "SIGN",
    }[registry_name]
    if not matches:
        raise M2ValidationFailure(
            Decision.HUMAN_REVIEW,
            f"{reason_prefix}_UNRESOLVED",
        )
    if len(matches) > 1:
        raise M2ValidationFailure(
            Decision.HUMAN_REVIEW,
            f"{reason_prefix}_AMBIGUOUS",
        )
    return matches[0]


def _normalize_label(label: str) -> str:
    if type(label) is not str or not label.strip():
        raise TypeError("semantic label must be a non-empty string")
    return re.sub(r"\s+", " ", label.strip().casefold())
