"""Knowledge-cutoff-specific full-universe rank intervals."""
from collections import defaultdict
from datetime import timedelta

from quant.contracts import LabelRow, LabeledDataset, SampleKey, aware, finite, market_time, require, session_of, validate_label_groups
from .intervals import percentile_intervals


def snapshot_cutoff(data):
    """Latest represented information, not a fit cutoff or vendor-vintage claim."""
    latest = max((market_time(data.sessions[-1], 21),
                  *(r.available_at for table in (data.bars, data.outcome_prices, data.exit_references) for r in table)))
    return market_time(session_of(latest) + timedelta(days=1), 0)


def _identify(keys, observations, cutoff):
    grouped = defaultdict(list)
    for key in keys:
        grouped[key.as_of].append(key)
    labels = {}
    for rows in grouped.values():
        values, times, reasons = [], [], []
        for key in rows:
            entry, end, value, ready, reason = observations[key]
            if end is not None and end >= session_of(cutoff):
                value, ready, reason = None, None, "HORIZON_NOT_MATURE_AT_CUTOFF"
            elif ready is not None and ready >= cutoff:
                value, ready, reason = None, None, "OUTCOME_NOT_AVAILABLE_AT_CUTOFF"
            values.append(value); times.append(ready if value is not None else None); reasons.append(reason)
        intervals = percentile_intervals(tuple(values))
        observed = sum(v is not None for v in values)
        group_ready = max((x for x in times if x is not None), default=None)
        for key, value, ready, bounds, reason in zip(rows, values, times, intervals, reasons):
            entry, end, *_ = observations[key]
            labels[key] = LabelRow(key, entry, end, group_ready, value, bounds,
                len(rows), observed, cutoff, ready, reason if value is None else None)
    result=tuple(labels[key] for key in keys)
    validate_label_groups(result)
    return result


def build_label_rows(data, spec, *, knowledge_cutoff=None):
    """Shared label-only interface for data audits and feature-backed pipelines."""
    universes=tuple(u for u in data.universes if session_of(u.as_of) in data.scoring_dates)
    require(all(u.universe_id==spec.universe_id and u.available_at<=u.as_of for u in universes),
            "LABEL_SCORING_UNIVERSE_INVALID")
    keys=tuple(sorted(SampleKey(symbol,u.as_of) for u in universes for symbol in u.symbols))
    cutoff = aware(knowledge_cutoff) if knowledge_cutoff is not None else snapshot_cutoff(data)
    bars = {(b.symbol, b.session): b for b in data.bars}
    outcome_prices = {(q.symbol, q.session): q for q in data.outcome_prices}
    exit_references = {(r.symbol, r.session): r for r in data.exit_references}
    positions = {d: i for i, d in enumerate(data.sessions)}
    observations = {}
    for key in keys:
        pos = positions[session_of(key.as_of)]
        end_pos = pos + spec.horizon_sessions
        if end_pos >= len(data.sessions):
            observations[key] = (None, None, None, None, "FUTURE_CALENDAR_UNAVAILABLE")
            continue
        entry, end = data.sessions[pos+1], data.sessions[end_pos]
        # Do not even compute a return from a horizon/evidence outside the view.
        if end >= session_of(cutoff):
            observations[key] = (entry, end, None, None, "HORIZON_NOT_MATURE_AT_CUTOFF")
            continue
        source = outcome_prices if data.outcome_prices_enabled else bars
        first, last = source.get((key.symbol, entry)), source.get((key.symbol, end))
        if last is None:
            last = exit_references.get((key.symbol, end))
        reason = "OUTCOME_PRICE_UNAVAILABLE"
        if data.outcome_prices_enabled:
            anchor = source.get((key.symbol, session_of(key.as_of)))
            if (anchor is None or anchor.available_at > key.as_of or first is None or last is None
                    or not (anchor.basis_id == first.basis_id == last.basis_id)):
                observations[key] = (entry, end, None, None, "OUTCOME_PRICE_OR_BASIS_UNAVAILABLE")
                continue
        value = ready = None
        if first is not None and last is not None:
            ready = max(first.available_at, last.available_at)
            if ready < cutoff:
                value = last.return_close / first.return_open - 1
                require(finite(value), "NONFINITE_OBSERVED_RETURN")
                reason = None
            else:
                ready, reason = None, "OUTCOME_NOT_AVAILABLE_AT_CUTOFF"
        observations[key] = (entry, end, value, ready, reason)
    return _identify(keys, observations, cutoff)


def build_labels(data, panel, *, knowledge_cutoff=None):
    labels=build_label_rows(data,panel.spec,knowledge_cutoff=knowledge_cutoff)
    return LabeledDataset(panel,labels)


def label_rows_at_cutoff(labels, cutoff):
    """Censor before re-ranking; widening requires the original source input."""
    cutoff=aware(cutoff)
    validate_label_groups(labels)
    require(all(cutoff<=y.knowledge_cutoff for y in labels),"CANNOT_UNCENSOR_WITHOUT_SOURCE")
    observations={y.key:(y.entry_date,y.label_end_date,y.raw_return,y.outcome_available_at,y.missing_reason)
                  for y in labels}
    return _identify(tuple(y.key for y in labels),observations,cutoff)


def labels_at_cutoff(dataset, cutoff):
    return LabeledDataset(dataset.panel,label_rows_at_cutoff(dataset.labels,cutoff))
