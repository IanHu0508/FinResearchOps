"""Bounded-memory, fixed-config XGBoost experiment after explicit data review."""

import argparse
from datetime import date
import gzip
import hashlib
import json

from quant.artifacts.store import _private_root
from quant.contracts import TARGET_ID, ResearchSpec, canonical, market_time, primitive, require
from quant.data.market_store import MarketStore
from quant.data.freeze import load_frozen_panel_review
from quant.evaluation.prediction import evaluate_predictions, summarize_daily
from quant.models.baseline.xgboost_model import XGBoostModel, restore_xgboost_model
from quant.models.views import vector_names, vectors
from quant.pipeline import prepare_window_day
from quant.labels.forward import labels_at_cutoff
from quant.splits.walk_forward import EvaluationBatch, FoldWindow, day_partition, training_batch


def run_xgboost_store(store_path, output_root, window, panel_review, *,
                      factor_result=None, dataset_loader=None):
    """Consume an accepted source-bound interval review, not a boolean waiver.

    No source selection, source repair, model search or early stopping occurs
    here. Unknown full-date outcomes are never converted to survivor ranks.
    """
    output_root = _private_root(output_root)
    require(not output_root.exists(), "XGBOOST_OUTPUT_ALREADY_EXISTS")
    panel_review=load_frozen_panel_review(panel_review,store_path,window)
    with MarketStore(store_path) as store:
        require(panel_review.get('phase_a_accepted') is True and panel_review.get('frozen_dataset_id')
                and panel_review.get('target_id') == TARGET_ID, 'PHASE_A_INTERVAL_ADMISSION_REQUIRED')
        require(panel_review["source_snapshot_id"] == store.metadata["source_snapshot_id"], "XGBOOST_REVIEW_SOURCE_MISMATCH")
        require(not panel_review["known_input_identity_errors"]
                and not panel_review["repeated_quote_identity_candidates"], "XGBOOST_IDENTITY_REVIEW_INCOMPLETE")
        expected_dates = [str(d) for d in store.sessions if window.train_start <= d <= window.test_end]
        coverage = panel_review["coverage"]
        require([r["date"] for r in coverage] == expected_dates, "XGBOOST_REVIEW_DATES_MISMATCH")
        unavailable = [r for r in coverage if r["disposition"] == "OUTCOME_UNAVAILABLE"]
        require(not unavailable, 'HORIZON_COVERAGE_INCOMPLETE')
        selected = {kind: [r for r in coverage if r["split"] == kind and r["disposition"] == "AVAILABLE"]
                    for kind in ("train", "validation", "test")}
        require(all(selected.values()), "XGBOOST_EMPTY_REVIEWED_SPLIT")
        spec = ResearchSpec(store.universe_id)
        if factor_result is not None:
            require(factor_result["source_snapshot_id"] == store.metadata["source_snapshot_id"]
                    and factor_result["window"] == primitive(window) and factor_result["spec"] == primitive(spec),
                    "XGBOOST_FACTOR_COMPARISON_MISMATCH")
        output_root.mkdir(parents=True, exist_ok=False)
        (output_root / "daily").mkdir()
        definition = {"schema_version": "quant.xgboost-experiment/v2", "source_snapshot_id": store.metadata["source_snapshot_id"],
                      "phase_a_frozen_dataset_id":panel_review['frozen_dataset_id'],
                      "phase_a_bundle_artifact_sha256":panel_review['bundle_artifact_sha256'],
                      "spec": primitive(spec), "window": primitive(window), "view": "tabular", "ablation": "stock-only",
                      "evaluation_scope": "FULL_ORIGINAL_UNIVERSE_PARTIAL_IDENTIFICATION",
                      "complete_day_role":"SENSITIVITY_ONLY_SAME_PREDICTIONS", "unavailable_date_count": len(unavailable),
                      "tuning": "NONE_FIXED_CONFIG_NO_EARLY_STOPPING", "coverage": coverage}
        (output_root / "definition.json").write_text(canonical(definition) + "\n")

        def prepared(item):
            day = date.fromisoformat(item["date"])
            # An explicit caller may reuse previously prepared, source-bound
            # date blocks. The normal CLI still builds through prepare_dataset.
            dataset = (prepare_window_day(store.read_day(day), spec,window) if dataset_loader is None
                       else dataset_loader(day, spec, store.metadata["source_snapshot_id"]))
            require(dataset.panel.spec == spec and dataset.panel.data_kind == "REAL_DATA"
                    and all(r.key.as_of.date() == day for r in dataset.panel.rows),
                    "XGBOOST_CACHED_DATASET_SCOPE_MISMATCH")
            if item['split']!='test':
                cutoff=market_time(window.validation_start if item['split']=='train' else window.test_start,0)
                dataset=labels_at_cutoff(dataset,cutoff)
            require(len(dataset.labels) == item["original_pool_count"]
                    and dataset.labels[0].observed_count==item['observed_count']
                    and primitive(dataset.labels[0].knowledge_cutoff)==item['knowledge_cutoff']
                    and day_partition(dataset.labels, window) == (item["split"], "AVAILABLE"),
                    "XGBOOST_PREPARATION_REVIEW_MISMATCH")
            require(dataset.dataset_id == item.get('dataset_id'),
                    'XGBOOST_FROZEN_DATASET_CONTENT_MISMATCH')
            return dataset

        def train_batches():
            with (output_root / "training-inputs.jsonl").open("x") as journal:
                for index, item in enumerate(selected["train"]):
                    dataset = prepared(item)
                    count = len(dataset.labels)
                    dataset_id = dataset.dataset_id
                    journal.write(canonical({"date": item["date"], "count": count,
                        "supervised_count":dataset.labels[0].observed_count,"dataset_id": dataset_id}) + "\n")
                    journal.flush()
                    if dataset.labels[0].observed_count:
                        yield training_batch(dataset,window.validation_start)
                    if index % 20 == 0:
                        print(canonical({"training_dates_prepared": index + 1, "total": len(selected["train"])}), flush=True)

        fitted = XGBoostModel().fit_batches(train_batches())
        (output_root / "model.json").write_text(canonical(fitted.artifact) + "\n")
        restored = restore_xgboost_model(json.loads((output_root / "model.json").read_text()))
        metrics, hashes, clipping = {}, {}, {}
        first_prediction = True
        for kind in ("validation", "test"):
            daily = []
            clip_count = 0
            for index, item in enumerate(selected[kind]):
                dataset = prepared(item)
                rows = dataset.panel.rows
                predictions, raw = restored.predict_vectors(tuple(r.key for r in rows), vectors(rows, "tabular", "stock-only"),
                                                            feature_names=vector_names("tabular", "stock-only"))
                if first_prediction:
                    require(predictions == fitted.predict(rows), "XGBOOST_MODEL_REPLAY_MISMATCH")
                    first_prediction = False
                metric = evaluate_predictions(predictions, EvaluationBatch(rows, dataset.labels))["daily"][0]
                daily.append(metric)
                clipped = sum(not 0 <= s <= 1 for s in raw)
                clip_count += clipped
                document = {"schema_version": "quant.xgboost-day/v2", "date": item["date"], "split": kind,
                            "knowledge_cutoff":primitive(dataset.labels[0].knowledge_cutoff),
                            "dataset_id": dataset.dataset_id, "model_version": restored.model_version,
                            "columns": ["symbol", "raw_score", "predicted_target_percentile", "reference_holding_return", "target_interval", "supervised", "missing_reason"],
                            "samples": [[y.key.symbol, r, p.predicted_target_percentile, y.raw_return,
                                         primitive(y.target_interval),y.supervised,y.missing_reason]
                                        for y, p, r in zip(dataset.labels, predictions, raw)],
                            "metric": metric, "clipped_scores": clipped}
                encoded = gzip.compress((canonical(document) + "\n").encode(), mtime=0)
                name = item["date"] + ".json.gz"
                path = output_root / "daily" / name
                with path.open("xb") as stream:
                    stream.write(encoded)
                require(json.loads(gzip.decompress(path.read_bytes())) == document, "XGBOOST_DAY_READBACK_MISMATCH")
                hashes[name] = hashlib.sha256(encoded).hexdigest()
                if index % 20 == 0:
                    print(canonical({"split": kind, "evaluated_dates": index + 1, "total": len(selected[kind])}), flush=True)
            metrics[kind] = summarize_daily(daily)
            clipping[kind] = clip_count
            if factor_result is not None:
                for factor in factor_result["metrics"][kind].values():
                    require([d["as_of"] for d in factor["daily"]] == [d["as_of"] for d in daily],
                            "XGBOOST_FACTOR_DATE_COVERAGE_MISMATCH")
        result = {**definition, "model_version": restored.model_version, "training_cutoff": primitive(restored.training_cutoff),
                  "training_rows": fitted.artifact["training_rows"], "training_dates": fitted.artifact["training_dates"],
                  "metrics": metrics, "clipped_scores": clipping, "daily_artifact_sha256": hashes,
                  "fixed_factor_metrics": factor_result["metrics"] if factor_result is not None else None,
                  "portfolio": {"status": "DEFERRED"},
                  "limitations": store.metadata["limitations"] + [
                      "Unknown members are absent from supervised loss but retained in full-universe predictions and IC bounds.",
                      "Nonrandom missing supervision remains; rank intervals are not terminal-value or execution rules.",
                      "Overlapping twenty-session labels are dependent; no IID significance or tradable Sharpe.",
                      "One frozen XGBoost configuration and stock-only view; no DL or market-context ablation."]}
        (output_root / "result.json").write_text(canonical(result) + "\n")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--panel-review", required=True)
    parser.add_argument("--factor-result")
    for name in ("train-start", "validation-start", "validation-end", "test-start", "test-end"):
        parser.add_argument("--" + name, type=date.fromisoformat, required=True)
    args = parser.parse_args()
    from pathlib import Path
    review = json.loads(Path(args.panel_review).read_text())
    factors = json.loads(Path(args.factor_result).read_text()) if args.factor_result else None
    window = FoldWindow(args.train_start, args.validation_start, args.validation_end, args.test_start, args.test_end)
    result = run_xgboost_store(args.store, output_root=args.output, window=window, panel_review=review, factor_result=factors)
    print(canonical({"model_version": result["model_version"], "training_rows": result["training_rows"]}), flush=True)


if __name__ == "__main__":
    main()
