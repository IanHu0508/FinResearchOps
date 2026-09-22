"""Optional XGBoost adapter. Third-party imports occur only in the ML runtime.

The streaming fit consumes complete TrainingBatch date blocks produced by the
same preparation/split contracts as the in-memory fit. Predict takes no labels.
"""

from datetime import datetime
import json

from quant.contracts import FEATURE_DEFINITION_ID, TARGET_ID, Prediction, canonical, fingerprint, primitive, require
from quant.models.views import vector_names, vectors
from quant.splits.walk_forward import TrainingBatch

DEFAULT_PARAMETERS = {
    "objective": "reg:squarederror", "tree_method": "hist", "max_depth": 4,
    "eta": 0.05, "min_child_weight": 1, "subsample": 1.0, "colsample_bytree": 1.0,
    "lambda": 1.0, "alpha": 0.0, "seed": 20260919, "nthread": 4,
    "base_score": 0.5, "disable_default_eval_metric": 1,
}
LOSS_ID = "half-squared-interval-distance/v1"
PREREGISTERED_RUNTIME = {"xgboost":"3.4.1","numpy":"2.5.3"}


def interval_objective(lower, upper, weights):
    """Native identity-link custom objective; weight gradients exactly once.

    The DMatrix carries no point labels. Unknown members have already been
    excluded from the supervised batch; they still receive inference scores.
    """
    np, _ = _libraries()
    lower, upper, weights = (np.asarray(v,dtype=np.float64) for v in (lower,upper,weights))
    require(lower.shape == upper.shape == weights.shape and lower.ndim == 1
            and bool(np.isfinite(lower).all() and np.isfinite(upper).all() and np.isfinite(weights).all())
            and bool(((0<=lower)&(lower<=upper)&(upper<=1)&(weights>0)).all()),
            "XGBOOST_INTERVAL_INPUT_INVALID")
    def objective(prediction, matrix):
        require(matrix.num_row() == len(lower) and matrix.get_label().size == 0,
                "XGBOOST_POINT_LABELS_FORBIDDEN")
        values=np.asarray(prediction,dtype=np.float64).reshape(-1)
        require(values.shape == lower.shape and bool(np.isfinite(values).all()), "XGBOOST_NONFINITE_PREDICTION")
        residual=values-np.clip(values,lower,upper)
        active=(values<lower)|(values>upper)|(lower==upper)
        return (residual*weights).astype(np.float32),(active*weights).astype(np.float32)
    return objective


def _libraries():
    import numpy as np
    import xgboost as xgb
    return np, xgb


def _matrix(values, names):
    np, xgb = _libraries()
    matrix = np.asarray([[np.nan if v is None else v for v in row] for row in values], dtype=np.float32)
    require(matrix.ndim == 2 and matrix.shape[1] == len(names), "XGBOOST_FEATURE_SHAPE_INVALID")
    require(not np.isinf(matrix).any(), "XGBOOST_INFINITE_FEATURE")
    return matrix


class XGBoostModel:
    def __init__(self, *, ablation="stock-only", parameters=None, num_boost_round=200):
        self.ablation = ablation
        self.view = "tabular"
        vector_names(self.view, ablation)
        self.parameters = dict(DEFAULT_PARAMETERS if parameters is None else parameters)
        require(self.parameters.get("objective") == "reg:squarederror", "XGBOOST_OBJECTIVE_MISMATCH")
        self.parameters.setdefault("base_score",0.5)
        self.parameters.setdefault("disable_default_eval_metric",1)
        require(self.parameters["base_score"]==0.5 and self.parameters["disable_default_eval_metric"]==1,
                "XGBOOST_INTERVAL_INITIALIZATION_INVALID")
        require(type(num_boost_round) is int and num_boost_round > 0, "XGBOOST_ROUNDS_INVALID")
        self.num_boost_round = num_boost_round

    def fit(self, train):
        return self.fit_batches((train,))

    def fit_batches(self, batches):
        """Project one bounded block at a time; do not retain full sequences.

        A scoring date must occur in exactly one batch so its original
        date-equal weights cannot be changed by arbitrary chunking.
        """
        np, xgb = _libraries()
        require(xgb.__version__==PREREGISTERED_RUNTIME['xgboost']
                and np.__version__==PREREGISTERED_RUNTIME['numpy'],'XGBOOST_RUNTIME_NOT_PREREGISTERED')
        names = vector_names(self.view, self.ablation)
        xs, ys, ws, ids = [], [], [], []
        seen_dates = set()
        cutoff = boundary = None
        for batch in batches:
            require(isinstance(batch, TrainingBatch), "XGBOOST_TRAINING_BATCH_REQUIRED")
            dates = {r.key.as_of for r in batch.rows}
            require(not (dates & seen_dates), "XGBOOST_TRAINING_DATE_REPEATED")
            seen_dates.update(dates)
            require(boundary is None or boundary == batch.validation_start, "XGBOOST_SPLIT_BOUNDARY_CHANGED")
            boundary = batch.validation_start
            ready = max(batch.label_available_at)
            cutoff = max(cutoff, ready) if cutoff is not None else ready
            xs.append(_matrix(vectors(batch.rows, self.view, self.ablation), names))
            ys.append(np.asarray(batch.targets, dtype=np.float64))
            ws.append(np.asarray(batch.weights, dtype=np.float64))
            ids.append(batch.dataset_id)
        require(bool(xs), "XGBOOST_EMPTY_TRAINING")
        x = np.concatenate(xs); y = np.concatenate(ys); weights = np.concatenate(ws)
        require(y.ndim==2 and y.shape[1]==2,"XGBOOST_INTERVAL_TARGET_REQUIRED")
        training = xgb.DMatrix(x, weight=weights, feature_names=list(names), nthread=4)
        # No eval_set, early stopping or test-dependent parameter selection.
        booster = xgb.train(self.parameters, training, num_boost_round=self.num_boost_round,
                            obj=interval_objective(y[:,0],y[:,1],weights))
        state = {"schema_version": "quant.xgboost-model/v2", "view": self.view, "ablation": self.ablation,
                 "loss_id":LOSS_ID,"training_point_labels_present":False,
                 "target_id": TARGET_ID, "feature_definition_id": FEATURE_DEFINITION_ID,
                 "feature_names": list(names), "training_cutoff": primitive(cutoff),
                 "validation_start": primitive(boundary), "training_dataset_ids": ids,
                 "training_dataset_id": fingerprint(ids), "training_rows": len(y),
                 "training_dates": len(seen_dates), "date_equal_weight_sum": float(weights.sum(dtype=np.float64)),
                 "parameters": self.parameters, "num_boost_round": self.num_boost_round,
                 "xgboost_version": xgb.__version__, "numpy_version": np.__version__,
                 "prediction_bound_policy": "CLIP_TO_UNIT_INTERVAL; RAW_SCORES_AVAILABLE_VIA_PREDICT_VECTORS",
                 "score_semantics": "Estimated future return target percentile, not an upward probability.",
                 "booster": json.loads(booster.save_raw(raw_format="json"))}
        return FittedXGBoost(booster, state)


class FittedXGBoost:
    def __init__(self, booster, state):
        self._booster = booster
        self._state = json.loads(canonical(state))
        self.ablation, self.view = state["ablation"], state["view"]
        self.training_cutoff = datetime.fromisoformat(state["training_cutoff"])
        self._model_version = "xgboost-v2-" + fingerprint(self._state)[:24]

    @property
    def model_version(self):
        return self._model_version

    @property
    def artifact(self):
        return {**json.loads(canonical(self._state)), "model_version": self.model_version}

    def predict_vectors(self, keys, values, *, feature_names):
        """Equivalent bounded tabular path for persisted shared model views."""
        np, xgb = _libraries()
        expected = vector_names(self.view, self.ablation)
        require(tuple(feature_names) == expected, "XGBOOST_FEATURE_DEFINITION_MISMATCH")
        require(len(keys) == len(values) and len(set(keys)) == len(keys), "XGBOOST_PREDICTION_KEYS_INVALID")
        matrix = _matrix(values, expected)
        raw = self._booster.predict(xgb.DMatrix(matrix, feature_names=list(expected), nthread=4))
        require(bool(np.isfinite(raw).all()), "XGBOOST_NONFINITE_PREDICTION")
        bounded = np.clip(raw, 0, 1)
        return tuple(Prediction(k, float(v)) for k, v in zip(keys, bounded)), tuple(float(v) for v in raw)

    def predict(self, rows):
        return self.predict_vectors(tuple(r.key for r in rows), vectors(rows, self.view, self.ablation),
                                    feature_names=vector_names(self.view, self.ablation))[0]


def restore_xgboost_model(document):
    np, xgb = _libraries()
    state = {k: v for k, v in document.items() if k != "model_version"}
    require(state.get("schema_version") == "quant.xgboost-model/v2" and state.get("loss_id")==LOSS_ID
            and state.get("training_point_labels_present") is False, "XGBOOST_MODEL_SCHEMA_INVALID")
    require(state.get("target_id") == TARGET_ID and state.get("feature_definition_id") == FEATURE_DEFINITION_ID,
            "XGBOOST_RESEARCH_DEFINITION_MISMATCH")
    require("xgboost-v2-" + fingerprint(state)[:24] == document.get("model_version"), "XGBOOST_MODEL_CONTENT_MISMATCH")
    require(state.get("xgboost_version") == xgb.__version__, "XGBOOST_RUNTIME_VERSION_MISMATCH")
    require(state.get("numpy_version") == np.__version__, "XGBOOST_RUNTIME_VERSION_MISMATCH")
    require(state.get("view") == "tabular" and state.get("feature_names") == list(vector_names("tabular", state.get("ablation"))),
            "XGBOOST_MODEL_VIEW_INVALID")
    booster = xgb.Booster()
    booster.load_model(bytearray(canonical(state["booster"]).encode()))
    fitted = FittedXGBoost(booster, state)
    require(fitted.model_version == document.get("model_version"), "XGBOOST_MODEL_CONTENT_MISMATCH")
    return fitted
