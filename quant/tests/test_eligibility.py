from datetime import date, timedelta
from copy import deepcopy
import json
import tempfile
import unittest

from quant.contracts import ContractError, ResearchSpec, session_of
from quant.data.eligibility import LiquidationStageExclusions, UNIVERSE_ID
from quant.data.identity import IdentityMap
from quant.data.market_store import MarketStore, build_store
from quant.pipeline import prepare_dataset
from quant.tests import test_market_store


def document(sessions, *, published=70, effective=85):
    return {"schema_version": "quant.liquidation-stage-exclusions/v1",
            "coverage_start": str(sessions[0]), "coverage_end": str(sessions[-1]),
            "evidence_ids": ["synthetic-complete-catalog"], "events": [{
                "symbol": "600001.SH", "published_date": str(sessions[published]),
                "effective_date": str(sessions[effective]), "evidence_ids": ["synthetic-phase-notice"]}]}


class EligibilityTests(unittest.TestCase):
    def test_effectiveness_and_publication_both_gate_exclusion(self):
        days = [date(2020,1,1)+timedelta(days=i) for i in range(105)]
        rules = LiquidationStageExclusions(document(days), IdentityMap())
        self.assertFalse(rules.excludes("ticker:600001.SH", days[84]))
        self.assertTrue(rules.excludes("ticker:600001.SH", days[85]))
        self.assertFalse(rules.excludes("ticker:000001.SZ", days[90]))
        later = LiquidationStageExclusions(document(days, published=95), IdentityMap())
        self.assertFalse(later.excludes("ticker:600001.SH", days[95]))
        self.assertTrue(later.excludes("ticker:600001.SH", days[96]))
        with self.assertRaisesRegex(ContractError, "COVERAGE_MISSING"):
            rules.excludes("ticker:600001.SH", days[-1]+timedelta(days=1))

    def test_missing_evidence_duplicate_security_and_incomplete_coverage_fail(self):
        days = [date(2020,1,1)+timedelta(days=i) for i in range(105)]
        bad = document(days);bad['events'][0]['evidence_ids']=[]
        with self.assertRaisesRegex(ContractError, "EVIDENCE_REQUIRED"):
            LiquidationStageExclusions(bad, IdentityMap())
        bad = document(days);bad['events']*=2
        with self.assertRaisesRegex(ContractError, "DUPLICATE"):
            LiquidationStageExclusions(bad, IdentityMap())
        with tempfile.TemporaryDirectory() as directory:
            raw,path,days=test_market_store.MarketStoreTests().make_raw(directory)
            bad=document(days);bad['coverage_end']=str(days[-2])
            with self.assertRaisesRegex(ContractError, "COVERAGE_MISSING"):
                build_store(raw,path,liquidation_exclusions_document=bad)
            self.assertFalse(path.exists())

    def test_prior_holdings_keep_outcomes_and_historical_market_pools_change_as_of(self):
        with tempfile.TemporaryDirectory() as directory:
            raw,path,days=test_market_store.MarketStoreTests().make_raw(directory,rename_at=200,session_count=130)
            manifest=document(days)
            build_store(raw,path,liquidation_exclusions_document=manifest)
            with MarketStore(path) as store:
                self.assertEqual(UNIVERSE_ID,store.universe_id)
                early=prepare_dataset(store.read_day(days[80]),ResearchSpec(store.universe_id))
                self.assertEqual(3,len(early.labels))
                self.assertTrue(all(y.complete for y in early.labels))
                self.assertIn('600001.SH',{y.key.symbol for y in early.labels})
                later=store.read_day(days[90])
                self.assertEqual(2,len(later.universes[-1].symbols))
                self.assertTrue(any(b.symbol=='600001.SH' and b.session==days[100] for b in later.bars))
                for pool in later.universes:
                    self.assertEqual(session_of(pool.as_of)<days[85], '600001.SH' in pool.symbols)
                self.assertEqual(130,store.connection.execute("SELECT COUNT(*) FROM bars WHERE security_id='ticker:600001.SH'").fetchone()[0])
                self.assertEqual(45,store.connection.execute('SELECT COUNT(*) FROM eligibility_exclusions').fetchone()[0])
                self.assertTrue(all(':eligibility:' in u.source_id for u in later.universes))
                snapshot=store.metadata['source_snapshot_id']
            # Manifest changes bind the dataset even if another notice would
            # happen to remove the same members in this selected score block.
            other=deepcopy(manifest);other['evidence_ids']=['different-synthetic-catalog']
            second=path.with_name('other.sqlite');build_store(raw,second,liquidation_exclusions_document=other)
            with MarketStore(second) as store:self.assertNotEqual(snapshot,store.metadata['source_snapshot_id'])


if __name__ == '__main__':unittest.main()
