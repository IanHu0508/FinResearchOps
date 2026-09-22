"""Actual frozen-artifact checks; all source frames are fabricated fixtures."""
from pathlib import Path
import json
import tempfile
import unittest

from quant.contracts import ContractError,ResearchSpec,evaluation_cutoff
from quant.data.freeze import load_frozen_panel_review
from quant.data.market_store import MarketStore,build_store
from quant.labels.forward import build_label_rows
from quant.pipeline import prepare_dataset
from quant.splits.walk_forward import FoldWindow
from quant.tests.freeze_fixtures import synthetic_freeze
from quant.tests import test_market_store as fixtures


class IntervalAdmissionTests(unittest.TestCase):
    def test_boolean_waiver_and_fake_identifier_are_not_admission(self):
        with self.assertRaisesRegex(ContractError,'FROZEN_BUNDLE'):
            load_frozen_panel_review({'phase_a_accepted':True,'frozen_dataset_id':'fabricated'},'unused',None)

    def test_readable_source_rules_coverage_and_evidence_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            raw,path,days=fixtures.MarketStoreTests().make_raw(directory,session_count=180)
            build_store(raw,path)
            window=FoldWindow(days[80],days[110],days[139],days[140],days[149])
            descriptor=synthetic_freeze(path,window)
            review=load_frozen_panel_review(descriptor,path,window)
            self.assertEqual(descriptor['frozen_dataset_id'],review['frozen_dataset_id'])
            proof=path.parent/'synthetic-data-checks.json'
            proof.write_text(proof.read_text()+' ')
            with self.assertRaisesRegex(ContractError,'EVIDENCE_CONTENT'):
                load_frozen_panel_review(descriptor,path,window)

    def test_canonical_bytes_and_manifest_hashes_cannot_be_waived(self):
        with tempfile.TemporaryDirectory() as directory:
            raw,path,days=fixtures.MarketStoreTests().make_raw(directory,session_count=180)
            build_store(raw,path)
            window=FoldWindow(days[80],days[110],days[139],days[140],days[149])
            descriptor=synthetic_freeze(path,window)
            p=Path(descriptor['bundle_root'])/'coverage.json'
            original=p.read_bytes()
            p.write_bytes(original+b' ')
            with self.assertRaisesRegex(ContractError,'ARTIFACT_CONTENT'):
                load_frozen_panel_review(descriptor,path,window)
            p.write_bytes(original)
            with path.open('ab') as stream:stream.write(b'\0')
            with self.assertRaisesRegex(ContractError,'CANONICAL_CONTENT'):
                load_frozen_panel_review(descriptor,path,window)

    def test_label_only_reader_matches_shared_feature_pipeline(self):
        from quant.tests.test_identity import identity_map
        for missing in (False,True):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as directory:
                raw,path,days=fixtures.MarketStoreTests().make_raw(directory,session_count=140,rename_at=90,
                    halt_missing_at=85 if missing else None)
                build_store(raw,path,identity_map=identity_map(days[90]))
                with MarketStore(path) as store:
                    spec=ResearchSpec(store.universe_id)
                    for day in ((days[80],) if missing else (days[80],days[95])):
                        full=store.read_day(day);small=store.read_label_day(day)
                        for cutoff in (evaluation_cutoff('development'),):
                            self.assertEqual(prepare_dataset(full,spec,knowledge_cutoff=cutoff).labels,
                                build_label_rows(small,spec,knowledge_cutoff=cutoff))
                        self.assertFalse(small.bars)
                    with self.assertRaisesRegex(ContractError,'WARMUP'):
                        store.read_label_day(days[70])

    def test_source_bound_cache_cannot_change_features_before_supervised_fit(self):
        from dataclasses import replace
        from unittest.mock import patch
        from quant.pipeline import prepare_window_day
        from quant.real_xgboost import run_xgboost_store
        with tempfile.TemporaryDirectory() as directory:
            raw,path,days=fixtures.MarketStoreTests().make_raw(directory,session_count=180)
            build_store(raw,path)
            window=FoldWindow(days[80],days[110],days[139],days[140],days[149])
            descriptor=synthetic_freeze(path,window)
            def altered(day,spec,source_id):
                with MarketStore(path) as store:
                    dataset=prepare_window_day(store.read_day(day),spec,window)
                row=dataset.panel.rows[0]
                row=replace(row,scalars=(12345.,*row.scalars[1:]))
                return replace(dataset,panel=replace(dataset.panel,rows=(row,*dataset.panel.rows[1:])))
            def no_fit(_model,batches):
                # Admission must reject the first cached block before yielding
                # a supervised training batch to a model.
                next(iter(batches))
                self.fail('Altered cache reached supervised fitting')
            with patch('quant.real_xgboost.XGBoostModel.fit_batches',no_fit):
                with self.assertRaisesRegex(ContractError,'FROZEN_DATASET_CONTENT'):
                    run_xgboost_store(path,path.parent/'out',window,descriptor,dataset_loader=altered)
