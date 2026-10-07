"""Dated, restorable state estimators for numerical research."""

from datetime import datetime
import json

from quant.contracts import TARGET_ID, fingerprint, require
from quant.state_data import timestamp_ns
from quant.models.state_metric import (
    ExactStateIndex, coefficients, date_kernel, interval_predictions, normalize,
)


class FittedStateRetrieval:
    def __init__(self, data, memory, artifact, *, workers=4):
        import numpy as np
        require(artifact["schema_version"] == "quant.state-model/v1"
                and artifact["target_id"] == TARGET_ID
                and artifact["frozen_dataset_id"] == data.manifest["frozen_dataset_id"]
                and artifact["fit_cutoff"] == memory["cutoff"]
                and artifact["training_rows"] == len(memory["ids"]),
                "STATE_MODEL_MEMORY_BINDING")
        self.data, self.memory, self.artifact = data, memory, artifact
        self.cutoff = datetime.fromisoformat(artifact["fit_cutoff"])
        require(self.cutoff.tzinfo is not None
                and (memory["available"] < timestamp_ns(self.cutoff)).all(),
                "STATE_MODEL_FUTURE_LABEL")
        weights = np.asarray(artifact["weights"], dtype=np.float64)
        self.index = ExactStateIndex(data.values[memory["ids"]], data.day_codes[memory["ids"]],
                                     memory["ids"], artifact["transform"], weights,
                                     artifact["eta"], workers=workers)
        self.model_version = "state-v3-" + fingerprint(artifact)[:24]
        self.center, self.risk_center = artifact["rank_center"], artifact["risk_center"]

    def predict_arrays(self, values, *, as_of):
        import numpy as np
        when = datetime.fromisoformat(as_of)
        require(when.tzinfo is not None and when >= self.cutoff,
                "STATE_MODEL_AFTER_SCORING")
        values = np.asarray(values)
        _, missing = normalize(values, self.artifact["transform"])
        valid = ((~missing[:, :5]).sum(axis=1) >= 4)
        valid &= ((~missing[:, 5:7]).sum(axis=1) >= 1)
        valid &= ((~missing[:, 7:10]).sum(axis=1) >= 2)
        valid &= ((~missing[:, 10:]).sum(axis=1) >= 3)
        k = 64
        output = {
            "scores": np.full(len(values), self.center),
            "risk_raw": np.full(len(values), self.risk_center),
            "fallback": (~valid).astype(np.uint8),
            "neighbors": np.full((len(values), k), -1, dtype=np.int32),
            "date_count": np.zeros(len(values), dtype=np.uint16),
            "effective_dates": np.zeros(len(values)), "mean_distance": np.zeros(len(values)),
        }
        ids = np.flatnonzero(valid)
        for start in range(0, len(ids), 128):
            current = ids[start:start + 128]
            retrieved = self.index.query(values[current], k=k, cap=8)
            accepted, neighbours, distances = [], [], []
            for query, (selected, distance) in zip(current, retrieved):
                if len(selected) < k:
                    output["fallback"][query] = 2
                    continue
                accepted.append(query); neighbours.append(selected); distances.append(distance)
            if not accepted:
                continue
            selected, distance = np.stack(neighbours), np.stack(distances)
            dates = self.index.days[selected]
            kernel = date_kernel(distance, dates, self.artifact["tau"],
                                 equal=self.artifact["kind"] == "ordinary")
            output["scores"][accepted] = interval_predictions(self.memory["targets"][selected],
                                                              kernel, self.center)
            output["risk_raw"][accepted] = (kernel * self.memory["risk"][selected]).sum(axis=1)
            output["neighbors"][accepted] = selected
            output["mean_distance"][accepted] = (kernel * np.sqrt(distance)).sum(axis=1)
            for row, query in enumerate(accepted):
                unique, reverse = np.unique(dates[row], return_inverse=True)
                date_weights = np.bincount(reverse, weights=kernel[row])
                output["date_count"][query] = len(unique)
                output["effective_dates"][query] = 1 / np.sum(date_weights ** 2)
        return output

    def neighbour_weights(self, values, neighbors):
        """Reconstruct exact persisted weights without storing large weight files."""
        import numpy as np
        from quant.models.state_metric import pair_distances
        q, qm = normalize(values, self.artifact["transform"])
        selected = np.asarray(neighbors)
        require(selected.ndim == 2 and len(selected) == len(q)
                and (selected >= 0).all() and (selected < len(self.index.values)).all(),
                "STATE_NEIGHBOR_RECONSTRUCTION_INVALID")
        c, cm = normalize(self.index.values[selected].reshape(-1, 14),
                           self.artifact["transform"])
        c, cm = c.reshape(len(q), selected.shape[1], 14), cm.reshape(len(q), selected.shape[1], 14)
        distance = pair_distances(q, c, qm, cm, self.index.weights, self.artifact["eta"])
        return date_kernel(distance, self.index.days[selected], self.artifact["tau"],
                           equal=self.artifact["kind"] == "ordinary")


def retrieval_artifact(data, memory, transform, *, kind, parameters=None, mixed=False,
                       fit_details=None):
    import numpy as np
    require(kind in ("ordinary", "rank", "risk"), "STATE_MODEL_KIND")
    if kind == "ordinary":
        weights, eta, tau = np.ones(14) / 14, 0., 1.
    else:
        weights, eta, tau = coefficients(parameters, mixed=mixed)
    w = memory["weights"] / memory["weights"].sum()
    center = float(interval_predictions(memory["targets"][None, :, :], w[None, :], .5)[0])
    return {
        "schema_version": "quant.state-model/v1", "target_id": TARGET_ID,
        "frozen_dataset_id": data.manifest["frozen_dataset_id"], "kind": kind,
        "fit_cutoff": memory["cutoff"], "training_rows": len(memory["ids"]),
        "latest_label_available_ns": int(memory["available"].max()),
        "transform": transform, "weights": weights.tolist(), "eta": eta, "tau": tau,
        "parameters": None if parameters is None else np.asarray(parameters).tolist(),
        "rank_center": center, "risk_center": float(np.dot(w, memory["risk"])),
        "k": 64, "date_cap": 8, "fit_details": fit_details,
        "score_semantics": "20-session full-pool future return rank",
        "risk_semantics": "expected terminal negative-return loss component",
    }


class FittedStateXGB:
    def __init__(self, booster, artifact):
        self.booster, self.artifact = booster, artifact
        self.cutoff = datetime.fromisoformat(artifact["fit_cutoff"])
        self.model_version = "state-xgb-" + fingerprint(artifact)[:24]

    def predict_arrays(self, values, *, as_of):
        import numpy as np
        import xgboost as xgb
        when = datetime.fromisoformat(as_of)
        require(when.tzinfo is not None and when >= self.cutoff, "STATE_XGB_AFTER_SCORING")
        values = np.asarray(values, dtype=np.float32)
        require(values.ndim == 2 and values.shape[1] == 14 and not np.isinf(values).any(),
                "STATE_XGB_INPUT_SHAPE")
        matrix = np.column_stack((values, np.isnan(values).astype(np.float32)))
        prediction = self.booster.predict(xgb.DMatrix(matrix, nthread=4))
        require(np.isfinite(prediction).all(), "STATE_XGB_NONFINITE")
        return np.clip(prediction, 0, 1) if self.artifact["kind"] == "rank" else np.maximum(prediction, 0)


def fit_state_xgb(data, memory, *, kind):
    import numpy as np
    import xgboost as xgb
    from quant.models.baseline.xgboost_model import DEFAULT_PARAMETERS, interval_objective
    require(kind in ("rank", "risk"), "STATE_XGB_KIND")
    require(datetime.fromisoformat(memory["cutoff"]).tzinfo is not None
            and (memory["available"] < timestamp_ns(datetime.fromisoformat(memory["cutoff"]))).all(),
            "STATE_XGB_FUTURE_LABEL")
    values = np.asarray(data.values[memory["ids"]], dtype=np.float32)
    matrix = np.column_stack((values, np.isnan(values).astype(np.float32)))
    require(not np.isinf(matrix).any(), "STATE_XGB_INFINITE")
    parameters = dict(DEFAULT_PARAMETERS)
    weights = memory["weights"]
    if kind == "rank":
        dm = xgb.DMatrix(matrix, weight=weights, nthread=4)
        objective = interval_objective(memory["targets"][:, 0], memory["targets"][:, 1], weights)
        booster = xgb.train(parameters, dm, num_boost_round=200, obj=objective)
    else:
        parameters["base_score"] = float(np.average(memory["risk"], weights=weights))
        dm = xgb.DMatrix(matrix, label=memory["risk"], weight=weights, nthread=4)
        booster = xgb.train(parameters, dm, num_boost_round=200)
    artifact = {
        "schema_version": "quant.state-xgboost/v1", "kind": kind,
        "fit_cutoff": memory["cutoff"], "training_rows": len(memory["ids"]),
        "frozen_dataset_id": data.manifest["frozen_dataset_id"],
        "input_features": list(data.manifest["features"]), "input_columns": 28,
        "parameters": parameters, "rounds": 200, "xgboost_version": xgb.__version__,
        "booster": json.loads(booster.save_raw(raw_format="json")),
    }
    return FittedStateXGB(booster, artifact)


def restore_state_xgb(artifact):
    import xgboost as xgb
    from quant.state_data import FEATURES
    require(artifact.get("schema_version") == "quant.state-xgboost/v1"
            and artifact["xgboost_version"] == xgb.__version__ and artifact["input_columns"] == 28
            and artifact["input_features"] == list(FEATURES) and artifact["kind"] in ("rank", "risk")
            and datetime.fromisoformat(artifact["fit_cutoff"]).tzinfo is not None,
            "STATE_XGB_RESTORE_METADATA")
    booster = xgb.Booster()
    booster.load_model(bytearray(json.dumps(artifact["booster"]), "utf-8"))
    return FittedStateXGB(booster, artifact)
