import json
from pathlib import Path
import tempfile
import unittest
import shutil
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from quant.models.analogues import (FittedAnalogue, FittedFusion, batch_interval_center, fit_array_memory,
                                   fusion_scores, interval_center)
from quant.inference import score_day
from quant.analogue_study import forecast
from quant.tests import test_scoring as scoring_fixture


class AnaloguesTests(unittest.TestCase):
    def data(self, constant=False):
        rng = np.random.default_rng(19)
        x = np.zeros((160, 12)) if constant else rng.normal(size=(160, 12))
        if not constant:
            x[::7, 0] = np.nan
            x[::11, 5] = np.nan
        y = rng.uniform(0, 1, size=160)
        targets = np.c_[np.maximum(0, y - .03), np.minimum(1, y + .02)]
        return x, targets, np.repeat(np.arange(20), 8), np.full(160, 1 / 8)

    def fit(self, root, constant=False):
        x, y, days, weights = self.data(constant)
        fit_array_memory(x, y, weights, days, np.arange(160), root,
                         training_cutoff="2017-12-01T21:00:00+08:00", fit_date="2018-01-01",
                         training_dataset_id="synthetic")
        return x, y, days

    def test_exact_index_matches_independent_exhaustive_distance_and_date_caps(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "memory"
            x, y, days = self.fit(folder)
            for alpha in (0., .25, .5):
                model = FittedAnalogue(folder, alpha=alpha)
                queries = x[1:5].copy()
                queries[0, 1] = np.nan
                actual = model.predict_arrays(queries, as_of="2018-01-02T21:30:00+08:00", ks=(32, 64))
                p = 8 if alpha == 0 else 12
                mask = np.isnan(x[:, :p])
                normalized = np.where(mask, 0., (x[:, :p] - model.means) / model.scales)
                for q, values in enumerate(queries):
                    qm = np.isnan(values[:p])
                    qz = np.where(qm, 0., (values[:p] - model.means) / model.scales)
                    squared = (normalized - qz) ** 2
                    distances = ((1 - alpha) * squared[:, :8].mean(axis=1)
                                 + (alpha * squared[:, 8:].mean(axis=1) if p == 12 else 0)
                                 + .25 * (mask | qm).mean(axis=1))
                    order = sorted(range(len(x)), key=lambda j: (distances[j], j))
                    for k in (32, 64):
                        selected, counts = [], {}
                        for j in order:
                            day = int(days[j])
                            if counts.get(day, 0) == k // 8:
                                continue
                            counts[day] = counts.get(day, 0) + 1
                            selected.append(j)
                            if len(selected) == k:
                                break
                        self.assertEqual(selected, actual[k]["neighbors"][q].tolist())
                        weights = tuple(1 / counts[int(days[j])] for j in selected)
                        expected = interval_center(tuple(map(tuple, y[selected])), weights, model.state["training_center"])
                        self.assertAlmostEqual(expected, actual[k]["scores"][q], places=14)

    def test_all_equal_distances_have_stable_date_symbol_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "memory"
            self.fit(folder, constant=True)
            model = FittedAnalogue(folder)
            actual = model.predict_arrays(np.zeros((1, 12)), as_of="2018-01-02T21:30:00+08:00")[32]
            self.assertEqual([day * 8 + j for day in range(8) for j in range(4)], actual["neighbors"][0].tolist())

    def test_flat_optimum_uses_training_center_and_batch_equals_scalar_loss(self):
        targets = np.asarray([[[.2, .8], [.3, .7]], [[.1, .1], [.9, .9]]])
        weights = np.asarray([[1., 3.], [1., 3.]])
        self.assertEqual(.55, interval_center(((.2, .8), (.3, .7)), (1., 3.), .55))
        actual = batch_interval_center(targets, weights, .55)
        for i in range(2):
            self.assertAlmostEqual(interval_center(tuple(map(tuple, targets[i])), tuple(weights[i]), .55), actual[i], places=14)

    def test_fallback_zero_weight_and_later_model_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "memory"
            x, _, _ = self.fit(folder)
            model = FittedAnalogue(folder)
            q = x[:1].copy()
            q[:, :8] = np.nan
            result = model.predict_arrays(q, as_of="2018-01-02T21:30:00+08:00")[32]
            self.assertEqual(1, result["fallback"][0])
            self.assertEqual(model.state["training_center"], result["scores"][0])
            np.testing.assert_array_equal([.8], fusion_scores([.8], result["scores"], .5, result["fallback"]))
            np.testing.assert_array_equal([.8], fusion_scores([.8], [.3], 0., [0]))
            with self.assertRaisesRegex(ValueError, "ANALOGUE_MODEL_AFTER_SCORING"):
                model.predict_arrays(x[:1], as_of="2017-12-15T21:30:00+08:00")

    def test_restored_arrays_are_content_bound_and_predict_has_no_query_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "memory"
            x, _, _ = self.fit(folder)
            before = FittedAnalogue(folder).predict_arrays(x[:2], as_of="2018-01-02T21:30:00+08:00")[32]
            after = FittedAnalogue(folder).predict_arrays(x[:2], as_of="2018-01-02T21:30:00+08:00")[32]
            np.testing.assert_array_equal(before["scores"], after["scores"])
            with (folder / "targets.npy").open("ab") as f:
                f.write(b"tampered")
            with self.assertRaisesRegex(ValueError, "ANALOGUE_ARRAY_CONTENT_MISMATCH"):
                FittedAnalogue(folder)

    def test_future_records_modified_and_deleted_leave_nn_and_fusion_scoring_identical(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "memory"
            self.fit(folder)
            (Path(temporary) / "workspace").mkdir()
            path, sessions = scoring_fixture.make_store(Path(temporary) / "workspace")
            day = sessions[85]
            nn = FittedAnalogue(folder)
            fused = FittedFusion(scoring_fixture.StubModel(), nn, .5)
            originals = {name: score_day(path, day, model) for name, model in (("nn", nn), ("fusion", fused))}
            changed = path.with_name("changed.sqlite")
            shutil.copy2(path, changed)
            with sqlite3.connect(changed) as connection:
                tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                for table in tables:
                    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                    if {"session", "close"} <= columns:
                        connection.execute(f'UPDATE "{table}" SET close=777 WHERE session>?', (day.isoformat(),))
            removed = path.with_name("removed.sqlite")
            shutil.copy2(path, removed)
            scoring_fixture.delete_later_rows(removed, day)
            for name, model in (("nn", nn), ("fusion", fused)):
                for candidate_path in (changed, removed):
                    result = score_day(candidate_path, day, model)
                    self.assertEqual(originals[name].predictions, result.predictions)
                    self.assertEqual(originals[name].ranks, result.ranks)
            zero = FittedFusion(scoring_fixture.StubModel(), nn, 0.)
            self.assertEqual(score_day(path, day, scoring_fixture.StubModel()).predictions,
                             score_day(path, day, zero).predictions)

    def test_forecast_resume_requires_receipts_or_exact_replay_before_signing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            memory = root / "memory"
            x, _, _ = self.fit(memory)
            text = "2018-01-02"
            data = SimpleNamespace(np=np, output=root, days={text: {"rows": 2}},
                build_memory=lambda _: memory, evaluation_dates=lambda _: [text],
                values=lambda _: x[1:3])
            forecast(data, (2018,))
            with patch.object(FittedAnalogue, "predict_arrays", side_effect=AssertionError("validated dates must be reused")):
                forecast(data, (2018,))
            directory = root / "predictions/a0-k32/2018"
            path = directory / (text + ".npz")
            with np.load(path, allow_pickle=False) as saved:
                arrays = {key: saved[key].copy() for key in saved.files}
            arrays["scores"][:] = (.999, .001)
            np.savez_compressed(path, **arrays)
            with self.assertRaisesRegex(ValueError, "ANALOGUE_PREDICTION_(CONTENT_MISMATCH|RECEIPT_CHANGED)"):
                forecast(data, (2018,))
            (directory / "RECEIPT.json").unlink()
            with self.assertRaisesRegex(ValueError, "ANALOGUE_RESUME_PREDICTION_CHANGED"):
                forecast(data, (2018,))
            self.assertFalse((directory / "RECEIPT.json").exists())


if __name__ == "__main__":
    unittest.main()
