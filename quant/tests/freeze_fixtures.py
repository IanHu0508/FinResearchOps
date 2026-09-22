"""Fabricated, tiny research evidence bundles for admission-negative tests only."""
import json
from pathlib import Path

from quant.artifacts.store import write_run
from quant.contracts import TARGET_ID, evaluation_cutoff, fingerprint, primitive
from quant.data.freeze import CHECKS, file_digest, frozen_data_id, preparation_code_hashes
from quant.data.market_store import MarketStore
from quant.evaluation.rank_bounds import METHOD
from quant.pipeline import prepare_window_day
from quant.splits.walk_forward import day_partition


def synthetic_freeze(store_path, window):
    root=Path(store_path).parent/'synthetic-freeze'
    coverage=[]
    with MarketStore(store_path) as store:
        metadata=store.metadata
        from quant.contracts import ResearchSpec
        for day in store.sessions:
            if not window.train_start<=day<=window.test_end:continue
            dataset=prepare_window_day(store.read_day(day),ResearchSpec(store.universe_id),window)
            kind,status=day_partition(dataset.labels,window)
            coverage.append({'date':str(day),'split':kind,'disposition':status,
                'dataset_id':dataset.dataset_id,
                'original_pool_count':len(dataset.labels),'observed_count':dataset.labels[0].observed_count,
                'knowledge_cutoff':primitive(dataset.labels[0].knowledge_cutoff)})
    proof=Path(store_path).parent/'synthetic-data-checks.json'
    proof.write_text(json.dumps({'status':'COMPLETED','source_snapshot_id':metadata['source_snapshot_id'],
        'checks':dict.fromkeys(CHECKS,'COMPLETED'),'synthetic_fixture_only_not_financial_evidence':True}))
    rules={'schema_version':'quant.interval-data-rules/v1','target_id':TARGET_ID,
        'universe_id':metadata['universe_id'],'identity_map_id':metadata['identity_map_id'],
        'preparation_code_sha256':preparation_code_hashes()}
    cover={'schema_version':'quant.interval-coverage/v1','views':{'fixture':{
        'window':primitive(window),'evaluation_phase':'development','items':coverage}}}
    docs={'source.json':{'schema_version':'quant.canonical-source/v1','path':str(Path(store_path).resolve()),
              'source_snapshot_id':metadata['source_snapshot_id'],'sha256':file_digest(store_path)},
        'rules.json':rules,'coverage.json':cover,
        'label-protocol.json':{'schema_version':'quant.partial-identification-protocol/v1','target_id':TARGET_ID,
             'rank_ic_method':METHOD,'development_cutoff':primitive(evaluation_cutoff('development')),
             'complete_day_role':'sensitivity_only_same_predictions_no_retraining'},
        'acceptance.json':{'schema_version':'quant.phase-a-acceptance/v1','status':'COMPLETED','phase_b_allowed':True,
             'known_input_identity_errors':[],'repeated_quote_identity_candidates':[],
             'checks':dict.fromkeys(CHECKS,'COMPLETED'),
             'evidence':{k:{'path':str(proof.resolve()),'sha256':file_digest(proof)} for k in CHECKS}},
        'replay.json':{'schema_version':'quant.interval-replay/v1','status':'COMPLETED',
             'source_snapshot_id':metadata['source_snapshot_id'],'rules_id':fingerprint(rules),'coverage_id':fingerprint(cover)}}
    identity=frozen_data_id(docs)
    # Emulate a real-input admission wrapper around fabricated test wire only.
    # This helper must never be used for an actual market-data acceptance.
    write_run(root,docs,data_kind='REAL_DATA',dataset_id=identity,data_audit_status='COMPLETED')
    return {'bundle_root':str(root),'coverage_view':'fixture','frozen_dataset_id':identity}
