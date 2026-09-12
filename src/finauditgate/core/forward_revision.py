"""Apply one explicit, bounded set of forecast edits and recalculate it.

The revision records model-proposed corrections, not verified financial facts.
Only existing financial inputs can change; source material, scenario identity,
dates, and ratings are outside this function. No values are fitted to a price.
"""

from copy import deepcopy
import math

from finauditgate.core.forward_scenarios import calculate_forward


SCENARIO_FIELDS = frozenset({
    "revenue", "operating_margin", "net_nonoperating_income",
    "effective_tax_rate", "noncontrolling_attribution", "diluted_ordinary_shares",
    "non_working_capital_adjustments", "operating_asset_liability_cash_effect",
    "cash_capex", "fx_reporting_per_price_currency", "exit_pe",
    "cash_dividend_per_traded_unit",
})
COMMON_FIELDS = frozenset({"market_price", "shares_per_traded_unit"})
_CHANGE_FIELDS = {
    "scenario_id", "field", "expected_before", "replacement",
    "correction_basis", "reason", "evidence_refs",
}
_ASSUMPTION_FIELDS = {"value", "basis_type", "reason", "evidence_refs"}
_BASES = {"reported", "company_guidance", "reference_comparison", "analyst_assumption"}
_NATURES = {"profit", "loss", "unknown"}


def _object(value, keys, path):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{path}: expected exactly {', '.join(sorted(keys))}")


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: expected nonempty text")


def _refs(value, path):
    if (not isinstance(value, list)
            or any(not isinstance(ref, str) or not ref.strip() for ref in value)):
        raise ValueError(f"{path}: expected a list of nonempty source references")


def _number(value, path):
    if value is None:
        return
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))):
        raise ValueError(f"{path}: expected a finite JSON number or null")


def _assumption(value, path):
    _object(value, _ASSUMPTION_FIELDS, path)
    _number(value["value"], f"{path}.value")
    if not isinstance(value["basis_type"], str) or value["basis_type"] not in _BASES:
        raise ValueError(f"{path}.basis_type: unsupported assumption basis")
    _text(value["reason"], f"{path}.reason")
    _refs(value["evidence_refs"], f"{path}.evidence_refs")


def _attribution(value, path, *, full):
    _object(value, {"nature", "amount"}, path)
    if not isinstance(value["nature"], str) or value["nature"] not in _NATURES:
        raise ValueError(f"{path}.nature: expected profit, loss or unknown")
    if full:
        _assumption(value["amount"], f"{path}.amount")
        amount = value["amount"]["value"]
    else:
        amount = value["amount"]
        _number(amount, f"{path}.amount")
    if amount is not None and amount < 0:
        raise ValueError(f"{path}.amount: expected nonnegative magnitude or null")


def apply_forward_revision(draft, changes) -> dict:
    """Return a detached effective draft, its arithmetic, and actual edits.

    Expected old values are exact numeric values (or null), with an explicit
    nature/amount pair for noncontrolling attribution. Each target may occur
    once. Replacements retain their full proposed rationale and references.
    A no-op replacement is omitted from applied_changes; a rationale-only edit
    is retained. The calculator validates and recomputes the effective draft.
    Neither shape checks nor successful arithmetic certify a correction's
    source support or economic appropriateness.
    """
    if not isinstance(draft, dict) or not isinstance(draft.get("scenarios"), list):
        raise ValueError("forward_revision.draft: expected a draft with scenarios")
    if not isinstance(changes, list):
        raise ValueError("forward_revision.changes: expected a list")
    effective = deepcopy(draft)
    scenarios = {}
    for scenario in effective["scenarios"]:
        sid = scenario.get("scenario_id") if isinstance(scenario, dict) else None
        if not isinstance(sid, str) or sid in scenarios:
            raise ValueError("forward_revision.draft: scenario IDs must be unique strings")
        scenarios[sid] = scenario
    seen, applied = set(), []
    for index, change in enumerate(changes):
        path = f"forward_revision.changes[{index}]"
        _object(change, _CHANGE_FIELDS, path)
        sid, field = change["scenario_id"], change["field"]
        if sid is None:
            target, allowed = effective, COMMON_FIELDS
        elif isinstance(sid, str) and sid in {"F1", "F2", "F3"} and sid in scenarios:
            target, allowed = scenarios[sid], SCENARIO_FIELDS
        else:
            raise ValueError(f"{path}.scenario_id: target does not exist or is not allowed")
        if not isinstance(field, str) or field not in allowed or field not in target:
            raise ValueError(f"{path}.field: target does not exist or is not allowed")
        key = (sid, field)
        if key in seen:
            raise ValueError(f"{path}: duplicate correction target")
        seen.add(key)
        _text(change["correction_basis"], f"{path}.correction_basis")
        _text(change["reason"], f"{path}.reason")
        _refs(change["evidence_refs"], f"{path}.evidence_refs")
        before, after = target[field], change["replacement"]
        expected = change["expected_before"]
        if field == "noncontrolling_attribution":
            _attribution(before, f"{path}.original", full=True)
            _attribution(expected, f"{path}.expected_before", full=False)
            _attribution(after, f"{path}.replacement", full=True)
            actual = {"nature": before["nature"], "amount": before["amount"]["value"]}
        else:
            _assumption(before, f"{path}.original")
            _number(expected, f"{path}.expected_before")
            _assumption(after, f"{path}.replacement")
            actual = before["value"]
        if expected != actual:
            raise ValueError(f"{path}.expected_before: does not match the original input")
        if after == before:
            continue
        applied.append({
            "scenario_id": sid, "field": field,
            "before": deepcopy(before), "after": deepcopy(after),
            "expected_before": deepcopy(expected),
            "correction_basis": change["correction_basis"],
            "reason": change["reason"], "evidence_refs": deepcopy(change["evidence_refs"]),
        })
        target[field] = deepcopy(after)
    return {
        "effective_forward_draft": effective,
        "effective_forward_calculations": calculate_forward(effective),
        "applied_changes": applied,
    }
