from dataclasses import replace
import gzip
import hashlib
import json
import tempfile
import unittest

from quant.contracts import SCALAR_NAMES
from quant.data.market_store import build_store
from quant.features import build_panel
from quant.models.baseline.factors import factor_predictions
from quant.real_baseline import evaluate_store
from quant.splits.walk_forward import FoldWindow
from quant.synthetic import make_synthetic_data
from quant.tests import test_market_store


class FixedFactorTests(unittest.TestCase):
    def test_factor_direction_same_date_ranking_and_market_isolation(self):
        data, spec = make_synthetic_data(score_start=60, score_end=60)
        original = build_panel(data, spec).rows
        rows = []
        for i, row in enumerate(original):
            values = list(row.scalars)
            for field in ("return_20", "return_5", "volatility_20"):
                values[SCALAR_NAMES.index(field)] = i + 1.0
            rows.append(replace(row, scalars=tuple(values)))
        predictions = factor_predictions(tuple(rows))
        self.assertEqual([i / 5 for i in range(6)],
                         [p.predicted_target_percentile for p in predictions["momentum_20"]])
        self.assertEqual([(5 - i) / 5 for i in range(6)],
                         [p.predicted_target_percentile for p in predictions["reversal_5"]])
        changed = tuple(replace(r, relative_features=(50.0, -20.0),
                                market_context=(0.1, 0.3, 0.2, 0.4), market_context_id="f" * 64) for r in rows)
        self.assertEqual(predictions, factor_predictions(changed))

    def test_disk_experiment_preserves_purge_and_readback_without_fitting(self):
        with tempfile.TemporaryDirectory() as directory:
            root, store, sessions = test_market_store.MarketStoreTests().make_raw(directory, session_count=180)
            build_store(root, store)
            window = FoldWindow(sessions[80], sessions[110], sessions[139], sessions[140], sessions[149])
            output = root.parent / "evaluation"
            from quant.tests.freeze_fixtures import synthetic_freeze
            result = evaluate_store(store, output, window,frozen_review=synthetic_freeze(store,window))
            self.assertEqual("NONE_FIXED_RULES", result["training"])
            self.assertEqual({"AVAILABLE": 30, "PURGED": 40}, result["coverage_date_counts"])
            self.assertEqual(10, result["metrics"]["test"]["momentum_20"]["days"])
            self.assertEqual(result, json.loads((output / "result.json").read_text()))
            for name, digest in result["daily_artifact_sha256"].items():
                wire = (output / "daily" / name).read_bytes()
                self.assertEqual(digest, hashlib.sha256(wire).hexdigest())
                day = json.loads(gzip.decompress(wire))
                self.assertEqual(2, len(day["samples"]))
            self.assertNotIn("sharpe", result)


if __name__ == "__main__":
    unittest.main()
