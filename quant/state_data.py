"""Dated state and outcome cache from the existing admitted local snapshots.

Preparation verifies source bytes before restricted deserialization. Outcome
values and availability remain separate; each fitting cutoff reidentifies the
original full-pool rank intervals. Unknown outcomes never become zero losses.
"""

from datetime import date, datetime, timedelta, timezone
from dataclasses import is_dataclass
import gzip
import io
import json
from pathlib import Path
import pickle
from zoneinfo import ZoneInfo

from quant.contracts import SCALAR_NAMES, finite, fingerprint, require
from quant.models.analogues import file_digest
from quant.artifacts.store import _private_root

FEATURES = (
    "return_1", "return_5", "return_20", "return_60", "relative_return_20",
    "volatility_20", "downside_deviation_20",
    "volume_ratio_20", "amount_ratio_20", "illiquidity_20",
    "median_return", "breadth", "cross_sectional_dispersion", "market_volatility_20",
)
GROUPS = ((0, 1, 2, 3, 4), (5, 6), (7, 8, 9), (10, 11, 12, 13))
SCHEMA = "quant.state-inputs/v4"
CACHE_FILE = "cache-v4.json"
ARRAY_FILES = ("values.npy", "returns.npy", "available.npy", "days.npy")


def timestamp_ns(value):
    require(type(value) is datetime and value.tzinfo is not None,
            "STATE_AWARE_TIME_REQUIRED")
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000


def downside_label(value):
    require(value is None or finite(value), "RISK_RETURN_INVALID")
    return None if value is None else max(-value, 0.)


def _zone_attribute(cls, attribute):
    if cls is ZoneInfo and attribute == "_unpickle":
        return ZoneInfo._unpickle
    raise pickle.UnpicklingError("LOCAL_CACHE_ATTRIBUTE_FORBIDDEN")


class _CacheReader(pickle.Unpickler):
    def find_class(self, module, name):
        if module == "quant.contracts":
            import quant.contracts as contracts
            cls = getattr(contracts, name, None)
            if isinstance(cls, type) and is_dataclass(cls):
                return cls
        if module == "datetime" and name in ("date", "datetime", "timezone", "timedelta"):
            return {"date": date, "datetime": datetime, "timezone": timezone,
                    "timedelta": timedelta}[name]
        if module == "zoneinfo" and name == "ZoneInfo":
            return ZoneInfo
        if module == "builtins" and name == "getattr":
            return _zone_attribute
        raise pickle.UnpicklingError("LOCAL_CACHE_GLOBAL_FORBIDDEN:" + module + "." + name)


def read_admitted(path, expected_digest):
    raw = Path(path).read_bytes()
    from hashlib import sha256
    require(sha256(raw).hexdigest() == expected_digest, "STATE_SOURCE_CONTENT_MISMATCH")
    return _CacheReader(io.BytesIO(gzip.decompress(raw))).load()


def _json(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    from quant.contracts import canonical
    with Path(path).open("x") as stream:
        stream.write(canonical(value) + "\n")


def _labels(value):
    if isinstance(value, dict):
        for key in ("dataset", "data", "labeled"):
            if key in value and hasattr(value[key], "labels"):
                return value[key].labels
        if "labels" in value:
            return value["labels"]
    if hasattr(value, "labels"):
        return value.labels
    if isinstance(value, tuple) and value and hasattr(value[0], "raw_return"):
        return value
    raise ValueError("STATE_SOURCE_LABELS_REQUIRED")


def prepare(inputs, output):
    inputs, output = _private_root(inputs), _private_root(output)
    import numpy as np
    if (output / CACHE_FILE).exists():
        StateInputs(inputs, output)
        return
    output.mkdir(parents=True, exist_ok=False)
    croot, droot, broot = (inputs / name for name in
                           ("phase-c-inputs-v1", "phase-d-inputs-v1", "execution-matrix-v1"))
    c, d, b = (_json(p / "manifest.json") for p in (croot, droot, broot))
    frozen = _json(inputs / "frozen-a/manifest.json")
    require(c["frozen_dataset_id"] == d["frozen_dataset_id"] == b["frozen_dataset_id"]
            == frozen["dataset_id"], "STATE_FROZEN_INPUT_ID_MISMATCH")
    require(file_digest(croot / "hybrid.npy") == c["hybrid_file_sha256"]
            == d["history_hybrid_sha256"], "STATE_HISTORY_CONTENT_MISMATCH")
    require(file_digest(droot / "future-hybrid.npy") == d["future_hybrid_sha256"],
            "STATE_FUTURE_CONTENT_MISMATCH")
    matrices = (np.load(croot / "hybrid.npy", mmap_mode="r", allow_pickle=False),
                np.load(droot / "future-hybrid.npy", mmap_mode="r", allow_pickle=False))
    names = c["hybrid_names"]
    columns = tuple(names.index(name) for name in FEATURES)
    count = sum(len(x) for x in matrices)
    values = np.lib.format.open_memmap(output / "values.npy", mode="w+", dtype=np.float64,
                                       shape=(count, len(FEATURES)))
    raw_return = np.lib.format.open_memmap(output / "returns.npy", mode="w+", dtype=np.float64,
                                           shape=(count,))
    available = np.lib.format.open_memmap(output / "available.npy", mode="w+", dtype=np.int64,
                                          shape=(count,))
    day_codes = np.lib.format.open_memmap(output / "days.npy", mode="w+", dtype=np.int32,
                                          shape=(count,))
    raw_return[:] = np.nan
    available[:] = np.iinfo(np.int64).max
    offset = 0
    for matrix in matrices:
        for start in range(0, len(matrix), 65536):
            block = matrix[start:start + 65536][:, columns]
            values[offset + start:offset + start + len(block)] = block
        offset += len(matrix)
    original = {day["date"]: day for day in b["days"]}
    days = []
    for code, entry in enumerate(d["days"]):
        shift = len(matrices[0]) if entry["storage"] == "future" else 0
        start, stop = entry["row_start"] + shift, entry["row_stop"] + shift
        filename = entry["date"] + ".pickle.gz"
        expected = entry["source_cache_sha256"]
        candidates = (
            inputs / "prepared" / filename,
            inputs / "phase-d-data-v1" / filename,
            inputs / "final-data-cache-v1" / filename,
        )
        path = next((p for p in candidates if p.is_file() and file_digest(p) == expected), None)
        require(path is not None, "STATE_ADMITTED_SOURCE_CACHE_REQUIRED:" + entry["date"])
        labels = _labels(read_admitted(path, expected))
        require(len(labels) == stop - start
                and all(label.key.as_of.date().isoformat() == entry["date"] for label in labels),
                "STATE_LABEL_POOL_ALIGNMENT")
        symbols = [label.key.symbol for label in labels]
        require(symbols == sorted(set(symbols)), "STATE_LABEL_ORDER")
        known = 0
        for index, label in enumerate(labels, start):
            if label.raw_return is not None:
                require(label.outcome_available_at is not None and finite(label.raw_return),
                        "STATE_OUTCOME_AVAILABILITY_REQUIRED")
                raw_return[index] = label.raw_return
                available[index] = timestamp_ns(label.outcome_available_at)
                known += 1
        day_codes[start:stop] = code
        metadata = {
            "date": entry["date"], "code": code, "start": start, "stop": stop,
            "rows": stop - start, "known": known, "source_cache_sha256": expected,
            "label_end_date": entry["label_end_date"],
            "market_regime": original[entry["date"]]["source_entry"]["market_regime"]
                             if entry["date"] in original else entry["market_regime"],
        }
        _write(output / (entry["date"] + ".json"), {"symbols": symbols})
        metadata["symbols_sha256"] = file_digest(output / (entry["date"] + ".json"))
        days.append(metadata)
        if code % 100 == 0:
            print(json.dumps({"stage": "STATE_CACHE_DATE", "date": entry["date"],
                              "rows": stop}), flush=True)
    for array in (values, raw_return, available, day_codes):
        array.flush()
    files = {name: file_digest(output / name)
             for name in ("values.npy", "returns.npy", "available.npy", "days.npy")}
    _write(output / CACHE_FILE, {
        "schema_version": SCHEMA, "frozen_dataset_id": frozen["dataset_id"],
        "features": list(FEATURES), "columns": list(columns), "rows": count,
        "files": files, "days": days, "new_data_source": False,
        "source_manifests": {name: file_digest(inputs / name / "manifest.json") for name in
                             ("phase-c-inputs-v1", "phase-d-inputs-v1", "execution-matrix-v1")},
    })


class StateInputs:
    def __init__(self, inputs, output, *, verify=True):
        inputs, output = _private_root(inputs), _private_root(output)
        import numpy as np
        self.np, self.inputs, self.output = np, Path(inputs), Path(output)
        self.manifest = _json(self.output / CACHE_FILE)
        require(self.manifest["schema_version"] == SCHEMA
                and self.manifest["features"] == list(FEATURES)
                and set(self.manifest["files"]) == set(ARRAY_FILES),
                "STATE_CACHE_SCHEMA")
        if verify:
            require(all(file_digest(self.output / name) == digest
                        for name, digest in self.manifest["files"].items()), "STATE_CACHE_CONTENT")
            require(all(file_digest(self.inputs / name / "manifest.json") == digest
                        for name, digest in self.manifest["source_manifests"].items()),
                    "STATE_CACHE_SOURCE_CHANGED")
        self.values, self.returns, self.available, self.day_codes = (
            np.load(self.output / name, mmap_mode="r", allow_pickle=False)
            for name in ("values.npy", "returns.npy", "available.npy", "days.npy"))
        self.days = {row["date"]: row for row in self.manifest["days"]}
        require(len(self.values) == len(self.returns) == len(self.available) == len(self.day_codes)
                == self.manifest["rows"], "STATE_CACHE_ROW_ALIGNMENT")
        require(self.values.shape == (self.manifest["rows"], len(FEATURES))
                and len(self.days) == len(self.manifest["days"])
                and list(self.days) == sorted(self.days), "STATE_CACHE_LAYOUT")
        stop = 0
        for code, info in enumerate(self.manifest["days"]):
            require(date.fromisoformat(info["date"]).isoformat() == info["date"]
                    and info["code"] == code and info["start"] == stop
                    and info["stop"] - info["start"] == info["rows"] and info["rows"] >= 2
                    and type(info.get("symbols_sha256")) is str
                    and len(info["symbols_sha256"]) == 64, "STATE_CACHE_DAY_LAYOUT")
            stop = info["stop"]
            if verify:
                self.symbols(info["date"])
        require(stop == self.manifest["rows"], "STATE_CACHE_FINAL_OFFSET")

    def symbols(self, text):
        from hashlib import sha256
        require(text in self.days, "STATE_SCORE_DATE_UNKNOWN")
        path = self.output / (text + ".json")
        require(not path.is_symlink() and path.resolve().parent == self.output,
                "STATE_SYMBOL_PATH_INVALID")
        raw = path.read_bytes()
        require(sha256(raw).hexdigest() == self.days[text]["symbols_sha256"],
                "STATE_SYMBOL_CONTENT_MISMATCH")
        value = json.loads(raw)
        require(type(value) is dict and set(value) == {"symbols"}, "STATE_SYMBOL_SHAPE")
        symbols = value["symbols"]
        require(type(symbols) is list and len(symbols) == self.days[text]["rows"]
                and all(type(x) is str for x in symbols) and symbols == sorted(set(symbols)),
                "STATE_SYMBOL_IDENTITY")
        return symbols

    def rows(self, text):
        info = self.days[text]
        return self.values[info["start"]:info["stop"]]

    def supervision(self, cutoff):
        np = self.np
        boundary = datetime.fromisoformat(cutoff)
        require(boundary.tzinfo is not None, "STATE_AWARE_CUTOFF_REQUIRED")
        cutoff_date = boundary.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        nanoseconds = timestamp_ns(boundary)
        ids, targets, weights, risk, ready = [], [], [], [], []
        for info in self.manifest["days"]:
            if info["date"] >= cutoff_date:
                break
            if info["label_end_date"] is None or info["label_end_date"] >= cutoff_date:
                continue
            start, stop = info["start"], info["stop"]
            known = np.isfinite(self.returns[start:stop]) & (self.available[start:stop] < nanoseconds)
            if not known.any():
                continue
            indices = np.flatnonzero(known)
            returns = np.asarray(self.returns[start:stop][known])
            order = np.argsort(returns, kind="stable")
            sorted_values = returns[order]
            left = np.searchsorted(sorted_values, sorted_values, side="left")
            right = np.searchsorted(sorted_values, sorted_values, side="right")
            ranks = np.empty(len(returns))
            ranks[order] = (left + right - 1) / 2
            missing = info["rows"] - len(returns)
            intervals = np.column_stack((ranks / (info["rows"] - 1),
                                         (ranks + missing) / (info["rows"] - 1)))
            ids.append(indices + start)
            targets.append(intervals)
            weights.append(np.full(len(indices), 1 / len(indices)))
            risk.append(np.maximum(-returns, 0))
            ready.append(self.available[start:stop][known])
        require(bool(ids), "STATE_EMPTY_SUPERVISION")
        return {
            "cutoff": cutoff, "ids": np.concatenate(ids), "targets": np.concatenate(targets),
            "weights": np.concatenate(weights), "risk": np.concatenate(risk),
            "available": np.concatenate(ready),
        }
