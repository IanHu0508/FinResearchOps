"""Partial rank identification and interval loss; no missing point imputation."""
import math

from quant.contracts import finite, require
from .ranks import average_ranks


def percentile_intervals(values):
    require(len(values) >= 2 and all(v is None or finite(v) for v in values), "INTERVAL_RETURN_INPUT_INVALID")
    known = [v for v in values if v is not None]
    if not known:
        return (None,) * len(values)
    missing = len(values) - len(known)
    ranks = iter(average_ranks(known))
    result = []
    for value in values:
        if value is None:
            result.append(None)
        else:
            rank = next(ranks)
            result.append(((rank-1)/(len(values)-1), (rank+missing-1)/(len(values)-1)))
    return tuple(result)


def interval_residual(prediction, interval):
    require(finite(prediction) and type(interval) is tuple and len(interval) == 2
            and all(finite(v) for v in interval) and 0 <= interval[0] <= interval[1] <= 1,
            "INTERVAL_LOSS_INPUT_INVALID")
    return prediction - min(max(prediction, interval[0]), interval[1])


def squared_interval_loss(prediction, interval):
    return interval_residual(prediction, interval) ** 2


def half_squared_interval_derivatives(prediction, interval):
    residual = interval_residual(prediction, interval)
    # At a wide interval's boundary choose the zero generalized curvature.
    # A singleton is ordinary squared error, including at its exact minimum.
    curvature = float(interval[0] == interval[1] or prediction < interval[0] or prediction > interval[1])
    return residual, curvature


def fit_interval_constant(intervals, weights):
    """Minimize weighted interval distance; select the leftmost flat minimizer.

    This is a fitted prediction, not a midpoint assignment to unknown labels.
    All inputs must be observed-outcome intervals from a supervised batch.
    """
    require(bool(intervals) and len(intervals) == len(weights)
            and all(finite(w) and w > 0 for w in weights), "INTERVAL_FIT_INPUT_INVALID")
    for interval in intervals:
        interval_residual(0.0, interval)
    if all(bounds[0] == bounds[1] for bounds in intervals):
        return math.fsum(w*bounds[0] for bounds,w in zip(intervals,weights))/math.fsum(weights)
    left, right = max(x[0] for x in intervals), min(x[1] for x in intervals)
    if left <= right:
        return left
    low, high = min(x[0] for x in intervals), max(x[1] for x in intervals)
    for _ in range(80):
        candidate = (low+high)/2
        gradient = math.fsum(w * interval_residual(candidate, bounds) for bounds, w in zip(intervals, weights))
        if gradient >= 0:
            high = candidate
        else:
            low = candidate
    return high
