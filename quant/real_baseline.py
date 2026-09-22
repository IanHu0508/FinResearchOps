"""Bounded-memory fixed-factor diagnostics from a private canonical market store.

Acquisition and construction are explicit separate steps. This evaluator does
not fit ML models, tune signs, select a best factor, or compute portfolio P&L.
"""

import argparse
from collections import Counter, defaultdict
from datetime import date
import gzip
import hashlib
import json

from quant.artifacts.store import _private_root
from quant.contracts import ResearchSpec, canonical, primitive, require
from quant.data.market_store import MarketStore
from quant.data.freeze import load_frozen_panel_review
from quant.evaluation.prediction import evaluate_predictions, summarize_daily
from quant.models.baseline.factors import FACTOR_DEFINITIONS, FACTOR_NAMES, factor_predictions
from quant.pipeline import prepare_window_day
from quant.splits.walk_forward import EvaluationBatch, FoldWindow, day_partition


def evaluate_store(store_path, output_root, window, *, frozen_review):
    output_root = _private_root(output_root)
    review=load_frozen_panel_review(frozen_review,store_path,window)
    reviewed={r['date']:r for r in review['coverage']}
    output_root.mkdir(parents=True, exist_ok=False)
    partitions = output_root / "daily"
    partitions.mkdir()
    combined = defaultdict(list)
    coverage, hashes = [], {}
    with MarketStore(store_path) as store:
        run = {"schema_version": "quant.real-factor-experiment/v2", "data_kind": "REAL_DATA",
               "source_snapshot_id": store.metadata["source_snapshot_id"],
               "source_assumptions": store.metadata, "spec": primitive(ResearchSpec(store.universe_id)),
               "window": primitive(window), "factor_definitions": primitive(FACTOR_DEFINITIONS),
               "composite": "percentile(mean(component percentiles)); fixed equal weights",
               "score_semantics": "Uncalibrated fixed-factor percentile proxies, not upward probabilities.",
               "training": "NONE_FIXED_RULES", "feature_ablation": "stock-only"}
        run['phase_a_frozen_dataset_id']=review['frozen_dataset_id']
        with (output_root / "definition.json").open("x") as stream:
            stream.write(canonical(run) + "\n")
        days = [day for day in store.sessions if window.train_start <= day <= window.test_end]
        require([str(day) for day in days] == [row['date'] for row in review['coverage']],
                'FACTOR_REVIEW_DATE_COVERAGE_MISMATCH')
        for index, day in enumerate(days):
            data = store.read_day(day)
            dataset = prepare_window_day(data, ResearchSpec(store.universe_id),window)
            predictions = factor_predictions(dataset.panel.rows)
            kind, disposition = day_partition(dataset.labels,window)
            checked=reviewed.get(str(day))
            require(checked is not None and checked['original_pool_count']==len(dataset.labels)
                    and checked['observed_count']==dataset.labels[0].observed_count
                    and checked['knowledge_cutoff']==primitive(dataset.labels[0].knowledge_cutoff)
                    and (checked['split'],checked['disposition'])==(kind,disposition),
                    'FACTOR_REVIEW_COVERAGE_MISMATCH')
            require(dataset.dataset_id == checked.get('dataset_id'),
                    'FACTOR_FROZEN_DATASET_CONTENT_MISMATCH')
            missing = [y.key.symbol for y in dataset.labels if y.raw_return is None]
            item = {"date": day.isoformat(), "split": kind, "disposition": disposition,
                    "count": len(dataset.labels), "unknown_outcome_symbols": missing,
                    "observed_count":dataset.labels[0].observed_count,
                    "knowledge_cutoff":primitive(dataset.labels[0].knowledge_cutoff),
                    "entry_date": primitive(dataset.labels[0].entry_date),
                    "label_end_date": primitive(dataset.labels[0].label_end_date),
                    "missing_reason": dataset.labels[0].missing_reason}
            coverage.append(item)
            daily_metrics = {}
            if disposition == "AVAILABLE":
                batch = EvaluationBatch(dataset.panel.rows, dataset.labels)
                for name, values in predictions.items():
                    metric = evaluate_predictions(values, batch)["daily"][0]
                    combined[(kind, name)].append(metric)
                    daily_metrics[name] = metric
            document = {"schema_version": "quant.real-factor-day/v2", **item,
                        "dataset_id": dataset.dataset_id,
                        "columns": ["symbol", "reference_holding_return", "target_interval", *FACTOR_NAMES],
                        "samples": [[label.key.symbol, label.raw_return, primitive(label.target_interval),
                                     *(predictions[name][i].predicted_target_percentile for name in FACTOR_NAMES)]
                                    for i, label in enumerate(dataset.labels)],
                        "metrics": daily_metrics}
            encoded = gzip.compress((canonical(document) + "\n").encode(), mtime=0)
            name = day.isoformat() + ".json.gz"
            with (partitions / name).open("xb") as stream:
                stream.write(encoded)
            hashes[name] = hashlib.sha256(encoded).hexdigest()
            # Read back the exact persisted diagnostic; never report only RAM output.
            restored = json.loads(gzip.decompress((partitions / name).read_bytes()))
            require(restored["metrics"] == document["metrics"] and restored["samples"] == document["samples"],
                    "DAILY_RESULT_READBACK_MISMATCH")
            if index % 10 == 0:
                print(canonical({"evaluated_dates": index + 1, "total_dates": len(days),
                                 "date": day, "disposition": disposition, "rows": len(dataset.labels)}), flush=True)
        metrics = {kind: {name: summarize_daily(combined[(kind, name)])
                         for name in FACTOR_NAMES if combined[(kind, name)]}
                   for kind in ("train", "validation", "test")}
        result = {**run, "coverage": coverage,
                  "coverage_date_counts": dict(Counter(r["disposition"] for r in coverage)),
                  "coverage_sample_counts": {status: sum(r["count"] for r in coverage if r["disposition"] == status)
                                             for status in {r["disposition"] for r in coverage}},
                  "metrics": metrics, "daily_artifact_sha256": hashes,
                  "portfolio": {"status": "DEFERRED", "reason": "EXECUTION_RULES_NOT_IMPLEMENTED"},
                  "limitations": store.metadata["limitations"] + [
                      "Unknown members remain in full pools; partial IC uses conservative outer bounds, complete days are sensitivity only.",
                      "Fixed rules were predeclared; train/validation/test are chronological reporting segments, not evidence of trained-model generalization.",
                      "Overlapping twenty-session outcomes are dependent; no IID t-test or annualized IC significance claim.",
                      "Quintile forward returns are theoretical overlapping diagnostics, not executable daily P&L.",
                      "No market-context ablation or learned ML/DL comparison has been performed."]}
    for name, document in (("result.json", result), ("coverage.json", coverage)):
        with (output_root / name).open("x") as stream:
            stream.write(canonical(document) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--panel-review",required=True)
    parser.add_argument("--train-start", type=date.fromisoformat, required=True)
    parser.add_argument("--validation-start", type=date.fromisoformat, required=True)
    parser.add_argument("--validation-end", type=date.fromisoformat, required=True)
    parser.add_argument("--test-start", type=date.fromisoformat, required=True)
    parser.add_argument("--test-end", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    window = FoldWindow(args.train_start, args.validation_start, args.validation_end, args.test_start, args.test_end)
    from pathlib import Path
    result = evaluate_store(args.store, args.output, window,
                            frozen_review=json.loads(Path(args.panel_review).read_text()))
    print(canonical({"coverage_date_counts": result["coverage_date_counts"]}), flush=True)


if __name__ == "__main__":
    main()
