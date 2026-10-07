"""Learned numerical/cosine geometry and exact dated historical retrieval.

The training candidate set is a declared approximation. Inference searches
the complete supplied dated memory, including distance ties and date caps.
NumPy and SciPy stay optional and are imported only when used.
"""

from datetime import datetime
import math

from quant.contracts import require


def _np():
    import numpy as np
    return np


def transform_fit(values, weights):
    np = _np()
    values, weights = np.asarray(values), np.asarray(weights)
    require(values.ndim == 2 and values.shape[1] == 14 and weights.shape == (len(values),)
            and np.isfinite(weights).all() and (weights > 0).all()
            and not np.isinf(values).any(), "STATE_TRANSFORM_INPUT")
    means, scales = [], []
    for column in range(14):
        ok = np.isfinite(values[:, column])
        total = float(weights[ok].sum())
        mean = float(np.dot(weights[ok], values[ok, column]) / total) if total else 0.
        var = float(np.dot(weights[ok], (values[ok, column] - mean) ** 2) / total) if total else 0.
        means.append(mean)
        scales.append(math.sqrt(var) if var > 1e-24 else 1.)
    return {"means": means, "scales": scales}


def normalize(values, transform):
    np = _np()
    values = np.asarray(values, dtype=np.float64)
    require(values.ndim == 2 and values.shape[1] == 14 and not np.isinf(values).any(),
            "STATE_VALUE_SHAPE")
    missing = ~np.isfinite(values)
    z = np.where(missing, 0., (values - np.asarray(transform["means"])) /
                 np.asarray(transform["scales"]))
    require(np.isfinite(z).all(), "STATE_NORMALIZATION_OVERFLOW")
    return z, missing


def coefficients(parameters, *, mixed):
    np = _np()
    parameters = np.asarray(parameters, dtype=np.float64)
    require(parameters.shape == (16 if mixed else 15,) and np.isfinite(parameters).all(),
            "STATE_METRIC_PARAMETER_SHAPE")
    logits = parameters[:14] - parameters[:14].max()
    w = np.exp(logits)
    w /= w.sum()
    eta = float(parameters[14]) if mixed else 0.
    tau = math.exp(float(parameters[-1]))
    require(0 <= eta <= 1 and math.exp(-3) <= tau <= math.exp(3), "STATE_METRIC_DOMAIN")
    return w, eta, tau


def pair_distances(query, candidates, query_missing, candidate_missing, weights, eta, *,
                   gradients=False):
    """One candidate tensor per query; weighted cosine has an explicit neutral case."""
    np = _np()
    query, candidates = np.asarray(query), np.asarray(candidates)
    qm, cm = np.asarray(query_missing), np.asarray(candidate_missing)
    require(query.ndim == 2 and query.shape[1] == 14 and candidates.ndim == 3
            and candidates.shape[0] == len(query) and candidates.shape[2] == 14
            and qm.shape == query.shape and cm.shape == candidates.shape, "STATE_PAIR_SHAPE")
    delta = (query[:, None, :] - candidates) ** 2
    numerical = np.einsum("qki,i->qk", delta, weights)
    level = np.einsum("qki,i->qk", delta[:, :, :5], weights[:5])
    q2 = np.einsum("qi,i->q", query[:, :5] ** 2, weights[:5])[:, None]
    c2 = np.einsum("qki,i->qk", candidates[:, :, :5] ** 2, weights[:5])
    dot = np.einsum("qi,qki,i->qk", query[:, :5], candidates[:, :, :5], weights[:5])
    valid = (~qm[:, :5].any(axis=1)[:, None]) & (~cm[:, :, :5].any(axis=2))
    valid &= (q2 > 1e-24) & (c2 > 1e-24)
    norm = np.sqrt(np.maximum(q2 * c2, 1e-48))
    cosine = np.where(valid, np.clip(dot / norm, -1, 1), 0.)
    mass = float(weights[:5].sum())
    shape = 2 * (1 - cosine)
    distance = numerical + eta * (mass * shape - level) + .25 * (qm[:, None, :] | cm).mean(axis=2)
    distance = np.maximum(distance, 0.)
    if not gradients:
        return distance
    derivative = delta.copy()
    dc = query[:, None, :5] * candidates[:, :, :5] / norm[:, :, None]
    dc -= .5 * cosine[:, :, None] * (
        query[:, None, :5] ** 2 / np.maximum(q2[:, :, None], 1e-24)
        + candidates[:, :, :5] ** 2 / np.maximum(c2[:, :, None], 1e-24))
    dc = np.where(valid[:, :, None], dc, 0.)
    derivative[:, :, :5] = (1 - eta) * delta[:, :, :5] + eta * (
        shape[:, :, None] - 2 * mass * dc)
    return distance, derivative, mass * shape - level


def date_kernel(distances, days, tau, *, equal=False):
    np = _np()
    distances, days = np.asarray(distances), np.asarray(days)
    counts = (days[:, :, None] == days[:, None, :]).sum(axis=2)
    logw = -np.log(counts)
    if not equal:
        logw = logw - distances / tau ** 2
    logw -= logw.max(axis=1, keepdims=True)
    result = np.exp(logw)
    result /= result.sum(axis=1, keepdims=True)
    return result


def interval_predictions(targets, weights, preferred):
    """Bracketed Newton solution of interval loss; labels never become midpoints."""
    np = _np()
    lower, upper = targets[:, :, 0], targets[:, :, 1]
    left, right = lower.max(axis=1), upper.min(axis=1)
    flat = left <= right
    p = np.clip(np.full(len(targets), preferred), lower.min(axis=1), upper.max(axis=1))
    p[flat] = np.clip(preferred, left[flat], right[flat])
    active_rows = np.flatnonzero(~flat)
    if len(active_rows):
        l, u, w = lower[active_rows], upper[active_rows], weights[active_rows]
        lo, hi = l.min(axis=1), u.max(axis=1)
        value = p[active_rows]
        for _ in range(80):
            clipped = np.clip(value[:, None], l, u)
            gradient = (w * (value[:, None] - clipped)).sum(axis=1)
            done = (np.abs(gradient) < 1e-15) | ((hi - lo) < 2e-15)
            if done.all():
                break
            curvature = (w * ((value[:, None] < l) | (value[:, None] > u) | (l == u))).sum(axis=1)
            hi = np.where((gradient > 0) & ~done, value, hi)
            lo = np.where((gradient < 0) & ~done, value, lo)
            step = value - gradient / np.maximum(curvature, 1e-30)
            step = np.where((step > lo) & (step < hi), step, (lo + hi) / 2)
            value = np.where(done, value, step)
        p[active_rows] = value
    return p


def metric_objective(parameters, train, *, mixed, task, regularization):
    """Analytic gradient within the current top-64 candidate membership."""
    np = _np()
    w, eta, tau = coefficients(parameters, mixed=mixed)
    total_loss, total_gradient = 0., np.zeros(len(parameters))
    qweights = train["query_weights"]
    denominator = float(qweights.sum())
    risk_variance = max(float(train.get("risk_variance", 1)), 1e-12)
    for start in range(0, len(train["query_z"]), 128):
        stop = start + 128
        q, c = train["query_z"][start:stop], train["candidate_z"][start:stop]
        qm, cm = train["query_missing"][start:stop], train["candidate_missing"][start:stop]
        distances = pair_distances(q, c, qm, cm, w, eta)
        if "candidate_valid" in train:
            distances = np.where(train["candidate_valid"][start:stop], distances, np.inf)
        order = np.argsort(distances, axis=1, kind="stable")[:, :64]
        rows = np.arange(len(q))[:, None]
        c, cm = c[rows, order], cm[rows, order]
        distance, gd, ge = pair_distances(q, c, qm, cm, w, eta, gradients=True)
        days = train["candidate_days"][start:stop][rows, order]
        kernel = date_kernel(distance, days, tau)
        weighted = (gd * w).sum(axis=2, keepdims=True)
        g_logits = (gd - weighted) * w
        g_logw = -g_logits / tau ** 2
        if mixed:
            g_logw = np.concatenate((g_logw, (-ge / tau ** 2)[:, :, None]), axis=2)
        g_logw = np.concatenate((g_logw, (2 * distance / tau ** 2)[:, :, None]), axis=2)
        if task == "risk":
            targets = train["candidate_risk"][start:stop][rows, order]
            prediction = (kernel * targets).sum(axis=1)
            gp = (kernel[:, :, None] * (targets - prediction[:, None])[:, :, None] * g_logw).sum(axis=1)
            residual = prediction - train["query_risk"][start:stop]
            scale = risk_variance
        else:
            targets = train["candidate_targets"][start:stop][rows, order]
            prediction = interval_predictions(targets, kernel, train["center"])
            clipped = np.clip(prediction[:, None], targets[:, :, 0], targets[:, :, 1])
            active = (prediction[:, None] < targets[:, :, 0]) | (
                prediction[:, None] > targets[:, :, 1]) | (targets[:, :, 0] == targets[:, :, 1])
            curvature = (kernel * active).sum(axis=1)
            gp = (kernel[:, :, None] * (clipped - prediction[:, None])[:, :, None] * g_logw).sum(axis=1)
            gp /= np.maximum(curvature[:, None], 1e-30)
            query_target = train["query_targets"][start:stop]
            residual = prediction - np.clip(prediction, query_target[:, 0], query_target[:, 1])
            scale = 1.
        weight = qweights[start:stop] / denominator
        total_loss += float(np.dot(weight, residual ** 2)) / scale
        total_gradient += (weight[:, None] * 2 * residual[:, None] * gp).sum(axis=0) / scale
    penalty = float(((w - 1 / 14) ** 2).sum()) + eta ** 2
    gpenalty = 2 * (w - 1 / 14)
    total_gradient[:14] += regularization * w * (gpenalty - np.dot(w, gpenalty))
    if mixed:
        total_gradient[14] += 2 * regularization * eta
    return total_loss + regularization * penalty, total_gradient


def fit_metric(train, *, mixed, task, regularization, initialization=0):
    from scipy.optimize import minimize
    np = _np()
    size = 16 if mixed else 15
    initial = np.zeros(size)
    if initialization:
        initial[:14] = np.random.default_rng(17).normal(0, .25, 14)
    if mixed:
        initial[14] = .5
    bounds = [(-6., 6.)] * 14 + ([(0., 1.)] if mixed else []) + [(-3., 3.)]
    require(task in ("rank", "risk") and train["candidate_z"].shape[1] >= 64,
            "STATE_METRIC_FIT_TASK")
    def members(parameters):
        w, eta, _ = coefficients(parameters, mixed=mixed)
        distance = pair_distances(train["query_z"], train["candidate_z"],
                                  train["query_missing"], train["candidate_missing"], w, eta)
        if "candidate_valid" in train:
            require((train["candidate_valid"].sum(axis=1) >= 64).all(),
                    "STATE_LEARNING_VALID_CANDIDATES")
            distance = np.where(train["candidate_valid"], distance, np.inf)
        return np.argsort(distance, axis=1, kind="stable")[:, :64]
    def fixed(member_ids):
        rows = np.arange(len(member_ids))[:, None]
        value = dict(train)
        for key in ("candidate_z", "candidate_missing", "candidate_days",
                    "candidate_targets", "candidate_risk", "candidate_valid"):
            if key in train:
                value[key] = train[key][rows, member_ids]
        return value
    member_ids = members(initial)
    used, refreshes, result = 0, 0, None
    parameters = initial
    while used < 100:
        frozen = fixed(member_ids)
        def objective(p):
            return metric_objective(p, frozen, mixed=mixed, task=task,
                                    regularization=regularization)
        result = minimize(objective, parameters, jac=True, method="L-BFGS-B", bounds=bounds,
                          options={"maxiter": 100 - used, "ftol": 1e-11, "gtol": 1e-6})
        used += max(int(result.nit), 1)
        parameters = result.x
        refreshed = members(parameters)
        if refreshes or np.array_equal(refreshed, member_ids) or used >= 100:
            break
        member_ids = refreshed
        refreshes = 1
    result["frozen_candidate_loss"] = float(result.fun)
    result["fun"] = metric_objective(result.x, train, mixed=mixed, task=task,
                                     regularization=regularization)[0]
    result["nit"] = used
    result["membership_refreshes"] = refreshes
    result["fit_approximation"] = "FIXED_64_FROM_CAUSAL_CANDIDATE_POOL_WITH_ONE_REFRESH"
    return result


class ExactStateIndex:
    """All supplied memory rows, with pattern offsets and exact capped neighbours."""
    def __init__(self, values, days, sample_ids, transform, weights, eta, *, workers=4):
        from scipy.spatial import cKDTree
        np = _np()
        self.values, self.days, self.sample_ids = np.asarray(values), np.asarray(days), np.asarray(sample_ids)
        self.transform, self.weights, self.eta = transform, np.asarray(weights), eta
        self.workers, self.groups = workers, []
        require(self.values.shape == (len(days), 14) and self.sample_ids.shape == self.days.shape
                and np.all(np.diff(self.sample_ids) > 0) and len(days) > 0
                and self.weights.shape == (14,) and (self.weights >= 0).all()
                and np.isfinite(self.weights).all() and abs(self.weights.sum() - 1) < 1e-12
                and 0 <= eta <= 1, "STATE_INDEX_MEMORY_ALIGNMENT")
        z, missing = normalize(values, transform)
        points, valid = self._embed(z, missing)
        codes = (missing.astype(np.int32) * (1 << np.arange(14))).sum(axis=1)
        codes += (~valid).astype(np.int32) * (1 << 14)
        for code in np.unique(codes):
            ids = np.flatnonzero(codes == code)
            pattern = ((int(code) >> np.arange(14)) & 1).astype(bool)
            self.groups.append((ids, pattern, bool(code & (1 << 14)),
                                cKDTree(points[ids], leafsize=32, balanced_tree=False)))
        del z, points

    def _embed(self, z, missing):
        np = _np()
        factor = np.sqrt(self.weights.copy())
        factor[:5] *= math.sqrt(1 - self.eta)
        normal = z * factor
        if not self.eta:
            return normal, np.ones(len(z), dtype=bool)
        trend = z[:, :5] * np.sqrt(self.weights[:5])
        norm = np.linalg.norm(trend, axis=1)
        valid = (~missing[:, :5].any(axis=1)) & (norm > 1e-12)
        unit = np.where(valid[:, None], trend / np.maximum(norm[:, None], 1e-12), 0)
        return np.column_stack((normal, unit * math.sqrt(self.eta * self.weights[:5].sum()))), valid

    def query(self, values, *, k=64, cap=8):
        np = _np()
        z, missing = normalize(values, self.transform)
        points, valid = self._embed(z, missing)
        result = [None] * len(points)
        for start in range(0, len(points), 128):
            pending = np.arange(start, min(start + 128, len(points)))
            # One extra neighbour can prove a strict boundary without fetching
            # a fixed fourfold shortlist. Date caps and ties still expand until
            # the all-memory result is proven. Tree shape changes only runtime.
            limit = min(k + 1, len(self.values))
            while len(pending):
                candidates, distances, borders = [], [], []
                for ids, pattern, invalid, tree in self.groups:
                    offset = .25 * (missing[pending] | pattern).mean(axis=1)
                    offset += self.eta * self.weights[:5].sum() * ((~valid[pending]).astype(int) + int(invalid))
                    take = min(limit, len(ids))
                    d, index = tree.query(points[pending], k=take, workers=self.workers)
                    d, index = np.asarray(d).reshape(len(pending), take), np.asarray(index).reshape(len(pending), take)
                    candidates.append(ids[index])
                    distances.append(d ** 2 + offset[:, None])
                    if take < len(ids):
                        borders.append(d[:, -1] ** 2 + offset)
                ci, ds = np.concatenate(candidates, axis=1), np.concatenate(distances, axis=1)
                orders = np.lexsort((self.sample_ids[ci], self.days[ci], ds), axis=1)
                edge = np.min(np.stack(borders), axis=0) if borders else np.full(len(pending), np.inf)
                remaining = []
                for row, original in enumerate(pending):
                    picked, pd, counts = [], [], {}
                    for position in orders[row]:
                        index = int(ci[row, position])
                        day = int(self.days[index])
                        if counts.get(day, 0) < cap:
                            picked.append(index); pd.append(float(ds[row, position]))
                            counts[day] = counts.get(day, 0) + 1
                            if len(picked) == k:
                                break
                    if (len(picked) == k and pd[-1] < edge[row] - 1e-13) or limit == len(self.values):
                        result[original] = (np.asarray(picked, dtype=np.int64), np.asarray(pd))
                    else:
                        remaining.append(original)
                # Expand through boundary ties; never infer global neighbours from a fixed shortlist.
                pending = np.asarray(remaining, dtype=np.int64)
                limit = min(len(self.values), limit * 2)
        return result
