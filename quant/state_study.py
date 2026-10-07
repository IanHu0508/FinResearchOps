"""Recoverable state-metric study on the admitted, unchanged full scoring pool.

Learning uses explicitly bounded dated queries. Final predictions enumerate
every original evaluation date and every original member. No test-year result
selects weights, preprocessing, candidates, or model architecture.
"""

import argparse
from datetime import date, datetime
import gc
import json
from pathlib import Path
import time

from quant.artifacts.store import _private_root
from quant.contracts import canonical, fingerprint, require
from quant.models.analogues import file_digest
from quant.models.state_metric import (
    ExactStateIndex, coefficients, date_kernel, fit_metric, interval_predictions,
    normalize, transform_fit,
)
from quant.models.state_models import (
    FittedStateRetrieval, fit_state_xgb, restore_state_xgb, retrieval_artifact,
)
from quant.state_data import CACHE_FILE, StateInputs, timestamp_ns
from quant.state_artifacts import save_arrays

SCHEMA = "quant.state-study/v1"
REGULARIZATIONS = (.01, .001, 0.)
STRUCTURES = ("numeric", "mixed")


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = canonical(value) + "\n"
    if path.exists():
        require(path.read_text() == text, "STATE_APPEND_ONLY_CONFLICT:" + path.name)
    else:
        with path.open("x") as stream:
            stream.write(text)


def code(cutoff):
    return datetime.fromisoformat(cutoff).date().isoformat()


def cutoff(text):
    return text + "T00:00:00+08:00"


def half_start(text):
    return text[:4] + ("-01-01" if int(text[5:7]) <= 6 else "-07-01")


class StateStudy:
    def __init__(self, inputs, cache, *, root=None, verify=True, query_limit=4096):
        self.inputs, self.cache = _private_root(inputs), _private_root(cache)
        self.root = _private_root(root or self.cache / "study")
        self.root.mkdir(parents=True, exist_ok=True)
        self.data = StateInputs(self.inputs, self.cache, verify=verify)
        self.query_limit = query_limit
        require(type(query_limit) is int and 64 <= query_limit <= 10000, "STATE_QUERY_BUDGET")
        self.cache_hash = file_digest(self.cache / CACHE_FILE)
        self.protocol = {
            "schema_version": SCHEMA, "cache_sha256": self.cache_hash,
            "query_limit": query_limit, "candidate_limit": 1024,
            "k": 64, "date_cap": 8, "regularizations": list(REGULARIZATIONS),
            "initializations": [0, 17], "structures": list(STRUCTURES),
            "all_periods_previously_seen": True,
            "final_universe_reduced": False, "training_approximation": True,
            "membership_refresh_limit": 1, "iteration_budget": 100,
        }
        write(self.root / "PROTOCOL.json", self.protocol)

    def view(self, boundary):
        import numpy as np
        path = self.root / "views" / code(boundary)
        manifest_path = path / "manifest.json"
        keys = ("ids", "targets", "weights", "risk", "available")
        if manifest_path.exists():
            manifest = read(manifest_path)
            require(manifest["cutoff"] == boundary and manifest["cache_sha256"] == self.cache_hash,
                    "STATE_VIEW_CONTEXT_CHANGED")
            require(all(file_digest(path / name) == digest for name, digest in manifest["files"].items()),
                    "STATE_VIEW_CONTENT_CHANGED")
            memory = {key: np.load(path / (key + ".npy"), mmap_mode="r", allow_pickle=False) for key in keys}
            memory["cutoff"] = boundary
        else:
            memory = self.data.supervision(boundary)
            path.mkdir(parents=True, exist_ok=True)
            hashes = {}
            for key in keys:
                filename = key + ".npy"
                target = path / filename
                if target.exists():
                    existing = np.load(target, mmap_mode="r", allow_pickle=False)
                    np.testing.assert_array_equal(existing, memory[key])
                else:
                    np.save(target, memory[key], allow_pickle=False)
                hashes[filename] = file_digest(target)
            transform = transform_fit(self.data.values[memory["ids"]], memory["weights"])
            weights = memory["weights"] / memory["weights"].sum()
            center = float(interval_predictions(memory["targets"][None], weights[None], .5)[0])
            risk_center = float(np.dot(weights, memory["risk"]))
            variance = float(np.dot(weights, (memory["risk"] - risk_center) ** 2))
            manifest = {
                "schema_version": "quant.state-view/v1", "cutoff": boundary,
                "cache_sha256": self.cache_hash, "files": hashes,
                "rows": len(memory["ids"]), "transform": transform,
                "rank_center": center, "risk_center": risk_center, "risk_variance": variance,
                "latest_available_ns": int(memory["available"].max()),
            }
            write(manifest_path, manifest)
        require(int(memory["available"].max()) < timestamp_ns(datetime.fromisoformat(boundary)),
                "STATE_VIEW_FUTURE_LABEL")
        return memory, manifest

    def _anchor_ids(self, memory, *, period=None):
        import numpy as np
        groups = []
        for info in self.data.manifest["days"]:
            text = info["date"]
            if text < "2012-01-01":
                continue
            if period is not None and not period[0] <= text < period[1]:
                continue
            lo, hi = np.searchsorted(memory["ids"], [info["start"], info["stop"]])
            if lo < hi:
                groups.append((text, int(lo), int(hi)))
        require(bool(groups), "STATE_NO_LEARNING_QUERIES")
        # All dates or a fixed evenly spaced date grid; securities selected by
        # immutable row identity, never by outcomes or prediction scores.
        if len(groups) > self.query_limit:
            positions = np.unique(np.linspace(0, len(groups) - 1, self.query_limit, dtype=int))
            groups = [groups[i] for i in positions]
        per_date = max(1, self.query_limit // len(groups))
        chosen = []
        for _, lo, hi in groups:
            candidates = np.arange(lo, hi)
            identities = memory["ids"][candidates].astype(np.uint64)
            hashes = identities * np.uint64(11400714819323198485)
            chosen.extend(candidates[np.argsort(hashes, kind="stable")[:per_date]].tolist())
        return np.asarray(chosen, dtype=np.int64)

    def _candidate_rows(self, query_ids, memory, transform):
        """Union of two exact initial views; each date has at most eight rows."""
        import numpy as np
        values = self.data.values[memory["ids"]]
        days = self.data.day_codes[memory["ids"]]
        ids = memory["ids"]
        selections = []
        for eta in (0., .5):
            index = ExactStateIndex(values, days, ids, transform, np.ones(14) / 14, eta)
            selections.append(index.query(self.data.values[query_ids], k=512, cap=4))
            del index
            gc.collect()
        result = []
        for a, b in zip(*selections):
            union = np.unique(np.concatenate((a[0], b[0])))
            require(len(union) >= 64, "STATE_CAUSAL_MEMORY_TOO_SMALL")
            result.append(union)
        return result

    def tensor(self, boundary):
        import numpy as np
        path = self.root / "learning" / code(boundary)
        receipt = path / "receipt.json"
        if receipt.exists():
            meta = read(receipt)
            require(meta["cutoff"] == boundary and meta["protocol_sha256"] == fingerprint(self.protocol),
                    "STATE_LEARNING_CONTEXT_CHANGED")
            require(file_digest(path / "tensor.npz") == meta["tensor_sha256"],
                    "STATE_LEARNING_CONTENT_CHANGED")
            with np.load(path / "tensor.npz", allow_pickle=False) as archive:
                return {key: archive[key].copy() for key in archive.files}, meta
        memory, view = self.view(boundary)
        chosen = self._anchor_ids(memory)
        query_ids = memory["ids"][chosen]
        query_dates = self.data.day_codes[query_ids]
        transform = view["transform"]
        count = len(query_ids)
        tensor = {
            "query_z": np.zeros((count, 14)), "query_missing": np.zeros((count, 14), bool),
            "candidate_z": np.zeros((count, 1024, 14)),
            "candidate_missing": np.ones((count, 1024, 14), bool),
            "candidate_valid": np.zeros((count, 1024), bool),
            "candidate_days": np.full((count, 1024), -1, dtype=np.int32),
            "candidate_targets": np.zeros((count, 1024, 2)),
            "candidate_risk": np.zeros((count, 1024)),
            "query_targets": np.asarray(memory["targets"][chosen]),
            "query_risk": np.asarray(memory["risk"][chosen]), "query_weights": np.zeros(count),
            "risk_variance": np.asarray(view["risk_variance"]), "center": np.asarray(view["rank_center"]),
        }
        q, qm = normalize(self.data.values[query_ids], transform)
        tensor["query_z"][:], tensor["query_missing"][:] = q, qm
        unique, inverse, counts = np.unique(query_dates, return_inverse=True, return_counts=True)
        tensor["query_weights"][:] = 1 / counts[inverse]
        by_half = {}
        for row, identifier in enumerate(query_ids):
            info = self.data.manifest["days"][int(self.data.day_codes[identifier])]
            by_half.setdefault(half_start(info["date"]), []).append(row)
        bindings = []
        for text, rows in sorted(by_half.items()):
            past, past_view = self.view(cutoff(text))
            local = self._candidate_rows(query_ids[rows], past, transform)
            for row, selected in zip(rows, local):
                n = len(selected)
                candidate_ids = past["ids"][selected]
                z, missing = normalize(self.data.values[candidate_ids], transform)
                tensor["candidate_z"][row, :n] = z
                tensor["candidate_missing"][row, :n] = missing
                tensor["candidate_valid"][row, :n] = True
                tensor["candidate_days"][row, :n] = self.data.day_codes[candidate_ids]
                tensor["candidate_targets"][row, :n] = past["targets"][selected]
                tensor["candidate_risk"][row, :n] = past["risk"][selected]
            bindings.append({"query_half": text, "pool_cutoff": past["cutoff"],
                             "latest_candidate_available_ns": past_view["latest_available_ns"],
                             "queries": len(rows)})
            print(canonical({"stage": "LEARNING_CAUSAL_HALF", "fit": code(boundary),
                             "half": text, "queries": len(rows)}), flush=True)
        path.mkdir(parents=True, exist_ok=True)
        if (path / "tensor.npz").exists():
            with np.load(path / "tensor.npz", allow_pickle=False) as old:
                for key in tensor:
                    np.testing.assert_array_equal(old[key], tensor[key])
        else:
            np.savez(path / "tensor.npz", **tensor)
        meta = {
            "schema_version": "quant.state-learning/v1", "cutoff": boundary,
            "protocol_sha256": fingerprint(self.protocol), "queries": count,
            "query_ids_sha256": fingerprint(query_ids.tolist()), "bindings": bindings,
            "tensor_sha256": file_digest(path / "tensor.npz"), "not_full_market_evaluation": True,
        }
        write(receipt, meta)
        return tensor, meta

    def _validation_ids(self, memory, period):
        chosen = self._anchor_ids(memory, period=period)
        return memory["ids"][chosen], memory["targets"][chosen], memory["risk"][chosen]

    def validate_metric(self, fit_boundary, known_boundary, period, parameters, *, task, mixed):
        import numpy as np
        past, view = self.view(fit_boundary)
        known, _ = self.view(known_boundary)
        ids, targets, risk = self._validation_ids(known, period)
        artifact = retrieval_artifact(self.data, past, view["transform"], kind=task,
                                      parameters=parameters, mixed=mixed)
        model = FittedStateRetrieval(self.data, past, artifact)
        output = model.predict_arrays(self.data.values[ids], as_of=cutoff(period[0]))
        days = self.data.day_codes[ids]
        _, inverse, count = np.unique(days, return_inverse=True, return_counts=True)
        weights = 1 / count[inverse]
        residual = (output["risk_raw"] - risk) if task == "risk" else (
            output["scores"] - np.clip(output["scores"], targets[:, 0], targets[:, 1]))
        loss = float(np.dot(weights, residual ** 2) / weights.sum())
        return {"loss": loss, "date_weight_sum": float(weights.sum()), "queries": len(ids),
                "fallback": int((output["fallback"] != 0).sum()), "exact_memory_search": True,
                "selection_queries_are_bounded": True}

    def inner_blocks(self, boundary):
        end = code(boundary)
        eligible = []
        for year in range(2013, int(end[:4]) + 1):
            for start, stop in ((f"{year}-01-01", f"{year}-07-01"),
                                (f"{year}-07-01", f"{year+1}-01-01")):
                dates = [d for d in self.data.manifest["days"] if start <= d["date"] < stop]
                if dates and stop <= end and dates[-1]["label_end_date"] is not None \
                        and dates[-1]["label_end_date"] < end:
                    eligible.append((start, stop))
        require(len(eligible) >= 2, "STATE_INNER_HISTORY_TOO_SHORT")
        return eligible[-2:]

    def fit(self, boundary, *, xgb=True):
        import numpy as np
        folder = self.root / "models" / code(boundary)
        completed = folder / "manifest.json"
        if completed.exists():
            meta = read(completed)
            require(meta["cutoff"] == boundary and (not xgb or "xgb-risk" in meta["model_names"])
                    and meta["protocol_sha256"] == fingerprint(self.protocol)
                    and all(file_digest(folder / name) == h for name, h in meta["files"].items()),
                    "STATE_FIT_CONTENT_CHANGED")
            return meta
        folder.mkdir(parents=True, exist_ok=True)
        losses, attempts = {}, []
        for period in self.inner_blocks(boundary):
            train_boundary = cutoff(period[0])
            tensor, tensor_meta = self.tensor(train_boundary)
            for task in ("rank", "risk"):
                for structure in STRUCTURES:
                    mixed = structure == "mixed"
                    for penalty in REGULARIZATIONS:
                        for initialization in (0, 1):
                            name = f"{task}-{structure}-r{penalty:g}-i{initialization}"
                            path = self.root / "fits" / code(train_boundary) / (name + ".json")
                            if path.exists():
                                result = read(path)
                                require(result["tensor_sha256"] == tensor_meta["tensor_sha256"],
                                        "STATE_METRIC_FIT_INPUT_CHANGED")
                            else:
                                start = time.monotonic()
                                fitted = fit_metric(tensor, mixed=mixed, task=task,
                                                    regularization=penalty, initialization=initialization)
                                result = {
                                    "name": name, "task": task, "structure": structure,
                                    "regularization": penalty, "initialization": initialization,
                                    "parameters": fitted.x.tolist(), "iterations": int(fitted.nit),
                                    "optimizer_success": bool(fitted.success), "optimizer_message": str(fitted.message),
                                    "candidate_loss": float(fitted.fun),
                                    "frozen_candidate_loss": float(fitted.frozen_candidate_loss),
                                    "refreshes": int(fitted.membership_refreshes),
                                    "tensor_sha256": tensor_meta["tensor_sha256"],
                                    "seconds": time.monotonic() - start,
                                }
                                require(np.isfinite(fitted.x).all() and np.isfinite(fitted.fun),
                                        "STATE_FIT_NONFINITE_RESULT")
                                write(path, result)
                            valpath = self.root / "validation" / code(boundary) / period[0] / (name + ".json")
                            if valpath.exists():
                                validation = read(valpath)
                                require(validation["fit_sha256"] == file_digest(path)
                                        and validation["period"] == list(period)
                                        and validation["knowledge_cutoff"] == boundary,
                                        "STATE_VALIDATION_CONTEXT_CHANGED")
                            else:
                                validation = self.validate_metric(train_boundary, boundary, period,
                                    result["parameters"], task=task, mixed=mixed)
                                validation.update(fit_sha256=file_digest(path), period=list(period),
                                                  knowledge_cutoff=boundary)
                                write(valpath, validation)
                            losses.setdefault(name, []).append(validation)
                            attempts.append(result)
                            print(canonical({"stage": "STATE_INNER_CANDIDATE", "outer": code(boundary),
                                             "period": period[0], "name": name, "loss": validation["loss"]}),
                                  flush=True)
        selected = {}
        for task in ("rank", "risk"):
            for structure in STRUCTURES:
                names = [f"{task}-{structure}-r{r:g}-i{i}" for r in REGULARIZATIONS for i in (0, 1)]
                score = {name: sum(v["loss"] * v["date_weight_sum"] for v in losses[name]) /
                              sum(v["date_weight_sum"] for v in losses[name]) for name in names}
                minimum = min(score.values())
                selected[task + "-" + structure] = next(n for n in names if score[n] - minimum <= 1e-12)
        tensor, tensor_meta = self.tensor(boundary)
        memory, view = self.view(boundary)
        ordinary = retrieval_artifact(self.data, memory, view["transform"], kind="ordinary")
        write(folder / "ordinary.json", ordinary)
        for target, name in selected.items():
            task, structure = target.split("-")
            chosen = next(x for x in attempts if x["name"] == name)
            fitpath = folder / (target + "-fit.json")
            if fitpath.exists():
                result = read(fitpath)
                require(result["tensor_sha256"] == tensor_meta["tensor_sha256"]
                        and result["name"] == name, "STATE_FINAL_FIT_CONTEXT_CHANGED")
            else:
                fitted = fit_metric(tensor, mixed=structure == "mixed", task=task,
                                    regularization=chosen["regularization"], initialization=chosen["initialization"])
                result = {**chosen, "parameters": fitted.x.tolist(), "iterations": int(fitted.nit),
                          "optimizer_success": bool(fitted.success), "optimizer_message": str(fitted.message),
                          "candidate_loss": float(fitted.fun), "frozen_candidate_loss": float(fitted.frozen_candidate_loss),
                          "refreshes": int(fitted.membership_refreshes), "tensor_sha256": tensor_meta["tensor_sha256"]}
                write(fitpath, result)
            artifact = retrieval_artifact(self.data, memory, view["transform"], kind=task,
                                          parameters=result["parameters"], mixed=structure == "mixed",
                                          fit_details=result)
            write(folder / (target + ".json"), artifact)
        if xgb:
            for kind in ("rank", "risk"):
                path = folder / ("xgb-" + kind + ".json")
                if not path.exists():
                    model = fit_state_xgb(self.data, memory, kind=kind)
                    write(path, model.artifact)
                    del model
                    gc.collect()
        model_names = ["ordinary", "rank-numeric", "rank-mixed", "risk-numeric", "risk-mixed"]
        if xgb:
            model_names += ["xgb-rank", "xgb-risk"]
        meta = {
            "schema_version": "quant.state-model-set/v1", "cutoff": boundary,
            "protocol_sha256": fingerprint(self.protocol), "selected": selected,
            "inner_blocks": [list(x) for x in self.inner_blocks(boundary)],
            "files": {name + ".json": file_digest(folder / (name + ".json")) for name in model_names},
            "model_names": model_names, "training_rows": len(memory["ids"]),
        }
        write(completed, meta)
        return meta

    def load(self, boundary, name):
        folder = self.root / "models" / code(boundary)
        receipt = read(folder / "manifest.json")
        path = folder / (name + ".json")
        require(file_digest(path) == receipt["files"][path.name]
                and receipt["cutoff"] == boundary
                and receipt["protocol_sha256"] == fingerprint(self.protocol), "STATE_MODEL_SET_CHANGED")
        artifact = read(path)
        require(artifact["fit_cutoff"] == boundary
                and artifact["frozen_dataset_id"] == self.data.manifest["frozen_dataset_id"],
                "STATE_MODEL_DATA_CONTEXT_CHANGED")
        if name.startswith("xgb"):
            return restore_state_xgb(artifact)
        memory, _ = self.view(boundary)
        return FittedStateRetrieval(self.data, memory, artifact)

    def dates(self, year):
        path = self.inputs / ("phase-c-v2" if year < 2024 else "phase-d-v1")
        return sorted(p.name.removesuffix(".json.gz") for p in
                      (path / f"{year}-xgboost_stock_context/daily").glob("*.json.gz"))

    def forecast(self, year, *, names=None):
        import numpy as np
        boundary = cutoff(f"{year}-01-01")
        meta = self.fit(boundary)
        names = names or meta["model_names"]
        dates = self.dates(year)
        require(bool(dates), "STATE_ORIGINAL_SCORING_DATES_REQUIRED")
        for name in names:
            folder = self.root / "predictions" / name / str(year)
            folder.mkdir(parents=True, exist_ok=True)
            receipt_path = folder / "RECEIPT.json"
            if receipt_path.exists():
                receipt = read(receipt_path)
                artifact = read(self.root / "models" / code(boundary) / (name + ".json"))
                prefix = "state-xgb-" if name.startswith("xgb") else "state-v3-"
                require(receipt["dates"] == dates and receipt["year"] == year
                        and receipt["model"] == name and receipt["cutoff"] == boundary
                        and receipt["protocol_sha256"] == fingerprint(self.protocol)
                        and receipt["model_version"] == prefix + fingerprint(artifact)[:24]
                        and set(receipt["files"]) == set(dates)
                        and all(file_digest(folder / (d + ".npz")) == h
                        for d, h in receipt["files"].items()), "STATE_PREDICTION_RECEIPT_CHANGED")
                continue
            model = self.load(boundary, name)
            hashes = {}
            for ordinal, text in enumerate(dates):
                path = folder / (text + ".npz")
                output = model.predict_arrays(self.data.rows(text), as_of=text + "T21:30:00+08:00")
                values = output if isinstance(output, dict) else {"scores" if name == "xgb-rank" else "risk_raw": output}
                save_arrays(path, values)
                hashes[text] = file_digest(path)
                if ordinal % 20 == 0:
                    print(canonical({"stage": "STATE_FORECAST_DATE", "year": year, "model": name,
                                     "date": text, "rows": len(self.data.rows(text))}), flush=True)
            write(receipt_path, {
                "schema_version": "quant.state-predictions/v1", "year": year, "model": name,
                "model_version": model.model_version, "cutoff": boundary, "dates": dates, "files": hashes,
                "protocol_sha256": fingerprint(self.protocol), "all_original_members": True,
            })
            del model
            gc.collect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("fit", "forecast", "meta", "schedule", "evaluate", "run"))
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--root")
    parser.add_argument("--year", type=int, required=True)
    args = parser.parse_args()
    study = StateStudy(args.inputs, args.cache, root=args.root)
    if args.action == "fit":
        study.fit(cutoff(f"{args.year}-01-01"))
    elif args.action == "forecast":
        study.forecast(args.year)
    elif args.action == "evaluate":
        from quant.state_evaluation import StateEvaluator
        StateEvaluator(study).comparison()
    elif args.action == "run":
        from quant.state_meta import StateMeta
        from quant.state_evaluation import StateEvaluator
        require(args.year == 2018, "STATE_FULL_STUDY_START_REQUIRED")
        meta = StateMeta(study)
        meta.schedule()
        for year in range(2018, 2026):
            study.forecast(year)
            meta.forecast(year)
        StateEvaluator(study).comparison()
    else:
        from quant.state_meta import StateMeta
        meta = StateMeta(study)
        if args.action == "schedule":
            print(canonical(meta.schedule()), flush=True)
        else:
            meta.forecast(args.year)


if __name__ == "__main__":
    main()
