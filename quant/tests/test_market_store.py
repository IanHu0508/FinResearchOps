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
    def test_halted_activity_or_price_range_is_unresolved_not_rewritten(self):
        day = date(2020, 1, 2)
        positive_activity = table(day, trade="0")
        raw = list(table(day, trade="0", volume="0", amount="0").rows[0])
        raw[4] = "9.9"
        nonflat = Table(FIELDS, (tuple(raw),), b"")
        for observation, reason in [(positive_activity, "SUSPENDED_ACTIVITY_UNRESOLVED"),
                                    (nonflat, "SUSPENDED_RANGE_UNRESOLVED")]:
            with self.subTest(reason=reason):
                normalizer = ReferenceNormalizer()
                rows, issues = normalizer.day(day, 0, observation, "synthetic")
                self.assertEqual([], rows)
                self.assertEqual(reason, issues[0][1])
                self.assertFalse(normalizer.previous)
                next_day = day + timedelta(days=1)
                rows, issues = normalizer.day(next_day, 1, table(next_day), "next")
                self.assertFalse(issues)
                self.assertEqual(1, rows[0][4])
                self.assertEqual(str(next_day), rows[0][3])

    def test_cdr_does_not_enter_ordinary_share_history_or_universe(self):
        normalizer = ReferenceNormalizer()
        first = date(2020, 1, 1)
        for ordinal in range(61):
            day = first + timedelta(days=ordinal)
            ordinary = table(day, code="sh.688001")
            receipt = table(day, code="sh.689001")
            rows, issues = normalizer.day(day, ordinal,
                Table(FIELDS, ordinary.rows + receipt.rows, b""), "synthetic")
            self.assertFalse(issues)
            self.assertEqual(["ticker:688001.SH"], [row[0].symbol for row in rows])
        self.assertTrue(rows[0][5])
        self.assertNotIn("ticker:689001.SH", normalizer.previous)

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
    def make_raw(self, directory, *, gap=False, session_count=105, rename_at=None, alias_conflict_at=None,
                 alias_conflict_field="amount", halt_missing_at=None):
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
            if i == halt_missing_at:
                for column in range(2,7):
                    rows[0][column] = str(10 + (i-1)/100)
                rows[0][7] = rows[0][8] = ""
                rows[0][10] = "0"
            if halt_missing_at is not None and i == halt_missing_at + 1:
                rows[0][6] = str(10 + (i-2)/100)
            if rename_at is not None:
                # Non-unit reference factor before the ticker switch, retained
                # after it. Old/new aliases coexist only before the switch.
                if i == 75:
                    rows[0][6] = "9.0"
                alias = list(rows[0])
                alias[1] = "sh.600009"
                if i == alias_conflict_at:
                    alias[FIELDS.index(alias_conflict_field)] = "1001"
                if i < rename_at:
                    rows.append(alias)
                else:
                    rows[0] = alias
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
                        self.assertEqual("OUTCOME_PRICE_OR_BASIS_UNAVAILABLE", dataset.labels[1].missing_reason)
                with self.assertRaisesRegex(ContractError, "ALREADY_EXISTS"):
                    build_store(root, destination)

    def test_incomplete_cache_cannot_become_a_canonical_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root, destination, sessions = self.make_raw(directory)
            (root / ("daily-" + str(sessions[-1]) + ".json")).unlink()
            with self.assertRaisesRegex(ContractError, "ACQUISITION_INCOMPLETE"):
                build_store(root, destination)
            self.assertFalse(destination.exists())

    def test_identity_store_projects_asof_ticker_across_history_and_future(self):
        from quant.tests.test_identity import identity_map
        with tempfile.TemporaryDirectory() as directory:
            root,destination,sessions=self.make_raw(directory,session_count=130,rename_at=90)
            metadata=build_store(root,destination,identity_map=identity_map(sessions[90]))
            self.assertEqual("quant.canonical-market-store/v9",metadata["schema_version"])
            with MarketStore(destination) as store:
                # Exactly two economic/provisional identities each day, even
                # when the raw archive has three rows. Every input shares keys.
                self.assertEqual(260,store.connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0])
                for score,expected in [(80,"600001.SH"),(95,"600009.SH")]:
                    data=store.read_day(sessions[score])
                    dataset=prepare_dataset(data,ResearchSpec(UNIVERSE_ID))
                    self.assertEqual({expected,"000001.SZ"},{r.key.symbol for r in dataset.panel.rows})
                    self.assertTrue(all(y.complete for y in dataset.labels))
                    self.assertTrue(all(len(u.symbols)==2 for u in data.universes))
                    label=next(y for y in dataset.labels if y.key.symbol==expected)
                    self.assertAlmostEqual((10+(score+20)/100)/(10+(score+1)/100)-1,label.raw_return)
                retained=store.connection.execute("SELECT consecutive_sessions,factor,trading_symbol FROM bars WHERE security_id=? AND session=?",("security:example-a",str(sessions[90]))).fetchone()
                self.assertEqual(91,retained[0])
                self.assertGreater(retained[1],1)
                self.assertEqual("600009.SH",retained[2])
                d=json.loads(store.connection.execute("SELECT document FROM identity_decisions WHERE session=?",(str(sessions[80]),)).fetchone()[0])
                self.assertEqual(["600001.SH","600009.SH"],d["provider_symbols"])
                self.assertEqual("600001.SH",d["selected_symbol"])

    def test_future_alias_price_conflict_keeps_original_pool_and_invalidates_labels(self):
        from quant.tests.test_identity import identity_map
        with tempfile.TemporaryDirectory() as directory:
            root,destination,sessions=self.make_raw(directory,rename_at=90,alias_conflict_at=85,
                                                   alias_conflict_field="preclose")
            build_store(root,destination,identity_map=identity_map(sessions[90]))
            with MarketStore(destination) as store:
                dataset=prepare_dataset(store.read_day(sessions[80]),ResearchSpec(UNIVERSE_ID))
                self.assertEqual(2,len(dataset.labels))
                self.assertTrue(all(not y.complete for y in dataset.labels))
                reason=store.connection.execute("SELECT reason FROM issues WHERE session=?",(str(sessions[85]),)).fetchone()[0]
                self.assertEqual("IDENTITY_QUOTE_CONFLICT:preclose",reason)

    def test_store_refuses_legacy_version_and_changed_identity_map(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            root,destination,_=self.make_raw(directory)
            original=build_store(root,destination)
            for mutation,message in [({"schema_version":"quant.canonical-market-store/v1"},"STORE_VERSION_INVALID"),
                                     ({"identity_map_id":"0"*64},"STORE_IDENTITY_MAP_MISMATCH")]:
                with sqlite3.connect(destination) as c:
                    c.execute("UPDATE metadata SET document=?",(json.dumps({**original,**mutation}),))
                with self.assertRaisesRegex(ContractError,message):
                    MarketStore(destination)

    def test_quality_counts_retired_codes_separately_from_security_ids(self):
        from quant.data.quality import audit_store
        from quant.tests.test_identity import identity_map
        with tempfile.TemporaryDirectory() as directory:
            root,destination,sessions=self.make_raw(directory,rename_at=90)
            build_store(root,destination,identity_map=identity_map(sessions[90]))
            master_rows=[[code,"Synthetic","1990-01-01",str(sessions[89]) if code=="sh.600001" else "","1","1"]
                         for code in ("sh.600001","sh.600009","sz.000001")]
            wire=frame(["0","ok","query_stock_basic","anonymous","1","2000",json.dumps({"record":master_rows}),"","",
                        "code,code_name,ipoDate,outDate,type,status"],"46")
            master=decode_table(wire,method="query_stock_basic",field_index=9)
            save_response(root,"master-p1",master,query={},seconds=0)
            report=audit_store(destination,root,[root/"master-p1.json"])
            self.assertEqual("quant.market-quality-report/v3",report["schema_version"])
            self.assertEqual(2,report["observed_security_ids"])
            self.assertEqual(3,report["observed_trading_symbols"])
            self.assertEqual(["600001.SH"],report["observed_codes_with_master_outdate"])
            self.assertEqual(90,report["reconciled_extra_alias_rows_removed"])
            self.assertFalse(report["all_security_identities_verified"])
            self.assertNotIn("later_delisted_observed_symbols",report)


if __name__ == "__main__":
    unittest.main()
