"""Frozen, content-addressed validation profiles for private development.

A profile is the reviewed answer key for one question on one frozen private
document.  Three shapes are allowed:

* an acceptable answer: exactly two reviewed evidence spans (CURRENT and
  COMPARISON) plus the one allowlisted growth calculation;
* a post-cutoff document: the same shape, but `declared_published_at` is later
  than `task_cutoff`, so the gate must return `HUMAN_REVIEW`;
* no admissible evidence: an empty `evidence_allowlist` and a null
  `calculation`, so any proposal must return `HUMAN_REVIEW`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from finauditgate.core.operations import (
    ALLOWLISTED_OPERATIONS,
    output_unit_for,
)
from finauditgate.contracts import REVIEWED_PROFILE_MODES
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.private_storage import (
    PrivateWorkspaceAnchor,
    require_private_storage_root,
    resolve_private_workspace_anchor,
)


PROFILE_SCHEMA_VERSION = "finauditgate.private-dev-validation-profile/v2"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_MAX_PROFILE_BYTES = 1_048_576
_PROFILE_FIELDS = {
    "schema_version",
    "validation_profile",
    "source_id",
    "document_name",
    "document_sha256",
    "declared_published_at",
    "task_cutoff",
    "task_question",
    "accepted_mode",
    "accepted_risk_class",
    "evidence_allowlist",
    "calculation",
}
_EVIDENCE_FIELDS = {
    "evidence_id",
    "role",
    "byte_start",
    "byte_end",
    "span_sha256",
    "value",
    "normalized_semantics",
}
_SEMANTIC_FIELDS = {
    "metric",
    "metric_basis",
    "fiscal_period",
    "currency",
    "unit",
    "scale",
    "sign",
}
_CALCULATION_FIELDS = {
    "operation",
    "operand_ids",
    "output_unit",
    "quantize",
    "decimal_context",
}
_DECIMAL_CONTEXT_FIELDS = {
    "precision",
    "rounding",
    "emin",
    "emax",
    "capitals",
    "clamp",
}


@dataclass(frozen=True, slots=True)
class PrivateDevValidationProfile:
    """One exact private source/question/hash and deterministic rule set."""

    policy_bytes: bytes
    sha256: str
    registries_sha256: str
    private_workspace_anchor: PrivateWorkspaceAnchor | None = None

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        private_workspace_anchor: PrivateWorkspaceAnchor | None = None,
    ) -> "PrivateDevValidationProfile":
        if not isinstance(path, Path):
            raise TypeError("profile path must be a pathlib.Path")
        anchor = private_workspace_anchor or resolve_private_workspace_anchor(
            path,
            purpose="private-validation-profile",
        )
        resolved = require_private_storage_root(
            path,
            purpose="private-validation-profile",
            anchor=anchor,
        )
        if resolved.is_symlink() or not resolved.is_file():
            raise ValueError("PRIVATE_VALIDATION_PROFILE_REQUIRED")
        with resolved.open("rb") as profile_file:
            payload = profile_file.read(_MAX_PROFILE_BYTES + 1)
        if len(payload) > _MAX_PROFILE_BYTES:
            raise ValueError("PRIVATE_VALIDATION_PROFILE_TOO_LARGE")
        profile = cls.from_bytes(payload)
        return cls(
            policy_bytes=profile.policy_bytes,
            sha256=profile.sha256,
            registries_sha256=profile.registries_sha256,
            private_workspace_anchor=anchor,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "PrivateDevValidationProfile":
        if type(payload) is not bytes:
            raise TypeError("profile payload must be immutable bytes")
        if not payload or len(payload) > _MAX_PROFILE_BYTES:
            raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
        try:
            parsed = json.loads(payload)
        except (RecursionError, UnicodeError, ValueError) as exc:
            raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID") from exc
        validate_profile_payload(parsed)
        canonical = canonical_json_bytes(parsed)
        if canonical != payload:
            raise ValueError("PRIVATE_VALIDATION_PROFILE_NOT_CANONICAL")
        return cls(
            policy_bytes=canonical,
            sha256=sha256_hex(canonical),
            registries_sha256=sha256_hex(
                canonical_json_bytes(parsed["evidence_allowlist"])
            ),
        )

    @property
    def policy(self) -> dict[str, object]:
        payload = json.loads(self.policy_bytes)
        if not isinstance(payload, dict):
            raise RuntimeError("validated profile could not be reopened")
        return payload


def validate_profile_payload(payload: object) -> None:
    """Raise `ValueError` unless the payload is one closed profile shape."""

    if type(payload) is not dict or set(payload) != _PROFILE_FIELDS:
        raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
    string_fields = {
        "validation_profile",
        "source_id",
        "document_name",
        "document_sha256",
        "declared_published_at",
        "task_cutoff",
        "task_question",
        "accepted_mode",
        "accepted_risk_class",
    }
    if any(
        type(payload[field_name]) is not str
        or not payload[field_name].strip()
        for field_name in string_fields
    ):
        raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
    try:
        declared_published_at = date.fromisoformat(
            payload["declared_published_at"]
        )
        task_cutoff = date.fromisoformat(payload["task_cutoff"])
    except ValueError as exc:
        raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID") from exc
    if (
        payload["schema_version"] != PROFILE_SCHEMA_VERSION
        or not _SHA256_HEX.fullmatch(payload["document_sha256"])
        or Path(payload["document_name"]).name != payload["document_name"]
        or payload["declared_published_at"] != declared_published_at.isoformat()
        or payload["task_cutoff"] != task_cutoff.isoformat()
        or payload["accepted_mode"] not in REVIEWED_PROFILE_MODES
        or payload["accepted_risk_class"] != "LOW"
    ):
        raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
    evidence_allowlist = payload["evidence_allowlist"]
    calculation = payload["calculation"]
    if type(evidence_allowlist) is not list:
        raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
    if not evidence_allowlist:
        if calculation is not None:
            raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
        return
    if len(evidence_allowlist) != 2:
        raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
    seen_ids: set[str] = set()
    seen_roles: set[str] = set()
    role_ids: dict[str, str] = {}
    role_items: dict[str, dict[str, object]] = {}
    for item in evidence_allowlist:
        if type(item) is not dict or set(item) != _EVIDENCE_FIELDS:
            raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
        if (
            type(item["evidence_id"]) is not str
            or not item["evidence_id"].strip()
            or item["evidence_id"] in seen_ids
            or item["role"] not in {"CURRENT", "COMPARISON"}
            or item["role"] in seen_roles
            or type(item["byte_start"]) is not int
            or type(item["byte_end"]) is not int
            or not 0 <= item["byte_start"] < item["byte_end"]
            or type(item["span_sha256"]) is not str
            or not _SHA256_HEX.fullmatch(item["span_sha256"])
            or type(item["value"]) is not str
            or not item["value"].strip()
        ):
            raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
        try:
            value = Decimal(item["value"])
        except InvalidOperation as exc:
            raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID") from exc
        if not value.is_finite():
            raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
        semantics = item["normalized_semantics"]
        if (
            type(semantics) is not dict
            or set(semantics) != _SEMANTIC_FIELDS
            or any(
                type(semantics[name]) is not str or not semantics[name].strip()
                for name in _SEMANTIC_FIELDS
            )
        ):
            raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
        seen_ids.add(item["evidence_id"])
        seen_roles.add(item["role"])
        role_ids[item["role"]] = item["evidence_id"]
        role_items[item["role"]] = item
    if seen_roles != {"CURRENT", "COMPARISON"}:
        raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
    current_semantics = role_items["CURRENT"]["normalized_semantics"]
    comparison_semantics = role_items["COMPARISON"]["normalized_semantics"]
    shared_formula_semantics = {
        "metric",
        "metric_basis",
        "currency",
        "unit",
        "scale",
        "sign",
    }
    if (
        any(
            current_semantics[name] != comparison_semantics[name]
            for name in shared_formula_semantics
        )
        or current_semantics["fiscal_period"]
        == comparison_semantics["fiscal_period"]
    ):
        raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")

    if type(calculation) is not dict or set(calculation) != _CALCULATION_FIELDS:
        raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
    # The answer's unit follows from the operation and from what the operands
    # are denominated in, so the profile cannot declare a combination the
    # calculation would not produce.
    if (
        calculation["operation"] not in ALLOWLISTED_OPERATIONS
        or calculation["output_unit"]
        != output_unit_for(calculation["operation"], current_semantics["unit"])
        or calculation["quantize"] != "0.01"
        or calculation["operand_ids"]
        != [role_ids["CURRENT"], role_ids["COMPARISON"]]
    ):
        raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
    context = calculation["decimal_context"]
    if (
        type(context) is not dict
        or set(context) != _DECIMAL_CONTEXT_FIELDS
        or type(context["precision"]) is not int
        or context["precision"] != 28
        or context["rounding"] != "ROUND_HALF_EVEN"
        or type(context["emin"]) is not int
        or context["emin"] != -999999
        or type(context["emax"]) is not int
        or context["emax"] != 999999
        or type(context["capitals"]) is not int
        or context["capitals"] != 1
        or type(context["clamp"]) is not int
        or context["clamp"] != 0
    ):
        raise ValueError("PRIVATE_VALIDATION_PROFILE_INVALID")
