"""Bounded canonical snapshots of untrusted model proposals.

Before validation, every proposal an Adapter returns is converted into a
canonical JSON envelope with fixed size limits.  Well-formed `ModelCandidate`
values round-trip exactly; anything else (oversized text, cycles, unsupported
types) is replaced by a stable descriptor with a fingerprint, so the attempt is
still replayable and distinct malformed proposals keep distinct identities.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re

from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.ports.model import (
    CalculationCandidate,
    EvidenceCandidate,
    ModelCandidate,
)


PROPOSAL_SNAPSHOT_SCHEMA_VERSION = "finauditgate.model-proposal-snapshot/v1"
CANDIDATE_SCHEMA_VERSION = "finauditgate.candidate/v2"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_MAX_SNAPSHOT_DEPTH = 32
_MAX_SNAPSHOT_NODES = 256
_MAX_CONTAINER_ITEMS = 64
_MAX_INTEGER_BITS = 4096
_MAX_TEXT_LENGTH = 4_096
_MAX_BYTES_LENGTH = 4_096
_FAILURE_REASONS = frozenset(
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
_EVIDENCE_FIELDS = (
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
)
_CALCULATION_FIELDS = ("operation", "operand_ids", "output_unit", "quantize")


@dataclass(slots=True)
class _SnapshotState:
    active_ids: set[int]
    nodes: int = 0


@dataclass(slots=True)
class _ReplayState:
    nodes: int = 0


def proposal_snapshot(proposal: object) -> dict[str, object]:
    """Serialize an untrusted proposal into a bounded canonical envelope."""

    state = _SnapshotState(active_ids=set())
    try:
        value_snapshot = _snapshot_value(proposal, state, depth=0)
    except (
        AttributeError,
        LookupError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
    ) as exc:
        value_snapshot = _failure_snapshot(
            "SNAPSHOT_FAILED",
            {
                "exception": _exception_tag(exc),
                "value_type": _type_tag(proposal),
            },
        )
    return {
        "schema_version": PROPOSAL_SNAPSHOT_SCHEMA_VERSION,
        "value": value_snapshot,
    }


def candidate_from_proposal_snapshot(
    snapshot: dict[str, object],
) -> tuple[dict[str, object], ModelCandidate]:
    """Rebuild the canonical candidate payload and value from one snapshot."""

    if (
        set(snapshot) != {"schema_version", "value"}
        or snapshot["schema_version"] != PROPOSAL_SNAPSHOT_SCHEMA_VERSION
    ):
        raise ValueError("unsupported proposal snapshot schema")
    proposal = _value_from_snapshot(snapshot["value"])
    payload = candidate_payload(proposal)
    candidate_bytes = canonical_json_bytes(payload)
    normalized_payload = json.loads(candidate_bytes)
    if not isinstance(normalized_payload, dict):
        raise TypeError("canonical candidate must be an object")
    candidate = candidate_from_payload(normalized_payload)
    if canonical_json_bytes(candidate_payload(candidate)) != candidate_bytes:
        raise ValueError("candidate is not canonically representable")
    return normalized_payload, candidate


def candidate_payload(candidate: ModelCandidate) -> dict[str, object]:
    """The persisted `candidate.json` form; every semantic field is present."""

    evidence_payloads = [
        {name: getattr(item, name) for name in _EVIDENCE_FIELDS}
        for item in tuple(candidate.evidence)
    ]
    raw_operand_ids = candidate.calculation.operand_ids
    serialized_operand_ids = (
        list(raw_operand_ids)
        if type(raw_operand_ids) is tuple
        else raw_operand_ids
    )
    return {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "evidence": evidence_payloads,
        "calculation": {
            **asdict(candidate.calculation),
            "operand_ids": serialized_operand_ids,
        },
    }


def candidate_from_payload(payload: dict[str, object]) -> ModelCandidate:
    if payload.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
        raise ValueError("unsupported candidate schema")
    if set(payload) != {"schema_version", "evidence", "calculation"}:
        raise ValueError("candidate payload fields do not match the schema")
    raw_evidence = payload["evidence"]
    raw_calculation = payload["calculation"]
    if not isinstance(raw_evidence, list) or not isinstance(
        raw_calculation, dict
    ):
        raise TypeError("invalid candidate payload")
    if any(
        not isinstance(item, dict) or set(item) != set(_EVIDENCE_FIELDS)
        for item in raw_evidence
    ):
        raise ValueError("evidence candidate fields do not match the schema")
    if set(raw_calculation) != set(_CALCULATION_FIELDS):
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


def _snapshot_value(
    value: object,
    state: _SnapshotState,
    *,
    depth: int,
) -> dict[str, object]:
    value_type = type(value)
    state.nodes += 1
    if state.nodes > _MAX_SNAPSHOT_NODES:
        return _failure_snapshot(
            "NODE_LIMIT",
            {"nodes": state.nodes, "value_type": _type_tag(value)},
        )
    if depth > _MAX_SNAPSHOT_DEPTH:
        return _failure_snapshot(
            "DEPTH_LIMIT",
            {"depth": depth, "value_type": _type_tag(value)},
        )
    if value is None:
        return {"kind": "none"}
    if value_type is bool:
        return {"kind": "bool", "value": value}
    if value_type is int:
        bit_length = value.bit_length()
        if bit_length > _MAX_INTEGER_BITS:
            return _failure_snapshot(
                "INTEGER_LIMIT",
                {
                    "bit_length": bit_length,
                    "magnitude_sha256": _oversized_integer_fingerprint(value),
                    "negative": value < 0,
                },
            )
        return {"kind": "int", "value": value}
    if value_type is str:
        if len(value) > _MAX_TEXT_LENGTH:
            return _failure_snapshot(
                "TEXT_LIMIT",
                {"length": len(value), "value_sha256": _text_fingerprint(value)},
            )
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return _failure_snapshot(
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
        if len(value) > _MAX_BYTES_LENGTH:
            return _failure_snapshot(
                "BYTES_LIMIT",
                {"length": len(value), "value_sha256": sha256_hex(value)},
            )
        return {"kind": "bytes", "hex": value.hex()}

    value_id = id(value)
    if value_id in state.active_ids:
        return _failure_snapshot(
            "CYCLIC_VALUE",
            {"depth": depth, "value_type": _type_tag(value)},
        )
    state.active_ids.add(value_id)
    try:
        if value_type is ModelCandidate:
            return {
                "kind": "model_candidate",
                "evidence": _snapshot_value(value.evidence, state, depth=depth + 1),
                "calculation": _snapshot_value(
                    value.calculation, state, depth=depth + 1
                ),
            }
        if value_type is EvidenceCandidate:
            return {
                "kind": "evidence_candidate",
                **{
                    name: _snapshot_value(
                        getattr(value, name), state, depth=depth + 1
                    )
                    for name in _EVIDENCE_FIELDS
                },
            }
        if value_type is CalculationCandidate:
            return {
                "kind": "calculation_candidate",
                **{
                    name: _snapshot_value(
                        getattr(value, name), state, depth=depth + 1
                    )
                    for name in _CALCULATION_FIELDS
                },
            }
        if value_type in {tuple, list}:
            if len(value) > _MAX_CONTAINER_ITEMS:
                return _failure_snapshot(
                    "CONTAINER_LIMIT",
                    {"length": len(value), "value_type": _type_tag(value)},
                )
            return {
                "kind": "tuple" if value_type is tuple else "list",
                "items": [
                    _snapshot_value(item, state, depth=depth + 1)
                    for item in value
                ],
            }
        if value_type is dict:
            if len(value) > _MAX_CONTAINER_ITEMS:
                return _failure_snapshot(
                    "CONTAINER_LIMIT",
                    {"length": len(value), "value_type": "dict"},
                )
            if not all(type(key) is str for key in value):
                return _failure_snapshot(
                    "UNSUPPORTED_TYPE",
                    {
                        "key_types": sorted(_type_tag(key) for key in value),
                        "value_type": "dict",
                    },
                )
            oversized_keys = [key for key in value if len(key) > _MAX_TEXT_LENGTH]
            if oversized_keys:
                return _failure_snapshot(
                    "TEXT_LIMIT",
                    {
                        "key_fingerprints": sorted(
                            _text_fingerprint(key) for key in oversized_keys
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
                        sha256_hex(key.encode("utf-8", errors="surrogatepass"))
                    )
            if invalid_keys:
                return _failure_snapshot(
                    "TEXT_ENCODING_INVALID",
                    {"key_fingerprints": sorted(invalid_keys), "value_type": "dict_key"},
                )
            return {
                "kind": "dict",
                "items": [
                    {
                        "key": key,
                        "value": _snapshot_value(
                            value[key], state, depth=depth + 1
                        ),
                    }
                    for key in sorted(value)
                ],
            }
        return _failure_snapshot(
            "UNSUPPORTED_TYPE",
            {"value_type": _type_tag(value)},
        )
    finally:
        state.active_ids.remove(value_id)


def _failure_snapshot(reason: str, details: dict[str, object]) -> dict[str, object]:
    fingerprint = sha256_hex(
        canonical_json_bytes({"reason": reason, "details": details})
    )
    return {"kind": "unsupported", "reason": reason, "fingerprint": fingerprint}


def _text_fingerprint(value: str) -> str:
    digest = hashlib.sha256()
    for start in range(0, len(value), 1_024):
        digest.update(
            value[start : start + 1_024].encode("utf-8", errors="surrogatepass")
        )
    return digest.hexdigest()


def _oversized_integer_fingerprint(value: int) -> str:
    digest = hashlib.sha256()
    digest.update(b"negative" if value < 0 else b"nonnegative")
    magnitude = abs(value)
    byte_length = (magnitude.bit_length() + 7) // 8
    digest.update(magnitude.to_bytes(byte_length, byteorder="big"))
    return digest.hexdigest()


def _type_tag(value: object) -> str:
    value_type = type(value)
    if value is None:
        return "none"
    for candidate_type, tag in (
        (bool, "bool"),
        (int, "int"),
        (str, "str"),
        (float, "float"),
        (bytes, "bytes"),
        (ModelCandidate, "model_candidate"),
        (EvidenceCandidate, "evidence_candidate"),
        (CalculationCandidate, "calculation_candidate"),
        (tuple, "tuple"),
        (list, "list"),
        (dict, "dict"),
    ):
        if value_type is candidate_type:
            return tag
    return "unsupported"


def _exception_tag(exc: BaseException) -> str:
    for exception_type, tag in (
        (AttributeError, "attribute_error"),
        (KeyError, "key_error"),
        (IndexError, "index_error"),
        (OverflowError, "overflow_error"),
        (RecursionError, "recursion_error"),
        (TypeError, "type_error"),
        (ValueError, "value_error"),
    ):
        if type(exc) is exception_type:
            return tag
    return "lookup_error"


def _value_from_snapshot(
    snapshot: object,
    state: _ReplayState | None = None,
    *,
    depth: int = 0,
) -> object:
    if state is None:
        state = _ReplayState()
    state.nodes += 1
    if state.nodes > _MAX_SNAPSHOT_NODES or depth > _MAX_SNAPSHOT_DEPTH:
        raise ValueError("proposal snapshot exceeds structural limits")
    if not isinstance(snapshot, dict) or type(snapshot.get("kind")) is not str:
        raise ValueError("proposal snapshot value is malformed")
    kind = snapshot["kind"]
    keys = set(snapshot)
    if kind == "none" and keys == {"kind"}:
        return None
    if kind == "bool" and keys == {"kind", "value"}:
        if type(snapshot["value"]) is not bool:
            raise TypeError("proposal bool snapshot is malformed")
        return snapshot["value"]
    if kind == "int" and keys == {"kind", "value"}:
        if (
            type(snapshot["value"]) is not int
            or snapshot["value"].bit_length() > _MAX_INTEGER_BITS
        ):
            raise TypeError("proposal int snapshot is malformed")
        return snapshot["value"]
    if kind == "str" and keys == {"kind", "value"}:
        if (
            type(snapshot["value"]) is not str
            or len(snapshot["value"]) > _MAX_TEXT_LENGTH
        ):
            raise TypeError("proposal string snapshot is malformed")
        return snapshot["value"]
    if kind == "float" and keys == {"kind", "hex"}:
        if type(snapshot["hex"]) is not str or len(snapshot["hex"]) > 32:
            raise TypeError("proposal float snapshot is malformed")
        return float.fromhex(snapshot["hex"])
    if kind == "bytes" and keys == {"kind", "hex"}:
        if (
            type(snapshot["hex"]) is not str
            or len(snapshot["hex"]) > _MAX_BYTES_LENGTH * 2
        ):
            raise TypeError("proposal bytes snapshot is malformed")
        return bytes.fromhex(snapshot["hex"])
    if kind in {"tuple", "list"} and keys == {"kind", "items"}:
        items = snapshot["items"]
        if not isinstance(items, list) or len(items) > _MAX_CONTAINER_ITEMS:
            raise TypeError("proposal sequence snapshot is malformed")
        values = [
            _value_from_snapshot(item, state, depth=depth + 1) for item in items
        ]
        return tuple(values) if kind == "tuple" else values
    if kind == "dict" and keys == {"kind", "items"}:
        items = snapshot["items"]
        if not isinstance(items, list) or len(items) > _MAX_CONTAINER_ITEMS:
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
            result[item["key"]] = _value_from_snapshot(
                item["value"], state, depth=depth + 1
            )
            previous_key = item["key"]
        return result
    if kind == "model_candidate" and keys == {"kind", "evidence", "calculation"}:
        return ModelCandidate(
            evidence=_value_from_snapshot(snapshot["evidence"], state, depth=depth + 1),
            calculation=_value_from_snapshot(
                snapshot["calculation"], state, depth=depth + 1
            ),
        )
    if kind == "evidence_candidate" and keys == {"kind", *_EVIDENCE_FIELDS}:
        return EvidenceCandidate(
            **{
                name: _value_from_snapshot(snapshot[name], state, depth=depth + 1)
                for name in _EVIDENCE_FIELDS
            }
        )
    if kind == "calculation_candidate" and keys == {"kind", *_CALCULATION_FIELDS}:
        return CalculationCandidate(
            **{
                name: _value_from_snapshot(snapshot[name], state, depth=depth + 1)
                for name in _CALCULATION_FIELDS
            }
        )
    if kind == "unsupported" and keys == {"kind", "reason", "fingerprint"}:
        if (
            snapshot["reason"] not in _FAILURE_REASONS
            or type(snapshot["fingerprint"]) is not str
            or not _SHA256_HEX.fullmatch(snapshot["fingerprint"])
        ):
            raise ValueError("unsupported proposal reason is malformed")
        raise ValueError("proposal contains an unsupported value")
    raise ValueError("proposal snapshot value is malformed")
