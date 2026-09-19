from dataclasses import fields, replace
from datetime import datetime, timedelta
import math
import unittest

from quant.contracts import ContractError, MARKET_NAMES, SCALAR_NAMES, market_time, session_of
from quant.features import build_panel
from quant.models.views import vectors
from quant.synthetic import make_synthetic_data


class DataContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.spec = make_synthetic_data(score_start=60, score_end=62)

    def test_input_has_no_fundamental_valuation_or_industry_dependencies(self):
        self.assertEqual({"sessions", "bars", "universes", "scoring_dates", "data_kind", "price_basis"},
                         {f.name for f in fields(self.data)})
        panel = build_panel(self.data, self.spec)
        self.assertFalse(hasattr(panel.rows[0], "industry"))
        self.assertFalse(any(word in name for name in SCALAR_NAMES
                             for word in ("earnings", "book", "revenue", "profit", "industry", "shares")))

    def test_spec_rejects_other_horizon_lookback_or_old_target(self):
        for updates in ({"horizon_sessions": 5}, {"lookback_sessions": 30},
                        {"target_id": "cn-a-industry-loo-equalweight-20d-percentile/v1"},
                        {"horizon_sessions": True}, {"feature_definition_id": "other"}):
            with self.subTest(updates=updates), self.assertRaises(ContractError):
                replace(self.spec, **updates)

    def test_only_explicit_scoring_dates_produce_rows(self):
        panel = build_panel(self.data, self.spec)
        self.assertGreater(len(self.data.universes), len(self.data.scoring_dates))
        self.assertEqual(18, len(panel.rows))
        self.assertEqual(set(self.data.scoring_dates), {session_of(r.key.as_of) for r in panel.rows})
        self.assertTrue(all(len(r.sequence) == 60 and r.data_cutoff <= r.key.as_of for r in panel.rows))

    def test_insufficient_history_and_missing_scoring_snapshot_are_errors(self):
        data, spec = make_synthetic_data(score_start=30, score_end=31)
        with self.assertRaisesRegex(ContractError, "INSUFFICIENT_SEQUENCE"):
            build_panel(data, spec)
        with self.assertRaisesRegex(ContractError, "SCORING_UNIVERSE_MISSING"):
            replace(self.data, universes=tuple(u for u in self.data.universes
                    if session_of(u.as_of) != self.data.scoring_dates[0]))

    def test_naive_timestamps_and_incomplete_bars_are_rejected(self):
        bar = self.data.bars[0]
        with self.assertRaisesRegex(ContractError, "TIMEZONE"):
            replace(bar, available_at=datetime(2020, 1, 2, 16))
        with self.assertRaisesRegex(ContractError, "DAILY_BAR_NOT_FINISHED"):
            replace(bar, available_at=market_time(bar.session, 9))

    def test_invalid_price_volume_and_amount_are_rejected(self):
        for updates in ({"high": 1}, {"close": float("nan")}, {"volume": -1},
                        {"amount": -1}, {"amount": float("inf")}, {"amount": True}, {"amount": 0}):
            with self.subTest(updates=updates), self.assertRaises(ContractError):
                replace(self.data.bars[0], **updates)

    def test_duplicate_bar_and_universe_cannot_be_merged_silently(self):
        with self.assertRaisesRegex(ContractError, "DUPLICATE_MARKET_BAR"):
            replace(self.data, bars=self.data.bars + (self.data.bars[0],))
        with self.assertRaisesRegex(ContractError, "MULTIPLE_UNIVERSES"):
            replace(self.data, universes=self.data.universes + (self.data.universes[0],))

    def test_today_universe_cannot_be_backdated(self):
        snapshot = self.data.universes[0]
        with self.assertRaisesRegex(ContractError, "FUTURE_UNIVERSE"):
            replace(snapshot, available_at=snapshot.as_of + timedelta(days=1))

    def test_market_context_has_known_median_breadth_and_dispersion(self):
        day, previous = self.data.sessions[60], self.data.sessions[59]
        returns = dict(zip(self.data.universes[0].symbols, (0.01, 0.02, -0.01, 0.0, 0.04, -0.02)))
        bars = tuple(replace(b, return_close=100) if b.session == previous else
                     replace(b, return_close=100 * (1 + returns[b.symbol])) if b.session == day else b
                     for b in self.data.bars)
        panel = build_panel(replace(self.data, bars=bars, scoring_dates=(day,)), self.spec)
        contexts = {r.market_context for r in panel.rows}
        self.assertEqual(1, len(contexts))
        context = next(iter(contexts))
        self.assertAlmostEqual(0.005, context[MARKET_NAMES.index("median_return")])
        self.assertAlmostEqual(0.5, context[MARKET_NAMES.index("breadth")])
        self.assertAlmostEqual(math.sqrt(7 / 15000), context[MARKET_NAMES.index("cross_sectional_dispersion")])
        first = panel.rows[0]
        self.assertAlmostEqual(0.005, first.relative_features[0])
        self.assertEqual(1, len({r.market_context_id for r in panel.rows}))

    def test_market_volatility_uses_each_historical_pool(self):
        baseline = build_panel(self.data, self.spec)
        snapshots = tuple(replace(u, symbols=u.symbols[:-1]) if session_of(u.as_of) == self.data.sessions[45]
                          else u for u in self.data.universes)
        changed = build_panel(replace(self.data, universes=snapshots), self.spec)
        self.assertEqual(baseline.rows[0].scalars, changed.rows[0].scalars)
        self.assertEqual(baseline.rows[0].market_context[:3], changed.rows[0].market_context[:3])
        self.assertNotEqual(baseline.rows[0].market_context[3], changed.rows[0].market_context[3])
        self.assertNotEqual(baseline.rows[0].market_context_id, changed.rows[0].market_context_id)
        self.assertEqual(vectors(baseline.rows, "tabular", "stock-only"),
                         vectors(changed.rows, "tabular", "stock-only"))

    def test_context_warmup_membership_is_required_not_current_pool_backfill(self):
        snapshots = tuple(u for u in self.data.universes if session_of(u.as_of) != self.data.sessions[45])
        with self.assertRaisesRegex(ContractError, "MARKET_UNIVERSE_HISTORY_MISSING"):
            build_panel(replace(self.data, universes=snapshots), self.spec)

    def test_historical_market_statistic_requires_its_historical_availability(self):
        day = self.data.sessions[45]
        bars = tuple(replace(b, available_at=market_time(self.data.sessions[46], 16))
                     if b.symbol == "SYN005.SH" and b.session == day else b for b in self.data.bars)
        with self.assertRaisesRegex(ContractError, "MISSING_OR_UNAVAILABLE_MARKET_CONTEXT"):
            build_panel(replace(self.data, bars=bars), self.spec)

    def test_market_sources_are_bound_without_quadratic_provenance_duplication(self):
        baseline = build_panel(self.data, self.spec)
        day = self.data.sessions[60]
        bars = tuple(replace(b, source_id="changed-other-stock-source")
                     if b.symbol == "SYN005.SH" and b.session == day else b for b in self.data.bars)
        changed = build_panel(replace(self.data, bars=bars), self.spec)
        self.assertEqual(baseline.rows[0].scalars, changed.rows[0].scalars)
        self.assertNotEqual(baseline.rows[0].market_context_id, changed.rows[0].market_context_id)
        self.assertTrue(any(s.startswith("market-context:") for s in changed.rows[0].source_ids))
        self.assertLessEqual(len(changed.rows[0].source_ids), 63)

    def test_other_stock_market_input_updates_every_rows_data_cutoff(self):
        day = self.data.sessions[60]
        cutoff = market_time(day, 17, 45)
        bars = tuple(replace(b, available_at=cutoff)
                     if b.symbol == "SYN005.SH" and b.session == day else b for b in self.data.bars)
        panel = build_panel(replace(self.data, bars=bars), self.spec)
        rows = [r for r in panel.rows if session_of(r.key.as_of) == day]
        self.assertTrue(all(r.data_cutoff == cutoff for r in rows))

    def test_missing_or_future_bars_do_not_shrink_the_pool(self):
        day = self.data.sessions[60]
        missing = tuple(b for b in self.data.bars if not (b.symbol == "SYN000.SH" and b.session == day))
        future = tuple(replace(b, available_at=market_time(self.data.sessions[61], 16))
                       if b.symbol == "SYN000.SH" and b.session == day else b for b in self.data.bars)
        for bars in (missing, future):
            with self.assertRaisesRegex(ContractError, "MISSING_OR_UNAVAILABLE"):
                build_panel(replace(self.data, bars=bars), self.spec)

    def test_per_day_membership_does_not_use_current_survivor_intersection(self):
        snapshots = self.data.universes[:-1] + (replace(self.data.universes[-1],
                     symbols=self.data.universes[-1].symbols[1:]),)
        panel = build_panel(replace(self.data, universes=snapshots), self.spec)
        self.assertEqual(17, len(panel.rows))
        self.assertEqual(2, sum(r.key.symbol == "SYN000.SH" for r in panel.rows))

    def test_ratios_and_zscores_use_twenty_prior_sessions_not_today(self):
        prior_days = set(self.data.sessions[40:60])
        day = self.data.sessions[60]
        bars = tuple(replace(b, volume=10, amount=1000)
                     if b.symbol == "SYN000.SH" and b.session in prior_days else
                     replace(b, volume=100, amount=5000)
                     if b.symbol == "SYN000.SH" and b.session == day else b for b in self.data.bars)
        row = build_panel(replace(self.data, bars=bars), self.spec).rows[0]
        self.assertEqual(10, row.scalars[SCALAR_NAMES.index("volume_ratio_20")])
        self.assertEqual(5, row.scalars[SCALAR_NAMES.index("amount_ratio_20")])
        self.assertIsNone(row.scalars[SCALAR_NAMES.index("volume_zscore_20")])
        self.assertIsNone(row.scalars[SCALAR_NAMES.index("amount_zscore_20")])

    def test_zero_amount_historical_day_is_masked_not_divided_by_zero(self):
        day = self.data.sessions[55]
        bars = tuple(replace(b, volume=0, amount=0, tradable=False)
                     if b.symbol == "SYN000.SH" and b.session == day else b for b in self.data.bars)
        snapshots = tuple(replace(u, symbols=u.symbols[1:]) if session_of(u.as_of) == day
                          else u for u in self.data.universes)
        row = build_panel(replace(self.data, bars=bars, universes=snapshots), self.spec).rows[0]
        self.assertEqual(0.95, row.scalars[SCALAR_NAMES.index("liquidity_observation_fraction_20")])
        self.assertTrue(math.isfinite(row.scalars[SCALAR_NAMES.index("illiquidity_20")]))
        self.assertEqual((0.0, 0.0, 0.0), row.sequence[54][-3:])

    def test_nontrading_current_member_is_rejected(self):
        day = self.data.sessions[60]
        bars = tuple(replace(b, volume=0, amount=0, tradable=False)
                     if b.symbol == "SYN000.SH" and b.session == day else b for b in self.data.bars)
        with self.assertRaisesRegex(ContractError, "INELIGIBLE_STOCK"):
            build_panel(replace(self.data, bars=bars), self.spec)

    def test_gap_uses_comparable_return_prices_across_corporate_actions(self):
        prior, day = self.data.sessions[59:61]
        bars = tuple(replace(b, open=201, high=205, low=190, close=200, return_close=100)
                     if b.symbol == "SYN000.SH" and b.session == prior else
                     replace(b, open=102, high=104, low=101, close=103, return_open=102, return_close=103)
                     if b.symbol == "SYN000.SH" and b.session == day else b for b in self.data.bars)
        row = build_panel(replace(self.data, bars=bars), self.spec).rows[0]
        self.assertAlmostEqual(0.02, row.scalars[SCALAR_NAMES.index("overnight_gap")])
        self.assertAlmostEqual(0.03, row.sequence[-1][0])

    def test_mutable_inputs_cannot_change_prepared_snapshots(self):
        with self.assertRaisesRegex(ContractError, "IMMUTABLE"):
            replace(self.data, bars=list(self.data.bars))
        row = build_panel(self.data, self.spec).rows[0]
        for updates in ({"scalars": list(row.scalars)}, {"relative_features": list(row.relative_features)},
                        {"market_context": list(row.market_context)}):
            with self.subTest(updates=updates), self.assertRaisesRegex(ContractError, "IMMUTABLE"):
                replace(row, **updates)

    def test_one_scoring_cutoff_and_one_market_context_per_date(self):
        panel = build_panel(self.data, self.spec)
        row = panel.rows[0]
        changed = replace(row, key=replace(row.key, as_of=row.key.as_of + timedelta(minutes=1)))
        with self.assertRaisesRegex(ContractError, "MIXED_SCORING_CUTOFF"):
            replace(panel, rows=tuple(sorted((changed,) + panel.rows[1:], key=lambda r: r.key)))
        changed = replace(row, market_context=(row.market_context[0] + 0.1,) + row.market_context[1:])
        with self.assertRaisesRegex(ContractError, "MIXED_MARKET_CONTEXT"):
            replace(panel, rows=(changed,) + panel.rows[1:])


if __name__ == "__main__":
    unittest.main()
