"""Predeclared, unfitted reference signals; no outcome data enters scoring."""

from collections import defaultdict

from quant.contracts import Prediction, SCALAR_NAMES, require
from quant.labels.ranks import percentiles

FACTOR_DEFINITIONS = {
    "momentum_20": ("return_20", 1),
    "reversal_5": ("return_5", -1),
    "low_volatility_20": ("volatility_20", -1),
}
FACTOR_NAMES = (*FACTOR_DEFINITIONS, "equal_weight_reference")


def factor_predictions(rows):
    """Percentile proxies, not calibrated probabilities or fitted regressions.

    The composite directions and equal weights are fixed before outcomes are
    viewed. Every score uses the complete same-date input pool.
    """
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.key.as_of].append(row)
    output = {name: {} for name in FACTOR_NAMES}
    for day_rows in grouped.values():
        components = []
        for name, (field, direction) in FACTOR_DEFINITIONS.items():
            values = [row.scalars[SCALAR_NAMES.index(field)] for row in day_rows]
            require(all(value is not None for value in values), "FIXED_FACTOR_INPUT_MISSING")
            ranked = percentiles(tuple(direction * value for value in values))
            components.append(ranked)
            output[name].update((row.key, Prediction(row.key, q)) for row, q in zip(day_rows, ranked))
        combined = percentiles(tuple(sum(values) / len(components) for values in zip(*components)))
        output["equal_weight_reference"].update((row.key, Prediction(row.key, q)) for row, q in zip(day_rows, combined))
    return {name: tuple(mapping[row.key] for row in rows) for name, mapping in output.items()}
