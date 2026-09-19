"""The shared preparation and experiment Interface, independent of agents."""

from quant.artifacts import write_run
from quant.contracts import fingerprint, primitive, require
from quant.data.serialization import to_document
from quant.evaluation import evaluate_predictions
from quant.features import build_panel
from quant.inference import build_signals
from quant.labels import build_labels
from quant.splits import prepare_fold


def prepare_dataset(data, spec):
    return build_labels(data, build_panel(data, spec))


def run_experiment(data, spec, window, model, *, artifact_root=None):
    dataset = prepare_dataset(data, spec)
    fold = prepare_fold(dataset, window)
    fitted = model.fit(fold.train)
    validation_predictions = fitted.predict(fold.validation.rows)
    test_predictions = fitted.predict(fold.test.rows)
    validation = evaluate_predictions(validation_predictions, fold.validation)
    test = evaluate_predictions(test_predictions, fold.test)
    as_ofs = tuple(sorted({row.key.as_of for row in fold.test.rows}))
    signals = build_signals(test_predictions, dataset.panel, as_ofs=as_ofs, model=fitted,
                            dataset_id=dataset.dataset_id)
    result = {"schema_version": "quant.experiment/v2", "data_kind": data.data_kind,
        "dataset_id": dataset.dataset_id, "source_snapshot_id": fingerprint(data),
        "spec": primitive(spec), "model_version": fitted.model_version,
        "feature_ablation": fitted.ablation, "input_view": fitted.view,
        "fold": primitive(window), "counts": {"panel": len(dataset.panel.rows),
            "train": len(fold.train.rows), "validation": len(fold.validation.rows),
            "test": len(fold.test.rows), "purged_train": len(fold.purged_train),
            "purged_validation": len(fold.purged_validation),
            "unavailable_labels": len(fold.unavailable_labels)},
        "validation": validation, "test": test,
        "portfolio": {"status": "DEFERRED", "reason": "EXECUTION_RULES_NOT_IMPLEMENTED"},
        "claims": {"mechanical_pipeline": "COMPLETED", "real_data_audit": "NOT_STARTED",
                   "investment_effectiveness": "NOT_STARTED"},
        "signals": list(signals)}
    if artifact_root is not None:
        manifest = write_run(artifact_root, {
            "input.json": to_document(data),
            "dataset.json": {"schema_version": "quant.prepared-dataset/v2", **primitive(dataset)},
            "split.json": {"schema_version": "quant.prepared-fold/v2", **primitive(fold)},
            "model.json": fitted.artifact, "result.json": result,
        }, data_kind=data.data_kind, dataset_id=dataset.dataset_id)
        require(manifest["dataset_id"] == result["dataset_id"], "PERSISTED_DATASET_MISMATCH")
    return result
