"""Small dated risk calibration and historical-neighbour mixture models."""

from datetime import datetime
import numpy as np
from scipy.optimize import minimize, lsq_linear

from quant.contracts import fingerprint, require
from quant.models.state_metric import interval_predictions
from quant.state_data import timestamp_ns


def check_oof_times(as_of_ns, model_cutoff_ns, label_available_ns, fitting_cutoff):
    boundary = timestamp_ns(datetime.fromisoformat(fitting_cutoff))
    arrays = tuple(np.asarray(x) for x in (as_of_ns, model_cutoff_ns, label_available_ns))
    require(all(x.ndim == 1 and np.issubdtype(x.dtype, np.integer) for x in arrays)
            and arrays[0].shape == arrays[1].shape == arrays[2].shape
            and len(arrays[0]) > 0 and (arrays[1] < arrays[0]).all()
            and (arrays[2] > arrays[0]).all() and (arrays[2] < boundary).all()
            and (arrays[0] < boundary).all(), "STATE_OOF_TIME_LEAKAGE")


def fit_risk_calibration(prediction, losses, weights, *, fitting_cutoff,
                         as_of_ns, model_cutoff_ns, label_available_ns):
    check_oof_times(as_of_ns, model_cutoff_ns, label_available_ns, fitting_cutoff)
    x, y, w = map(lambda a: np.asarray(a, dtype=np.float64), (prediction, losses, weights))
    require(x.shape == y.shape == w.shape and np.isfinite(x).all() and np.isfinite(y).all()
            and np.isfinite(w).all() and (x >= 0).all() and (y >= 0).all() and (w > 0).all(),
            "STATE_CALIBRATION_INPUT")
    w = w / w.sum()
    if np.all(x == x[0]):
        # A constant predictor has no identified affine slope. Use its exact
        # weighted constant optimum instead of arbitrary cancelling coefficients.
        center = float(np.dot(w, y))
        return {"schema_version": "quant.risk-calibration/v1", "fit_cutoff": fitting_cutoff,
                "intercept": center, "slope": 0., "rows": len(x),
                "loss": float(np.dot(w, (center - y) ** 2)), "optimizer_success": True,
                "latest_label_available_ns": int(np.max(label_available_ns)),
                "oof_binding": fingerprint({
                    "as_of": np.asarray(as_of_ns).tolist(),
                    "models": np.asarray(model_cutoff_ns).tolist(),
                    "labels": np.asarray(label_available_ns).tolist(),
                })}
    matrix = np.column_stack((np.ones(len(x)), x))
    linear = lsq_linear(matrix * np.sqrt(w[:, None]), y * np.sqrt(w),
                        bounds=([-np.inf, 0.], [np.inf, np.inf]))
    def objective(p):
        raw = p[0] + p[1] * x
        residual = np.maximum(raw, 0) - y
        gradient = 2 * (w * residual * (raw > 0)) @ matrix
        return float(np.dot(w, residual ** 2)), gradient
    result = minimize(objective, linear.x, jac=True, method="L-BFGS-B",
                      bounds=[(None, None), (0., None)],
                      options={"maxiter": 100, "ftol": 1e-15, "gtol": 1e-12})
    require(np.isfinite(result.x).all() and np.isfinite(result.fun), "STATE_CALIBRATION_NONFINITE")
    return {"schema_version": "quant.risk-calibration/v1", "fit_cutoff": fitting_cutoff,
            "intercept": float(result.x[0]), "slope": float(result.x[1]),
            "rows": len(x), "loss": float(result.fun),
            "optimizer_success": bool(result.success),
            "latest_label_available_ns": int(np.max(label_available_ns)),
            "oof_binding": fingerprint({
                "as_of": np.asarray(as_of_ns).tolist(),
                "models": np.asarray(model_cutoff_ns).tolist(),
                "labels": np.asarray(label_available_ns).tolist(),
            })}


def calibrate_risk(prediction, artifact, *, as_of):
    require(datetime.fromisoformat(as_of) >= datetime.fromisoformat(artifact["fit_cutoff"]),
            "STATE_CALIBRATION_AFTER_SCORING")
    values = np.asarray(prediction, dtype=np.float64)
    require(np.isfinite(values).all() and (values >= 0).all()
            and artifact["slope"] >= 0, "STATE_CALIBRATION_VALUE")
    return np.maximum(0, artifact["intercept"] + artifact["slope"] * values)


def gate_inputs(values, risk=None):
    values = np.asarray(values, dtype=np.float64)
    require(values.ndim == 2 and values.shape[1] == 14, "STATE_GATE_INPUT_SHAPE")
    result = values[:, (13, 11, 2, 4)]
    if risk is not None:
        risk = np.asarray(risk)
        require(risk.shape == (len(values),) and not np.isinf(risk).any()
                and (risk[np.isfinite(risk)] >= 0).all(),
                "STATE_GATE_RISK_INPUT")
        result = np.column_stack((result, risk))
    return result


def gate_design(values, means=None, scales=None, *, weights=None):
    values = np.asarray(values, dtype=np.float64)
    require(values.ndim == 2 and not np.isinf(values).any(), "STATE_GATE_DESIGN_INPUT")
    if means is None:
        w = np.ones(len(values)) if weights is None else np.asarray(weights, dtype=np.float64)
        require(w.shape == (len(values),) and np.isfinite(w).all() and (w > 0).all(),
                "STATE_GATE_DESIGN_WEIGHTS")
        present = np.isfinite(values)
        mass = (present * w[:, None]).sum(axis=0)
        safe = np.where(present, values, 0.)
        means = (safe * w[:, None]).sum(axis=0) / np.maximum(mass, 1e-30)
        residual = np.where(present, values - means, 0.)
        scales = np.sqrt((residual ** 2 * w[:, None]).sum(axis=0) / np.maximum(mass, 1e-30))
        scales = np.where(scales > 1e-12, scales, 1.)
    means, scales = np.asarray(means), np.asarray(scales)
    require(means.shape == scales.shape == (values.shape[1],) and np.isfinite(means).all()
            and np.isfinite(scales).all() and (scales > 0).all(), "STATE_GATE_DESIGN_SCALE")
    z = np.where(np.isfinite(values), (values - means) / scales, 0)
    return np.column_stack((np.ones(len(z)), z)), np.asarray(means), np.asarray(scales)


def gate_mixture(coefficients, design):
    logits = np.column_stack((np.zeros(len(design)), design @ coefficients.T))
    logits -= logits.max(axis=1, keepdims=True)
    values = np.exp(logits)
    return values / values.sum(axis=1, keepdims=True)


def gate_objective(parameters, design, neighbour_targets, neighbour_weights, query_targets,
                   date_weights, center, penalty, risk_valid=None, fallback_scores=None):
    beta = np.asarray(parameters).reshape(2, design.shape[1])
    pi = gate_mixture(beta, design)
    pooled_targets = neighbour_targets.reshape(len(design), -1, 2)
    pooled_weights = (pi[:, :, None] * neighbour_weights).reshape(len(design), -1)
    prediction = interval_predictions(pooled_targets, pooled_weights, center)
    if risk_valid is not None:
        prediction = np.where(risk_valid, prediction, fallback_scores)
    clipped = np.clip(prediction[:, None, None],
                      neighbour_targets[:, :, :, 0], neighbour_targets[:, :, :, 1])
    active = (prediction[:, None, None] < neighbour_targets[:, :, :, 0]) | (
        prediction[:, None, None] > neighbour_targets[:, :, :, 1]) | (
        neighbour_targets[:, :, :, 0] == neighbour_targets[:, :, :, 1])
    curvature = (pi[:, :, None] * neighbour_weights * active).sum(axis=(1, 2))
    d_pi = (neighbour_weights * (clipped - prediction[:, None, None])).sum(axis=2)
    d_pi /= np.maximum(curvature[:, None], 1e-30)
    d_logit = pi * (d_pi - (pi * d_pi).sum(axis=1, keepdims=True))
    if risk_valid is not None:
        d_logit = np.where(risk_valid[:, None], d_logit, 0)
    residual = prediction - np.clip(prediction, query_targets[:, 0], query_targets[:, 1])
    weights = date_weights / date_weights.sum()
    gradient = ((2 * weights * residual)[:, None] * d_logit[:, 1:]).T @ design
    loss = float(np.dot(weights, residual ** 2)) + penalty * float((beta ** 2).sum())
    gradient += 2 * penalty * beta
    return loss, gradient.ravel()


def fit_gate(values, neighbour_targets, neighbour_weights, query_targets, date_weights, *,
             center, penalty, fitting_cutoff, as_of_ns, model_cutoff_ns, label_available_ns,
             risk_valid=None, fallback_scores=None):
    check_oof_times(as_of_ns, model_cutoff_ns, label_available_ns, fitting_cutoff)
    values = np.asarray(values, dtype=np.float64)
    targets, weights = np.asarray(neighbour_targets), np.asarray(neighbour_weights)
    require(values.ndim == 2 and values.shape[1] in (4, 5)
            and targets.shape[:3] == weights.shape and targets.shape[0] == len(values)
            and targets.shape[1] == 3 and targets.shape[3] == 2
            and np.isfinite(targets).all() and np.isfinite(weights).all()
            and (weights >= 0).all() and np.allclose(weights.sum(axis=2), 1, atol=1e-12),
            "STATE_GATE_NEIGHBOUR_SHAPE")
    query_targets, date_weights = np.asarray(query_targets), np.asarray(date_weights)
    require(query_targets.shape == (len(values), 2) and np.isfinite(query_targets).all()
            and (query_targets[:, 0] <= query_targets[:, 1]).all()
            and ((0 <= query_targets) & (query_targets <= 1)).all()
            and date_weights.shape == (len(values),) and np.isfinite(date_weights).all()
            and (date_weights > 0).all() and np.isfinite(penalty) and penalty >= 0,
            "STATE_GATE_TARGET_INPUT")
    design, means, scales = gate_design(values, weights=date_weights)
    if risk_valid is not None:
        risk_valid, fallback_scores = np.asarray(risk_valid), np.asarray(fallback_scores)
        require(risk_valid.dtype == bool and risk_valid.shape == fallback_scores.shape == (len(values),)
                and np.isfinite(fallback_scores).all() and ((0 <= fallback_scores) & (fallback_scores <= 1)).all(),
                "STATE_GATE_RISK_FALLBACK_SHAPE")
    initial = np.zeros(2 * design.shape[1])
    result = minimize(gate_objective, initial,
                      args=(design, targets, weights, query_targets, date_weights, center, penalty,
                            risk_valid, fallback_scores),
                      jac=True, method="L-BFGS-B",
                      options={"maxiter": 100, "ftol": 1e-12})
    require(np.isfinite(result.x).all() and np.isfinite(result.fun), "STATE_GATE_NONFINITE")
    return {"schema_version": "quant.state-gate/v2", "fit_cutoff": fitting_cutoff,
            "risk_input": values.shape[1] == 5, "means": means.tolist(), "scales": scales.tolist(),
            "coefficients": result.x.reshape(2, -1).tolist(), "penalty": penalty,
            "center": center, "training_rows": len(values), "loss": float(result.fun),
            "iterations": int(result.nit), "optimizer_success": bool(result.success),
            "conditional_risk_rows": None if risk_valid is None else int(risk_valid.sum()),
            "latest_label_available_ns": int(np.max(label_available_ns))}


def apply_gate(values, targets, weights, artifact, *, as_of):
    require(datetime.fromisoformat(as_of) >= datetime.fromisoformat(artifact["fit_cutoff"]),
            "STATE_GATE_AFTER_SCORING")
    design, _, _ = gate_design(np.asarray(values), np.asarray(artifact["means"]),
                               np.asarray(artifact["scales"]))
    pi = gate_mixture(np.asarray(artifact["coefficients"]), design)
    combined = (pi[:, :, None] * weights).reshape(len(values), -1)
    scores = interval_predictions(targets.reshape(len(values), -1, 2), combined, artifact["center"])
    return scores, pi
