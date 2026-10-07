from datetime import datetime
from types import SimpleNamespace
import unittest

import numpy as np

from quant.models.state_metric import transform_fit
from quant.models.state_models import (
    FittedStateRetrieval, fit_state_xgb, restore_state_xgb, retrieval_artifact,
)
from quant.state_data import FEATURES, timestamp_ns


def fixture():
    random = np.random.default_rng(18)
    values = random.normal(size=(128, 14))
    target = np.clip(.5 + .2 * values[:, 0], 0, 1)
    weights = np.full(128, 1 / 8)
    data = SimpleNamespace(values=values, day_codes=np.arange(128) // 8,
                           manifest={"frozen_dataset_id": "f" * 64, "features": list(FEATURES)})
    memory = {"cutoff": "2025-01-01T00:00:00+08:00", "ids": np.arange(128),
              "targets": np.column_stack((target, target)), "weights": weights,
              "risk": np.maximum(-values[:, 0], 0) * .05,
              "available": np.full(128, timestamp_ns(datetime.fromisoformat("2024-12-01T00:00:00+08:00")))}
    return data, memory


class StateModelTests(unittest.TestCase):
    def test_whole_pool_fallback_time_guard_and_weight_reconstruction(self):
        data, memory = fixture()
        transform = transform_fit(data.values, memory["weights"])
        artifact = retrieval_artifact(data, memory, transform, kind="ordinary")
        model = FittedStateRetrieval(data, memory, artifact, workers=1)
        queries = data.values[:3].copy()
        queries[2] = np.nan
        output = model.predict_arrays(queries, as_of="2025-01-02T21:30:00+08:00")
        self.assertEqual(3, len(output["scores"]))
        self.assertEqual(1, output["fallback"][2])
        reconstructed = model.neighbour_weights(queries[:2], output["neighbors"][:2])
        expected = (reconstructed * memory["risk"][output["neighbors"][:2]]).sum(axis=1)
        np.testing.assert_allclose(expected, output["risk_raw"][:2], rtol=0, atol=1e-15)
        with self.assertRaisesRegex(ValueError, "AFTER_SCORING"):
            model.predict_arrays(queries, as_of="2024-12-31T21:30:00+08:00")
        memory["available"][0] = timestamp_ns(datetime.fromisoformat(memory["cutoff"]))
        with self.assertRaisesRegex(ValueError, "FUTURE_LABEL"):
            FittedStateRetrieval(data, memory, artifact)

    def test_same_input_xgb_models_restore_with_separate_rank_and_risk_targets(self):
        data, memory = fixture()
        for kind in ("rank", "risk"):
            model = fit_state_xgb(data, memory, kind=kind)
            saved = restore_state_xgb(model.artifact)
            original = model.predict_arrays(data.values, as_of="2025-01-02T21:30:00+08:00")
            actual = saved.predict_arrays(data.values, as_of="2025-01-02T21:30:00+08:00")
            np.testing.assert_array_equal(original, actual)
            self.assertEqual(28, saved.artifact["input_columns"])
            self.assertTrue((actual >= 0).all())
            if kind == "rank":
                self.assertTrue((actual <= 1).all())
