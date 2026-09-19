"""Original synthetic market-only fixtures, not real securities or an exchange calendar."""

from datetime import date, timedelta
import math

from quant.contracts import ResearchSpec, market_time
from quant.data.records import MarketBar, ResearchData, UniverseSnapshot
from quant.splits import FoldWindow


def make_synthetic_data(*, session_count=230, score_start=60, score_end=209):
    sessions, current = [], date(2020, 1, 2)
    while len(sessions) < session_count:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    symbols = tuple(f"SYN{i:03d}.SH" for i in range(6))
    bars = []
    for index, symbol in enumerate(symbols):
        price = 20.0 + 3 * index
        for offset, day in enumerate(sessions):
            opening = price * (1 + 0.0002 * math.sin(offset + index))
            price = opening * (1 + (index - 2.5) * 0.0003 + 0.003 * math.sin(offset / 9 + index))
            volume = 1000 + index * 100 + 50 * math.sin(offset / 3)
            amount = volume * (opening + price) / 2
            bars.append(MarketBar(symbol, day, market_time(day, 16), opening,
                max(opening, price) * 1.005, min(opening, price) * 0.995, price,
                volume, amount, opening, price, True, f"synthetic-bar-{index}-{offset}"))
    spec = ResearchSpec("synthetic-cn-a-six/v1")
    # Explicit warm-up memberships support past market volatility without using
    # the current scoring pool as a historical survivor intersection.
    universes = tuple(UniverseSnapshot(spec.universe_id, market_time(day), market_time(day, 17),
        symbols, f"synthetic-universe-{day}") for day in sessions[1:score_end + 1])
    data = ResearchData(tuple(sessions), tuple(bars), universes,
                        tuple(sessions[score_start:score_end + 1]), "SYNTHETIC")
    return data, spec


def synthetic_fold(data):
    return FoldWindow(data.sessions[60], data.sessions[125], data.sessions[155],
                      data.sessions[180], data.sessions[209])
