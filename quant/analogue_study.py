"""Recoverable, read-only reuse of admitted historical inputs for Quant V2.

The CLI takes explicit input/output roots; no private workstation paths or data
are packaged. Original A--D input, label, model and prediction artifacts remain
unchanged. This study is retrospective, including all formerly revealed years.
"""

import argparse
from datetime import datetime
import gc
import gzip
import json
from pathlib import Path
import resource
import time

from quant.contracts import canonical, fingerprint, require
from quant.models.analogues import COMPACT_COLUMNS, FittedAnalogue, file_digest, fit_array_memory


def write_json(path, value):
    path = Path(path)
    with path.open("x") as stream:
        stream.write(canonical(value) + "\n")


def read(path):
    return json.loads(Path(path).read_text())


def prepare_cache(inputs, output):
    import numpy as np
    inputs, output = Path(inputs), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    croot, droot, broot = (inputs / name for name in ("phase-c-inputs-v1", "phase-d-inputs-v1", "execution-matrix-v1"))
    c, d, b = (read(root / "manifest.json") for root in (croot, droot, broot))
    frozen = read(inputs / "frozen-a/manifest.json")
    require(c["frozen_dataset_id"] == d["frozen_dataset_id"] == b["frozen_dataset_id"] == frozen["dataset_id"],
            "ANALOGUE_FROZEN_INPUT_ID_MISMATCH")
    require(file_digest(croot / "manifest.json") == d["history_manifest_sha256"]
            and file_digest(broot / "manifest.json") == c["b_matrix_manifest_sha256"], "ANALOGUE_INPUT_MANIFEST_MISMATCH")
    require(file_digest(croot / "hybrid.npy") == c["hybrid_file_sha256"] == d["history_hybrid_sha256"]
            and file_digest(droot / "future-hybrid.npy") == d["future_hybrid_sha256"], "ANALOGUE_INPUT_CONTENT_MISMATCH")
    history = np.load(croot / "hybrid.npy", mmap_mode="r", allow_pickle=False)
    future = np.load(droot / "future-hybrid.npy", mmap_mode="r", allow_pickle=False)
    require(history.shape[1] == future.shape[1] == 468, "ANALOGUE_SHARED_INPUT_SHAPE")
    compact = np.lib.format.open_memmap(output / "compact.npy", mode="w+", dtype=np.float64,
                                       shape=(len(history) + len(future), 12))
    offset = 0
    for matrix in (history, future):
        for start in range(0, len(matrix), 65536):
            block = matrix[start:start + 65536][:, COMPACT_COLUMNS]
            compact[offset + start:offset + start + len(block)] = block
        offset += len(matrix)
    compact.flush()
    original = {item["date"]: item for item in b["days"]}
    days = []
    for code, item in enumerate(d["days"]):
        shift = len(history) if item["storage"] == "future" else 0
        days.append({"date": item["date"], "day_code": code, "rows": item["rows"],
                     "start": item["row_start"] + shift, "stop": item["row_stop"] + shift,
                     "market_regime": original[item["date"]]["source_entry"]["market_regime"]
                     if item["storage"] == "history" else item["market_regime"],
                     "label_end_date": item["label_end_date"]})
    write_json(output / "cache.json", {"schema_version": "quant.analogue-input-cache/v1",
               "frozen_dataset_id": c["frozen_dataset_id"], "columns": list(COMPACT_COLUMNS),
               "compact_sha256": file_digest(output / "compact.npy"), "shape": list(compact.shape),
               "source_manifests": {name: file_digest(inputs / name / "manifest.json")
                                    for name in ("phase-c-inputs-v1", "phase-d-inputs-v1", "execution-matrix-v1")},
               "days": days, "new_data_source": False, "retrospective": True})
    print(canonical({"stage": "CACHE_PREPARED", "rows": len(compact), "columns": 12}), flush=True)


class StudyInputs:
    def __init__(self, inputs, output):
        import numpy as np
        self.np, self.inputs, self.output = np, Path(inputs), Path(output)
        self.cache = read(self.output / "cache.json")
        require(file_digest(self.output / "compact.npy") == self.cache["compact_sha256"]
                and all(file_digest(self.inputs / name / "manifest.json") == digest
                        for name, digest in self.cache["source_manifests"].items()), "ANALOGUE_CACHE_CONTENT_MISMATCH")
        self.compact = np.load(self.output / "compact.npy", mmap_mode="r", allow_pickle=False)
        require(list(self.compact.shape) == self.cache["shape"], "ANALOGUE_CACHE_SHAPE")
        self.days = {item["date"]: item for item in self.cache["days"]}
        self.d = read(self.inputs / "phase-d-inputs-v1/manifest.json")
        self.d_days = {item["date"]: item for item in self.d["days"]}
        self.b = read(self.inputs / "execution-matrix-v1/manifest.json")
        self.b_days = {item["date"]: item for item in self.b["days"]}

    def values(self, text):
        d = self.days[text]
        return self.compact[d["start"]:d["stop"]]

    def evaluation_dates(self, year):
        folder = self.inputs / ("phase-c-v2" if year < 2024 else "phase-d-v1") / f"{year}-xgboost_stock_context/daily"
        return sorted(path.name.removesuffix(".json.gz") for path in folder.glob("*.json.gz"))

    def baseline(self, text):
        year = int(text[:4])
        folder = self.inputs / ("phase-c-v2" if year < 2024 else "phase-d-v1") / f"{year}-xgboost_stock_context/daily"
        if year < 2024:
            receipt = read(folder.parent / "RECEIPT.json")
            require(file_digest(folder.parent / "result.json") == receipt["result_sha256"], "ANALOGUE_XGB_BASELINE_RECEIPT_CHANGED")
            original = read(folder.parent / "result.json")
        else:
            original = read(folder.parent / "PREDICTION_RECEIPT.json")
        path = folder / (text + ".json.gz")
        require(file_digest(path) == original["daily_sha256"][path.name], "ANALOGUE_XGB_BASELINE_CHANGED")
        value = json.loads(gzip.decompress(path.read_bytes()))
        require(value["metadata"]["frozen_dataset_id"] == self.cache["frozen_dataset_id"]
                and value["metadata"]["fit_cutoff"][:10] == f"{year}-01-01", "ANALOGUE_XGB_BASELINE_CONTEXT")
        return value

    def build_memory(self, year):
        np = self.np
        folder = self.output / "memory" / str(year)
        if (folder / "memory.json").is_file():
            return folder
        fit_date = f"{year}-01-01"
        identifiers, targets, weights, codes, ready, bindings = [], [], [], [], [], []
        if year < 2024:
            view_root = self.inputs / f"execution-matrix-v1/views/{year}"
            view = read(view_root / "manifest.json")
            for name in ("manifest.json", "indices.npy", "targets.npy", "weights.npy"):
                relative = f"views/{year}/{name}"
                require(file_digest(view_root / name) == self.b["files"][relative], "ANALOGUE_SUPERVISION_CONTENT_MISMATCH")
            all_ids = np.load(view_root / "indices.npy", mmap_mode="r")
            all_targets = np.load(view_root / "targets.npy", mmap_mode="r")
            all_weights = np.load(view_root / "weights.npy", mmap_mode="r")
            for info in view["days"]:
                if info["supervised_count"] == 0:
                    continue
                require(info["date"] < fit_date and info["label_end_date"] < fit_date
                        and info["knowledge_cutoff"][:10] == fit_date
                        and datetime.fromisoformat(info["latest_label_available_at"]) < datetime.fromisoformat(view["cutoff"]),
                        "ANALOGUE_SUPERVISION_TIME_LEAKAGE")
                lo, hi = info["supervised_start"], info["supervised_stop"]
                require(hi - lo == info["supervised_count"], "ANALOGUE_SUPERVISION_COUNT")
                identifiers.append(all_ids[lo:hi])
                targets.append(all_targets[lo:hi])
                weights.append(all_weights[lo:hi])
                codes.append(np.full(hi - lo, self.days[info["date"]]["day_code"], dtype=np.int32))
                ready.append(info["latest_label_available_at"])
                bindings.append(info["label_set_id"])
        else:
            for text, day in self.d_days.items():
                if str(year) not in day["training_views"]:
                    continue
                info = day["training_views"][str(year)]
                require(text < fit_date and info["label_end_date"] < fit_date
                        and info["knowledge_cutoff"][:10] == fit_date, "ANALOGUE_SUPERVISION_TIME_LEAKAGE")
                path = self.inputs / f"phase-d-inputs-v1/training/{year}/{text}.npz"
                require(file_digest(path) == info["file_sha256"], "ANALOGUE_SUPERVISION_CONTENT_MISMATCH")
                with np.load(path, allow_pickle=False) as z:
                    pos, y, w = (z[name].copy() for name in ("positions", "targets", "weights"))
                if not len(pos):
                    continue
                require(len(pos) == info["supervised_count"] and pos[0] >= 0 and pos[-1] < day["rows"]
                        and datetime.fromisoformat(info["latest_label_available_at"]) < datetime.fromisoformat(info["knowledge_cutoff"]),
                        "ANALOGUE_SUPERVISION_ALIGNMENT")
                identifiers.append(self.days[text]["start"] + pos)
                targets.append(y)
                weights.append(w)
                codes.append(np.full(len(pos), self.days[text]["day_code"], dtype=np.int32))
                ready.append(info["latest_label_available_at"])
                bindings.append(info["label_set_id"])
        ids, y, w, day_codes = map(np.concatenate, (identifiers, targets, weights, codes))
        del identifiers, targets, weights, codes
        fit_array_memory(self.compact[ids], y, w, day_codes, ids, folder,
                         training_cutoff=max(ready), fit_date=fit_date,
                         training_dataset_id=fingerprint({"frozen_id": self.cache["frozen_dataset_id"],
                                                         "fit_date": fit_date, "label_sets": bindings}))
        return folder


def benchmark(data, year, alpha):
    folder = data.build_memory(year)
    tick = time.perf_counter()
    model = FittedAnalogue(folder, alpha=alpha)
    build_seconds = time.perf_counter() - tick
    dates = data.evaluation_dates(year)
    results = []
    for text in (dates[0], dates[len(dates) // 2], dates[-1]):
        tick = time.perf_counter()
        result = model.predict_arrays(data.values(text), as_of=text + "T21:30:00+08:00", ks=(32, 64))
        seconds = time.perf_counter() - tick
        results.append({"date": text, "queries": len(result[32]["scores"]), "seconds": seconds,
                        "queries_per_second": len(result[32]["scores"]) / seconds,
                        "fallback_32": int((result[32]["fallback"] != 0).sum()),
                        "fallback_64": int((result[64]["fallback"] != 0).sum())})
        print(canonical({"stage": "BENCHMARK_DATE", "alpha": alpha, **results[-1]}), flush=True)
    result = {"year": year, "alpha": alpha, "build_seconds": build_seconds,
              "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "training_rows": len(model.values), "patterns": len(model.groups), "dates": results}
    write_json(data.output / f"benchmark-{year}-a{alpha:g}.json", result)
    return result


def forecast(data, years):
    """Persist every fixed candidate/date without reading evaluation outcomes."""
    np = data.np
    for year in years:
        folder = data.build_memory(year)
        dates = data.evaluation_dates(year)
        for alpha in (0., .25, .5):
            model = FittedAnalogue(folder, alpha=alpha)
            for k in (32, 64):
                destination = data.output / "predictions" / f"a{alpha:g}-k{k}" / str(year)
                destination.mkdir(parents=True, exist_ok=True)
            for ordinal, text in enumerate(dates):
                paths = {k: data.output / "predictions" / f"a{alpha:g}-k{k}" / str(year) / (text + ".npz") for k in (32, 64)}
                if all(path.exists() for path in paths.values()):
                    content_bound = True
                    for k, path in paths.items():
                        with np.load(path, allow_pickle=False) as z:
                            require(z["scores"].shape == (data.days[text]["rows"],)
                                    and z["neighbors"].shape == (data.days[text]["rows"], k)
                                    and np.isfinite(z["scores"]).all(), "ANALOGUE_SAVED_PREDICTION_INVALID")
                        receipt_path = path.parent / "RECEIPT.json"
                        if not receipt_path.exists():
                            content_bound = False
                            continue
                        receipt = read(receipt_path)
                        metadata = read(folder / f"model-a{alpha:g}-k{k}.json")
                        require(receipt["model_version"] == metadata["model_version"]
                                and receipt["model_sha256"] == file_digest(folder / f"model-a{alpha:g}-k{k}.json")
                                and receipt["prediction_dates"] == dates,
                                "ANALOGUE_PREDICTION_CONTEXT_MISMATCH")
                        require(file_digest(path) == receipt["date_sha256"].get(text),
                                "ANALOGUE_PREDICTION_CONTENT_MISMATCH")
                    if content_bound:
                        continue
                    # Interrupted files without an annual receipt are only
                    # reusable after an exact replay against the restored model.
                started = time.perf_counter()
                result = model.predict_arrays(data.values(text), as_of=text + "T21:30:00+08:00", ks=(32, 64))
                for k, path in paths.items():
                    if path.exists():
                        with np.load(path, allow_pickle=False) as z:
                            require(all(np.array_equal(z[name], result[k][name]) for name in result[k]), "ANALOGUE_RESUME_PREDICTION_CHANGED")
                        continue
                    temporary = path.with_suffix(".npz.pending")
                    require(not temporary.exists(), "ANALOGUE_PARTIAL_FILE_REQUIRES_INSPECTION")
                    with temporary.open("xb") as stream:
                        np.savez_compressed(stream, **result[k])
                    temporary.rename(path)
                    with np.load(path, allow_pickle=False) as z:
                        require(all(np.array_equal(z[name], result[k][name]) for name in result[k]), "ANALOGUE_PREDICTION_WRITE_CHANGED")
                print(canonical({"stage": "FORECAST_DATE", "year": year, "alpha": alpha, "date": text,
                                 "date_ordinal": ordinal + 1, "total_dates": len(dates),
                                 "queries": data.days[text]["rows"], "seconds": time.perf_counter() - started,
                                 "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), flush=True)
            for k in (32, 64):
                destination = data.output / "predictions" / f"a{alpha:g}-k{k}" / str(year)
                receipt = destination / "RECEIPT.json"
                hashes = {text: file_digest(destination / (text + ".npz")) for text in dates}
                metadata = read(folder / f"model-a{alpha:g}-k{k}.json")
                value = {"schema_version": "quant.analogue-predictions/v1", "year": year, "alpha": alpha, "k": k,
                         "model_version": metadata["model_version"], "model_sha256": file_digest(folder / f"model-a{alpha:g}-k{k}.json"),
                         "fit_date": metadata["fit_date"], "training_cutoff": metadata["training_cutoff"],
                         "date_sha256": hashes, "prediction_dates": dates, "future_outcomes_embedded": False,
                         "retrospective": True, "status": "COMPLETED"}
                if receipt.exists():
                    require(read(receipt) == value, "ANALOGUE_PREDICTION_RECEIPT_CHANGED")
                else:
                    write_json(receipt, value)
            del model
            gc.collect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "benchmark", "forecast"))
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--alpha", type=float, default=.25)
    parser.add_argument("--years", type=int, nargs="+", default=list(range(2018, 2026)))
    args = parser.parse_args()
    if args.operation == "prepare":
        prepare_cache(args.inputs, args.output)
    elif args.operation == "benchmark":
        benchmark(StudyInputs(args.inputs, args.output), args.year, args.alpha)
    else:
        require(all(year in range(2018, 2026) for year in args.years), "ANALOGUE_YEAR_INVALID")
        forecast(StudyInputs(args.inputs, args.output), args.years)


if __name__ == "__main__":
    main()
