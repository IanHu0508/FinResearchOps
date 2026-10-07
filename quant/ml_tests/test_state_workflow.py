"""Small dated integration of the real study/OOF/meta orchestration."""

from datetime import date, datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from quant.models.analogues import file_digest
from quant.state_data import ARRAY_FILES, CACHE_FILE, FEATURES, SCHEMA, timestamp_ns
from quant.state_meta import StateMeta
from quant.state_study import StateStudy, cutoff


def cache_fixture(root):
    repo, private = root / "finaudit-gate", root / "private"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='synthetic-study'\n")
    inputs, cache = private / "inputs", private / "cache"
    inputs.mkdir(parents=True)
    cache.mkdir()
    random = np.random.default_rng(128)
    days = []
    for year in range(2010, 2019):
        for month in range(1, 13):
            day = date(year, month, 5)
            start = len(days) * 8
            symbols = [f"S{i:02d}" for i in range(8)]
            path = cache / (day.isoformat() + ".json")
            path.write_text(json.dumps({"symbols": symbols}))
            days.append({"date": day.isoformat(), "code": len(days), "start": start, "stop": start + 8,
                         "rows": 8, "label_end_date": (day + timedelta(days=30)).isoformat(),
                         "symbols_sha256": file_digest(path), "market_regime": "synthetic"})
    values = random.normal(size=(len(days) * 8, 14))
    returns = .02 * values[:, 0] - .01 * values[:, 5] + random.normal(0, .01, len(values))
    available = np.repeat([timestamp_ns(datetime.fromisoformat(
        (date.fromisoformat(d["date"]) + timedelta(days=31)).isoformat() + "T21:30:00+08:00"))
        for d in days], 8)
    np.save(cache / "values.npy", values)
    np.save(cache / "returns.npy", returns)
    np.save(cache / "available.npy", available)
    np.save(cache / "days.npy", np.repeat(np.arange(len(days), dtype=np.int32), 8))
    (cache / CACHE_FILE).write_text(json.dumps({"schema_version": SCHEMA, "features": list(FEATURES),
        "frozen_dataset_id": "f" * 64, "rows": len(values), "days": days, "source_manifests": {},
        "files": {name: file_digest(cache / name) for name in ARRAY_FILES}}))
    return inputs, cache


class StateWorkflowTests(unittest.TestCase):
    def test_real_orchestration_fits_dated_oof_and_restores_calibration_and_gates(self):
        with TemporaryDirectory() as directory:
            inputs, cache = cache_fixture(Path(directory))
            study = StateStudy(inputs, cache, query_limit=64)
            meta = StateMeta(study)
            # The first legal OOF block has an explicit cold start. The next
            # cutoff has mature OOF labels and exercises calibration/selection.
            block, receipt = meta.block("2014-07-01", "2015-01-01")
            self.assertEqual("quant.state-oof/v2", receipt["schema_version"])
            self.assertEqual(48, len(block["ids"]))
            self.assertTrue((block["model_cutoff"] < block["as_of"]).all())
            self.assertFalse(block["selected_risk_valid"].any())
            boundary = cutoff("2015-07-01")
            setup = meta.risk_setup(boundary)
            self.assertFalse(setup["cold_start"])
            self.assertGreater(setup["training_rows"], 0)
            gates = meta.gates(boundary)
            self.assertEqual({"G0", "G1"}, set(gates["gates"]))
            self.assertEqual(setup, meta.risk_setup(boundary))
            self.assertEqual(gates, meta.gates(boundary))
            for calibration in setup["calibrators"].values():
                self.assertLess(calibration["latest_label_available_ns"], timestamp_ns(datetime.fromisoformat(boundary)))
