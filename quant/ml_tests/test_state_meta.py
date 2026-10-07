from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import numpy as np

from quant.contracts import fingerprint
from quant.models.analogues import file_digest
from quant.models.state_metric import transform_fit
from quant.models.state_models import FittedStateRetrieval, retrieval_artifact
from quant.ml_tests.test_state_models import fixture
from quant.state_meta import StateMeta, bounded_ic, merged_diagnostics, neighbour_pool, saved_pool
from quant.state_study import StateStudy, write
from quant.state_data import timestamp_ns


class StateMetaTests(unittest.TestCase):
    def _cached_risk_fixture(self, root, *, slope=0.):
        boundary = "2025-01-01T00:00:00+08:00"
        study = SimpleNamespace(data=None, root=root, protocol={"fixed": "inputs"})
        meta = StateMeta(study)
        names = ("constant", "ordinary", "xgb-risk", "risk-numeric", "risk-mixed")
        rows = {"losses": np.array([.01, .05]), "date_weights": np.array([1., 3.]),
                **{"raw-" + name: np.full(2, .04) for name in names}}
        meta.rows_before = lambda cutoff: (rows, [])
        view = root / "views/2025-01-01/manifest.json"
        write(view, {"fixed": "view"})
        center = float(np.average(rows["losses"], weights=rows["date_weights"]))
        calibrators = {name: {"schema_version": "quant.risk-calibration/v1", "fit_cutoff": boundary,
                            "intercept": center, "slope": 0., "rows": 2, "loss": .0003,
                            "optimizer_success": True, "latest_label_available_ns": 1,
                            "oof_binding": "synthetic"} for name in names}
        calibrators["constant"].update(slope=slope, intercept=center - slope * .04)
        value = {"schema_version": "quant.state-risk-setup/v1",
                 "context": {"fit_cutoff": boundary, "protocol_sha256": meta.binding,
                             "oof": [], "view_sha256": file_digest(view)},
                 "selected": "constant", "calibrators": calibrators,
                 "causal_calibrated_oof_mse": {name: .0003 for name in names},
                 "cold_start": False, "training_rows": 2,
                 "selector_uses_past_predictions": True, "tie_order": list(names)}
        path = root / "meta/2025-01-01/risk.json"
        write(path, value)
        return meta, path, value, boundary

    def test_cached_unidentified_constant_calibration_requires_preserved_revision(self):
        for slope in (1e12, .001):
            with self.subTest(slope=slope), TemporaryDirectory() as directory:
                meta, path, _, boundary = self._cached_risk_fixture(Path(directory), slope=slope)
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "CONSTANT_CALIBRATION_REVISION_REQUIRED"):
                    meta.risk_setup(boundary)
                self.assertEqual(before, path.read_bytes())

    def test_cached_stable_constant_calibration_is_read_without_rewriting(self):
        with TemporaryDirectory() as directory:
            meta, path, value, boundary = self._cached_risk_fixture(Path(directory))
            before = path.read_bytes()
            self.assertEqual(value, meta.risk_setup(boundary))
            self.assertEqual(before, path.read_bytes())

    def _cached_oof_fixture(self, root, *, slope, centers):
        study = SimpleNamespace(data=None, root=root, protocol={"fixed": "inputs"})
        meta = StateMeta(study)
        history = []
        for start, center in zip(("2014-07-01", "2015-01-01"), centers):
            prior = root / "oof" / start
            prior.mkdir(parents=True)
            np.savez(prior / "predictions.npz", **{"raw-constant": np.full(2, center)})
            value = {"schema_version": "quant.state-oof/v2",
                     "prediction_sha256": file_digest(prior / "predictions.npz")}
            write(prior / "receipt.json", value)
            history.append({"start": start, "receipt_sha256": fingerprint(value)})
        start, stop = "2016-01-01", "2016-07-01"
        model = root / "models" / start / "manifest.json"
        risk = root / "meta" / start / "risk.json"
        write(model, {"synthetic": "model"})
        write(risk, {"context": {"oof": history},
                     "calibrators": {"constant": {"intercept": .02, "slope": slope}}})
        path = root / "oof" / start
        path.mkdir(parents=True)
        np.savez(path / "predictions.npz", ids=np.arange(2))
        value = {"schema_version": "quant.state-oof/v2", "period": [start, stop],
                 "protocol_sha256": meta.binding,
                 "prediction_sha256": file_digest(path / "predictions.npz"),
                 "models_sha256": file_digest(model), "risk_setup_sha256": file_digest(risk)}
        write(path / "receipt.json", value)
        return meta, path, risk, value, start, stop

    def test_cached_oof_cannot_bypass_unidentified_constant_calibration(self):
        with TemporaryDirectory() as directory:
            meta, path, risk, _, start, stop = self._cached_oof_fixture(
                Path(directory), slope=1e12, centers=[.04])
            before = (risk.read_bytes(), (path / "receipt.json").read_bytes())
            with self.assertRaisesRegex(ValueError, "OOF_CONSTANT_CALIBRATION_REVISION_REQUIRED"):
                meta.block(start, stop)
            self.assertEqual(before, (risk.read_bytes(), (path / "receipt.json").read_bytes()))

    def test_cached_oof_accepts_stable_constant_without_rewriting(self):
        with TemporaryDirectory() as directory:
            meta, path, risk, value, start, stop = self._cached_oof_fixture(
                Path(directory), slope=0., centers=[.04])
            before = (risk.read_bytes(), (path / "receipt.json").read_bytes())
            arrays, receipt = meta.block(start, stop)
            np.testing.assert_array_equal(arrays["ids"], np.arange(2))
            self.assertEqual(receipt, value)
            self.assertEqual(before, (risk.read_bytes(), (path / "receipt.json").read_bytes()))

    def test_cached_oof_does_not_reject_identified_slope_across_different_past_centers(self):
        with TemporaryDirectory() as directory:
            meta, _, _, value, start, stop = self._cached_oof_fixture(
                Path(directory), slope=.3, centers=[.03, .05])
            _, receipt = meta.block(start, stop)
            self.assertEqual(receipt, value)

    def test_g1_uses_g0_scores_and_neighbour_mixture_when_conditional_risk_is_unavailable(self):
        data = SimpleNamespace(values=np.zeros((2, 14)))
        meta = StateMeta(SimpleNamespace(data=data, root=Path("."), protocol={}))
        targets = np.zeros((2, 3, 64, 2))
        for index, value in enumerate((.2, .5, .8)):
            targets[:, index] = value
        arrays = {"ids": np.array([0, 1]), "targets": targets, "weights": np.full((2, 3, 64), 1 / 64),
                  "as_of": np.full(2, timestamp_ns(datetime.fromisoformat("2025-01-02T21:30:00+08:00"))),
                  "selected_risk": np.array([.04, .05]), "selected_risk_valid": np.array([False, True])}
        common = {"fit_cutoff": "2025-01-01T00:00:00+08:00", "center": .5}
        g0 = {**common, "risk_input": False, "means": [0.] * 4, "scales": [1.] * 4,
              "coefficients": np.array([[2., 0, 0, 0, 0], [-2., 0, 0, 0, 0]]).tolist()}
        g1 = {**common, "risk_input": True, "means": [0.] * 5, "scales": [1.] * 5,
              "coefficients": np.array([[-2., 0, 0, 0, 0, 0], [2., 0, 0, 0, 0, 0]]).tolist()}
        baseline = meta._apply_gate(arrays, g0)
        actual = meta._apply_gate(arrays, g1, g0_result=baseline)
        self.assertEqual(baseline[0][0], actual[0][0])
        np.testing.assert_array_equal(baseline[1][0], actual[1][0])
        self.assertGreater(actual[0][1], baseline[0][1])
        with self.assertRaisesRegex(ValueError, "G1_REQUIRES_G0"):
            meta._apply_gate(arrays, g1)
    def test_saved_neighbour_reconstruction_matches_actual_index_with_explicit_fallback(self):
        data, memory = fixture()
        artifact = retrieval_artifact(data, memory, transform_fit(data.values, memory["weights"]), kind="ordinary")
        model = FittedStateRetrieval(data, memory, artifact, workers=1)
        values = data.values[:3].copy()
        values[2] = np.nan
        output = model.predict_arrays(values, as_of="2025-01-02T21:30:00+08:00")
        targets, weights = neighbour_pool(model, values, output)
        restored, mass, ids = saved_pool(data, memory, artifact, values, output)
        np.testing.assert_array_equal(targets, restored)
        np.testing.assert_array_equal(weights, mass)
        self.assertTrue((ids[2] == -1).all())
        self.assertEqual(1., mass[2].sum())

    def test_overlapping_neighbours_add_mass_once_and_preserve_losing_examples(self):
        data = SimpleNamespace(returns=np.array([-.2, .1]), day_codes=np.array([1, 2]))
        output = merged_diagnostics(data, np.array([[0, 0, 1], [-1, -1, -1]]),
                                    np.array([[.2, .3, .5], [1., 0., 0.]]))
        self.assertEqual(2, output["unique_neighbours"][0])
        self.assertEqual(2., output["effective_dates"][0])
        self.assertAlmostEqual(-.05, output["history_mean_return"][0])
        self.assertEqual(.5, output["history_loss_frequency"][0])
        self.assertEqual(-.2, output["history_lower_decile"][0])
        self.assertTrue(np.isnan(output["history_mean_return"][1]))
        self.assertIsNone(bounded_ic(np.array([.1, .8]), np.array([1, 1]), np.array([np.nan, np.nan])))

    def test_meta_training_joins_only_cutoff_supervision_without_zero_loss_imputation(self):
        memory = {"ids": np.array([10, 12]), "targets": np.array([[.1, .2], [.7, .8]]),
                  "risk": np.array([.05, 0.]), "available": np.array([20, 30], np.int64)}
        data = SimpleNamespace(returns=np.arange(13) * -.01)
        study = SimpleNamespace(data=data, root=Path("."), protocol={}, view=lambda b: (memory, {}))
        meta = StateMeta(study)
        meta.blocks_before = lambda b: [("2024-01-01", "2024-07-01")]
        arrays = {"ids": np.array([10, 11, 12]), "days": np.array([1, 1, 1]),
                  "as_of": np.array([1, 1, 1]), "model_cutoff": np.array([0, 0, 0])}
        meta.block = lambda a, b: (arrays, {"id": "unchanged"})
        rows, binding = meta.rows_before("2025-01-01T00:00:00+08:00")
        np.testing.assert_array_equal(rows["ids"], [10, 12])
        np.testing.assert_array_equal(rows["losses"], [.05, 0.])
        np.testing.assert_array_equal(rows["date_weights"], [.5, .5])
        self.assertEqual(1, len(binding))
        # The unlabelled row is absent only from supervised learning; the
        # immutable OOF/scoring block still has all three selected identities.
        np.testing.assert_array_equal(arrays["ids"], [10, 11, 12])

    def test_completed_forecast_cannot_be_reused_with_another_model_version(self):
        with TemporaryDirectory() as directory:
            study = StateStudy.__new__(StateStudy)
            study.root = Path(directory)
            study.protocol = {"fixed": "inputs"}
            study.fit = lambda b: {"model_names": ["ordinary"]}
            study.dates = lambda y: ["2025-01-02"]
            artifact = {"fit_cutoff": "2025-01-01T00:00:00+08:00"}
            write(study.root / "models/2025-01-01/ordinary.json", artifact)
            folder = study.root / "predictions/ordinary/2025"
            folder.mkdir(parents=True)
            np.savez(folder / "2025-01-02.npz", scores=np.array([.5]))
            write(folder / "RECEIPT.json", {
                "dates": ["2025-01-02"], "year": 2025, "model": "ordinary",
                "cutoff": artifact["fit_cutoff"], "protocol_sha256": fingerprint(study.protocol),
                "model_version": "wrong", "files": {"2025-01-02": file_digest(folder / "2025-01-02.npz")}})
            with self.assertRaisesRegex(ValueError, "PREDICTION_RECEIPT_CHANGED"):
                study.forecast(2025)
