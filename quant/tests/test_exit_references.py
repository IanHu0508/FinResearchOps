from dataclasses import replace
from datetime import timedelta
import tempfile
import unittest

from quant.contracts import ContractError, ResearchSpec, market_time
from quant.data.records import ExitReference
from quant.data.serialization import from_document, to_document
from quant.data.market_store import MarketStore, UNIVERSE_ID, build_store
from quant.data.share_conversions import HOLDER_CONVENTION, METHOD_ID
from quant.pipeline import prepare_dataset
from quant.splits.walk_forward import FoldWindow, partition_label
from quant.synthetic import make_synthetic_data
from quant.tests import test_market_store


class ExitReferenceTests(unittest.TestCase):
    def test_exit_only_reference_restores_label_without_changing_features(self):
        data,spec=make_synthetic_data(score_start=80,score_end=80)
        baseline=prepare_dataset(data,spec)
        symbol=baseline.labels[0].key.symbol;end=data.sessions[100]
        last=next(b for b in data.bars if b.symbol==symbol and b.session==end)
        reference=ExitReference(symbol,end,market_time(end,21),last.return_close*0.7,'synthetic-share-conversion',METHOD_ID)
        updated=replace(data,bars=tuple(b for b in data.bars if (b.symbol,b.session)!=(symbol,end)),exit_references=(reference,))
        result=prepare_dataset(updated,spec)
        self.assertEqual(baseline.panel,result.panel)
        self.assertTrue(all(y.complete for y in result.labels))
        self.assertAlmostEqual((1+baseline.labels[0].raw_return)*0.7-1,result.labels[0].raw_return)
        self.assertEqual(updated,from_document(to_document(updated)))
        old=to_document(updated);old['schema_version']='quant.research-input/v2'
        with self.assertRaisesRegex(ContractError,'INPUT_SCHEMA_INVALID'):from_document(old)

    def test_missing_entry_is_not_created_by_exit_reference(self):
        data,spec=make_synthetic_data(score_start=80,score_end=80)
        symbol=data.universes[-1].symbols[0];entry,end=data.sessions[81],data.sessions[100]
        reference=ExitReference(symbol,end,market_time(end,21),20,'synthetic-conversion',METHOD_ID)
        changed=replace(data,bars=tuple(b for b in data.bars if b.symbol!=symbol or b.session not in (entry,end)),exit_references=(reference,))
        result=prepare_dataset(changed,spec)
        self.assertTrue(all(not y.complete for y in result.labels))

    def test_later_confirmation_controls_purge_and_observed_bar_cannot_be_overridden(self):
        data,spec=make_synthetic_data(score_start=80,score_end=80)
        symbol=data.universes[-1].symbols[0];end=data.sessions[100]
        ref=ExitReference(symbol,end,market_time(data.sessions[103],21),20,'synthetic-confirmation',METHOD_ID)
        with self.assertRaisesRegex(ContractError,'CANNOT_OVERRIDE_BAR'):replace(data,exit_references=(ref,))
        changed=replace(data,bars=tuple(b for b in data.bars if (b.symbol,b.session)!=(symbol,end)),exit_references=(ref,))
        result=prepare_dataset(changed,spec)
        window=FoldWindow(data.sessions[80],data.sessions[101],data.sessions[110],data.sessions[111],data.sessions[120])
        from quant.labels.forward import labels_at_cutoff
        view=labels_at_cutoff(result,market_time(window.validation_start,0))
        self.assertEqual(5,sum(y.supervised for y in view.labels))
        self.assertEqual({('train','OUTCOME_UNAVAILABLE'),('train','AVAILABLE')},
                         {partition_label(y,window) for y in view.labels})

    def test_verified_conversion_store_does_not_merge_universes_or_fabricate_bars(self):
        with tempfile.TemporaryDirectory() as directory:
            raw,path,sessions=test_market_store.MarketStoreTests().make_raw(directory,rename_at=90)
            doc={'schema_version':'quant.verified-share-conversions/v1','holder_convention':HOLDER_CONVENTION,'events':[{
                'old_symbol':'600001.SH','successor_symbol':'600009.SH','effective_date':str(sessions[90]),
                'first_reference_date':str(sessions[90]),'last_reference_date':str(sessions[104]),
                'old_anchor_date':str(sessions[89]),'successor_anchor_date':str(sessions[90]),
                'ratio':'0.5','confirmation_date':str(sessions[92]),'evidence_ids':['synthetic-share-conversion-notice']}]}
            build_store(raw,path,share_conversions_document=doc)
            with MarketStore(path) as store:
                data=store.read_day(sessions[80]);result=prepare_dataset(data,ResearchSpec(UNIVERSE_ID))
                self.assertEqual(3,len(result.labels))
                self.assertTrue(all(y.complete for y in result.labels))
                y=next(y for y in result.labels if y.key.symbol=='600001.SH')
                self.assertAlmostEqual(0.5*11/10.81-1,y.raw_return)
                self.assertEqual(0,store.connection.execute("SELECT COUNT(*) FROM bars WHERE security_id='ticker:600001.SH' AND session>=?",(str(sessions[90]),)).fetchone()[0])
                self.assertEqual(15,store.connection.execute('SELECT COUNT(*) FROM exit_references').fetchone()[0])
                self.assertEqual(90,store.connection.execute("SELECT COUNT(*) FROM bars WHERE security_id='ticker:600001.SH'").fetchone()[0])


if __name__=='__main__':unittest.main()
