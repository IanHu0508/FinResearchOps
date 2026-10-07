import unittest

import numpy as np

from quant.models.analogues import batch_interval_center
from quant.models.state_metric import (
    ExactStateIndex, coefficients, date_kernel, fit_metric, interval_predictions,
    metric_objective, normalize, pair_distances,
)


def training_fixture():
    random = np.random.default_rng(31)
    q, c = random.normal(size=(12, 14)), random.normal(size=(12, 80, 14))
    values = np.clip(.5 + .15 * c[:, :, 0], 0, 1)
    targets = np.stack((np.clip(values - .002, 0, 1), np.clip(values + .002, 0, 1)), axis=2)
    qy = np.clip(.5 + .15 * q[:, 0], 0, 1)
    return {
        "query_z": q, "candidate_z": c, "query_missing": np.zeros(q.shape, bool),
        "candidate_missing": np.zeros(c.shape, bool),
        "candidate_days": np.broadcast_to(np.arange(80) // 8, (12, 80)),
        "candidate_targets": targets, "query_targets": np.column_stack((qy, qy)),
        "candidate_risk": np.maximum(c[:, :, 0], 0) * .05,
        "query_risk": np.maximum(q[:, 0], 0) * .05,
        "query_weights": np.ones(12), "risk_variance": .002, "center": .5,
    }


class StateMetricTests(unittest.TestCase):
    def test_padding_never_becomes_a_zero_loss_or_rank_training_neighbour(self):
        train = training_fixture()
        padded = dict(train)
        for key in ("candidate_z", "candidate_missing", "candidate_days",
                    "candidate_targets", "candidate_risk"):
            value = train[key]
            padding = np.zeros((len(value), 16, *value.shape[2:]), dtype=value.dtype)
            padded[key] = np.concatenate((value, padding), axis=1)
        padded["candidate_valid"] = np.concatenate((np.ones((12, 80), bool),
                                                   np.zeros((12, 16), bool)), axis=1)
        p = np.zeros(16);p[14] = .3
        for task in ("rank", "risk"):
            original = metric_objective(p, train, mixed=True, task=task, regularization=.001)
            actual = metric_objective(p, padded, mixed=True, task=task, regularization=.001)
            self.assertAlmostEqual(original[0], actual[0], places=13)
            np.testing.assert_allclose(original[1], actual[1], rtol=0, atol=1e-13)

    def test_interval_solver_matches_old_loss_without_assigning_midpoint_labels(self):
        random = np.random.default_rng(8)
        low = random.uniform(size=(100, 64))
        high = np.minimum(low + random.uniform(0, .2, low.shape), 1)
        targets = np.stack((low, high), axis=2)
        weights = random.uniform(.1, 1, low.shape)
        weights /= weights.sum(axis=1, keepdims=True)
        actual = interval_predictions(targets, weights, .5)
        expected = batch_interval_center(targets, weights, .5)
        np.testing.assert_allclose(actual, expected, atol=2e-13, rtol=0)
        flat = np.asarray([[[.2, .8], [.3, .7]]])
        self.assertEqual(.5, interval_predictions(flat, np.asarray([[.5, .5]]), .5)[0])

    def test_analytic_gradients_match_independent_perturbations(self):
        train = training_fixture()
        p = np.zeros(16)
        p[:14] = np.random.default_rng(5).normal(0, .2, 14)
        p[14] = .37
        for task in ("rank", "risk"):
            _, gradient = metric_objective(p, train, mixed=True, task=task, regularization=.001)
            numerical = []
            for index in range(len(p)):
                plus, minus = p.copy(), p.copy()
                plus[index] += 1e-6
                minus[index] -= 1e-6
                a = metric_objective(plus, train, mixed=True, task=task, regularization=.001)[0]
                b = metric_objective(minus, train, mixed=True, task=task, regularization=.001)[0]
                numerical.append((a - b) / 2e-6)
            np.testing.assert_allclose(gradient, numerical, atol=2e-7, rtol=1e-4)

    def test_fit_actually_changes_weights_using_labels(self):
        train = training_fixture()
        before = metric_objective(np.zeros(15), train, mixed=False, task="rank",
                                  regularization=.001)[0]
        result = fit_metric(train, mixed=False, task="rank", regularization=.001)
        after = metric_objective(result.x, train, mixed=False, task="rank",
                                 regularization=.001)[0]
        weights, _, _ = coefficients(result.x, mixed=False)
        self.assertLess(after, before)
        self.assertGreater(weights[0], 1 / 14)
        self.assertTrue(np.isfinite(result.x).all())

    def test_exact_index_matches_full_distances_with_missing_patterns_and_date_caps(self):
        random = np.random.default_rng(4)
        values = random.normal(size=(128, 14))
        values[:12, 0] = np.nan
        values[12:20, 5] = np.nan
        values[20] = 0
        queries = random.normal(size=(3, 14))
        queries[0, 2] = np.nan
        queries[1] = 0
        transform = {"means": [0.] * 14, "scales": [1.] * 14}
        weights = np.arange(1., 15.)
        weights /= weights.sum()
        days = np.arange(128) // 16
        ids = np.arange(128)
        index = ExactStateIndex(values, days, ids, transform, weights, .6, workers=1)
        found = index.query(queries, k=32, cap=4)
        q, qm = normalize(queries, transform)
        c, cm = normalize(values, transform)
        full = pair_distances(q, np.broadcast_to(c, (3, *c.shape)), qm,
                              np.broadcast_to(cm, (3, *cm.shape)), weights, .6)
        for row, (actual, distance) in enumerate(found):
            order = np.lexsort((ids, days, full[row]))
            picked, counts = [], {}
            for item in order:
                day = int(days[item])
                if counts.get(day, 0) < 4:
                    picked.append(item); counts[day] = counts.get(day, 0) + 1
                    if len(picked) == 32:
                        break
            np.testing.assert_array_equal(actual, picked)
            np.testing.assert_allclose(distance, full[row, actual], atol=2e-13, rtol=0)

    def test_equal_kernel_is_date_balanced_and_both_missing_are_not_perfect_similarity(self):
        days = np.asarray([[0, 0, 1]])
        weights = date_kernel(np.zeros((1, 3)), days, 1, equal=True)
        np.testing.assert_allclose(weights, [[.25, .25, .5]])
        values = np.zeros((1, 14))
        missing = np.ones((1, 14), dtype=bool)
        distance = pair_distances(values, values[:, None, :], missing, missing[:, None, :],
                                  np.ones(14) / 14, .5)
        self.assertGreater(distance[0, 0], .25)

    def test_exact_query_expands_past_dense_date_caps_and_boundary_ties(self):
        random = np.random.default_rng(92)
        values = random.normal(size=(4096, 14))
        values[:160] = 0
        values[160:220, 0] = np.nan
        values[220:290, 5] = np.nan
        days = np.arange(len(values)) // 12
        days[:160] = np.repeat([0, 1], 80)
        ids = np.arange(len(values))
        queries = np.concatenate((np.zeros((1, 14)), random.normal(size=(7, 14))))
        queries[1, 1] = np.nan
        queries[2, 5] = np.nan
        transform = {"means": [0.] * 14, "scales": [1.] * 14}
        weights = random.uniform(.1, 1, 14)
        weights /= weights.sum()
        q, qm = normalize(queries, transform)
        c, cm = normalize(values, transform)
        cases = [(eta, k, cap) for eta in (0., .63, 1.) for k, cap in ((64, 3), (512, 4))]
        for eta, k, cap in cases:
            index = ExactStateIndex(values, days, ids, transform, weights, eta, workers=1)
            actual = index.query(queries, k=k, cap=cap)
            full = pair_distances(q, np.broadcast_to(c, (len(q), *c.shape)), qm,
                                  np.broadcast_to(cm, (len(q), *cm.shape)), weights, eta)
            for row, (selected, distance) in enumerate(actual):
                counts, expected = {}, []
                for member in np.lexsort((ids, days, full[row])):
                    day = int(days[member])
                    if counts.get(day, 0) < cap:
                        counts[day] = counts.get(day, 0) + 1
                        expected.append(member)
                        if len(expected) == k:
                            break
                np.testing.assert_array_equal(selected, expected)
                np.testing.assert_allclose(distance, full[row, selected], rtol=0, atol=2e-13)
