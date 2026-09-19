from collections import defaultdict
from dataclasses import replace

from quant.contracts import LabelRow, LabeledDataset, require, session_of
from .ranks import percentiles


def build_labels(data, panel):
    """Next session open -> twentieth session close, with no industry subtraction.

    An outcome missing in the predeclared pool makes that day's target
    percentiles unavailable. Execution feasibility is a separate evaluation.
    """
    bars = {(b.symbol, b.session): b for b in data.bars}
    positions = {d: i for i, d in enumerate(data.sessions)}
    grouped = defaultdict(list)
    for row in panel.rows:
        grouped[row.key.as_of].append(row)
    labels = {}
    for as_of, rows in sorted(grouped.items()):
        pos = positions[session_of(as_of)]
        end_pos = pos + panel.spec.horizon_sessions
        if end_pos >= len(data.sessions):
            for row in rows:
                labels[row.key] = LabelRow(row.key, None, None, None, None, None,
                                          "FUTURE_CALENDAR_UNAVAILABLE")
            continue
        entry, end = data.sessions[pos + 1], data.sessions[end_pos]
        returns, available = {}, []
        for row in rows:
            first, last = bars.get((row.key.symbol, entry)), bars.get((row.key.symbol, end))
            if first is not None and last is not None:
                returns[row.key.symbol] = last.return_close / first.return_open - 1
                available.extend((first.available_at, last.available_at))
        reason = "CROSS_SECTION_OUTCOME_INCOMPLETE" if len(returns) != len(rows) else None
        ready = max(available) if available else None
        for row in rows:
            labels[row.key] = LabelRow(row.key, entry, end, ready,
                                      returns.get(row.key.symbol), None, reason)
        if reason is None:
            ranked = percentiles(tuple(returns[row.key.symbol] for row in rows))
            for row, q in zip(rows, ranked):
                labels[row.key] = replace(labels[row.key], target_percentile=q)
    require(len(labels) == len(panel.rows), "LABEL_COVERAGE_MISMATCH")
    return LabeledDataset(panel, tuple(labels[row.key] for row in panel.rows))
