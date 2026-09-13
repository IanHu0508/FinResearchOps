"""Arithmetic attribution of applied edits, not certification of their reasons."""

from copy import deepcopy

from .forward_revision import apply_forward_revision
from .forward_scenarios import calculate_forward


EFFECT_METRICS = ("parent_net_income", "eps_per_traded_unit", "operating_cash_flow", "return_with_dividend")


def revision_effects(draft, changes):
    applied = apply_forward_revision(draft, changes)["applied_changes"]
    current, effects = deepcopy(draft), []
    for edit in applied:
        target = current if edit["scenario_id"] is None else next(s for s in current["scenarios"] if s["scenario_id"] == edit["scenario_id"])
        field, before, after = edit["field"], edit["before"], edit["after"]
        pieces = [("input_change", after)]
        if field == "noncontrolling_attribution":
            direction_changed = before["nature"] != after["nature"]
            amount_changed = before["amount"]["value"] != after["amount"]["value"]
            if direction_changed and amount_changed:
                intermediate = {"nature": after["nature"], "amount": deepcopy(before["amount"])}
                pieces = [("direction_change", intermediate), ("amount_change", after)]
            elif direction_changed:
                pieces = [("direction_change", after)]
            elif amount_changed:
                pieces = [("amount_change", after)]
            else:
                pieces = [("rationale_only", after)]
        elif before["value"] == after["value"]:
            pieces = [("rationale_only", after)]
        for component, replacement in pieces:
            old = {r["scenario_id"]: r for r in calculate_forward(current)["scenario_results"]}
            target[field] = deepcopy(replacement)
            new = {r["scenario_id"]: r for r in calculate_forward(current)["scenario_results"]}
            scope = [edit["scenario_id"]] if edit["scenario_id"] is not None else list(new)
            rows = []
            for sid in scope:
                rows.append({"scenario_id": sid, "metrics": {
                    key: {"before": old[sid][key], "after": new[sid][key],
                          "delta": None if old[sid][key] is None or new[sid][key] is None else new[sid][key] - old[sid][key]}
                    for key in EFFECT_METRICS}})
            assumption = after["amount"] if field == "noncontrolling_attribution" else after
            effects.append({"scenario_id": edit["scenario_id"], "field": field, "component": component,
                            "declared_basis": edit["correction_basis"], "replacement_basis_type": assumption["basis_type"],
                            "results": rows})
    return effects
