"""Single schema/codec source for the local model's candidate tool.

The tool schema is closed.  Evidence ids, metric, basis, unit, scale and sign
are enumerations; `value` is a plain decimal string; `period` follows one
format; `exact_span` is one complete document line.  The model learns this
vocabulary from the schema itself and the decoder rejects anything outside it,
so a proposal that reaches the gate already speaks the vocabulary the reviewed
profiles and the synthetic registries are written in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from finauditgate.ports.model import (
    CalculationCandidate,
    EvidenceCandidate,
    ModelCandidate,
)


TOOL_NAME = "propose_financial_candidate"

EVIDENCE_IDS = ("current", "comparison")
METRICS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "profit_for_the_year",
    "profit_attributable_to_equity_holders",
    "basic_eps",
    "diluted_eps",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "sales_volume",
    "other",
)
METRIC_BASES = ("REPORTED", "ADJUSTED")
UNITS = ("MONETARY", "PER_SHARE", "COUNT", "PERCENT")
SCALES = ("UNIT", "THOUSAND", "MILLION", "BILLION")
SIGNS = ("POSITIVE", "NEGATIVE")


class ToolContractError(ValueError):
    """A closed tool argument failed the shared schema/codec contract."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _StringRule:
    error_code: str
    const: str | None = None
    enum: tuple[str, ...] | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class _ArrayRule:
    item: "_Rule"
    length: int
    error_code: str


@dataclass(frozen=True, slots=True)
class _ObjectRule:
    fields: tuple[tuple[str, "_Rule"], ...]
    error_code: str


_Rule: TypeAlias = _StringRule | _ArrayRule | _ObjectRule

_NOT_ALLOWLISTED = "TOOL_ARGUMENT_NOT_ALLOWLISTED"


def _string(
    error_code: str,
    *,
    const: str | None = None,
    enum: tuple[str, ...] | None = None,
    description: str | None = None,
) -> _StringRule:
    return _StringRule(
        error_code=error_code,
        const=const,
        enum=enum,
        description=description,
    )


_EVIDENCE_ERROR = "EVIDENCE_ARGUMENT_SHAPE_INVALID"
_CALCULATION_ERROR = "CALCULATION_ARGUMENT_SHAPE_INVALID"
_EVIDENCE_RULE = _ObjectRule(
    fields=(
        (
            "evidence_id",
            _string(
                _EVIDENCE_ERROR,
                enum=EVIDENCE_IDS,
                description=(
                    "current for the later period named in the question; "
                    "comparison for the earlier period."
                ),
            ),
        ),
        (
            "exact_span",
            _string(
                _EVIDENCE_ERROR,
                description=(
                    "The cited number exactly as printed (751,766), or the "
                    "complete document line that contains it, copied "
                    "byte-for-byte. The span must occur exactly once in the "
                    "document, so copy the complete line when the number "
                    "alone repeats."
                ),
            ),
        ),
        (
            "metric",
            _string(
                _EVIDENCE_ERROR,
                enum=METRICS,
                description=(
                    "The financial line item; other when none of the names "
                    "fits."
                ),
            ),
        ),
        (
            "metric_basis",
            _string(
                _EVIDENCE_ERROR,
                enum=METRIC_BASES,
                description=(
                    "REPORTED for IFRS, GAAP or as-reported figures; "
                    "ADJUSTED for non-IFRS, non-GAAP or adjusted figures."
                ),
            ),
        ),
        (
            "period",
            _string(
                _EVIDENCE_ERROR,
                description=(
                    "FYyyyy for a full fiscal-year flow (FY2025 for the year "
                    "ended 31 December 2025); yyyy-mm-dd for a balance as at "
                    "a date (2025-12-31)."
                ),
            ),
        ),
        (
            "value",
            _string(
                _EVIDENCE_ERROR,
                description=(
                    "The cited number as a plain decimal string: digits, an "
                    "optional leading minus and an optional decimal point; no "
                    "thousands separators, currency symbols or spaces "
                    "(751,766 becomes 751766; (1,234) becomes -1234)."
                ),
            ),
        ),
        (
            "currency",
            _string(
                _EVIDENCE_ERROR,
                description=(
                    "The currency abbreviation printed in the document, for "
                    "example RMB, USD, HKD or EUR."
                ),
            ),
        ),
        (
            "unit",
            _string(
                _EVIDENCE_ERROR,
                enum=UNITS,
                description=(
                    "MONETARY for currency amounts, PER_SHARE for per-share "
                    "amounts, COUNT for share or unit counts, PERCENT for "
                    "percentages."
                ),
            ),
        ),
        (
            "scale",
            _string(
                _EVIDENCE_ERROR,
                enum=SCALES,
                description=(
                    "The scale stated in the table heading (RMB million is "
                    "MILLION); per-share amounts and unscaled figures are "
                    "UNIT."
                ),
            ),
        ),
        (
            "sign",
            _string(
                _EVIDENCE_ERROR,
                enum=SIGNS,
                description=(
                    "NEGATIVE when the number is printed with a minus sign "
                    "or in parentheses."
                ),
            ),
        ),
    ),
    error_code=_EVIDENCE_ERROR,
)
_CALCULATION_RULE = _ObjectRule(
    fields=(
        (
            "operation",
            _string(_CALCULATION_ERROR, const="growth_rate_percent"),
        ),
        (
            "operand_ids",
            _ArrayRule(
                item=_string(_CALCULATION_ERROR, enum=EVIDENCE_IDS),
                length=2,
                error_code=_CALCULATION_ERROR,
            ),
        ),
        (
            "output_unit",
            _string(_CALCULATION_ERROR, const="PERCENT"),
        ),
        (
            "quantize",
            _string(_CALCULATION_ERROR, const="0.01"),
        ),
    ),
    error_code=_CALCULATION_ERROR,
)
_ARGUMENT_RULE = _ObjectRule(
    fields=(
        (
            "evidence",
            _ArrayRule(
                item=_EVIDENCE_RULE,
                length=2,
                error_code=_EVIDENCE_ERROR,
            ),
        ),
        ("calculation", _CALCULATION_RULE),
    ),
    error_code="TOOL_ARGUMENT_SHAPE_INVALID",
)


class CandidateToolContract:
    """Generate the JSON Schema and decode with the same immutable rules."""

    def tool_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": (
                    "Propose two unverified evidence spans and one "
                    "allowlisted financial calculation. The deterministic "
                    "gate verifies them."
                ),
                "parameters": _json_schema(_ARGUMENT_RULE),
            },
        }

    def decode(
        self,
        arguments: object,
        document: bytes,
    ) -> ModelCandidate:
        if type(document) is not bytes:
            raise TypeError("document must be immutable bytes")
        decoded = _decode(_ARGUMENT_RULE, arguments)
        raw_evidence = decoded["evidence"]
        evidence = tuple(
            _evidence_candidate(item, document) for item in raw_evidence
        )
        calculation = decoded["calculation"]
        return ModelCandidate(
            evidence=evidence,
            calculation=CalculationCandidate(
                operation=calculation["operation"],
                operand_ids=tuple(calculation["operand_ids"]),
                output_unit=calculation["output_unit"],
                quantize=calculation["quantize"],
            ),
        )

    def encode(
        self,
        candidate: ModelCandidate,
        document: bytes,
    ) -> dict[str, object]:
        """Reconstruct the one canonical tool argument from a candidate.

        This inverse is used by the offline trace verifier.  A proposal that
        cannot round-trip to the exact frozen document span is not the same
        proposal as the model response.
        """

        if type(candidate) is not ModelCandidate:
            raise ToolContractError("TOOL_ARGUMENT_SHAPE_INVALID")
        if type(document) is not bytes:
            raise TypeError("document must be immutable bytes")
        evidence_payloads: list[dict[str, object]] = []
        for evidence in candidate.evidence:
            if type(evidence) is not EvidenceCandidate or not (
                type(evidence.byte_start) is int
                and type(evidence.byte_end) is int
                and 0 <= evidence.byte_start < evidence.byte_end <= len(document)
            ):
                raise ToolContractError("EVIDENCE_ARGUMENT_SHAPE_INVALID")
            try:
                exact_span = document[
                    evidence.byte_start : evidence.byte_end
                ].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolContractError(
                    "EVIDENCE_ARGUMENT_SHAPE_INVALID"
                ) from exc
            evidence_payloads.append(
                {
                    "evidence_id": evidence.evidence_id,
                    "exact_span": exact_span,
                    "metric": evidence.metric,
                    "metric_basis": evidence.metric_basis,
                    "period": evidence.period,
                    "value": evidence.value,
                    "currency": evidence.currency,
                    "unit": evidence.unit,
                    "scale": evidence.scale,
                    "sign": evidence.sign,
                }
            )
        arguments: dict[str, object] = {
            "evidence": evidence_payloads,
            "calculation": {
                "operation": candidate.calculation.operation,
                "operand_ids": list(candidate.calculation.operand_ids),
                "output_unit": candidate.calculation.output_unit,
                "quantize": candidate.calculation.quantize,
            },
        }
        decoded = self.decode(arguments, document)
        if decoded != candidate:
            raise ToolContractError("TOOL_ARGUMENT_CANDIDATE_MISMATCH")
        return arguments


def _json_schema(rule: _Rule) -> dict[str, object]:
    if isinstance(rule, _StringRule):
        schema: dict[str, object] = (
            {"const": rule.const}
            if rule.const is not None
            else {"type": "string"}
        )
        if rule.enum is not None:
            schema["enum"] = list(rule.enum)
        if rule.description is not None:
            schema["description"] = rule.description
        return schema
    if isinstance(rule, _ArrayRule):
        return {
            "type": "array",
            "minItems": rule.length,
            "maxItems": rule.length,
            "items": _json_schema(rule.item),
        }
    properties = {
        name: _json_schema(field_rule)
        for name, field_rule in rule.fields
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [name for name, _ in rule.fields],
        "properties": properties,
    }


def _decode(rule: _Rule, value: object) -> object:
    if isinstance(rule, _StringRule):
        if type(value) is not str or not value.strip():
            raise ToolContractError(rule.error_code)
        if rule.const is not None and value != rule.const:
            raise ToolContractError(_NOT_ALLOWLISTED)
        if rule.enum is not None and value not in rule.enum:
            raise ToolContractError(_NOT_ALLOWLISTED)
        return value
    if isinstance(rule, _ArrayRule):
        if type(value) is not list or len(value) != rule.length:
            raise ToolContractError(rule.error_code)
        return [_decode(rule.item, item) for item in value]
    if type(value) is not dict:
        raise ToolContractError(rule.error_code)
    expected_fields = {name for name, _ in rule.fields}
    if set(value) != expected_fields:
        raise ToolContractError(rule.error_code)
    return {
        name: _decode(field_rule, value[name])
        for name, field_rule in rule.fields
    }


def _evidence_candidate(
    raw_evidence: dict[str, str],
    document: bytes,
) -> EvidenceCandidate:
    try:
        exact_span = raw_evidence["exact_span"].encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolContractError(_EVIDENCE_ERROR) from exc
    byte_start = document.find(exact_span)
    if byte_start < 0 or document.find(exact_span, byte_start + 1) >= 0:
        raise ToolContractError("EVIDENCE_SPAN_NOT_UNIQUE")
    return EvidenceCandidate(
        evidence_id=raw_evidence["evidence_id"],
        byte_start=byte_start,
        byte_end=byte_start + len(exact_span),
        metric=raw_evidence["metric"],
        metric_basis=raw_evidence["metric_basis"],
        period=raw_evidence["period"],
        value=raw_evidence["value"],
        currency=raw_evidence["currency"],
        unit=raw_evidence["unit"],
        scale=raw_evidence["scale"],
        sign=raw_evidence["sign"],
    )


CANDIDATE_TOOL_CONTRACT = CandidateToolContract()
