from dataclasses import replace
from datetime import date, timedelta
import json
import unittest

from quant.contracts import ABLATIONS, MARKET_NAMES, SCALAR_NAMES, ContractError, Prediction
from quant.evaluation import PortfolioDay, evaluate_portfolio, evaluate_predictions
from quant.evaluation.prediction import spearman
from quant.models.baseline import MeanModel, NearestNeighborsModel, restore_reference_model
from quant.models.views import VIEWS, vector_names, vectors
from quant.pipeline import prepare_dataset
from quant.splits import prepare_fold
from quant.splits.walk_forward import EvaluationBatch
from quant.synthetic import make_synthetic_data, synthetic_fold


class ModelAndEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.spec = make_synthetic_data()
        cls.dataset = prepare_dataset(cls.data, cls.spec)
        cls.fold = prepare_fold(cls.dataset, synthetic_fold(cls.data))

    def test_two_models_use_same_training_and_inference_interface(self):
        for model in (MeanModel(), NearestNeighborsModel()):
            fitted = model.fit(self.fold.train)
            predictions = fitted.predict(self.fold.test.rows)
            self.assertEqual(tuple(r.key for r in self.fold.test.rows), tuple(p.key for p in predictions))
            self.assertTrue(all(0 <= p.predicted_target_percentile <= 1 for p in predictions))

    def test_inference_rows_contain_no_outcome_fields(self):
        row = self.fold.test.rows[0]
        for field in ("target", "labels", "target_percentile", "raw_return", "label_end_date"):
            self.assertFalse(hasattr(row, field))

    def test_preprocessing_fitted_only_on_training_values(self):
        model = NearestNeighborsModel(view="tabular", ablation="stock-only").fit(self.fold.train)
        before = model.artifact
        poisoned = replace(self.fold.test.rows[0], scalars=(9999.0,) + self.fold.test.rows[0].scalars[1:])
        model.predict((poisoned,))
        self.assertEqual(before, model.artifact)
        expected = sum(r.scalars[0] * w for r, w in zip(self.fold.train.rows, self.fold.train.weights)) / sum(self.fold.train.weights)
        self.assertAlmostEqual(expected, model.transform.means[0])

    def test_missing_features_are_imputed_from_training_and_have_indicators(self):
        row = self.fold.train.rows[0]
        missing = replace(row, scalars=row.scalars[:3] + (None,) * (len(row.scalars) - 3))
        model = NearestNeighborsModel().fit(self.fold.train)
        values = vectors((missing,), "tabular", "stock-only")[0]
        self.assertEqual(1.0, values[len(row.scalars) + 3])
        self.assertEqual(1, len(model.predict((missing,))))

    def test_tabular_and_sequence_views_keep_identical_sample_identity(self):
        rows = self.fold.test.rows
        dimensions = {"tabular": (36, 48), "linear": (36, 184),
                      "sequence": (420, 432), "hybrid": (456, 468)}
        for view, sizes in dimensions.items():
            for ablation, dimension in zip(ABLATIONS, sizes):
                values = vectors(rows, view, ablation)
                self.assertEqual(len(rows), len(values))
                self.assertTrue(all(len(v) == dimension for v in values))
                self.assertEqual(dimension, len(vector_names(view, ablation)))

    def test_stock_only_views_exclude_both_relative_features_and_market_state(self):
        row = self.fold.test.rows[0]
        changed = replace(row, relative_features=(10.0, -10.0),
                          market_context=(0.2, 0.9, 0.4, 0.3), market_context_id="a" * 64)
        for view in VIEWS:
            self.assertEqual(vectors((row,), view, "stock-only"), vectors((changed,), view, "stock-only"))
            self.assertNotEqual(vectors((row,), view, "stock+context"), vectors((changed,), view, "stock+context"))

    def test_linear_market_interaction_can_reverse_order_without_market_only_columns(self):
        original = self.fold.test.rows[0]
        a = replace(original, scalars=(1.0,) + (0.0,) * (len(SCALAR_NAMES) - 1),
                    relative_features=(0.0, 0.0), market_context=(0.1, 0.5, 0.02, 0.03))
        b = replace(a, scalars=(2.0,) + a.scalars[1:])
        names = vector_names("linear", "stock+context")
        self.assertTrue(all(name not in names for name in MARKET_NAMES))
        column = names.index("interaction:return_1*median_return")
        positive = [x[column] for x in vectors((a, b), "linear", "stock+context")]
        negative = [x[column] for x in vectors(tuple(replace(r, market_context=(-0.1,) + r.market_context[1:])
                                                    for r in (a, b)), "linear", "stock+context")]
        self.assertLess(positive[0], positive[1])
        self.assertGreater(negative[0], negative[1])

    def test_old_or_unknown_model_views_do_not_silently_choose_an_ablation(self):
        for view, ablation in (("full_tabular", "stock-only"), ("tabular", "all")):
            with self.assertRaises(ContractError):
                vectors(self.fold.train.rows, view, ablation)

    def test_saved_reference_models_restore_identical_predictions(self):
        for adapter in tuple(model(ablation=ablation) for model in (MeanModel, NearestNeighborsModel)
                             for ablation in ABLATIONS):
            model = adapter.fit(self.fold.train)
            restored = restore_reference_model(json.loads(json.dumps(model.artifact)))
            self.assertEqual(model.model_version, restored.model_version)
            self.assertEqual(model.predict(self.fold.test.rows[:12]), restored.predict(self.fold.test.rows[:12]))
            self.assertEqual(adapter.ablation, restored.ablation)

    def test_old_reference_model_format_is_not_reinterpreted(self):
        artifact = MeanModel().fit(self.fold.train).artifact
        artifact["schema_version"] = "quant.reference-model/v1"
        with self.assertRaisesRegex(ContractError, "MODEL_SCHEMA"):
            restore_reference_model(artifact)

    def test_modified_model_state_is_detected(self):
        artifact = MeanModel().fit(self.fold.train).artifact
        artifact["state"]["value"] = 0.123
        with self.assertRaisesRegex(ContractError, "MODEL_CONTENT"):
            restore_reference_model(artifact)

    def test_perfect_and_reversed_predictions_have_known_rank_ic(self):
        batch = self.fold.test
        perfect = tuple(Prediction(y.key, y.target_percentile) for y in batch.labels)
        reverse = tuple(Prediction(y.key, 1 - y.target_percentile) for y in batch.labels)
        self.assertAlmostEqual(1.0, evaluate_predictions(perfect, batch)["mean_rank_ic"])
        self.assertAlmostEqual(-1.0, evaluate_predictions(reverse, batch)["mean_rank_ic"])

    def test_constant_predictions_are_undefined_not_zero_ic(self):
        values = MeanModel().fit(self.fold.train).predict(self.fold.test.rows)
        result = evaluate_predictions(values, self.fold.test)
        self.assertEqual(0, result["valid_days"])
        self.assertIsNone(result["mean_rank_ic"])
        self.assertFalse(result["selection_eligible"])
        self.assertIsNone(result["date_equal_top_minus_bottom_forward_return"])
        self.assertEqual(0, result["daily"][0]["quantiles"][0]["count"])

    def test_quantile_returns_use_raw_holding_returns_and_are_not_daily_pnl(self):
        as_of = self.fold.test.rows[0].key.as_of
        rows = tuple(r for r in self.fold.test.rows if r.key.as_of == as_of)
        labels = tuple(replace(y, raw_return=i / 10, target_interval=(i / 5,i / 5))
                       for i, y in enumerate(y for y in self.fold.test.labels if y.key.as_of == as_of))
        batch = EvaluationBatch(rows, labels)
        predictions = tuple(Prediction(y.key, y.target_percentile) for y in labels)
        result = evaluate_predictions(predictions, batch, quantile_groups=2)
        self.assertEqual("full_universe_rank_ic_outer_bounds", result["metric"])
        self.assertAlmostEqual(0.1, result["quantile_returns"][0]["date_equal_mean_forward_return"])
        self.assertAlmostEqual(0.4, result["quantile_returns"][1]["date_equal_mean_forward_return"])
        self.assertAlmostEqual(0.3, result["date_equal_top_minus_bottom_forward_return"])
        self.assertEqual(20, result["quantile_return_horizon_sessions"])
        self.assertEqual(1.0, result["by_year"][0]["mean_rank_ic"])
        self.assertNotIn("sharpe", result)

    def test_prediction_metrics_remain_date_equal_with_unequal_cross_sections(self):
        dates = sorted({r.key.as_of for r in self.fold.test.rows})[:2]
        first = [(r, y) for r, y in zip(self.fold.test.rows, self.fold.test.labels) if r.key.as_of == dates[0]]
        second = [(r, y) for r, y in zip(self.fold.test.rows, self.fold.test.labels) if r.key.as_of == dates[1]][:2]
        second = [(r, replace(y, raw_return=float(i), target_interval=(float(i),float(i)), universe_size=2, observed_count=2))
                  for i, (r, y) in enumerate(second)]
        batch = EvaluationBatch(tuple(r for r, _ in first + second), tuple(y for _, y in first + second))
        predictions = tuple(Prediction(y.key, y.target_percentile) for _, y in first)
        predictions += tuple(Prediction(y.key, 1 - y.target_percentile) for _, y in second)
        result = evaluate_predictions(predictions, batch)
        self.assertAlmostEqual(0.0, result["mean_rank_ic"])
        self.assertAlmostEqual(0.5, result["date_equal_interval_mse"])

    def test_yearly_summary_does_not_hide_a_reversed_later_period(self):
        as_of = self.fold.test.rows[0].key.as_of
        first = [(r, y) for r, y in zip(self.fold.test.rows, self.fold.test.labels) if r.key.as_of == as_of]
        shift = timedelta(days=366)
        later = []
        for row, label in first:
            key = replace(row.key, as_of=row.key.as_of + shift)
            later.append((replace(row, key=key), replace(label, key=key,
                          entry_date=label.entry_date + shift, label_end_date=label.label_end_date + shift,
                          available_at=label.available_at + shift,
                          outcome_available_at=label.outcome_available_at+shift, knowledge_cutoff=label.knowledge_cutoff+shift)))
        batch = EvaluationBatch(tuple(r for r, _ in first + later), tuple(y for _, y in first + later))
        predictions = tuple(Prediction(y.key, y.target_percentile) for _, y in first)
        predictions += tuple(Prediction(y.key, 1 - y.target_percentile) for _, y in later)
        report = evaluate_predictions(predictions, batch)
        self.assertEqual([1.0, -1.0], [period["mean_rank_ic"] for period in report["by_year"]])
        self.assertEqual(0.0, report["mean_rank_ic"])

    def test_evaluation_rejects_missing_and_duplicate_predictions(self):
        predictions = MeanModel().fit(self.fold.train).predict(self.fold.test.rows)
        for values in (predictions[1:], predictions[:-1] + predictions[:1]):
            with self.assertRaisesRegex(ContractError, "PREDICTION_COVERAGE"):
                evaluate_predictions(values, self.fold.test)

    def test_spearman_ties_and_strictly_increasing_transform(self):
        x, y = (0.1, 0.1, 0.4, 0.8), (2.0, 1.0, 3.0, 4.0)
        self.assertAlmostEqual(spearman(x, y), spearman(tuple(2 * a + 8 for a in x), y))
        self.assertIsNone(spearman((1, 1), (1, 2)))

    def test_daily_portfolio_returns_compound_after_costs(self):
        days = (PortfolioDay(date(2020, 1, 2), 0.02, 0.001, 0.0001, 0.5),
                PortfolioDay(date(2020, 1, 3), -0.01, 0.002, 0.0001, 0.7))
        result = evaluate_portfolio(days, return_horizon_sessions=1, method="synthetic-daily-ledger")
        self.assertAlmostEqual(1.019 * 0.988 - 1, result["net_cumulative_return"])
        self.assertAlmostEqual(-0.012, result["max_drawdown"])
        self.assertAlmostEqual(0.6, result["mean_daily_turnover"])

    def test_twenty_day_labels_cannot_masquerade_as_daily_portfolio_pnl(self):
        with self.assertRaisesRegex(ContractError, "DAILY_PNL_REQUIRED"):
            evaluate_portfolio((), return_horizon_sessions=20, method="bad-label-backtest")

    def test_duplicate_portfolio_dates_and_invalid_costs_rejected(self):
        day = PortfolioDay(date(2020, 1, 2), 0.01, 0.0, 0.0, 0.1)
        with self.assertRaisesRegex(ContractError, "PORTFOLIO_DATES"):
            evaluate_portfolio((day, day), return_horizon_sessions=1, method="bad")
        with self.assertRaises(ContractError):
            replace(day, cost_return=-0.1)


if __name__ == "__main__":
    unittest.main()
