"""Causal half-year predictions, risk calibration and neighbour-mixture fitting.

Historical query identities are chosen before looking at outcomes. An OOF
block freezes all base tuning and calibration at its own start. Later meta
training joins only labels available at that later fitting cutoff.
"""

from datetime import datetime, timedelta, timezone
import gc

from quant.contracts import fingerprint, require
from quant.evaluation.rank_bounds import rank_ic_bounds
from quant.models.analogues import file_digest
from quant.models.state_metric import interval_predictions
from quant.state_data import timestamp_ns
from quant.state_artifacts import save_arrays
from quant.state_study import REGULARIZATIONS, code, cutoff, read, write

RISK_ORDER = ("constant", "ordinary", "xgb-risk", "risk-numeric", "risk-mixed")


def date_weights(days):
    import numpy as np
    _, inverse, counts = np.unique(days, return_inverse=True, return_counts=True)
    return 1. / counts[inverse]


def neighbour_pool(model, values, output):
    """Reconstruct real neighbour intervals, with explicit constant fallbacks."""
    import numpy as np
    n = len(values)
    targets = np.full((n, 64, 2), model.center)
    weights = np.zeros((n, 64))
    weights[:, 0] = 1.
    valid = np.flatnonzero(output["fallback"] == 0)
    if len(valid):
        neighbors = output["neighbors"][valid]
        targets[valid] = model.memory["targets"][neighbors]
        weights[valid] = model.neighbour_weights(values[valid], neighbors)
    return targets, weights


def saved_pool(data, memory, artifact, values, output):
    """Read a frozen forecast's neighbours without rebuilding its search index."""
    import numpy as np
    from quant.models.state_metric import date_kernel, normalize, pair_distances
    n = len(values)
    targets = np.full((n, 64, 2), artifact["rank_center"])
    weights = np.zeros((n, 64))
    weights[:, 0] = 1.
    ids = np.full((n, 64), -1, np.int64)
    accepted = np.flatnonzero(output["fallback"] == 0)
    for start in range(0, len(accepted), 128):
        rows = accepted[start:start + 128]
        selected = output["neighbors"][rows]
        require(selected.shape == (len(rows), 64) and (selected >= 0).all()
                and (selected < len(memory["ids"])).all(), "STATE_META_NEIGHBOUR_ID")
        absolute = memory["ids"][selected]
        q, qm = normalize(values[rows], artifact["transform"])
        c, cm = normalize(data.values[absolute].reshape(-1, 14), artifact["transform"])
        c, cm = c.reshape(len(rows), 64, 14), cm.reshape(len(rows), 64, 14)
        distance = pair_distances(q, c, qm, cm, np.asarray(artifact["weights"]), artifact["eta"])
        kernel = date_kernel(distance, data.day_codes[absolute], artifact["tau"],
                             equal=artifact["kind"] == "ordinary")
        targets[rows], weights[rows], ids[rows] = memory["targets"][selected], kernel, absolute
    return targets, weights, ids


def merged_diagnostics(data, ids, weights):
    """Add repeated row contributions once; constants are not historical evidence."""
    import numpy as np
    n = len(ids)
    output = {key: np.full(n, np.nan) for key in
              ("history_mean_return", "history_loss_frequency", "history_lower_decile",
               "history_loss_component", "history_return_std", "effective_dates")}
    output["date_count"] = np.zeros(n, np.uint16)
    output["unique_neighbours"] = np.zeros(n, np.uint16)
    for row in range(n):
        valid = ids[row] >= 0
        if not valid.any():
            continue
        unique, inverse = np.unique(ids[row][valid], return_inverse=True)
        mass = np.bincount(inverse, weights=weights[row][valid])
        mass /= mass.sum()
        returns = data.returns[unique]
        require(np.isfinite(returns).all(), "STATE_META_UNKNOWN_NEIGHBOUR_OUTCOME")
        mean = float(np.dot(mass, returns))
        ordering = np.argsort(returns, kind="stable")
        decile = min(np.searchsorted(np.cumsum(mass[ordering]), .1), len(unique) - 1)
        days, reverse = np.unique(data.day_codes[unique], return_inverse=True)
        daily = np.bincount(reverse, weights=mass)
        output["history_mean_return"][row] = mean
        output["history_loss_frequency"][row] = np.dot(mass, returns < 0)
        output["history_lower_decile"][row] = returns[ordering[decile]]
        output["history_loss_component"][row] = np.dot(mass, np.maximum(-returns, 0))
        output["history_return_std"][row] = np.sqrt(np.dot(mass, (returns - mean) ** 2))
        output["effective_dates"][row] = 1 / np.sum(daily ** 2)
        output["date_count"][row], output["unique_neighbours"][row] = len(days), len(unique)
    return output


def bounded_ic(scores, days, returns):
    """Selection-sample IC bounds, explicitly separate from full-market IC."""
    import numpy as np
    lower = []
    for day in np.unique(days):
        rows = days == day
        if rows.sum() < 2:
            return None
        outcomes = tuple(float(v) if np.isfinite(v) else None for v in returns[rows])
        bounds = rank_ic_bounds(tuple(float(v) for v in scores[rows]), outcomes)
        if bounds["lower"] is None:
            return None
        lower.append(bounds["lower"])
    return float(np.mean(lower)) if lower else None


class StateMeta:
    def __init__(self, study):
        self.study, self.data, self.root = study, study.data, study.root
        self.binding = fingerprint(study.protocol)

    def blocks_before(self, boundary):
        """Whole half-years whose final twenty-session endpoint is past."""
        end, result = code(boundary), []
        for year in range(2013, int(end[:4]) + 1):
            for start, stop in ((f"{year}-01-01", f"{year}-07-01"),
                                (f"{year}-07-01", f"{year+1}-01-01")):
                days = [x for x in self.data.manifest["days"] if start <= x["date"] < stop]
                if not days or stop > end or days[-1]["label_end_date"] is None \
                        or days[-1]["label_end_date"] >= end:
                    continue
                try:
                    self.study.inner_blocks(cutoff(start))
                except ValueError as error:
                    require(str(error) == "STATE_INNER_HISTORY_TOO_SHORT", "STATE_META_BLOCK_FAILURE")
                    continue
                result.append((start, stop))
        return result

    def geometry(self, boundary, meta, task):
        names = [f"{task}-{structure}" for structure in ("numeric", "mixed")]
        scores = {}
        for name in names:
            candidate = meta["selected"][name]
            validations = [read(self.root / "validation" / code(boundary) / p[0] /
                                (candidate + ".json")) for p in meta["inner_blocks"]]
            scores[name] = sum(x["loss"] * x["date_weight_sum"] for x in validations) / \
                           sum(x["date_weight_sum"] for x in validations)
        minimum = min(scores.values())
        return next(name for name in names if scores[name] - minimum <= 1e-12)

    def _save_arrays(self, path, arrays):
        save_arrays(path, arrays)

    def block(self, start, stop):
        import numpy as np
        from quant.models.state_gate import calibrate_risk
        path = self.root / "oof" / start
        receipt_path = path / "receipt.json"
        if receipt_path.exists():
            receipt = read(receipt_path)
            require(receipt["schema_version"] == "quant.state-oof/v2"
                    and receipt["period"] == [start, stop] and receipt["protocol_sha256"] == self.binding
                    and receipt["prediction_sha256"] == file_digest(path / "predictions.npz")
                    and receipt["models_sha256"] == file_digest(self.root / "models" / start / "manifest.json")
                    and receipt["risk_setup_sha256"] == file_digest(self.root / "meta" / start / "risk.json"),
                    "STATE_OOF_CONTENT_CHANGED")
            setup = read(self.root / "meta" / start / "risk.json")
            centers = []
            for binding in setup["context"]["oof"]:
                prior = self.root / "oof" / binding["start"]
                prior_receipt = read(prior / "receipt.json")
                require(fingerprint(prior_receipt) == binding["receipt_sha256"]
                        and file_digest(prior / "predictions.npz") == prior_receipt["prediction_sha256"],
                        "STATE_OOF_RISK_INPUT_CHANGED")
                with np.load(prior / "predictions.npz", allow_pickle=False) as saved:
                    prediction = saved["raw-constant"]
                    require(len(prediction) > 0 and np.all(prediction == prediction[0]),
                            "STATE_OOF_CONSTANT_INPUT_CHANGED")
                    centers.append(float(prediction[0]))
                if centers[-1] != centers[0]:
                    break
            if centers and all(value == centers[0] for value in centers):
                calibration = setup["calibrators"]["constant"]
                require(calibration is not None and calibration["slope"] == 0.,
                        "STATE_OOF_CONSTANT_CALIBRATION_REVISION_REQUIRED")
            with np.load(path / "predictions.npz", allow_pickle=False) as saved:
                return {key: saved[key].copy() for key in saved.files}, receipt
        boundary = cutoff(start)
        meta = self.study.fit(boundary)
        setup = self.risk_setup(boundary)
        days = [x for x in self.data.manifest["days"] if start <= x["date"] < stop]
        pool = {"ids": np.arange(days[0]["start"], days[-1]["stop"], dtype=np.int64)}
        query_ids = pool["ids"][self.study._anchor_ids(pool, period=(start, stop))]
        values = self.data.values[query_ids]
        rank_name, risk_name = (self.geometry(boundary, meta, task) for task in ("rank", "risk"))
        mixture_names = ("ordinary", rank_name, risk_name)
        arrays = {
            "ids": query_ids, "days": self.data.day_codes[query_ids].copy(),
            "as_of": np.array([timestamp_ns(datetime.fromisoformat(
                self.data.manifest["days"][int(day)]["date"] + "T21:30:00+08:00"))
                for day in self.data.day_codes[query_ids]], dtype=np.int64),
            "model_cutoff": np.full(len(query_ids), timestamp_ns(datetime.fromisoformat(boundary)), np.int64),
            "targets": np.zeros((len(query_ids), 3, 64, 2)),
            "weights": np.zeros((len(query_ids), 3, 64)),
            "fallback": np.zeros((len(query_ids), 3), np.uint8),
        }
        memory, view = self.study.view(boundary)
        arrays["raw-constant"] = np.full(len(query_ids), view["risk_center"])
        arrays["valid-constant"] = np.zeros(len(query_ids), bool)
        for name in meta["model_names"]:
            if name == "xgb-rank" or name.startswith("rank-") and name != rank_name:
                continue
            model = self.study.load(boundary, name)
            output = model.predict_arrays(values, as_of=start + "T21:30:00+08:00")
            if name == "xgb-risk":
                arrays["raw-" + name] = output
                arrays["valid-" + name] = np.isfinite(values).any(axis=1)
            else:
                if name in RISK_ORDER:
                    arrays["raw-" + name] = output["risk_raw"]
                    arrays["valid-" + name] = output["fallback"] == 0
                if name in mixture_names:
                    column = mixture_names.index(name)
                    targets, weights = neighbour_pool(model, values, output)
                    arrays["targets"][:, column] = targets
                    arrays["weights"][:, column] = weights
                    arrays["fallback"][:, column] = output["fallback"]
            del model
            gc.collect()
        for name in RISK_ORDER:
            calibration = setup["calibrators"][name]
            raw = arrays["raw-" + name]
            arrays["cal-" + name] = raw if calibration is None else calibrate_risk(
                raw, calibration, as_of=start + "T21:30:00+08:00")
        arrays["selected_risk"] = arrays["cal-" + setup["selected"]].copy()
        arrays["selected_risk_valid"] = arrays["valid-" + setup["selected"]].copy()
        self._save_arrays(path / "predictions.npz", arrays)
        receipt = {
            "schema_version": "quant.state-oof/v2", "period": [start, stop],
            "protocol_sha256": self.binding, "queries": len(query_ids),
            "identity_selection": "fixed row hash before outcomes", "bounded_learning_sample": True,
            "prediction_sha256": file_digest(path / "predictions.npz"),
            "models_sha256": file_digest(self.root / "models" / start / "manifest.json"),
            "risk_setup_sha256": file_digest(self.root / "meta" / start / "risk.json"),
            "mixture_views": list(mixture_names), "selected_risk": setup["selected"],
            "constant_fallbacks": int((arrays["fallback"] != 0).any(axis=1).sum()),
        }
        write(receipt_path, receipt)
        print(canonical_progress("STATE_OOF_BLOCK", start=start, queries=len(query_ids)), flush=True)
        return arrays, receipt

    def rows_before(self, boundary, *, periods=None):
        import numpy as np
        memory, _ = self.study.view(boundary)
        blocks = self.blocks_before(boundary) if periods is None else periods
        parts, bindings = [], []
        for start, stop in blocks:
            arrays, receipt = self.block(start, stop)
            location = np.searchsorted(memory["ids"], arrays["ids"])
            safe = np.minimum(location, len(memory["ids"]) - 1)
            known = (location < len(memory["ids"])) & (memory["ids"][safe] == arrays["ids"])
            if not known.any():
                continue
            part = {key: value[known] for key, value in arrays.items()}
            selected = location[known]
            part.update(query_targets=memory["targets"][selected], losses=memory["risk"][selected],
                        label_available=memory["available"][selected],
                        returns=self.data.returns[part["ids"]].copy())
            parts.append(part)
            bindings.append({"start": start, "receipt_sha256": fingerprint(receipt)})
        if not parts:
            return None, bindings
        result = {key: np.concatenate([x[key] for x in parts]) for key in parts[0]}
        result["date_weights"] = date_weights(result["days"])
        return result, bindings

    def risk_setup(self, boundary):
        import numpy as np
        from quant.models.state_gate import fit_risk_calibration
        path = self.root / "meta" / code(boundary) / "risk.json"
        rows, bindings = self.rows_before(boundary)
        context = {"fit_cutoff": boundary, "protocol_sha256": self.binding, "oof": bindings,
                   "view_sha256": file_digest(self.root / "views" / code(boundary) / "manifest.json")}
        if path.exists():
            result = read(path)
            require(result["context"] == context, "STATE_RISK_SETUP_CONTEXT_CHANGED")
            if rows is not None:
                center = float(np.average(rows["losses"], weights=rows["date_weights"]))
                for name in RISK_ORDER:
                    prediction = rows["raw-" + name]
                    if np.all(prediction == prediction[0]):
                        calibration = result["calibrators"][name]
                        require(calibration is not None and calibration["slope"] == 0.
                                and abs(calibration["intercept"] - center) <= 1e-12,
                                "STATE_RISK_CONSTANT_CALIBRATION_REVISION_REQUIRED")
            return result
        calibrators, mse = {}, {}
        for name in RISK_ORDER:
            if rows is None:
                calibrators[name], mse[name] = None, None
                continue
            calibrators[name] = fit_risk_calibration(
                rows["raw-" + name], rows["losses"], rows["date_weights"], fitting_cutoff=boundary,
                as_of_ns=rows["as_of"], model_cutoff_ns=rows["model_cutoff"],
                label_available_ns=rows["label_available"])
            # Selection uses each block's already frozen calibrated prediction,
            # never a freshly fitted calibrator evaluated on its own labels.
            residual = rows["cal-" + name] - rows["losses"]
            mse[name] = float(np.average(residual ** 2, weights=rows["date_weights"]))
        minimum = min(mse.values()) if rows is not None else None
        selected = "constant" if rows is None else next(
            name for name in RISK_ORDER if mse[name] - minimum <= 1e-12)
        result = {"schema_version": "quant.state-risk-setup/v1", "context": context,
                  "selected": selected, "calibrators": calibrators, "causal_calibrated_oof_mse": mse,
                  "cold_start": rows is None, "training_rows": 0 if rows is None else len(rows["ids"]),
                  "selector_uses_past_predictions": True, "tie_order": list(RISK_ORDER)}
        write(path, result)
        return result

    def _fit_gate(self, rows, boundary, penalty, risk, *, constant=False, g0=None):
        import numpy as np
        from quant.models.state_gate import fit_gate, gate_inputs
        require(rows is not None, "STATE_GATE_HISTORY_EMPTY")
        risk_value = np.where(rows["selected_risk_valid"], rows["selected_risk"], np.nan) if risk else None
        values = gate_inputs(self.data.values[rows["ids"]], risk_value)
        if constant:
            values = np.zeros_like(values)
        w = rows["date_weights"] / rows["date_weights"].sum()
        center = float(interval_predictions(rows["query_targets"][None], w[None], .5)[0])
        # This is a fixed contribution to the fitting objective, not an OOF
        # forecast: the G0 model has already been fitted at this same cutoff.
        fallback = self._apply_gate(rows, g0, training_cutoff=boundary)[0] if risk else None
        fitted = fit_gate(values, rows["targets"], rows["weights"], rows["query_targets"],
                          rows["date_weights"], center=center, penalty=penalty,
                          fitting_cutoff=boundary, as_of_ns=rows["as_of"],
                          model_cutoff_ns=rows["model_cutoff"], label_available_ns=rows["label_available"],
                          risk_valid=rows["selected_risk_valid"] if risk else None, fallback_scores=fallback)
        fitted["constant_only"] = constant
        return fitted

    def _apply_gate(self, arrays, artifact, *, g0_result=None, training_cutoff=None):
        import numpy as np
        from quant.models.state_gate import apply_gate, gate_inputs
        risk = np.where(arrays["selected_risk_valid"], arrays["selected_risk"], np.nan) if artifact["risk_input"] else None
        values = gate_inputs(self.data.values[arrays["ids"]], risk)
        if artifact.get("constant_only"):
            values = np.zeros_like(values)
        first = (datetime(1970, 1, 1, tzinfo=timezone.utc) +
                 timedelta(microseconds=int(arrays["as_of"].min()) // 1000)).isoformat()
        if training_cutoff is not None:
            require(training_cutoff == artifact["fit_cutoff"], "STATE_GATE_TRAINING_CONTEXT")
            first = training_cutoff
        scores, pi = apply_gate(values, arrays["targets"], arrays["weights"], artifact, as_of=first)
        if artifact["risk_input"]:
            unavailable = ~arrays["selected_risk_valid"]
            if unavailable.any():
                require(g0_result is not None, "STATE_G1_REQUIRES_G0_FALLBACK")
                scores[unavailable], pi[unavailable] = g0_result[0][unavailable], g0_result[1][unavailable]
        return scores, pi

    def gates(self, boundary):
        import numpy as np
        path = self.root / "meta" / code(boundary) / "gates.json"
        self.risk_setup(boundary)
        rows, bindings = self.rows_before(boundary)
        context = {"fit_cutoff": boundary, "protocol_sha256": self.binding, "oof": bindings}
        if path.exists():
            result = read(path)
            require(result["schema_version"] == "quant.state-gate-setup/v2"
                    and result["context"] == context, "STATE_GATE_SETUP_CONTEXT_CHANGED")
            return result
        require(rows is not None, "STATE_GATE_HISTORY_EMPTY")
        validation_blocks = self.blocks_before(boundary)[-2:]
        output, attempts = {}, []
        for risk in (False, True):
            scores = {r: [] for r in REGULARIZATIONS}
            for start, stop in validation_blocks:
                past, _ = self.rows_before(cutoff(start))
                if past is None:
                    continue
                arrays, _ = self.block(start, stop)
                available = self.data.available[arrays["ids"]] < timestamp_ns(datetime.fromisoformat(boundary))
                outcomes = np.where(available, self.data.returns[arrays["ids"]], np.nan)
                for penalty in REGULARIZATIONS:
                    g0 = self.gates(cutoff(start))["gates"]["G0"] if risk else None
                    artifact = self._fit_gate(past, cutoff(start), penalty, risk, g0=g0)
                    baseline = self._apply_gate(arrays, g0) if risk else None
                    prediction, _ = self._apply_gate(arrays, artifact, g0_result=baseline)
                    value = bounded_ic(prediction, arrays["days"], outcomes)
                    scores[penalty].append(value)
                    attempts.append({"risk_input": risk, "period": [start, stop], "penalty": penalty,
                                     "bounded_query_ic_lower": value, "fit": artifact})
            eligible = {r: float(np.mean(v)) for r, v in scores.items() if v and all(x is not None for x in v)}
            if eligible:
                maximum = max(eligible.values())
                penalty = next(r for r in REGULARIZATIONS if r in eligible and maximum - eligible[r] <= 1e-12)
                output["G1" if risk else "G0"] = self._fit_gate(rows, boundary, penalty, risk,
                                                               g0=output.get("G0") if risk else None)
            else:
                output["G1" if risk else "G0"] = self._fit_gate(rows, boundary, .01, risk, constant=True,
                                                               g0=output.get("G0") if risk else None)
        result = {"schema_version": "quant.state-gate-setup/v2", "context": context,
                  "gates": output, "attempts": attempts,
                  "selection_scope": "bounded immutable learning queries; full outer pool unchanged"}
        write(path, result)
        return result

    def schedule(self, years=range(2018, 2026)):
        annual = [cutoff(f"{year}-01-01") for year in years]
        base = sorted(set(annual + [cutoff(start) for b in annual for start, _ in self.blocks_before(b)]))
        inner = sorted({cutoff(start) for b in base for start, _ in self.study.inner_blocks(b)})
        result = {"schema_version": "quant.state-schedule/v1", "protocol_sha256": self.binding,
                  "annual_cutoffs": annual, "all_base_cutoffs": base,
                  "shared_inner_cutoffs": inner, "metric_candidate_attempt_upper_bound": 24 * len(inner),
                  "final_geometry_refit_upper_bound": 4 * len(base), "xgb_fits": 2 * len(base),
                  "per_base_geometry_limit": 52, "identical_cutoff_outputs_reused": True,
                  "oof_query_limit_per_half": self.study.query_limit,
                  "final_scoring_pool_reduced": False, "all_periods_previously_seen": True}
        write(self.root / "SCHEDULE.json", result)
        return result

    def forecast(self, year):
        import numpy as np
        from quant.models.state_gate import calibrate_risk
        boundary = cutoff(f"{year}-01-01")
        meta = self.study.fit(boundary)
        setup, gates = self.risk_setup(boundary), self.gates(boundary)
        views = ("ordinary", self.geometry(boundary, meta, "rank"),
                 self.geometry(boundary, meta, "risk"))
        required = set(views) | set(RISK_ORDER[1:])
        self.study.forecast(year, names=[x for x in meta["model_names"] if x in required])
        dates = self.study.dates(year)
        context = {"protocol_sha256": self.binding, "year": year, "fit_cutoff": boundary,
                   "models_sha256": file_digest(self.root / "models" / code(boundary) / "manifest.json"),
                   "risk_sha256": file_digest(self.root / "meta" / code(boundary) / "risk.json"),
                   "gates_sha256": file_digest(self.root / "meta" / code(boundary) / "gates.json"),
                   "dates": dates, "views": list(views), "all_original_members": True}
        folders = {name: self.root / "predictions" / name / str(year) for name in ("G0", "G1", "risk")}
        receipts = {name: folder / "RECEIPT.json" for name, folder in folders.items()}
        if all(path.exists() for path in receipts.values()):
            for name, path in receipts.items():
                saved = read(path)
                require(saved["context"] == context and all(
                    file_digest(folders[name] / (day + ".npz")) == digest
                    for day, digest in saved["files"].items()), "STATE_META_FORECAST_CHANGED")
            return
        source_receipts = {name: read(self.root / "predictions" / name / str(year) / "RECEIPT.json")
                           for name in required}
        artifacts = {name: read(self.root / "models" / code(boundary) / (name + ".json")) for name in views}
        memory, view = self.study.view(boundary)
        hashes = {name: {} for name in folders}
        for ordinal, day in enumerate(dates):
            values, saved = self.data.rows(day), {}
            for name in required:
                path = self.root / "predictions" / name / str(year) / (day + ".npz")
                require(file_digest(path) == source_receipts[name]["files"][day],
                        "STATE_META_BASE_FORECAST_CHANGED")
                with np.load(path, allow_pickle=False) as arrays:
                    saved[name] = {key: arrays[key].copy() for key in arrays.files}
            risk = {"raw-constant": np.full(len(values), view["risk_center"]),
                    "valid-constant": np.zeros(len(values), bool)}
            for name in RISK_ORDER[1:]:
                risk["raw-" + name] = saved[name]["risk_raw"]
                risk["valid-" + name] = (np.isfinite(values).any(axis=1) if name == "xgb-risk"
                                        else saved[name]["fallback"] == 0)
            for name in RISK_ORDER:
                calibration = setup["calibrators"][name]
                risk["cal-" + name] = risk["raw-" + name] if calibration is None else calibrate_risk(
                    risk["raw-" + name], calibration, as_of=day + "T21:30:00+08:00")
            risk["risk_raw"] = risk["cal-" + setup["selected"]]
            risk["selected_risk_valid"] = risk["valid-" + setup["selected"]]
            pools = [saved_pool(self.data, memory, artifacts[name], values, saved[name]) for name in views]
            targets = np.stack([x[0] for x in pools], axis=1)
            weights = np.stack([x[1] for x in pools], axis=1)
            ids = np.stack([x[2] for x in pools], axis=1).reshape(len(values), -1)
            entry = {"ids": np.arange(self.data.days[day]["start"], self.data.days[day]["stop"]),
                     "as_of": np.full(len(values), timestamp_ns(datetime.fromisoformat(day + "T21:30:00+08:00"))),
                     "targets": targets, "weights": weights, "selected_risk": risk["risk_raw"],
                     "selected_risk_valid": risk["selected_risk_valid"]}
            outputs = {"risk": risk}
            g0_result = None
            for name in ("G0", "G1"):
                scores, pi = self._apply_gate(entry, gates["gates"][name], g0_result=g0_result)
                if name == "G0":
                    g0_result = (scores, pi)
                omega = (pi[:, :, None] * weights).reshape(len(values), -1)
                output = {"scores": scores, "pi": pi,
                          "fallback": np.stack([saved[x]["fallback"] for x in views], axis=1).max(axis=1)}
                if name == "G1":
                    output["risk_fallback_to_G0"] = (~risk["selected_risk_valid"]).astype(np.uint8)
                output.update(merged_diagnostics(self.data, ids, omega))
                outputs[name] = output
            for name, output in outputs.items():
                path = folders[name] / (day + ".npz")
                self._save_arrays(path, output)
                hashes[name][day] = file_digest(path)
            if ordinal % 20 == 0:
                print(canonical_progress("STATE_META_FORECAST_DATE", year=year, date=day, rows=len(values)), flush=True)
        for name, receipt in receipts.items():
            write(receipt, {"schema_version": "quant.state-meta-predictions/v1", "context": context,
                            "files": hashes[name], "risk_source": setup["selected"],
                            "training_approximation": True, "final_pool_reduced": False})


def canonical_progress(stage, **values):
    from quant.contracts import canonical
    return canonical({"stage": stage, **values})
