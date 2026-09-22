"""Conservative full-universe Spearman bounds with arbitrary missing outcomes.

The feasible rank set is relaxed to marginal rank boxes plus their exact sum.
Tie-aware variance extrema cover all completions, including unknown ties.
Partial bounds are outer bounds, not claimed to be sharp identification limits.
"""
from collections import Counter
from decimal import Decimal, localcontext
import math

from quant.contracts import finite, require
from quant.labels.ranks import average_ranks

METHOD = "rank-box-sum/tie-variance-outer-bound/v1"


def _ratio(numerator, qx, qy, direction=0):
    with localcontext() as context:
        context.prec = 60
        result = float(Decimal(numerator) / (Decimal(qx) * Decimal(qy)).sqrt())
    if direction:
        result = math.nextafter(result, -math.inf if direction < 0 else math.inf)
    return max(-1.0, min(1.0, result))


def _linear_extreme(coefficients, lower, upper, *, maximize):
    remaining = -sum(lower)  # Every centered rank completion has zero sum.
    require(0 <= remaining <= sum(b-a for a,b in zip(lower,upper)), "RANK_BOX_SUM_INFEASIBLE")
    objective = sum(x*y for x,y in zip(coefficients,lower))
    for index in sorted(range(len(lower)), key=lambda i: (coefficients[i],i), reverse=maximize):
        addition = min(remaining, upper[index]-lower[index])
        objective += coefficients[index]*addition
        remaining -= addition
        if remaining == 0:
            break
    require(remaining == 0, "RANK_BOX_SUM_INFEASIBLE")
    return objective


def rank_ic_bounds(scores, outcomes):
    require(len(scores) == len(outcomes) >= 2 and all(finite(x) for x in scores)
            and all(y is None or finite(y) for y in outcomes), "RANK_IC_SHAPE_INVALID")
    n = len(scores)
    known = [y for y in outcomes if y is not None]
    missing = n-len(known)
    result = {"method":METHOD,"lower":None,"upper":None,"point":None,
        "universe_size":n,"observed_count":len(known),"unknown_count":missing,
        "sharp":missing==0,"contains_undefined_completions":False,"reason":None}
    x = [int(2*r)-(n+1) for r in average_ranks(scores)]
    qx = sum(v*v for v in x)
    if not qx:
        return {**result,"contains_undefined_completions":True,"reason":"CONSTANT_PREDICTION_RANKS"}
    if not known:
        return {**result,"contains_undefined_completions":True,"reason":"NO_IDENTIFIED_OUTCOMES"}
    ties = list(Counter(known).values())
    qmax = (n*(n*n-1) - sum(k*k*k-k for k in ties)) // 3
    largest = max(ties)
    qmin = qmax - ((largest+missing)**3-(largest+missing)-(largest**3-largest)) // 3
    if qmin == 0:
        return {**result,"contains_undefined_completions":True,"reason":"RETURN_VARIANCE_NOT_IDENTIFIED_POSITIVE"}
    require(qx > 0 and 0 < qmin <= qmax, "RANK_VARIANCE_BOUNDS_INVALID")
    ranks = iter(average_ranks(known))
    lower, upper = [], []
    for value in outcomes:
        if value is None:
            lower.append(-(n-1)); upper.append(n-1)
        else:
            lo = int(2*next(ranks))-(n+1)
            lower.append(lo); upper.append(lo+2*missing)
    if missing == 0:
        point = _ratio(sum(a*b for a,b in zip(x,lower)),qx,qmax)
        return {**result,"lower":point,"upper":point,"point":point}
    clo = _linear_extreme(x,lower,upper,maximize=False)
    chi = _linear_extreme(x,lower,upper,maximize=True)
    lo = min(_ratio(clo,qx,qmin,-1),_ratio(clo,qx,qmax,-1))
    hi = max(_ratio(chi,qx,qmin,1),_ratio(chi,qx,qmax,1))
    require(lo <= hi, "RANK_IC_BOUNDS_REVERSED")
    return {**result,"lower":lo,"upper":hi,"rank_variance_sum_bounds_doubled":[qmin,qmax],
            "rank_covariance_sum_bounds_doubled":[clo,chi]}
