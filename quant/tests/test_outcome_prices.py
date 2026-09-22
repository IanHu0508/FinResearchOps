from copy import deepcopy
from datetime import date, timedelta
import unittest

from quant.contracts import ContractError, market_time
from quant.data.baostock import Table
from quant.data.identity import IdentityMap, SecurityIdentity, TickerPeriod
from quant.data.market_store import ReferenceNormalizer
from quant.data.outcome_prices import OutcomePriceNormalizer


FIELDS = ("date", "code", "open", "high", "low", "close", "preclose",
          "volume", "amount", "adjustflag", "tradestatus", "isST", "pctChg", "turn")
FIRST = date(2020, 1, 1)


def quote(ordinal, *, code="sh.600001", price="10", preclose=None,
          volume="100", amount="1000", trade="1", **changes):
    row = dict(zip(FIELDS, (str(FIRST + timedelta(days=ordinal)), code,
        price, price, price, price, price if preclose is None else preclose,
        volume, amount, "3", trade, "0", "0.000000", "1.000000")))
    row.update(changes)
    return row


class Harness:
    def __init__(self, case, mapping=None):
        self.case = case
        self.mapping = mapping or IdentityMap()
        self.features = ReferenceNormalizer(self.mapping)
        self.outcomes = OutcomePriceNormalizer(self.mapping)

    def push(self, ordinal, *raw, source=None):
        day = FIRST + timedelta(days=ordinal)
        source = source or "synthetic-source-" + str(ordinal)
        table = Table(FIELDS, tuple(tuple(r[f] for f in FIELDS) for r in raw), b"")
        feature_rows, issues = self.features.day(day, ordinal, table, source)
        raw_by_symbol = {r["code"][3:] + "." + r["code"][:2].upper(): r for r in raw}
        before = deepcopy((raw_by_symbol, feature_rows, issues, self.features.previous))
        result = self.outcomes.day(day, ordinal, raw_by_symbol, feature_rows,
                                   self.features.identity_decisions, issues, source)
        self.case.assertEqual(before, (raw_by_symbol, feature_rows, issues, self.features.previous))
        return result, feature_rows


class OutcomePriceTests(unittest.TestCase):
    def test_full_prices_use_the_same_forward_reference_formula(self):
        h = Harness(self)
        first, _ = h.push(0, quote(0))
        later, _ = h.push(1, quote(1, price="9.9", preclose="9"))
        self.assertEqual(10, first[0].return_close)
        self.assertAlmostEqual(10 / 9, later[0].factor)
        self.assertAlmostEqual(11, later[0].return_close)
        self.assertEqual(str(FIRST), later[0].chain_start)
        self.assertFalse(later[0].price_only)
        self.assertIsNone(later[0].confirmation_at)

    def test_sparse_entry_and_exit_wait_for_positive_trade_confirmation(self):
        h = Harness(self)
        h.push(0, quote(0))
        for ordinal in range(1, 21):
            out, features = h.push(ordinal, quote(ordinal, trade="0", volume="", amount=""))
            self.assertEqual([], out)
            self.assertEqual([], features)
        self.assertEqual(20, h.outcomes.pending_count)
        confirmed, features = h.push(21, quote(21, price="11", preclose="10"))
        self.assertEqual(21, len(confirmed))
        self.assertEqual(1, features[0][4])
        self.assertFalse(features[0][5])
        self.assertEqual(0, h.outcomes.pending_count)
        for row in confirmed[:-1]:
            self.assertTrue(row.price_only)
            self.assertEqual(market_time(FIRST + timedelta(days=21), 21), row.available_at)
            self.assertEqual(row.available_at, row.confirmation_at)
            self.assertEqual(10, row.open)
            self.assertEqual(str(FIRST), row.chain_start)
            self.assertIn("synthetic-source-" + str((row.session-FIRST).days), row.source_id)
            self.assertIn("synthetic-source-21", row.source_id)
        self.assertEqual(11, confirmed[-1].return_close)

    def test_complete_halted_rows_cannot_bypass_an_unconfirmed_sparse_segment(self):
        h = Harness(self)
        h.push(0, quote(0))
        h.push(1, quote(1, trade="0", volume="", amount=""))
        out, features = h.push(2, quote(2, trade="0", volume="0", amount="0"))
        self.assertEqual([], out)
        self.assertEqual(1, len(features))
        self.assertEqual(2, h.outcomes.pending_count)
        out, _ = h.push(3, quote(3))
        self.assertEqual([FIRST + timedelta(days=i) for i in (1, 2, 3)], [r.session for r in out])
        self.assertFalse(out[1].price_only)
        self.assertEqual(market_time(FIRST + timedelta(days=3), 21), out[1].confirmation_at)

    def test_zero_activity_trading_row_does_not_confirm_pending(self):
        h = Harness(self)
        h.push(0, quote(0))
        h.push(1, quote(1, trade="0", volume="", amount=""))
        out, _ = h.push(2, quote(2, volume="0", amount="0"))
        self.assertEqual([], out)
        self.assertEqual(2, h.outcomes.pending_count)
        self.assertEqual(3, len(h.push(3, quote(3))[0]))

    def test_finish_discards_unconfirmed_tail_and_never_emits_terminal_price(self):
        h = Harness(self)
        h.push(0, quote(0))
        h.push(1, quote(1, trade="0", volume="", amount=""))
        h.push(2, quote(2, trade="0", volume="", amount=""))
        self.assertEqual([], h.outcomes.finish())
        self.assertEqual(0, h.outcomes.pending_count)
        self.assertEqual(2, h.outcomes.discarded_pending_count)
        self.assertIn("OUTCOME_UNCONFIRMED_TAIL", [r[2] for r in h.outcomes.last_issues])
        self.assertEqual([FIRST + timedelta(days=i) for i in (1, 2)], [r[0] for r in h.outcomes.last_issues])
        with self.assertRaisesRegex(ContractError, "OUTCOME_NORMALIZER_FINISHED"):
            h.push(3, quote(3))

    def test_real_price_gap_discards_pending_and_starts_a_new_basis(self):
        for missing in (None, {"close": ""}):
            with self.subTest(missing=missing):
                h = Harness(self)
                h.push(0, quote(0))
                h.push(1, quote(1, trade="0", volume="", amount=""))
                if missing is None:
                    self.assertEqual([], h.push(2)[0])
                else:
                    self.assertEqual([], h.push(2, quote(2, trade="0", volume="", amount="", **missing))[0])
                self.assertEqual([FIRST + timedelta(days=i) for i in (1, 2)], [r[0] for r in h.outcomes.last_issues])
                self.assertTrue(h.outcomes.last_issues[0][2].startswith("OUTCOME_PENDING_DISCARDED:"))
                out, _ = h.push(3, quote(3, price="11"))
                self.assertEqual(1, len(out))
                self.assertEqual(str(FIRST + timedelta(days=3)), out[0].chain_start)
                self.assertEqual(1, h.outcomes.discarded_pending_count)

    def test_sparse_without_immediately_previous_price_cannot_anchor_itself(self):
        h = Harness(self)
        self.assertEqual([], h.push(0, quote(0, trade="0", volume="", amount=""))[0])
        h = Harness(self)
        h.push(0, quote(0))
        self.assertEqual([], h.push(2, quote(2, trade="0", volume="", amount=""))[0])
        self.assertEqual(0, h.outcomes.pending_count)

    def test_ambiguous_or_conflicting_observations_remain_rejected(self):
        cases = [
            dict(trade="1", volume="", amount=""),
            dict(trade="0", volume="1", amount=""),
            dict(trade="0", volume="", amount="2"),
            dict(trade="0", volume="bad", amount=""),
            dict(trade="0", volume="", amount="", low="9.9"),
            dict(trade="0", volume="", amount="", price="9", preclose="9"),
            dict(trade="0", volume="0", amount="0", price="10", preclose="9"),
            dict(trade="0", volume="100", amount="1000"),
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                h = Harness(self)
                h.push(0, quote(0))
                h.push(1, quote(1, trade="0", volume="", amount=""))
                self.assertEqual([], h.push(2, quote(2, **changes))[0])
                out, _ = h.push(3, quote(3))
                self.assertEqual(1, len(out))
                self.assertEqual(str(FIRST + timedelta(days=3)), out[0].chain_start)

    def test_explicit_zero_coherent_adjustment_retains_existing_guard(self):
        h = Harness(self)
        h.push(0, quote(0))
        h.push(1, quote(1, trade="0", volume="", amount=""))
        out, _ = h.push(2, quote(2, trade="0", volume="0", amount="0", price="9", preclose="9"))
        self.assertEqual([], out)
        out, _ = h.push(3, quote(3, price="9.9", preclose="9"))
        self.assertAlmostEqual(10, out[1].return_close)
        self.assertAlmostEqual(11, out[2].return_close)

    def test_price_only_alias_view_does_not_splice_or_ignore_full_conflicts(self):
        mapping = IdentityMap((SecurityIdentity("security:synthetic", (
            TickerPeriod("600001.SH", None, FIRST + timedelta(days=10)),
            TickerPeriod("600009.SH", FIRST + timedelta(days=10), None)), ("synthetic-notice",)),))
        h = Harness(self, mapping)
        h.push(0, quote(0), quote(0, code="sh.600009"))
        out, features = h.push(1, quote(1, trade="0", volume="", amount=""),
                               quote(1, code="sh.600009", trade="0", volume="", amount=""))
        self.assertEqual([], out)
        self.assertEqual([], features)
        out, _ = h.push(2, quote(2), quote(2, code="sh.600009"))
        self.assertEqual(2, len(out))
        self.assertTrue(out[0].price_only)
        self.assertEqual("security:synthetic", out[0].security_id)
        for different in ({"price": "11"}, {"volume": "0", "amount": "1"}):
            h = Harness(self, mapping)
            h.push(0, quote(0), quote(0, code="sh.600009"))
            h.push(1, quote(1, trade="0", volume="", amount=""), quote(1, code="sh.600009", trade="0", volume="", amount=""))
            right = dict(trade="0", volume="0", amount="0")
            right.update(different)
            out, _ = h.push(2, quote(2, trade="0", volume="0", amount="0"), quote(2, code="sh.600009", **right))
            self.assertEqual([], out)
            self.assertEqual(0, h.outcomes.pending_count)

    def test_original_feature_streak_and_eligibility_are_not_restored(self):
        h = Harness(self)
        for i in range(61):
            _, features = h.push(i, quote(i))
        self.assertTrue(features[0][5])
        _, missing = h.push(61, quote(61, trade="0", volume="", amount=""))
        self.assertEqual([], missing)
        prices, features = h.push(62, quote(62))
        self.assertEqual(2, len(prices))
        self.assertEqual(1, features[0][4])
        self.assertFalse(features[0][5])
        self.assertEqual(str(FIRST), prices[-1].chain_start)
        self.assertEqual(str(FIRST + timedelta(days=62)), features[0][3])

    def test_incomplete_alias_cannot_hide_known_activity_or_supply_missing_prices(self):
        mapping = IdentityMap((SecurityIdentity("security:synthetic", (
            TickerPeriod("600001.SH", None, FIRST + timedelta(days=10)),
            TickerPeriod("600009.SH", FIRST + timedelta(days=10), None)), ("synthetic-notice",)),))
        for case in ("known_activity", "complementary_prices"):
            with self.subTest(case=case):
                h = Harness(self, mapping)
                h.push(0, quote(0), quote(0, code="sh.600009"))
                left = quote(1, trade="0", volume="", amount="")
                right = quote(1, code="sh.600009", trade="0", volume="", amount="")
                if case == "known_activity":
                    right["amount"] = "1"
                else:
                    left["open"] = ""
                    right["close"] = ""
                out, _ = h.push(1, left, right)
                self.assertEqual([], out)
                self.assertEqual(0, h.outcomes.pending_count)

    def test_prior_source_is_bound_even_when_all_prices_are_equal(self):
        a, b = Harness(self), Harness(self)
        a.push(0, quote(0), source="synthetic-original-a")
        b.push(0, quote(0), source="synthetic-original-b")
        ar, _ = a.push(1, quote(1))
        br, _ = b.push(1, quote(1))
        self.assertEqual(ar[0].return_close, br[0].return_close)
        self.assertNotEqual(ar[0].source_id, br[0].source_id)

    def test_nonunit_outcome_basis_survives_feature_chain_restart(self):
        h = Harness(self)
        h.push(0, quote(0))
        h.push(1, quote(1, price="9", preclose="9"))
        h.push(2, quote(2, trade="0", volume="", amount="", price="9"))
        out, features = h.push(3, quote(3, price="10.8", preclose="9"))
        self.assertEqual(1, features[0][2])
        self.assertEqual(10.8, features[0][0].return_close)
        self.assertAlmostEqual(10 / 9, out[-1].factor)
        self.assertAlmostEqual(12, out[-1].return_close)
        self.assertAlmostEqual(10, out[0].return_close)

    def test_exact_amount_conflict_projection_keeps_feature_rejection(self):
        mapping = IdentityMap((SecurityIdentity("security:synthetic", (
            TickerPeriod("600001.SH", None, FIRST + timedelta(days=10)),
            TickerPeriod("600009.SH", FIRST + timedelta(days=10), None)), ("synthetic-notice",)),))
        h = Harness(self, mapping)
        h.push(0, quote(0), quote(0, code="sh.600009"))
        left = quote(1, price="10.5", preclose="10", amount="1049.99", pctChg="5.000000",
                     open="10.2", high="10.6", low="10", turn="1.000000")
        right = {**left, "code": "sh.600009", "amount": "1050.02", "turn": "1.000001"}
        before = deepcopy((left, right))
        out, features = h.push(1, left, right)
        self.assertEqual([], features)
        self.assertEqual(before, (left, right))
        self.assertEqual(1, len(out))
        self.assertEqual(10.5, out[0].return_close)
        self.assertFalse(out[0].price_only)
        self.assertIsNone(out[0].confirmation_at)
        self.assertIn("amount-conflict-price-consensus", out[0].source_id)
        self.assertEqual(market_time(FIRST + timedelta(days=1), 21), out[0].available_at)
        _, features = h.push(2, quote(2, price="10.5"), quote(2, code="sh.600009", price="10.5"))
        self.assertEqual(1, features[0][4])
        self.assertFalse(features[0][5])

    def test_positive_consensus_confirms_pending_without_selecting_amount(self):
        mapping = IdentityMap((SecurityIdentity("security:synthetic", (
            TickerPeriod("600001.SH", None, FIRST + timedelta(days=10)),
            TickerPeriod("600009.SH", FIRST + timedelta(days=10), None)), ("synthetic-notice",)),))
        h = Harness(self, mapping)
        h.push(0, quote(0), quote(0, code="sh.600009"))
        h.push(1, quote(1, trade="0", volume="", amount=""), quote(1, code="sh.600009", trade="0", volume="", amount=""))
        out, features = h.push(2, quote(2, amount="1000.01"), quote(2, code="sh.600009", amount="1000.03"))
        self.assertEqual([], features)
        self.assertEqual(2, len(out))
        self.assertEqual(0, h.outcomes.pending_count)
        self.assertTrue(out[0].price_only)
        self.assertEqual(market_time(FIRST + timedelta(days=2), 21), out[0].confirmation_at)
        self.assertEqual(out[0].confirmation_at, out[0].available_at)
        self.assertIn(out[1].source_id, out[0].source_id)

    def test_consensus_rejects_nonexact_prices_activity_flags_and_unmapped_identity(self):
        mapping = IdentityMap((SecurityIdentity("security:synthetic", (
            TickerPeriod("600001.SH", None, FIRST + timedelta(days=10)),
            TickerPeriod("600009.SH", FIRST + timedelta(days=10), None)), ("synthetic-notice",)),))
        changes = ({"close": "10.0000000001"}, {"preclose": "10.0000000001"},
            {"volume": "101"}, {"tradestatus": "0"}, {"adjustflag": "2"},
            {"pctChg": "0.000001"}, {"isST": "1"}, {"amount": ""},
            {"amount": "-2"}, {"amount": "0"}, {"amount": "NaN"}, {"amount": "Infinity"})
        for change in changes:
            with self.subTest(change=change):
                h = Harness(self, mapping)
                h.push(0, quote(0), quote(0, code="sh.600009"))
                right = quote(1, code="sh.600009", amount="1000.03")
                right.update(change)
                out, features = h.push(1, quote(1, amount="1000.01"), right)
                # The original rules may select a complete alias when the other
                # amount is missing/nonfinite. Preserve that established path;
                # it must never be mislabeled as exact amount consensus.
                self.assertTrue(all("amount-conflict-price-consensus" not in r.source_id for r in out))
                if not features:
                    self.assertEqual([], out)
        h = Harness(self)
        out, _ = h.push(0, quote(0, amount="1000.01"), quote(0, code="sh.600009", amount="1000.03"))
        self.assertEqual(2, len(out))
        self.assertTrue(all("amount-conflict-price-consensus" not in r.source_id for r in out))

    def test_consensus_provenance_binds_every_alias_and_is_order_independent(self):
        mapping = IdentityMap((SecurityIdentity("security:synthetic", (
            TickerPeriod("600001.SH", None, FIRST + timedelta(days=10)),
            TickerPeriod("600009.SH", FIRST + timedelta(days=10), None)), ("synthetic-notice",)),))
        left, right = quote(0, amount="1000.01"), quote(0, code="sh.600009", amount="1000.03")
        first = Harness(self, mapping).push(0, left, right)[0][0]
        reversed_order = Harness(self, mapping).push(0, right, left)[0][0]
        changed = Harness(self, mapping).push(0, left, {**right, "amount": "1000.04"})[0][0]
        self.assertEqual(first, reversed_order)
        self.assertEqual(first.return_close, changed.return_close)
        self.assertNotEqual(first.source_id, changed.source_id)

    def test_consensus_activity_must_be_complete_and_positive_in_native_representation(self):
        mapping = IdentityMap((SecurityIdentity("security:synthetic", (
            TickerPeriod("600001.SH", None, FIRST + timedelta(days=10)),
            TickerPeriod("600009.SH", FIRST + timedelta(days=10), None)), ("synthetic-notice",)),))
        cases = (("1e309", "1000.01", "1000.03"),
                 ("1e-999", "1000.01", "1000.03"),
                 ("100", "1e-999", "1000.03"))
        for volume, left_amount, right_amount in cases:
            with self.subTest(volume=volume, amounts=(left_amount, right_amount)):
                h = Harness(self, mapping)
                h.push(0, quote(0), quote(0, code="sh.600009"))
                h.push(1, quote(1, trade="0", volume="", amount=""),
                       quote(1, code="sh.600009", trade="0", volume="", amount=""))
                out, features = h.push(2, quote(2, volume=volume, amount=left_amount),
                    quote(2, code="sh.600009", volume=volume, amount=right_amount))
                self.assertEqual([], features)
                self.assertEqual([], out)
                self.assertEqual(0, h.outcomes.pending_count)
                self.assertEqual(1, h.outcomes.discarded_pending_count)


if __name__ == "__main__":
    unittest.main()
