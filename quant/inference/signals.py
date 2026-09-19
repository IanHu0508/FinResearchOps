from collections import defaultdict
from datetime import datetime
import re

from quant.contracts import ABLATIONS, TARGET_ID, aware, finite, require
from quant.labels.ranks import percentiles

SCHEMA_VERSION = "finresearchops.quant-signal/v2"
FIELDS = {"schema_version", "symbol", "market", "as_of", "universe_id", "universe_size",
          "horizon", "horizon_unit", "target_id", "predicted_target_percentile", "cross_sectional_model_rank",
          "feature_ablation",
          "model_version", "dataset_id", "training_cutoff", "data_cutoff", "data_kind", "usage"}


def validate_signal(value):
    require(isinstance(value, dict) and set(value) == FIELDS, "QUANT_SIGNAL_FIELDS_INVALID")
    require(value["schema_version"] == SCHEMA_VERSION and value["target_id"] == TARGET_ID,
            "QUANT_SIGNAL_VERSION_INVALID")
    require(value["market"] == "CN_A" and value["horizon"] == 20
            and type(value["horizon"]) is int and value["horizon_unit"] == "trading_sessions",
            "QUANT_SIGNAL_DOMAIN_INVALID")
    require(value["usage"] == "RESEARCH_ONLY_NOT_TRADE_OR_PROBABILITY", "QUANT_SIGNAL_USAGE_INVALID")
    require(value["data_kind"] in ("SYNTHETIC", "REAL_DATA"), "QUANT_SIGNAL_DATA_KIND_INVALID")
    require(value["feature_ablation"] in ABLATIONS, "QUANT_SIGNAL_ABLATION_INVALID")
    for key in ("symbol", "universe_id", "model_version"):
        require(isinstance(value[key], str) and bool(value[key].strip()), "QUANT_SIGNAL_ID_REQUIRED")
    require(isinstance(value["dataset_id"], str) and re.fullmatch(r"[0-9a-f]{64}", value["dataset_id"]) is not None,
            "QUANT_SIGNAL_DATASET_ID_INVALID")
    require(type(value["universe_size"]) is int and value["universe_size"] >= 2,
            "QUANT_SIGNAL_UNIVERSE_INVALID")
    for key in ("predicted_target_percentile", "cross_sectional_model_rank"):
        require(finite(value[key]) and 0 <= value[key] <= 1, "QUANT_SIGNAL_PERCENTILE_INVALID")
    try:
        as_of, train, data = (aware(datetime.fromisoformat(value[k]))
                             for k in ("as_of", "training_cutoff", "data_cutoff"))
    except (TypeError, ValueError) as exc:
        raise ValueError("QUANT_SIGNAL_TIMESTAMP_INVALID") from exc
    require(train < as_of and data <= as_of, "QUANT_SIGNAL_FUTURE_INFORMATION")
    return value


def build_signals(predictions, panel, *, as_ofs, model, dataset_id):
    require(bool(as_ofs) and len(set(as_ofs)) == len(as_ofs), "REQUESTED_SCORING_DATES_INVALID")
    rows = tuple(row for row in panel.rows if row.key.as_of in set(as_ofs))
    require({row.key.as_of for row in rows} == set(as_ofs), "REQUESTED_SCORING_DATE_MISSING")
    require(len(predictions) == len(rows) and len({p.key for p in predictions}) == len(rows)
            and {p.key for p in predictions} == {r.key for r in rows}, "SIGNAL_BATCH_COVERAGE_MISMATCH")
    require(bool(rows), "EMPTY_SIGNAL_BATCH")
    lookup = {r.key: r for r in rows}
    grouped = defaultdict(list)
    for prediction in predictions:
        grouped[prediction.key.as_of].append(prediction)
    result = []
    for as_of, group in sorted(grouped.items()):
        group.sort(key=lambda p: p.key.symbol)
        ranks = percentiles(tuple(p.predicted_target_percentile for p in group))
        for prediction, rank in zip(group, ranks):
            row = lookup[prediction.key]
            signal = {"schema_version": SCHEMA_VERSION, "symbol": row.key.symbol,
                "market": "CN_A", "as_of": as_of.isoformat(), "universe_id": panel.spec.universe_id,
                "universe_size": len(group), "horizon": 20, "horizon_unit": "trading_sessions",
                "target_id": TARGET_ID, "predicted_target_percentile": prediction.predicted_target_percentile,
                "cross_sectional_model_rank": rank, "feature_ablation": model.ablation,
                "model_version": model.model_version,
                "dataset_id": dataset_id, "training_cutoff": model.training_cutoff.isoformat(),
                "data_cutoff": row.data_cutoff.isoformat(), "data_kind": panel.data_kind,
                "usage": "RESEARCH_ONLY_NOT_TRADE_OR_PROBABILITY"}
            result.append(validate_signal(signal))
    return tuple(result)
