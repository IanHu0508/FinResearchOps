"""Protocol 23: a fixed rating rule over the program-recomputed forward scenarios.

The rule reads program calculations only, never model prose: each scenario's return
to the valuation date (with dividends when the draft states them), annualized over the
research horizon, weighted equally, rounded to the precision the report shows, then
banded. It is a reproducible reference beside the model's rating, not a fair value,
a forecast of the scenario weights, or a trading instruction.
"""

METHOD = "equal-weight-annualized-scenario-return/v1"


def rating_for(expected):
    """Buy from +20%, Overweight from +5%, Hold between, Underweight to -20%, Sell below."""
    if expected >= 0.20:
        return "Buy"
    if expected >= 0.05:
        return "Overweight"
    if expected > -0.05:
        return "Hold"
    if expected > -0.20:
        return "Underweight"
    return "Sell"


def annualized(value, horizon_months):
    """A return over the horizon expressed per year; a total loss stays a total loss."""
    if horizon_months == 12:
        return value
    return -1.0 if value <= -1.0 else (1.0 + value) ** (12 / horizon_months) - 1.0


def rule_rating(calculations, horizon_months):
    """The rule's reference rating from saved forward calculations; always a complete record."""
    rows = []
    for result in calculations["scenario_results"]:
        value, basis = result["return_with_dividend"], "with_dividend"
        if value is None:
            value, basis = result["return_ex_dividend"], "ex_dividend"
        rows.append({"scenario_id": result["scenario_id"], "return": value, "basis": basis if value is not None else None,
                     "annualized": annualized(value, horizon_months) if value is not None else None})
    reason = "NO_SCENARIOS" if not rows else "SCENARIO_RETURN_MISSING" if any(r["return"] is None for r in rows) else None
    if reason is not None:
        return {"method": METHOD, "status": "NOT_COMPUTABLE", "reason": reason, "rating": None,
                "horizon_months": horizon_months, "expected_return": None, "scenario_returns": rows}
    # Banded at the precision the report shows (0.01%), so a displayed edge is never rated below it.
    expected = round(sum(r["annualized"] for r in rows) / len(rows), 4)
    return {"method": METHOD, "status": "COMPUTED", "reason": None, "rating": rating_for(expected),
            "horizon_months": horizon_months, "expected_return": expected, "scenario_returns": rows}
