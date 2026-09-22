from datetime import date, timedelta
import json
import tempfile
import unittest

from quant.contracts import ContractError, ResearchSpec, fingerprint
from quant.data.acquisition import read_cached
from quant.data.baostock import Table
from quant.data.market_store import MarketStore, ReferenceNormalizer, UNIVERSE_ID, build_store
from quant.data.suspension_repairs import ReviewedSuspensionZeros
from quant.pipeline import prepare_dataset
from quant.tests import test_market_store
from quant.tests.test_market_store import FIELDS, table


def document(day, row, source='synthetic-source'):
    raw=dict(zip(FIELDS,row))
    return {'schema_version':'quant.reviewed-suspension-zeros/v1','row_fields':sorted(FIELDS),
            'repairs':[{'session':str(day),'symbol':'600001.SH','source_id':source,
                'original_row_sha256':fingerprint(raw),'announcement_date':str(day-timedelta(days=1)),
                'evidence_ids':['synthetic-full-day-suspension-notice'],
                'reason':'VERIFIED_FULL_DAY_SUSPENSION_ZERO_TRADES'}]}


class SuspensionRepairTests(unittest.TestCase):
    def test_missing_activity_recovered_without_changing_reference_prices(self):
        day=date(2020,1,2);raw=table(day,trade='0',volume='',amount='')
        repairs=ReviewedSuspensionZeros(document(day,raw.rows[0]))
        n=ReferenceNormalizer(suspension_repairs=repairs)
        n.day(day-timedelta(days=1),0,table(day-timedelta(days=1)),'prior')
        rows,issues=n.day(day,1,raw,'synthetic-source')
        self.assertFalse(issues)
        self.assertEqual((10,10,0,0,False),(rows[0][0].open,rows[0][0].close,rows[0][0].volume,rows[0][0].amount,rows[0][0].tradable))
        self.assertEqual(('2020-01-01',2),rows[0][3:5])
        self.assertEqual('',raw.rows[0][7])
        self.assertEqual({'volume':'','amount':''},n.applied_suspension_repairs[0]['original_fields'])
        repairs.require_all_used()

    def test_positive_stale_volume_can_be_corrected_only_for_reviewed_day(self):
        day=date(2020,1,2);raw=table(day,trade='0',volume='100',amount='0')
        n=ReferenceNormalizer(suspension_repairs=ReviewedSuspensionZeros(document(day,raw.rows[0])))
        rows,issues=n.day(day,0,raw,'synthetic-source')
        self.assertFalse(issues);self.assertEqual(0,rows[0][0].volume)
        other=day+timedelta(days=1)
        rows,issues=n.day(other,1,table(other,trade='0',volume='100',amount='0'),'other')
        self.assertFalse(rows);self.assertEqual('VOLUME_AMOUNT_ZERO_MISMATCH',issues[0][1])

    def test_no_halted_reference_or_positive_amount_cannot_be_reconstructed(self):
        day=date(2020,1,2)
        for raw in [table(day,trade='1',volume='',amount=''),table(day,trade='0',volume='100',amount='1000'),
                    table(day,11,10,trade='0',volume='',amount='')]:
            with self.subTest(raw=raw.rows),self.assertRaises(ContractError):
                ReferenceNormalizer(suspension_repairs=ReviewedSuspensionZeros(document(day,raw.rows[0]))).day(day,0,raw,'synthetic-source')

    def test_changed_source_row_and_late_evidence_fail_closed(self):
        day=date(2020,1,2);raw=table(day,trade='0',volume='',amount='');doc=document(day,raw.rows[0])
        for source,actual,code in [('changed',raw,'SOURCE_CHANGED'),('synthetic-source',table(day,11,11,trade='0',volume='',amount=''),'RAW_ROW_CHANGED')]:
            with self.subTest(code=code),self.assertRaisesRegex(ContractError,code):
                ReferenceNormalizer(suspension_repairs=ReviewedSuspensionZeros(doc)).day(day,0,actual,source)
        doc['repairs'][0]['announcement_date']=str(day+timedelta(days=1))
        with self.assertRaisesRegex(ContractError,'EVIDENCE_AFTER_SESSION'):
            ReviewedSuspensionZeros(doc)

    def test_unconsumed_evidence_does_not_claim_a_completed_repair(self):
        day=date(2020,1,2);raw=table(day,trade='0',volume='',amount='')
        with self.assertRaisesRegex(ContractError,'NOT_ALL_APPLIED'):
            ReviewedSuspensionZeros(document(day,raw.rows[0])).require_all_used()

    def test_store_recovery_restores_forward_label_and_records_original_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root,path,sessions=test_market_store.MarketStoreTests().make_raw(directory,halt_missing_at=85)
            name='daily-'+str(sessions[85]);raw=read_cached(root,name,method='query_daily_history_k_AStock',row_index=4,field_index=5)
            source='baostock:'+json.loads((root/(name+'.json')).read_text())['sha256']
            repair=document(sessions[85],raw.rows[0],source)
            build_store(root,path,suspension_repairs_document=repair)
            with MarketStore(path) as store:
                d=prepare_dataset(store.read_day(sessions[80]),ResearchSpec(UNIVERSE_ID))
                self.assertTrue(all(y.complete for y in d.labels))
                self.assertEqual(2,len(d.labels))
                receipt=json.loads(store.connection.execute('SELECT document FROM suspension_repairs').fetchone()[0])
                self.assertEqual({'amount':'','volume':''},receipt['original_fields'])
                self.assertTrue(receipt['price_fields_unchanged'])
                self.assertEqual(0,store.connection.execute('SELECT COUNT(*) FROM issues').fetchone()[0])


if __name__=='__main__':unittest.main()
