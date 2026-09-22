"""Read-only admission of an evidence-bound frozen interval research bundle.

An arbitrary truthy dictionary is not admission. This checks actual artifacts,
canonical bytes, current preparation rules, and the exact requested view.
Economic source review is still a necessary upstream responsibility.
"""
import hashlib
import json
from pathlib import Path
import sqlite3

from quant.artifacts.store import _private_root, read_run
from quant.contracts import TARGET_ID, evaluation_cutoff, fingerprint, primitive, require
from quant.evaluation.rank_bounds import METHOD

CHECKS = ('calendar_and_boundary','stable_security_identity_and_valid_intervals',
          'as_of_historical_universe','raw_vs_adjusted_reference_separation',
          'economic_endpoint_classification','label_available_at_and_information_sets',
          'full_common_primary_calendar','deterministic_rebuild_and_replay')
FILES = {'acceptance.json','source.json','rules.json','coverage.json','replay.json','label-protocol.json'}


def file_digest(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def preparation_code_hashes():
    root=Path(__file__).resolve().parents[1]
    paths=[root/'contracts.py',root/'models/views.py',root/'pipeline.py',
           root/'real_baseline.py',root/'real_xgboost.py',root/'models/baseline/xgboost_model.py',
           root/'artifacts/store.py']
    for directory in ('data','labels','features','splits','evaluation'):
        paths.extend((root/directory).glob('*.py'))
    return {str(p.relative_to(root)):file_digest(p) for p in sorted(set(paths))}


def frozen_data_id(documents):
    source=documents['source.json']
    return fingerprint({'source_snapshot_id':source['source_snapshot_id'],
        'canonical_sha256':source['sha256'],'rules':documents['rules.json'],
        'coverage':documents['coverage.json'],'protocol':documents['label-protocol.json']})


def _load_frozen_review(descriptor, store_path, coverage_schema):
    require(isinstance(descriptor,dict) and set(descriptor)=={'bundle_root','coverage_view','frozen_dataset_id'},
            'PHASE_A_FROZEN_BUNDLE_REQUIRED')
    root=_private_root(descriptor['bundle_root'])
    manifest,docs=read_run(root)
    require(set(docs)==FILES and manifest['data_kind']=='REAL_DATA', 'PHASE_A_BUNDLE_CONTENT_INVALID')
    require(manifest.get('data_audit_status')=='COMPLETED','PHASE_A_MANIFEST_NOT_ACCEPTED')
    required_versions={'acceptance.json':'quant.phase-a-acceptance/v1','source.json':'quant.canonical-source/v1',
        'rules.json':'quant.interval-data-rules/v1','coverage.json':coverage_schema,
        'replay.json':'quant.interval-replay/v1','label-protocol.json':'quant.partial-identification-protocol/v1'}
    require(all(docs[k].get('schema_version')==v for k,v in required_versions.items()),'PHASE_A_BUNDLE_VERSION_INVALID')
    source,rules,acceptance,protocol=(docs[n] for n in ('source.json','rules.json','acceptance.json','label-protocol.json'))
    identity=frozen_data_id(docs)
    require(identity==descriptor['frozen_dataset_id']==manifest['dataset_id'],'PHASE_A_FROZEN_ID_MISMATCH')
    canonical=_private_root(root/source['path'])
    require(canonical==Path(store_path).resolve() and file_digest(canonical)==source['sha256'], 'PHASE_A_CANONICAL_CONTENT_MISMATCH')
    connection=sqlite3.connect(canonical.as_uri()+'?mode=ro&immutable=1',uri=True)
    try:
        metadata=json.loads(connection.execute('SELECT document FROM metadata').fetchone()[0])
    finally:
        connection.close()
    require(metadata['source_snapshot_id']==source['source_snapshot_id']
            and metadata['universe_id']==rules['universe_id']
            and metadata['identity_map_id']==rules['identity_map_id'],'PHASE_A_SOURCE_RULE_MISMATCH')
    require(rules['target_id']==TARGET_ID and rules['preparation_code_sha256']==preparation_code_hashes(),
            'PHASE_A_PREPARATION_CODE_MISMATCH')
    require(protocol.get('target_id')==TARGET_ID and protocol.get('rank_ic_method')==METHOD
            and protocol.get('development_cutoff')==primitive(evaluation_cutoff('development'))
            and protocol.get('complete_day_role')=='sensitivity_only_same_predictions_no_retraining',
            'PHASE_A_LABEL_PROTOCOL_MISMATCH')
    require(acceptance.get('status')=='COMPLETED' and acceptance.get('phase_b_allowed') is True
            and acceptance.get('known_input_identity_errors')==[]
            and acceptance.get('repeated_quote_identity_candidates')==[], 'PHASE_A_INTERVAL_ADMISSION_REQUIRED')
    require(set(acceptance['checks'])==set(CHECKS)
            and all(acceptance['checks'][k]=='COMPLETED' for k in CHECKS)
            and set(acceptance['evidence'])==set(CHECKS),'PHASE_A_CHECKS_INCOMPLETE')
    for check in CHECKS:
        evidence=acceptance['evidence'][check]
        path=_private_root(root/evidence['path'])
        require(path.is_file() and file_digest(path)==evidence['sha256'],'PHASE_A_EVIDENCE_CONTENT_MISMATCH:'+check)
        report=json.loads(path.read_text())
        require(report.get('status')=='COMPLETED' and report.get('source_snapshot_id')==source['source_snapshot_id']
                and report.get('checks',{}).get(check)=='COMPLETED','PHASE_A_EVIDENCE_CHECK_INCOMPLETE:'+check)
    replay=docs['replay.json']
    require(replay.get('status')=='COMPLETED' and replay.get('source_snapshot_id')==source['source_snapshot_id']
            and replay.get('rules_id')==fingerprint(rules)
            and replay.get('coverage_id')==fingerprint(docs['coverage.json']), 'PHASE_A_REPLAY_BINDING_MISMATCH')
    view=docs['coverage.json']['views'].get(descriptor['coverage_view'])
    require(view is not None and view['evaluation_phase']=='development', 'PHASE_A_COVERAGE_VIEW_MISMATCH')
    return {'phase_a_accepted':True,'frozen_dataset_id':identity,'target_id':TARGET_ID,
        'source_snapshot_id':source['source_snapshot_id'],
        'known_input_identity_errors':acceptance['known_input_identity_errors'],
        'repeated_quote_identity_candidates':acceptance['repeated_quote_identity_candidates'],
        'coverage':view['items'],'view':view,'bundle_artifact_sha256':manifest['artifacts']}


def load_frozen_panel_review(descriptor, store_path, window):
    review=_load_frozen_review(descriptor,store_path,'quant.interval-coverage/v1')
    require(review['view']['window']==primitive(window),'PHASE_A_COVERAGE_VIEW_MISMATCH')
    # Opening D requires the separately frozen C/D protocol, not just A.
    require(window.test_end<evaluation_cutoff('development').date(),'FINAL_HOLDOUT_NOT_OPEN')
    return review


def load_frozen_study_review(descriptor, store_path, folds):
    """The registered annual development study, without a fake test segment."""
    review=_load_frozen_review(descriptor,store_path,'quant.window-study-coverage/v1')
    require(review['view']['folds']==primitive(folds),'WINDOW_STUDY_FOLDS_CHANGED')
    expected={(name,year) for name in ('rolling_2y','rolling_5y','expanding') for year in range(2018,2024)}
    require(len(folds)==18 and {(f['candidate'],f['evaluation_year']) for f in folds}==expected,
            'WINDOW_STUDY_REQUIRES_REGISTERED_18_FOLDS')
    for fold in folds:
        year=fold['evaluation_year'];name=fold['candidate']
        start=year-2 if name=='rolling_2y' else year-5 if name=='rolling_5y' else 2010
        require(fold['training_score_start']==f'{start}-01-01'
                and fold['fit_cutoff']==f'{year}-01-01T00:00:00+08:00'
                and fold['training_score_end_exclusive']==f'{year}-01-01'
                and fold['evaluation_score_start']==f'{year}-01-01'
                and fold['evaluation_score_end']==f'{year}-12-31'
                and fold['development_label_cutoff_exclusive']=='2024-01-01T00:00:00+08:00',
                'WINDOW_STUDY_DATE_RULE_CHANGED')
    return review
