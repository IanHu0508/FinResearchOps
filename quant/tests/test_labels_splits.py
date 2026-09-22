from collections import Counter
from dataclasses import replace
from datetime import timedelta
import unittest

from quant.contracts import ContractError, market_time
from quant.features import build_panel
from quant.labels import build_labels
from quant.labels.ranks import percentiles
from quant.pipeline import prepare_dataset
from quant.splits import prepare_fold
from quant.synthetic import make_synthetic_data, synthetic_fold


class LabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.spec = make_synthetic_data(score_start=60, score_end=60)

    def test_next_open_to_twentieth_session_close_ranks_unadjusted_holding_returns(self):
        returns = (0.125, 0.25, 0.375, -0.125, -0.25, -0.375)
        names = self.data.universes[0].symbols
        expected = dict(zip(names, returns))
        entry, end = self.data.sessions[61], self.data.sessions[80]
        bars = tuple(replace(b, return_open=100) if b.session == entry else
                     replace(b, return_close=100 * (1 + expected[b.symbol])) if b.session == end else b
                     for b in self.data.bars)
        dataset = prepare_dataset(replace(self.data, bars=bars), self.spec)
        for i, label in enumerate(dataset.labels):
            self.assertEqual(entry, label.entry_date)
            self.assertEqual(end, label.label_end_date)
            self.assertAlmostEqual(returns[i], label.raw_return)
        self.assertFalse(hasattr(dataset.labels[0], "industry_return"))
        self.assertFalse(hasattr(dataset.labels[0], "industry_adjusted_return"))
        self.assertEqual((0.6, 0.8, 1.0, 0.4, 0.2, 0.0), tuple(y.target_percentile for y in dataset.labels))

    def test_calendar_sessions_not_calendar_days_define_horizon(self):
        holiday = self.data.sessions[61]
        data = replace(self.data, sessions=tuple(d for d in self.data.sessions if d != holiday),
                       bars=tuple(b for b in self.data.bars if b.session != holiday))
        labels = prepare_dataset(data, self.spec).labels
        self.assertEqual(self.data.sessions[62], labels[0].entry_date)
        self.assertEqual(self.data.sessions[81], labels[0].label_end_date)
        self.assertGreater((labels[0].label_end_date - self.data.sessions[60]).days, 20)

    def test_missing_future_quote_does_not_re_rank_survivors(self):
        missing = ("SYN000.SH", self.data.sessions[80])
        data = replace(self.data, bars=tuple(b for b in self.data.bars if (b.symbol, b.session) != missing))
        labels = prepare_dataset(data, self.spec).labels
        self.assertEqual(6, len(labels))
        self.assertTrue(all(y.target_percentile is None for y in labels))
        self.assertEqual(5,sum(y.supervised for y in labels))
        self.assertEqual("OUTCOME_PRICE_UNAVAILABLE",labels[0].missing_reason)
        self.assertTrue(all(y.universe_size==6 and y.observed_count==5 for y in labels))

    def test_end_of_history_stays_unlabeled_instead_of_zero_return(self):
        data, spec = make_synthetic_data(score_start=220, score_end=229)
        labels = prepare_dataset(data, spec).labels
        self.assertTrue(all(not y.complete for y in labels))
        self.assertTrue(all(y.raw_return is None for y in labels))

    def test_two_stock_pool_needs_no_industry_or_peer_group(self):
        snapshots = tuple(replace(u, symbols=u.symbols[:2]) for u in self.data.universes)
        labels = prepare_dataset(replace(self.data, universes=snapshots), self.spec).labels
        self.assertEqual(2, len(labels))
        self.assertTrue(all(y.complete for y in labels))
        self.assertEqual({0.0, 1.0}, {y.target_percentile for y in labels})

    def test_label_availability_includes_the_last_known_outcome_in_the_whole_pool(self):
        delayed = market_time(self.data.sessions[82], 16)
        bars = tuple(replace(b, available_at=delayed)
                     if b.symbol == "SYN005.SH" and b.session == self.data.sessions[80]
                     else b for b in self.data.bars)
        labels = prepare_dataset(replace(self.data, bars=bars), self.spec).labels
        self.assertTrue(all(y.available_at == delayed for y in labels))

    def test_future_tradability_cannot_select_the_label_comparison_pool(self):
        bars = tuple(replace(b, tradable=False) if b.symbol == "SYN000.SH"
                     and b.session == self.data.sessions[80] else b for b in self.data.bars)
        baseline = prepare_dataset(self.data, self.spec)
        changed = prepare_dataset(replace(self.data, bars=bars), self.spec)
        self.assertEqual(baseline.labels, changed.labels)

    def test_raw_unadjusted_price_cannot_silently_replace_the_return_basis(self):
        with self.assertRaisesRegex(ContractError, "RETURN_BASIS"):
            replace(self.data, price_basis="UNADJUSTED_CLOSE")

    def test_changing_future_returns_does_not_change_features(self):
        data = replace(self.data, bars=tuple(replace(b, return_close=b.return_close * 1.5)
             if b.session == self.data.sessions[80] else b for b in self.data.bars))
        self.assertEqual(build_panel(self.data, self.spec), build_panel(data, self.spec))

    def test_percentile_convention_ties_and_all_equal(self):
        self.assertEqual((0.0, 0.5, 0.5, 1.0), percentiles((1.0, 2.0, 2.0, 3.0)))
        self.assertEqual((0.5, 0.5, 0.5), percentiles((2.0, 2.0, 2.0)))
        for values in ((1.0,), (0.0, float("nan"))):
            with self.assertRaises(ContractError):
                percentiles(values)

    def test_subtracting_common_market_return_cannot_change_rank(self):
        raw = (0.125, -0.25, 0.5, 0.0)
        self.assertEqual(percentiles(raw), percentiles(tuple(x - 0.25 for x in raw)))


class SplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.spec = make_synthetic_data()
        cls.dataset = prepare_dataset(cls.data, cls.spec)
        cls.window = synthetic_fold(cls.data)

    def test_whole_dates_purge_labels_not_twenty_rows(self):
        fold = prepare_fold(self.dataset, self.window)
        self.assertEqual(120, len(fold.purged_train))
        self.assertEqual(270, len(fold.train.rows))
        self.assertTrue(all(d < market_time(self.window.validation_start, 0) for d in fold.train.label_available_at))
        counts = Counter(r.key.as_of for r in fold.train.rows)
        self.assertEqual({6}, set(counts.values()))

    def test_validation_outcomes_cannot_cross_final_test_boundary(self):
        window = replace(self.window, validation_end=self.data.sessions[175])
        fold = prepare_fold(self.dataset, window)
        self.assertEqual(16 * 6, len(fold.purged_validation))
        self.assertTrue(all(y.label_end_date < window.test_start for y in fold.validation.labels))

    def test_delayed_label_publication_is_purged_even_if_end_date_is_earlier(self):
        cutoff = market_time(self.window.validation_start, 0)
        data = replace(self.data, bars=tuple(replace(b,available_at=cutoff)
            if b.session==self.data.sessions[100] else b for b in self.data.bars))
        # Future evidence is a label dependency, not a future feature input;
        # construct the latest labels on the unchanged historical panel.
        from quant.labels.forward import build_labels
        changed=build_labels(data,self.dataset.panel)
        fold = prepare_fold(changed, self.window)
        self.assertEqual(120, len(fold.purged_train))
        self.assertTrue(all(r.key.as_of.date()!=self.data.sessions[80] for r in fold.train.rows))

    def test_equal_date_weights_with_changing_universe_size(self):
        # Vary historical membership without changing equal total date weight.
        snapshots = tuple(replace(u, symbols=u.symbols[:3]) if u.as_of.date() == self.data.sessions[80]
                          else u for u in self.data.universes)
        dataset = prepare_dataset(replace(self.data, universes=snapshots), self.spec)
        fold = prepare_fold(dataset, self.window)
        totals = Counter()
        for row, weight in zip(fold.train.rows, fold.train.weights):
            totals[row.key.as_of] += weight
        for total in totals.values():
            self.assertAlmostEqual(1.0, total)

    def test_empty_training_after_purge_is_rejected(self):
        with self.assertRaisesRegex(ContractError, "EMPTY_TRAINING"):
            prepare_fold(self.dataset, replace(self.window, train_start=self.data.sessions[120]))

    def test_date_windows_must_be_ordered(self):
        with self.assertRaisesRegex(ContractError, "FOLD_ORDER"):
            replace(self.window, test_start=self.window.validation_end)

    def test_training_batch_rejects_leaked_labels_and_unequal_day_weights(self):
        train = prepare_fold(self.dataset, self.window).train
        with self.assertRaisesRegex(ContractError, "TRAINING_LABEL_LEAKAGE"):
            replace(train, label_available_at=tuple(market_time(self.window.validation_start) for _ in train.rows))
        with self.assertRaisesRegex(ContractError, "DATE_WEIGHTS"):
            replace(train, weights=tuple(1.0 for _ in train.rows))
        with self.assertRaisesRegex(ContractError, "IMMUTABLE"):
            replace(train, targets=list(train.targets))
        with self.assertRaisesRegex(ContractError, "TARGET_INVALID"):
            replace(train, targets=(True,) + train.targets[1:])

    def test_train_validation_and_test_keys_are_disjoint(self):
        fold = prepare_fold(self.dataset, self.window)
        groups = [{r.key for r in rows} for rows in (fold.train.rows, fold.validation.rows, fold.test.rows)]
        self.assertFalse(groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2])

    def test_partial_outcome_availability_cannot_silently_filter_one_stock(self):
        labels = (replace(self.dataset.labels[0], target_interval=None, raw_return=None,
                          outcome_available_at=None, missing_reason="MISSING"),) + self.dataset.labels[1:]
        with self.assertRaisesRegex(ContractError, "FULL_UNIVERSE"):
            replace(self.dataset, labels=labels)


if __name__ == "__main__":
    unittest.main()
