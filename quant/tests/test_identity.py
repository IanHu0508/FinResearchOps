from datetime import date
import unittest

from quant.contracts import ContractError
from quant.data.identity import IdentityMap, QuoteObservation, SecurityIdentity, TickerPeriod
from quant.data.market_store import ReferenceNormalizer
from quant.data.baostock import Table
from quant.tests.test_market_store import FIELDS, table


def identity_map(cutoff=date(2020, 1, 3)):
    return IdentityMap((SecurityIdentity("security:example-a", (
        TickerPeriod("600001.SH", None, cutoff),
        TickerPeriod("600009.SH", cutoff, None)), ("synthetic-notice:one-to-one",)),))


def quote(symbol, **values):
    return QuoteObservation(symbol, tuple(sorted(values.items())))


class IdentityTests(unittest.TestCase):
    def test_half_open_intervals_and_provisional_unknowns(self):
        m = identity_map()
        self.assertEqual(m.security_id("600001.SH"), m.security_id("600009.SH"))
        self.assertEqual("600001.SH", m.symbol_at("security:example-a", date(2020, 1, 2)))
        self.assertEqual("600009.SH", m.symbol_at("security:example-a", date(2020, 1, 3)))
        self.assertEqual("ticker:000001.SZ", m.security_id("000001.SZ"))

    def test_round_trip_binds_evidence_and_rejects_unknown_fields(self):
        m = identity_map()
        self.assertEqual(m.map_id, IdentityMap.from_document(m.document).map_id)
        doc = m.document
        doc["identities"][0]["evidence_ids"] = ["synthetic-notice:corrected"]
        self.assertNotEqual(m.map_id, IdentityMap.from_document(doc).map_id)
        doc["current_names"] = []
        with self.assertRaisesRegex(ContractError, "SCHEMA_INVALID"):
            IdentityMap.from_document(doc)

    def test_missing_evidence_gaps_and_ambiguous_codes_rejected(self):
        m = identity_map()
        with self.assertRaisesRegex(ContractError, "EVIDENCE_REQUIRED"):
            SecurityIdentity("security:test", m.identities[0].tickers, ())
        with self.assertRaisesRegex(ContractError, "NOT_CONTIGUOUS"):
            SecurityIdentity("security:test", (TickerPeriod("A",None,date(2020,1,2)),
                TickerPeriod("B",date(2020,1,3),None)), ("synthetic",))
        with self.assertRaisesRegex(ContractError, "AMBIGUOUS_TICKER"):
            IdentityMap(m.identities + (SecurityIdentity("security:other",m.identities[0].tickers,("synthetic",)),))

    def test_equivalent_aliases_select_effective_code_without_summing(self):
        m = identity_map()
        rows = (quote("600009.SH",close=10,volume=100), quote("600001.SH",close=10,volume=100))
        before = m.reconcile(date(2020,1,2), rows)
        self.assertEqual(1,len(before))
        self.assertEqual("600001.SH", before[0].selected_symbol)
        self.assertEqual(before,m.reconcile(date(2020,1,2),reversed(rows)))
        self.assertEqual("600009.SH",m.reconcile(date(2020,1,3),rows)[0].selected_symbol)

    def test_backfilled_alias_keeps_historical_ticker_and_source(self):
        d = identity_map().reconcile(date(2017,1,3), (quote("600009.SH",close=10),))[0]
        self.assertEqual(("security:example-a","600001.SH","600009.SH"),
                         (d.security_id,d.trading_symbol,d.selected_symbol))

    def test_conflicts_or_missing_comparison_fields_are_not_arbitrated(self):
        for other in [quote("600009.SH",close=10,amount=101),quote("600009.SH",close=10)]:
            d=identity_map().reconcile(date(2020,1,2),(quote("600001.SH",close=10,amount=100),other))[0]
            self.assertIsNone(d.selected_symbol)
            self.assertEqual(("amount",),d.conflicting_fields)

    def test_equal_prices_do_not_merge_unmapped_securities(self):
        rows = [quote("000001.SZ",close=10),quote("000002.SZ",close=10)]
        self.assertEqual(2,len(identity_map().reconcile(date(2020,1,2),rows)))
        with self.assertRaisesRegex(ContractError,"DUPLICATE_PROVIDER"):
            identity_map().reconcile(date(2020,1,2),(rows[0],rows[0]))

    def test_incomplete_alias_is_not_zero_filled_or_mixed_with_other_rows(self):
        m=identity_map()
        before=m.reconcile(date(2020,1,2),(quote("600001.SH",close=10,volume=0),
                                                quote("600009.SH",close=10,volume=None)))[0]
        self.assertEqual("600001.SH",before.selected_symbol)
        self.assertEqual(("600009.SH",),before.incomplete_symbols)
        neither=m.reconcile(date(2020,1,2),(quote("600001.SH",close=10,volume=None),
                                                 quote("600009.SH",close=None,volume=0)))[0]
        self.assertIsNone(neither.selected_symbol)  # Never splice partial quotes.
        conflict=m.reconcile(date(2020,1,2),(quote("600001.SH",close=10,volume=0),
                                                 quote("600009.SH",close=11,volume=None)))[0]
        self.assertIsNone(conflict.selected_symbol)
        self.assertEqual(("close",),conflict.conflicting_fields)

    def test_halted_partial_backfill_and_empty_retired_stub_preserve_real_quote(self):
        n=ReferenceNormalizer(identity_map(date(2020,1,3)))
        day=date(2020,1,1)
        n.day(day,0,table(day),"a")
        day=date(2020,1,2)
        complete=table(day,trade="0",volume="0",amount="0")
        partial=table(day,trade="0",volume="",amount="",code="sh.600009")
        rows,issues=n.day(day,1,Table(FIELDS,complete.rows+partial.rows,b""),"b")
        self.assertFalse(issues)
        self.assertEqual(0,rows[0][0].volume)
        day=date(2020,1,3)
        stub=list(table(day,trade="0").rows[0])
        for i in range(2,9):stub[i]=""
        rows,issues=n.day(day,2,Table(FIELDS,(tuple(stub),)+table(day,11,10,code="sh.600009").rows,b""),"c")
        self.assertFalse(issues)
        self.assertEqual(("2020-01-01",3),rows[0][3:5])
        self.assertEqual(("600001.SH",),n.identity_decisions[0].incomplete_symbols)
        # A partial record with observed volume is not an empty retirement stub.
        day=date(2020,1,4)
        stub[0]=str(day);stub[7]="999"
        rows,issues=n.day(day,3,Table(FIELDS,(tuple(stub),)+table(day,12,11,code="sh.600009").rows,b""),"d")
        self.assertFalse(rows)
        self.assertIn("volume",issues[0][1])

    def test_renaming_preserves_nonunit_factor_and_streak(self):
        n=ReferenceNormalizer(identity_map(date(2020,1,4)))
        n.day(date(2020,1,1),0,table(date(2020,1,1)),"a")
        n.day(date(2020,1,2),1,table(date(2020,1,2),9,9),"b")
        day=date(2020,1,3)
        raw=table(day,9.9,9)
        alias=table(day,9.9,9,code="sh.600009")
        before,issues=n.day(day,2,Table(FIELDS,raw.rows+alias.rows,b""),"c")
        self.assertFalse(issues)
        self.assertEqual(1,len(before))
        self.assertEqual(100,before[0][0].volume)
        after,issues=n.day(date(2020,1,4),3,table(date(2020,1,4),10.8,9.9,code="sh.600009"),"d")
        self.assertFalse(issues)
        self.assertEqual("security:example-a",after[0][0].symbol)
        self.assertAlmostEqual(12,after[0][0].return_close)
        self.assertEqual(("2020-01-01",4),after[0][3:5])

    def test_conflicting_alias_breaks_chain_and_preserves_conflict_fields(self):
        n=ReferenceNormalizer(identity_map())
        day=date(2020,1,1)
        n.day(day,0,table(day),"a")
        day=date(2020,1,2)
        raw=table(day); alias=table(day,amount="1001",code="sh.600009")
        values,issues=n.day(day,1,Table(FIELDS,raw.rows+alias.rows,b""),"b")
        self.assertFalse(values)
        self.assertEqual([("security:example-a","IDENTITY_QUOTE_CONFLICT:amount")],issues)
        self.assertEqual(("amount",),n.identity_decisions[0].conflicting_fields)
        values,_=n.day(date(2020,1,3),2,table(date(2020,1,3),code="sh.600009"),"c")
        self.assertEqual((1.0,"2020-01-03",1,False),values[0][2:6])


if __name__ == "__main__":
    unittest.main()
