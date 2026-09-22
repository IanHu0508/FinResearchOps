from datetime import date, timedelta
import json
import tempfile
import unittest

from quant.contracts import ContractError, fingerprint
from quant.data.baostock import Table, decode_table
from quant.data.acquisition import read_cached, save_response
from quant.data.market_store import MarketStore, ReferenceNormalizer, build_store
from quant.data.quote_choices import ReviewedQuoteChoices
from quant.tests.test_identity import identity_map
from quant.tests.test_market_store import FIELDS, table
from quant.tests import test_market_store


def choice_document(mapping, day, rows, source_id="synthetic-source", conflicts=("amount",)):
    raw = {r[1][3:] + "." + r[1][:2].upper(): dict(zip(FIELDS, r)) for r in rows}
    sid = mapping.security_id(next(iter(raw)))
    return {"schema_version": "quant.reviewed-quote-choices/v1", "identity_map_id": mapping.map_id,
            "row_fields": sorted(FIELDS), "choices": [{
                "session": str(day), "security_id": sid, "source_id": source_id,
                "selected_symbol": mapping.symbol_at(sid, day),
                "expected_row_sha256": {s: fingerprint(r) for s, r in raw.items()},
                "expected_conflicting_fields": list(conflicts), "reason": "SYNTHETIC_REVIEWED_AMOUNT_PRECISION",
                "evidence_ids": ["synthetic-audit:one"]}]}


class QuoteChoiceTests(unittest.TestCase):
    def sample(self):
        day = date(2020,1,2)
        rows = table(day).rows + table(day,code="sh.600009",amount="1001").rows
        mapping = identity_map()
        return day, rows, mapping, choice_document(mapping,day,rows)

    def test_bound_choice_preserves_original_quote_and_carries_audit(self):
        day, rows, mapping, doc = self.sample()
        choices = ReviewedQuoteChoices(doc,mapping)
        normalizer = ReferenceNormalizer(mapping,choices)
        before = tuple(rows)
        values,issues = normalizer.day(day,0,Table(FIELDS,rows,b""),"synthetic-source")
        self.assertFalse(issues)
        self.assertEqual(1000,values[0][0].amount)
        self.assertEqual(before,rows)
        self.assertEqual(["amount"],normalizer.applied_quote_choices[0]["expected_conflicting_fields"])
        self.assertIn(choices.manifest_id,values[0][0].source_id)
        choices.require_all_used()

    def test_changed_source_or_row_fails_instead_of_reusing_review(self):
        day, rows, mapping, doc = self.sample()
        variants=[("source-changed",rows,"QUOTE_CHOICE_SOURCE_CHANGED")]
        modified=list(rows[1]);modified[8]="1002"
        variants.append(("synthetic-source",(rows[0],tuple(modified)),"QUOTE_CHOICE_RAW_ROW_CHANGED"))
        for source,actual,message in variants:
            with self.subTest(message=message), self.assertRaisesRegex(ContractError,message):
                ReferenceNormalizer(mapping,ReviewedQuoteChoices(doc,mapping)).day(day,0,Table(FIELDS,actual,b""),source)

    def test_conflict_fields_and_alias_set_are_bound(self):
        day,rows,mapping,doc=self.sample()
        altered=list(rows[1]);altered[2]=altered[3]=altered[4]=altered[5]="11"
        for actual,message in [(rows[:1],"ALIASES_CHANGED"),((rows[0],tuple(altered)),"CONFLICT_CHANGED")]:
            with self.subTest(message=message), self.assertRaisesRegex(ContractError,message):
                ReferenceNormalizer(mapping,ReviewedQuoteChoices(doc,mapping)).day(day,0,Table(FIELDS,actual,b""),"synthetic-source")

    def test_no_global_tolerance_or_silent_unused_selection(self):
        day,rows,mapping,doc=self.sample()
        with self.assertRaisesRegex(ContractError,"NOT_ALL_APPLIED"):
            ReviewedQuoteChoices(doc,mapping).require_all_used()
        other_day=day-timedelta(days=1)
        other=table(other_day).rows+table(other_day,code="sh.600009",amount="1001").rows
        values,issues=ReferenceNormalizer(mapping,ReviewedQuoteChoices(doc,mapping)).day(other_day,0,Table(FIELDS,other,b""),"synthetic-source")
        self.assertFalse(values)
        self.assertEqual("IDENTITY_QUOTE_CONFLICT:amount",issues[0][1])

    def test_wrong_identity_and_non_effective_selected_alias_rejected(self):
        day,rows,mapping,doc=self.sample()
        bad=json.loads(json.dumps(doc));bad['identity_map_id']='0'*64
        with self.assertRaisesRegex(ContractError,'IDENTITY_MISMATCH'):
            ReviewedQuoteChoices(bad,mapping)
        bad=json.loads(json.dumps(doc));bad['choices'][0]['selected_symbol']='600009.SH'
        with self.assertRaisesRegex(ContractError,'NOT_EFFECTIVE_CODE'):
            ReviewedQuoteChoices(bad,mapping)

    def test_store_binds_choice_metadata_and_applied_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root,destination,sessions=test_market_store.MarketStoreTests().make_raw(directory,rename_at=90,alias_conflict_at=85)
            mapping=identity_map(sessions[90]);name='daily-'+str(sessions[85])
            raw=read_cached(root,name,method='query_daily_history_k_AStock',row_index=4,field_index=5)
            selected=tuple(r for r in raw.rows if r[1] in ('sh.600001','sh.600009'))
            source='baostock:'+json.loads((root/(name+'.json')).read_text())['sha256']
            doc=choice_document(mapping,sessions[85],selected,source)
            metadata=build_store(root,destination,identity_map=mapping,quote_choices_document=doc)
            with MarketStore(destination) as store:
                self.assertEqual(metadata['reviewed_quote_choices_id'],fingerprint(doc))
                self.assertEqual(1,store.connection.execute('SELECT COUNT(*) FROM reviewed_quote_choices').fetchone()[0])
                self.assertEqual(0,store.connection.execute('SELECT COUNT(*) FROM issues').fetchone()[0])
                self.assertEqual(105,store.connection.execute("SELECT MAX(consecutive_sessions) FROM bars WHERE security_id='security:example-a'").fetchone()[0])


if __name__=='__main__':unittest.main()
