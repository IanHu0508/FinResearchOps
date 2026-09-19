from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from quant.contracts import ContractError, ResearchSpec, market_time
from quant.data.acquisition import save_response
from quant.data.baostock import Table, decode_table
from quant.data.market_store import MarketStore, ReferenceNormalizer, UNIVERSE_ID, build_store
from quant.pipeline import prepare_dataset
from quant.tests.test_acquisition import frame

FIELDS = ("date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "adjustflag", "tradestatus", "isST")


def table(day, close=10.0, preclose=10.0, *, code="sh.600001", trade="1", volume="100", amount="1000", st="0"):
    return Table(FIELDS, ((str(day), code, str(close), str(close), str(close), str(close),
                          str(preclose), volume, amount, "3", trade, st),), b"")


class NormalizerTests(unittest.TestCase):
    def test_cash_ex_reference_uses_previous_close_over_ex_price(self):
        n = ReferenceNormalizer()
        n.day(date(2020, 1, 2), 0, table(date(2020, 1, 2)), "source-a")
        rows, issues = n.day(date(2020, 1, 3), 1, table(date(2020, 1, 3), 9.9, 9), "source-b")
        self.assertFalse(issues)
        bar = rows[0][0]
        self.assertAlmostEqual(11.0, bar.return_close)
        self.assertAlmostEqual(9.9, bar.close)
        self.assertEqual(market_time(bar.session, 21), bar.available_at)

    def test_gap_resets_history_instead_of_making_a_bridge_return(self):
        n = ReferenceNormalizer()
        n.day(date(2020, 1, 2), 0, table(date(2020, 1, 2)), "a")
        rows, _ = n.day(date(2020, 1, 6), 2, table(date(2020, 1, 6), 11, 11), "b")
        _, _, factor, chain, streak, eligible, _ = rows[0]
        self.assertEqual((1.0, "2020-01-06", 1, False), (factor, chain, streak, eligible))

    def test_unknown_numeric_and_halted_reference_change_are_not_zero_fills(self):
        n = ReferenceNormalizer()
        n.day(date(2020, 1, 2), 0, table(date(2020, 1, 2)), "a")
        rows, issues = n.day(date(2020, 1, 3), 1,
                             table(date(2020, 1, 3), 10, 9, trade="0", volume="0", amount="0"), "b")
        self.assertEqual([], rows)
        self.assertEqual("SUSPENDED_REFERENCE_CHANGE_UNRESOLVED", issues[0][1])
        rows, issues = n.day(date(2020, 1, 6), 2, table(date(2020, 1, 6), amount=""), "c")
        self.assertEqual([], rows)
        self.assertEqual("RAW_NUMERIC_INVALID", issues[0][1])

    def test_coherent_zero_trade_ex_quotes_preserve_reference_value(self):
        n = ReferenceNormalizer()
        original, _ = n.day(date(2020, 1, 2), 0, table(date(2020, 1, 2)), "a")
        rows, issues = n.day(date(2020, 1, 3), 1,
                             table(date(2020, 1, 3), 9, 9, trade="0", volume="0", amount="0"), "b")
        self.assertFalse(issues)
        self.assertAlmostEqual(original[0][0].return_close, rows[0][0].return_close)
        self.assertAlmostEqual(0, rows[0][0].return_open / original[0][0].return_close - 1)
        self.assertEqual(original[0][3], rows[0][3])
        self.assertEqual(2, rows[0][4])
        self.assertFalse(rows[0][5])

    def test_history_eligibility_uses_only_observed_prefix_and_keeps_st(self):
        n = ReferenceNormalizer()
        first = date(2020, 1, 1)
        previous_row = None
        for i in range(61):
            day = first + timedelta(days=i)
            rows, _ = n.day(day, i, table(day, st="1"), "src" + str(i))
            self.assertEqual(i == 60, rows[0][5])
            if i == 59:
                previous_row = rows[0]
        n.day(first + timedelta(days=61), 61, table(first + timedelta(days=61), 5, 5), "split")
        self.assertEqual(10, previous_row[0].return_close)
        self.assertFalse(previous_row[5])


class MarketStoreTests(unittest.TestCase):
    def make_raw(self, directory, *, gap=False, session_count=105):
        workspace = Path(directory)
        (workspace / "finaudit-gate").mkdir()
        (workspace / "finaudit-gate" / "pyproject.toml").write_text("# synthetic test workspace\n")
        root = workspace / "private" / "raw"
        root.mkdir(parents=True)
        sessions = [date(2020, 1, 1) + timedelta(days=i) for i in range(session_count)]
        plan = {"schema_version": "quant.baostock-acquisition/v1", "start": str(sessions[0]), "end": str(sessions[-1])}
        (root / "plan.json").write_text(json.dumps(plan))
        wire = frame(["0", "ok", "query_trade_dates", "anonymous", "1", "2000",
                      json.dumps({"record": [[str(d), "1"] for d in sessions]}), "", "", "calendar_date,is_trading_day"], "34")
        save_response(root, "calendar-p1", decode_table(wire, method="query_trade_dates", field_index=9), query={}, seconds=0)
        for i, day in enumerate(sessions):
            rows = [list(table(day, 10 + i / 100, 10 + max(i - 1, 0) / 100, code=code).rows[0])
                    for code in ("sh.600001", "sz.000001")]
            if gap and i == 90:
                rows[0][8] = ""
            wire = frame(["0", "ok", "query_daily_history_k_AStock", "anonymous",
                          json.dumps({"record": rows}), ",".join(FIELDS)], "99")
            save_response(root, "daily-" + str(day), decode_table(wire, method="query_daily_history_k_AStock", row_index=4, field_index=5), query={}, seconds=0)
        return root, workspace / "private" / "canonical.sqlite", sessions

    def test_disk_block_matches_contract_and_preserves_the_full_target_pool(self):
        for gap in (False, True):
            with self.subTest(gap=gap), tempfile.TemporaryDirectory() as directory:
                root, destination, sessions = self.make_raw(directory, gap=gap)
                build_store(root, destination)
                with MarketStore(destination) as store:
                    data = store.read_day(sessions[80])
                    self.assertFalse(store.metadata["historical_vendor_vintage_verified"])
                    dataset = prepare_dataset(data, ResearchSpec(UNIVERSE_ID))
                    self.assertEqual(2, len(dataset.panel.rows))
                    self.assertEqual([not gap, not gap], [y.complete for y in dataset.labels])
                    self.assertEqual(sessions[100], dataset.labels[0].label_end_date)
                    if gap:
                        self.assertEqual("CROSS_SECTION_OUTCOME_INCOMPLETE", dataset.labels[1].missing_reason)
                with self.assertRaisesRegex(ContractError, "ALREADY_EXISTS"):
                    build_store(root, destination)

    def test_incomplete_cache_cannot_become_a_canonical_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root, destination, sessions = self.make_raw(directory)
            (root / ("daily-" + str(sessions[-1]) + ".json")).unlink()
            with self.assertRaisesRegex(ContractError, "ACQUISITION_INCOMPLETE"):
                build_store(root, destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
