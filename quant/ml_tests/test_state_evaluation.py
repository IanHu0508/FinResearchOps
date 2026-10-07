import unittest

import numpy as np

from quant.state_evaluation import risk_day, select_rank, summarize_risk


class StateEvaluationTests(unittest.TestCase):
    def test_unknown_return_is_not_an_observed_zero_loss(self):
        day = risk_day(np.array([.1, .2, .3]), np.array([-.1, .1, np.nan]),
                       text="2024-01-02", regime="fixed")
        self.assertEqual(2, day["observed_count"])
        self.assertEqual(1, day["unknown_count"])
        self.assertAlmostEqual(.02, day["mse"])
        self.assertAlmostEqual(.1, day["bias"])
        unknown = risk_day(np.array([.1]), np.array([np.nan]), text="2024-01-03", regime=None)
        self.assertIsNone(unknown["mse"])
        self.assertEqual([], unknown["bins"])

    def test_downside_summary_dates_have_equal_mass_and_calibration_uses_loss_units(self):
        first = risk_day(np.array([.05]), np.array([-.1]), text="2024-01-02", regime=None)
        second = risk_day(np.full(9, .1), np.full(9, -.2), text="2024-01-03", regime=None)
        report = summarize_risk([first, second])
        self.assertAlmostEqual((.05 ** 2 + .1 ** 2) / 2, report["mse"])
        self.assertAlmostEqual(2., report["calibration_slope"])
        self.assertAlmostEqual(0., report["calibration_intercept"])

    def test_rank_selection_retains_tie_order_and_does_not_select_undefined_candidate(self):
        reports = {"xgb": {"selection_eligible": True, "mean_rank_ic_lower_bound": .05},
                   "nn": {"selection_eligible": True, "mean_rank_ic_lower_bound": .05 + 5e-13},
                   "undefined": {"selection_eligible": False, "mean_rank_ic_lower_bound": None}}
        self.assertEqual("xgb", select_rank(reports, ("xgb", "nn", "undefined")))
        reports["nn"]["mean_rank_ic_lower_bound"] += 1e-6
        self.assertEqual("nn", select_rank(reports, ("xgb", "nn", "undefined")))
