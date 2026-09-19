"""One shared market-only panel, including point-in-time historical market state."""

import math
import statistics

from quant.contracts import (
    FeatureRow, Panel, SCALAR_NAMES, SampleKey, fingerprint, market_time, require, session_of,
)


def _market_day(universe, previous_day, bars):
    require(universe.as_of >= market_time(session_of(universe.as_of), 15),
            "V1_REQUIRES_AFTER_CLOSE_SCORING")
    day = session_of(universe.as_of)
    source_rows, returns = [universe], []
    for symbol in universe.symbols:
        prior, current = bars.get((symbol, previous_day)), bars.get((symbol, day))
        require(prior is not None and current is not None
                and max(prior.available_at, current.available_at) <= universe.as_of,
                f"MISSING_OR_UNAVAILABLE_MARKET_CONTEXT:{symbol}:{day}")
        require(current.tradable and current.volume > 0 and current.amount > 0,
                f"UNIVERSE_CONTAINS_INELIGIBLE_STOCK:{symbol}:{day}")
        returns.append(current.return_close / prior.return_close - 1)
        source_rows.extend((prior, current))
    return (statistics.median(returns), sum(r > 0 for r in returns) / len(returns),
            statistics.stdev(returns)), tuple(source_rows)


def _ratio_and_z(value, history):
    mean = statistics.fmean(history)
    std = statistics.stdev(history)
    return (value / mean if mean > 0 else None,
            (value - mean) / std if std > 0 else None)


def _individual_features(history):
    sequence = tuple((
        bar.return_close / prior.return_close - 1,
        bar.return_open / prior.return_close - 1,
        bar.close / bar.open - 1,
        (bar.high - bar.low) / bar.close,
        math.log1p(bar.volume), math.log1p(bar.amount), float(bar.tradable),
    ) for prior, bar in zip(history, history[1:]))
    returns = tuple(row[0] for row in sequence)
    current = history[-1]
    volume_ratio, volume_z = _ratio_and_z(current.volume, [b.volume for b in history[-21:-1]])
    amount_ratio, amount_z = _ratio_and_z(current.amount, [b.amount for b in history[-21:-1]])
    # A halted/zero-amount observation is not infinite or zero illiquidity.
    illiquidity = [abs(r) / bar.amount for r, bar in zip(returns[-20:], history[-20:])
                   if bar.tradable and bar.amount > 0]
    values = {f"return_{n}": current.return_close / history[-n - 1].return_close - 1
              for n in (1, 5, 20, 60)}
    values.update({f"volatility_{n}": statistics.stdev(returns[-n:]) for n in (5, 20, 60)})
    values.update(
        overnight_gap=sequence[-1][1], intraday_return=sequence[-1][2],
        downside_deviation_20=math.sqrt(statistics.fmean(min(r, 0) ** 2 for r in returns[-20:])),
        range_to_close=sequence[-1][3], volume_ratio_20=volume_ratio, amount_ratio_20=amount_ratio,
        volume_zscore_20=volume_z, amount_zscore_20=amount_z,
        illiquidity_20=statistics.fmean(illiquidity) if illiquidity else None,
        liquidity_observation_fraction_20=len(illiquidity) / 20, log_amount=math.log1p(current.amount),
    )
    return tuple(values[name] for name in SCALAR_NAMES), sequence


def build_panel(data, spec):
    """Keep the entire declared scoring pool; context uses each historical day's pool.

    scoring_dates selects outputs explicitly. Other universe snapshots are
    warm-up inputs, not extra samples silently dropped for insufficient history.
    """
    bars = {(b.symbol, b.session): b for b in data.bars}
    positions = {d: i for i, d in enumerate(data.sessions)}
    universes = {session_of(u.as_of): u for u in data.universes}
    require(all(u.universe_id == spec.universe_id for u in data.universes),
            "UNIVERSE_DEFINITION_MISMATCH")
    market_cache, rows = {}, []
    for day in data.scoring_dates:
        universe = universes[day]
        as_of, pos = universe.as_of, positions[day]
        require(as_of >= market_time(day, 15), "V1_REQUIRES_AFTER_CLOSE_SCORING")
        require(pos >= spec.lookback_sessions, "INSUFFICIENT_SEQUENCE_HISTORY")
        history_dates = data.sessions[pos - spec.lookback_sessions:pos + 1]
        market_history, market_sources = [], []
        for index in range(pos - 19, pos + 1):
            context_day = data.sessions[index]
            snapshot = universes.get(context_day)
            require(snapshot is not None and snapshot.as_of <= as_of,
                    f"MARKET_UNIVERSE_HISTORY_MISSING:{context_day}")
            if context_day not in market_cache:
                market_cache[context_day] = _market_day(snapshot, data.sessions[index - 1], bars)
            values, sources = market_cache[context_day]
            market_history.append(values)
            market_sources.extend(sources)
        medians = tuple(v[0] for v in market_history)
        context = (*market_history[-1], statistics.stdev(medians))
        market_return_20 = math.prod(1 + r for r in medians) - 1
        # Bind every constituent and its dated inputs once per scoring date.
        # Rows reference this digest rather than duplicating all-market source IDs.
        context_id = fingerprint({"feature_definition_id": spec.feature_definition_id,
                                  "as_of": as_of, "source_rows": market_sources})
        context_cutoff = max(r.available_at for r in market_sources)
        for symbol in universe.symbols:
            history = [bars.get((symbol, d)) for d in history_dates]
            require(all(b is not None and b.available_at <= as_of for b in history),
                    f"MISSING_OR_UNAVAILABLE_MARKET_HISTORY:{symbol}:{day}")
            require(history[-1].tradable and history[-1].volume > 0 and history[-1].amount > 0,
                    f"UNIVERSE_CONTAINS_INELIGIBLE_STOCK:{symbol}:{day}")
            scalars, sequence = _individual_features(history)
            relative = (sequence[-1][0] - context[0],
                        scalars[SCALAR_NAMES.index("return_20")] - market_return_20)
            source_rows = (*history, universe)
            source_ids = {r.source_id for r in source_rows} | {"market-context:" + context_id}
            rows.append(FeatureRow(
                SampleKey(symbol, as_of), scalars, sequence, relative, context, context_id,
                max(context_cutoff, *(r.available_at for r in source_rows)), tuple(sorted(source_ids)),
            ))
    return Panel(spec, tuple(sorted(rows, key=lambda r: r.key)), data.data_kind)
