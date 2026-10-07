from datetime import datetime
import unittest

import numpy as np

from quant.models.state_gate import (
    calibrate_risk, check_oof_times, fit_risk_calibration, gate_design, gate_objective,
)
from quant.state_data import timestamp_ns


def ns(value):
    return timestamp_ns(datetime.fromisoformat(value))


class StateGateTests(unittest.TestCase):
    def test_calibration_learns_loss_units_and_refuses_future_outcomes(self):
        x = np.linspace(0, .1, 100)
        y = np.maximum(x * .7 - .005, 0)
        asof = np.full(100, ns("2024-01-02T21:30:00+08:00"))
        model = np.full(100, ns("2024-01-01T00:00:00+08:00"))
        available = np.full(100, ns("2024-02-01T21:00:00+08:00"))
        artifact = fit_risk_calibration(x, y, np.ones(100), fitting_cutoff="2025-01-01T00:00:00+08:00",
                                        as_of_ns=asof, model_cutoff_ns=model, label_available_ns=available)
        fitted = calibrate_risk(x, artifact, as_of="2025-01-02T21:30:00+08:00")
        np.testing.assert_allclose(fitted, y, atol=2e-6, rtol=0)
        available[0] = ns("2025-01-01T00:00:00+08:00")
        with self.assertRaisesRegex(ValueError, "TIME_LEAKAGE"):
            check_oof_times(asof, model, available, "2025-01-01T00:00:00+08:00")

    def test_constant_calibration_stays_stable_when_future_raw_center_changes(self):
        rows = 64
        losses = np.linspace(0., .08, rows)
        weights = np.tile([1., 2., 3., 4.], rows // 4)
        cutoff = "2025-01-01T00:00:00+08:00"
        artifact = fit_risk_calibration(
            np.full(rows, .04), losses, weights, fitting_cutoff=cutoff,
            as_of_ns=np.full(rows, ns("2024-01-02T21:30:00+08:00")),
            model_cutoff_ns=np.full(rows, ns("2024-01-01T00:00:00+08:00")),
            label_available_ns=np.full(rows, ns("2024-02-01T21:00:00+08:00")),
        )
        expected = float(np.average(losses, weights=weights))
        calibrated = calibrate_risk(np.array([0., .01, .04, .08]), artifact, as_of=cutoff)
        np.testing.assert_allclose(calibrated, expected, atol=1e-14, rtol=0)
        self.assertAlmostEqual(artifact["loss"],
                               float(np.average((losses - expected) ** 2, weights=weights)))

    def test_constant_calibration_still_rejects_unavailable_labels(self):
        cutoff = "2025-01-01T00:00:00+08:00"
        with self.assertRaisesRegex(ValueError, "STATE_OOF_TIME_LEAKAGE"):
            fit_risk_calibration(
                np.full(4, .04), np.array([0., .02, .04, .06]), np.ones(4),
                fitting_cutoff=cutoff,
                as_of_ns=np.full(4, ns("2024-01-02T21:30:00+08:00")),
                model_cutoff_ns=np.full(4, ns("2024-01-01T00:00:00+08:00")),
                label_available_ns=np.full(4, ns(cutoff)),
            )

    def test_mixture_gradient_matches_perturbed_interval_loss(self):
        rng = np.random.default_rng(2)
        design, _, _ = gate_design(rng.normal(size=(15, 5)))
        lower = rng.uniform(size=(15, 3, 64))
        target = np.stack((lower, np.minimum(lower + .002, 1)), axis=3)
        weights = np.ones((15, 3, 64)) / 64
        query = rng.uniform(size=15)
        query_target = np.column_stack((query, query))
        p = rng.normal(0, .1, 12)
        args = (design, target, weights, query_target, np.ones(15), .5, .001)
        loss, gradient = gate_objective(p, *args)
        numerical = []
        for i in range(len(p)):
            a, b = p.copy(), p.copy()
            a[i] += 1e-6;b[i] -= 1e-6
            numerical.append((gate_objective(a, *args)[0] - gate_objective(b, *args)[0]) / 2e-6)
        self.assertTrue(np.isfinite(loss))
        np.testing.assert_allclose(gradient, numerical, rtol=1e-4, atol=1e-7)

    def test_scaling_balances_dates_and_handles_an_entire_missing_column(self):
        values = np.array([[0., np.nan], [10., np.nan], [10., np.nan]])
        design, means, scales = gate_design(values, weights=np.array([1., .5, .5]))
        np.testing.assert_array_equal(means, [5., 0.])
        np.testing.assert_array_equal(scales, [5., 1.])
        np.testing.assert_array_equal(design[:, 1], [-1., 1., 1.])
        np.testing.assert_array_equal(design[:, 2], [0., 0., 0.])

    def test_unavailable_risk_has_only_the_fixed_g0_contribution_to_training_loss(self):
        design = np.ones((2, 6))
        target = np.full((2, 3, 64, 2), .6)
        weights = np.full((2, 3, 64), 1 / 64)
        query = np.array([[.1, .1], [.8, .8]])
        args = (design, target, weights, query, np.ones(2), .5, 0.,
                np.zeros(2, bool), np.array([.1, .8]))
        loss, gradient = gate_objective(np.ones(12), *args)
        self.assertEqual(0., loss)
        np.testing.assert_array_equal(gradient, np.zeros(12))
