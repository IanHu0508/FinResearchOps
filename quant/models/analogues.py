"""Exact historical analogue estimators with dated, interval-valued memory.

Optional NumPy/SciPy imports stay outside the dependency-free application.
Missingness patterns partition the index: within each pattern the missingness
penalty is constant for a query, so a numerical KD-tree remains exact. Date
caps are applied to the globally ordered candidates, never to an arbitrary
fixed shortlist. The index is rebuilt from content-bound persisted arrays.
"""

from datetime import datetime
import hashlib
import json
from pathlib import Path

from quant.contracts import Prediction, TARGET_ID, canonical, fingerprint, require
from quant.labels.intervals import fit_interval_constant

STOCK_NAMES = ("return_1", "return_5", "return_20", "return_60", "volatility_20",
               "volume_ratio_20", "amount_ratio_20", "relative_return_20")
MARKET_NAMES = ("median_return", "breadth", "cross_sectional_dispersion", "market_volatility_20")
COMPACT_COLUMNS = (0, 1, 2, 3, 7, 11, 12, 19, 20, 21, 22, 23)
SCHEMA = "quant.analogue-model/v1"
POLICY = {"loss": "half-squared-interval-distance/v1", "tie": "project-training-center/v1",
          "center_tie": "project-0.5/v1", "missing_penalty": 0.25,
          "date_cap_divisor": 8, "stock_min_observed": 6, "market_min_observed": 3,
          "neighbor_weights": "retrieved-date-equal/within-date-equal",
          "ordering": "distance,date,symbol", "search": "EXACT", "fallback": "training-center"}


def _libs():
    import numpy as np
    from scipy.spatial import cKDTree
    return np, cKDTree


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def interval_center(intervals, weights, preferred=0.5):
    """Preserve old loss implementation; choose a declared point on a flat optimum."""
    intervals = tuple((float(lower), float(upper)) for lower, upper in intervals)
    weights = tuple(float(weight) for weight in weights)
    require(len(intervals) == len(weights) and bool(intervals)
            and all(0 <= lower <= upper <= 1 for lower, upper in intervals)
            and all(weight > 0 for weight in weights) and 0 <= preferred <= 1, "INTERVAL_FIT_INPUT_INVALID")
    lower = max(item[0] for item in intervals)
    upper = min(item[1] for item in intervals)
    return min(max(preferred, lower), upper) if lower <= upper else fit_interval_constant(intervals, weights)


def array_center(targets, weights):
    np, _ = _libs()
    lower, upper = targets[:, 0], targets[:, 1]
    left, right = float(lower.max()), float(upper.min())
    if left <= right:
        return min(max(0.5, left), right)
    lo, hi = float(lower.min()), float(upper.max())
    for _ in range(80):
        p = (lo + hi) / 2
        gradient = float(np.sum(weights * (p - np.clip(p, lower, upper)), dtype=np.float64))
        if gradient >= 0:
            hi = p
        else:
            lo = p
    return hi


def fit_array_memory(values, targets, weights, days, sample_ids, folder, *,
                     training_cutoff, fit_date, training_dataset_id):
    """Persist one annual memory shared by the six fixed estimator configurations.

    The admitted caller supplies only chronological supervised training rows.
    sample_ids follow date/symbol order and bind to its immutable feature cache.
    No evaluation rows or labels may enter this function.
    """
    np, _ = _libs()
    values, targets = np.asarray(values), np.asarray(targets)
    weights, days, sample_ids = map(np.asarray, (weights, days, sample_ids))
    n = len(values)
    require(values.shape == (n, 12) and targets.shape == (n, 2) and n > 0, "ANALOGUE_TRAIN_SHAPE")
    require(weights.shape == days.shape == sample_ids.shape == (n,), "ANALOGUE_TRAIN_ALIGNMENT")
    require(not np.isinf(values).any() and np.isfinite(targets).all() and np.isfinite(weights).all()
            and (weights > 0).all() and ((0 <= targets[:, 0]) & (targets[:, 0] <= targets[:, 1])
            & (targets[:, 1] <= 1)).all(), "ANALOGUE_TRAIN_VALUES")
    require((np.diff(sample_ids) > 0).all() and (np.diff(days) >= 0).all(), "ANALOGUE_TRAIN_ORDER")
    counts = np.unique(days, return_counts=True)[1]
    require(np.allclose(weights, np.repeat(1 / counts, counts), rtol=0, atol=1e-12), "DATE_WEIGHTS_INVALID")
    cutoff = datetime.fromisoformat(training_cutoff)
    start = datetime.fromisoformat(fit_date + "T00:00:00+08:00")
    require(cutoff.tzinfo is not None and cutoff < start, "ANALOGUE_TRAINING_LABEL_LEAKAGE")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    means, scales = [], []
    for column in range(12):
        observed = ~np.isnan(values[:, column])
        total = float(weights[observed].sum(dtype=np.float64))
        mean = float(np.sum(values[observed, column] * weights[observed], dtype=np.float64) / total) if total else 0.
        variance = float(np.sum(weights[observed] * (values[observed, column] - mean) ** 2,
                                dtype=np.float64) / total) if total else 0.
        means.append(mean)
        scales.append(variance ** 0.5 if variance > 1e-24 else 1.)
    files = {}
    for name, array in (("values", values), ("targets", targets), ("days", days), ("sample_ids", sample_ids)):
        path = folder / (name + ".npy")
        np.save(path, array, allow_pickle=False)
        files[path.name] = file_digest(path)
    state = {"schema_version": SCHEMA, "target_id": TARGET_ID, "policy": POLICY,
             "feature_names": list(STOCK_NAMES + MARKET_NAMES), "means": means, "scales": scales,
             "training_center": array_center(targets, weights), "training_cutoff": training_cutoff,
             "fit_date": fit_date, "training_dataset_id": training_dataset_id,
             "training_rows": n, "training_dates": len(counts), "arrays": files}
    (folder / "memory.json").write_text(canonical(state) + "\n")
    for alpha in (0., 0.25, 0.5):
        for k in (32, 64):
            document = {**state, "alpha": alpha, "k": k}
            document["model_version"] = "analogue-" + fingerprint(document)[:24]
            (folder / f"model-a{alpha:g}-k{k}.json").write_text(canonical(document) + "\n")
    return state


class FittedAnalogue:
    """The same predict(rows) seam as XGB; array scoring is its streaming implementation."""

    def __init__(self, folder, *, alpha=0.25, k=32, verify=True, workers=4):
        np, tree = _libs()
        require(alpha in (0., 0.25, 0.5) and k in (32, 64) and 1 <= workers <= 4, "ANALOGUE_CONFIG")
        self.folder, self.alpha, self.k, self.workers = Path(folder), alpha, k, workers
        self.state = json.loads((self.folder / f"model-a{alpha:g}-k{k}.json").read_text())
        raw = {key: value for key, value in self.state.items() if key != "model_version"}
        require(self.state["schema_version"] == SCHEMA and self.state["policy"] == POLICY
                and self.state["target_id"] == TARGET_ID and self.state["alpha"] == alpha
                and self.state["k"] == k and self.state["model_version"] == "analogue-" + fingerprint(raw)[:24],
                "ANALOGUE_MODEL_CONTENT_MISMATCH")
        if verify:
            require(all(Path(name).name == name and file_digest(self.folder / name) == digest
                        for name, digest in self.state["arrays"].items()), "ANALOGUE_ARRAY_CONTENT_MISMATCH")
        self.values = np.load(self.folder / "values.npy", mmap_mode="r", allow_pickle=False)
        self.targets = np.load(self.folder / "targets.npy", mmap_mode="r", allow_pickle=False)
        self.days = np.load(self.folder / "days.npy", mmap_mode="r", allow_pickle=False)
        self.sample_ids = np.load(self.folder / "sample_ids.npy", mmap_mode="r", allow_pickle=False)
        self.p = 8 if alpha == 0 else 12
        self.means = np.asarray(self.state["means"][:self.p])
        self.scales = np.asarray(self.state["scales"][:self.p])
        self.factors = np.sqrt(np.r_[np.repeat((1 - alpha) / 8, 8),
                                     np.repeat(alpha / 4, 4)][:self.p])
        masks = np.isnan(self.values[:, :self.p])
        codes = (masks * (1 << np.arange(self.p))).sum(axis=1).astype(np.uint16)
        self.groups = []
        for code in np.unique(codes):
            indices = np.flatnonzero(codes == code)
            missing = ((int(code) >> np.arange(self.p)) & 1).astype(bool)
            columns = np.flatnonzero(~missing)
            normalized = ((self.values[indices[:, None], columns] - self.means[columns])
                          / self.scales[columns]) * self.factors[columns]
            # An all-missing pattern has no varying coordinates; a constant axis is exact.
            points = np.ascontiguousarray(normalized if len(columns) else np.zeros((len(indices), 1)))
            self.groups.append((indices, missing, columns, tree(points, leafsize=32, copy_data=False)))
        require(sum(len(group[0]) for group in self.groups) == len(self.values), "ANALOGUE_INDEX_COVERAGE")

    @property
    def view(self):
        return "analogue-state"

    @property
    def ablation(self):
        return "stock+context"

    @property
    def model_version(self):
        return self.state["model_version"]

    @property
    def training_cutoff(self):
        return datetime.fromisoformat(self.state["training_cutoff"])

    @property
    def artifact(self):
        return dict(self.state)

    def _normalize(self, values):
        np, _ = _libs()
        values = np.asarray(values, dtype=np.float64)
        require(values.ndim == 2 and values.shape[1] == 12 and not np.isinf(values).any(), "ANALOGUE_QUERY_SHAPE")
        missing = np.isnan(values[:, :self.p])
        z = np.where(missing, 0., (values[:, :self.p] - self.means) / self.scales) * self.factors
        return z, missing

    def _candidates(self, z, missing, limit):
        np, _ = _libs()
        all_ids, all_distances, borders = [], [], []
        for indices, pattern, columns, tree in self.groups:
            count = min(limit, len(indices))
            q = z[:, columns] if len(columns) else np.zeros((len(z), 1))
            ds, local = tree.query(q, k=list(range(1, count + 1)), eps=0., workers=self.workers)
            offset = (z[:, pattern] ** 2).sum(axis=1) + 0.25 * (missing | pattern).mean(axis=1)
            distances = ((tree.data[local] - q[:, None, :]) ** 2).sum(axis=2) + offset[:, None]
            all_ids.append(indices[local])
            all_distances.append(distances)
            borders.append(distances[:, -1] if count < len(indices) else np.full(len(z), np.inf))
        return np.concatenate(all_ids, axis=1), np.concatenate(all_distances, axis=1), np.asarray(borders).T

    def _select(self, ids, distances, k):
        np, _ = _libs()
        order = np.lexsort((self.sample_ids[ids], distances))
        ids, distances = ids[order], distances[order]
        selected, selected_distances, counts = [], [], {}
        for index, distance in zip(ids, distances):
            day = int(self.days[index])
            if counts.get(day, 0) >= k // 8:
                continue
            counts[day] = counts.get(day, 0) + 1
            selected.append(int(index))
            selected_distances.append(float(distance))
            if len(selected) == k:
                break
        return selected, selected_distances

    def _neighbors(self, z, missing, ks):
        np, _ = _libs()
        limit = max(ks) * 2
        pending = np.arange(len(z))
        answer = {k: [None] * len(z) for k in ks}
        while len(pending):
            ids, distances, borders = self._candidates(z[pending], missing[pending], limit)
            results = {k: [self._select(i, d, k) for i, d in zip(ids, distances)] for k in ks}
            exhausted = all(limit >= len(group[0]) for group in self.groups)
            complete = np.asarray([all(len(results[k][row][0]) == k for k in ks) for row in range(len(pending))])
            boundaries = np.asarray([max((items[row][1][-1] for items in results.values() if items[row][1]),
                                        default=float("inf")) for row in range(len(pending))])
            ready = np.full(len(pending), True) if exhausted else complete & (borders >= boundaries[:, None] - 1e-14).all(axis=1)
            for row in np.flatnonzero(ready):
                original = pending[row]
                if not (borders[row] > boundaries[row] + 1e-14).all():
                    qz, qm, boundary = z[original], missing[original], boundaries[row]
                    tie_ids, tie_distances = [], []
                    for indices, pattern, columns, tree in self.groups:
                        offset = float((qz[pattern] ** 2).sum() + 0.25 * (qm | pattern).mean())
                        if boundary < offset:
                            continue
                        radius = np.nextafter(np.sqrt(max(0., boundary - offset)) + 1e-13, np.inf)
                        q = qz[columns] if len(columns) else np.zeros(1)
                        local = np.asarray(tree.query_ball_point(q, radius, eps=0.), dtype=np.int64)
                        if len(local):
                            delta = tree.data[local] - q
                            tie_ids.extend(indices[local].tolist())
                            tie_distances.extend(((delta * delta).sum(axis=1) + offset).tolist())
                    merged_ids = np.r_[ids[row], np.asarray(tie_ids, dtype=np.int64)]
                    merged_distances = np.r_[distances[row], np.asarray(tie_distances)]
                    _, unique = np.unique(merged_ids, return_index=True)
                    for k in ks:
                        results[k][row] = self._select(merged_ids[unique], merged_distances[unique], k)
                for k in ks:
                    answer[k][original] = results[k][row]
            pending = pending[~ready]
            limit *= 2
        return answer

    def predict_arrays(self, values, *, as_of, ks=None, batch_size=128):
        np, _ = _libs()
        require(datetime.fromisoformat(as_of) >= datetime.fromisoformat(self.state["fit_date"] + "T00:00:00+08:00")
                and datetime.fromisoformat(as_of) > self.training_cutoff, "ANALOGUE_MODEL_AFTER_SCORING")
        ks = (self.k,) if ks is None else tuple(ks)
        require(all(k in (32, 64) for k in ks), "ANALOGUE_CONFIG")
        z, missing = self._normalize(values)
        answer = {k: {"scores": np.full(len(z), self.state["training_center"]),
                      "fallback": np.zeros(len(z), dtype=np.uint8),
                      "neighbors": np.full((len(z), k), -1, dtype=np.int32),
                      "date_count": np.zeros(len(z), dtype=np.uint16),
                      "mean_distance": np.zeros(len(z))} for k in ks}
        valid = (missing[:, :8].sum(axis=1) <= 2)
        if self.p == 12:
            valid &= (missing[:, 8:].sum(axis=1) <= 1)
        for k in ks:
            answer[k]["fallback"][~valid] = 1
        query_ids = np.flatnonzero(valid)
        for start in range(0, len(query_ids), batch_size):
            current = query_ids[start:start + batch_size]
            retrieved = self._neighbors(z[current], missing[current], ks)
            for k in ks:
                out = answer[k]
                supported_queries, supported_indices = [], []
                for query, (indices, distances) in zip(current, retrieved[k]):
                    if len(indices) < k:
                        out["fallback"][query] = 2
                        continue
                    targets = self.targets[indices]
                    if (targets[:, 0] == 0).all() and (targets[:, 1] == 1).all():
                        out["fallback"][query] = 3
                        continue
                    out["neighbors"][query] = indices
                    out["mean_distance"][query] = sum(distances) / k
                    supported_queries.append(query)
                    supported_indices.append(indices)
                if supported_queries:
                    indices = np.asarray(supported_indices)
                    dates = self.days[indices]
                    counts = (dates[:, :, None] == dates[:, None, :]).sum(axis=2)
                    weights = 1. / counts
                    out["date_count"][supported_queries] = np.rint(weights.sum(axis=1)).astype(np.uint16)
                    out["scores"][supported_queries] = batch_interval_center(self.targets[indices], weights,
                                                                             self.state["training_center"])
        return answer

    def predict(self, rows):
        from quant.contracts import SCALAR_NAMES
        require(bool(rows) and len({row.key.as_of for row in rows}) == 1, "ANALOGUE_ONE_SCORE_DATE")
        positions = tuple(SCALAR_NAMES.index(name) for name in STOCK_NAMES[:7])
        values = [tuple(row.scalars[index] for index in positions) + (row.relative_features[1],) + row.market_context for row in rows]
        scores = self.predict_arrays(values, as_of=rows[0].key.as_of.isoformat())[self.k]["scores"]
        return tuple(Prediction(row.key, float(score)) for row, score in zip(rows, scores))


def fusion_scores(xgb_scores, nn_scores, weight, fallback):
    np, _ = _libs()
    require(weight in (0., 0.25, 0.5, 1.), "FUSION_WEIGHT_INVALID")
    xgb, nn = np.asarray(xgb_scores), np.asarray(nn_scores)
    require(xgb.shape == nn.shape == np.asarray(fallback).shape and np.isfinite(xgb).all()
            and np.isfinite(nn).all() and ((0 <= xgb) & (xgb <= 1)).all()
            and ((0 <= nn) & (nn <= 1)).all(), "FUSION_INPUT_INVALID")
    return np.where(np.asarray(fallback) != 0, xgb, (1 - weight) * xgb + weight * nn)


def batch_interval_center(targets, weights, preferred):
    """Vectorized version of the same convex interval fit, never midpoint labels."""
    np, _ = _libs()
    lower, upper = targets[:, :, 0], targets[:, :, 1]
    left, right = lower.max(axis=1), upper.min(axis=1)
    flat = left <= right
    result = np.clip(preferred, left, np.maximum(left, right))
    active = ~flat
    if active.any():
        l, u, w = lower[active], upper[active], weights[active]
        lo, hi = l.min(axis=1), u.max(axis=1)
        for _ in range(80):
            p = (lo + hi) / 2
            gradient = (w * (p[:, None] - np.clip(p[:, None], l, u))).sum(axis=1)
            hi = np.where(gradient >= 0, p, hi)
            lo = np.where(gradient < 0, p, lo)
        result[active] = hi
    return result


class AnalogueModel:
    def __init__(self, artifact_root, *, k=32, alpha=0.25):
        self.artifact_root, self.k, self.alpha = artifact_root, k, alpha

    def fit(self, train):
        np, _ = _libs()
        from quant.contracts import SCALAR_NAMES
        order = sorted(range(len(train.rows)), key=lambda i: (train.rows[i].key.as_of, train.rows[i].key.symbol))
        positions = tuple(SCALAR_NAMES.index(name) for name in STOCK_NAMES[:7])
        rows = [train.rows[i] for i in order]
        values = np.asarray([tuple(row.scalars[j] for j in positions) + (row.relative_features[1],)
                             + row.market_context for row in rows], dtype=np.float64)
        dates = sorted({row.key.as_of for row in rows})
        day_map = {day: i for i, day in enumerate(dates)}
        fit_array_memory(values, np.asarray([train.targets[i] for i in order]),
                         np.asarray([train.weights[i] for i in order]),
                         np.asarray([day_map[row.key.as_of] for row in rows]), np.arange(len(rows)),
                         self.artifact_root, training_cutoff=max(train.label_available_at).isoformat(),
                         fit_date=train.validation_start.isoformat(), training_dataset_id=train.dataset_id)
        return FittedAnalogue(self.artifact_root, alpha=self.alpha, k=self.k)


class FittedFusion:
    def __init__(self, xgb, analogue, weight):
        require(weight in (0., 0.25, 0.5, 1.), "FUSION_WEIGHT_INVALID")
        self.xgb, self.analogue, self.weight = xgb, analogue, weight

    @property
    def view(self):
        return "global-local-fusion"

    @property
    def ablation(self):
        return "stock+context"

    @property
    def training_cutoff(self):
        return max(self.xgb.training_cutoff, self.analogue.training_cutoff)

    @property
    def artifact(self):
        state = {"schema_version": "quant.fusion-model/v1", "target_id": TARGET_ID, "weight": self.weight,
                 "xgb_model_version": self.xgb.model_version, "nn_model_version": self.analogue.model_version,
                 "training_cutoff": self.training_cutoff.isoformat(), "fallback": "XGB"}
        return {**state, "model_version": "fusion-" + fingerprint(state)[:24]}

    @property
    def model_version(self):
        return self.artifact["model_version"]

    def predict(self, rows):
        from quant.contracts import SCALAR_NAMES
        xgb = self.xgb.predict(rows)
        positions = tuple(SCALAR_NAMES.index(name) for name in STOCK_NAMES[:7])
        values = [tuple(row.scalars[i] for i in positions) + (row.relative_features[1],) + row.market_context for row in rows]
        nn = self.analogue.predict_arrays(values, as_of=rows[0].key.as_of.isoformat())[self.analogue.k]
        scores = fusion_scores([p.predicted_target_percentile for p in xgb], nn["scores"], self.weight, nn["fallback"])
        return tuple(Prediction(row.key, float(score)) for row, score in zip(rows, scores))
