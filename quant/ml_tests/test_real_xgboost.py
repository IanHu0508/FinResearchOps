from datetime import date
import json
import tempfile
import unittest

from quant.contracts import ContractError, ResearchSpec, TARGET_ID, fingerprint, primitive
from quant.data.market_store import MarketStore, UNIVERSE_ID, build_store
from quant.pipeline import prepare_dataset, prepare_window_day
from quant.real_baseline import evaluate_store
from quant.real_xgboost import run_xgboost_store
from quant.splits.walk_forward import FoldWindow, day_partition
from quant.tests import test_market_store


def review(store_path, window):
    from quant.tests.freeze_fixtures import synthetic_freeze
    return synthetic_freeze(store_path,window)


class RealXGBoostRunnerTests(unittest.TestCase):
    def test_synthetic_store_uses_shared_split_metrics_and_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            raw,path,sessions=test_market_store.MarketStoreTests().make_raw(directory,session_count=180)
            build_store(raw,path)
            window=FoldWindow(sessions[80],sessions[110],sessions[139],sessions[140],sessions[149])
            checked=review(path,window)
            factors=evaluate_store(path,raw.parent/'factors',window,frozen_review=checked)
            result=run_xgboost_store(path,raw.parent/'model-run',window,checked,factor_result=factors)
            self.assertEqual(20,result['training_rows'])
            self.assertEqual(10,result['training_dates'])
            self.assertEqual(10,result['metrics']['test']['days'])
            self.assertEqual(10,result['metrics']['validation']['days'])
            self.assertEqual(20,len(result['daily_artifact_sha256']))
            self.assertEqual(result,json.loads((raw.parent/'model-run/result.json').read_text()))
            self.assertLess(date.fromisoformat(result['training_cutoff'][:10]),window.validation_start)
            self.assertNotIn('sharpe',result)
            with MarketStore(path) as store:
                def cached(day,spec,source):
                    self.assertEqual(store.metadata['source_snapshot_id'],source)
                    return prepare_window_day(store.read_day(day),spec,window)
                reused=run_xgboost_store(path,raw.parent/'cached-run',window,checked,
                                         factor_result=factors,dataset_loader=cached)
                self.assertEqual(result['model_version'],reused['model_version'])
                self.assertEqual(result['daily_artifact_sha256'],reused['daily_artifact_sha256'])
                wrong=prepare_dataset(store.read_day(sessions[81]),ResearchSpec(UNIVERSE_ID))
                with self.assertRaisesRegex(ContractError,'CACHED_DATASET_SCOPE_MISMATCH'):
                    run_xgboost_store(path,raw.parent/'wrong-day-run',window,checked,
                                      dataset_loader=lambda *_:wrong)

    def test_partial_label_policy_does_not_bypass_other_phase_a_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            raw,path,sessions=test_market_store.MarketStoreTests().make_raw(directory,gap=True,session_count=180)
            build_store(raw,path)
            # Score before the gap, while both securities still qualify; the
            # missing future chain is what invalidates the labels being tested.
            window=FoldWindow(sessions[80],sessions[82],sessions[83],sessions[84],sessions[85])
            checked=review(path,window)
            checked={'phase_a_accepted':True,'frozen_dataset_id':'fabricated-boolean-waiver'}
            with self.assertRaisesRegex(ContractError,'PHASE_A_FROZEN_BUNDLE'):
                run_xgboost_store(path,raw.parent/'not-trained',window,checked)
            self.assertFalse((raw.parent/'not-trained').exists())


if __name__=='__main__':unittest.main()
