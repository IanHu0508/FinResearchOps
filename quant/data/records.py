"""Market-only inputs; acquisition and corporate-action reconstruction are external."""

from dataclasses import dataclass
from datetime import date, datetime

from quant.contracts import aware, finite, identifier, market_time, require, session_of


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    session: date
    available_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    return_open: float
    return_close: float
    tradable: bool
    source_id: str

    def __post_init__(self):
        require(identifier(self.symbol) and identifier(self.source_id), "BAR_ID_REQUIRED")
        require(type(self.session) is date and session_of(self.available_at) >= self.session,
                "BAR_AVAILABLE_BEFORE_SESSION")
        require(self.available_at >= market_time(self.session, 15), "DAILY_BAR_NOT_FINISHED")
        require(all(finite(v) and v > 0 for v in (self.open, self.high, self.low,
                self.close, self.return_open, self.return_close)), "PRICE_INVALID")
        require(self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high,
                "OHLC_INCONSISTENT")
        require(finite(self.volume) and self.volume >= 0, "VOLUME_INVALID")
        require(finite(self.amount) and self.amount >= 0, "AMOUNT_INVALID")
        require((self.volume == 0) == (self.amount == 0), "VOLUME_AMOUNT_ZERO_MISMATCH")
        require(type(self.tradable) is bool, "TRADABLE_FLAG_REQUIRED")


@dataclass(frozen=True)
class UniverseSnapshot:
    universe_id: str
    as_of: datetime
    available_at: datetime
    symbols: tuple[str, ...]
    source_id: str

    def __post_init__(self):
        require(aware(self.available_at) <= aware(self.as_of), "FUTURE_UNIVERSE_SNAPSHOT")
        require(type(self.symbols) is tuple and all(identifier(x) for x in self.symbols),
                "UNIVERSE_SYMBOLS_MUST_BE_IMMUTABLE")
        require(identifier(self.universe_id) and identifier(self.source_id) and len(self.symbols) >= 2,
                "UNIVERSE_METADATA_REQUIRED")
        require(tuple(sorted(set(self.symbols))) == self.symbols, "UNIVERSE_SYMBOLS_NOT_UNIQUE_SORTED")


@dataclass(frozen=True)
class ResearchData:
    sessions: tuple[date, ...]
    bars: tuple[MarketBar, ...]
    universes: tuple[UniverseSnapshot, ...]
    scoring_dates: tuple[date, ...]
    data_kind: str
    price_basis: str = "TOTAL_RETURN_REFERENCE"

    def __post_init__(self):
        for name, cls in (("bars", MarketBar), ("universes", UniverseSnapshot)):
            rows = getattr(self, name)
            require(type(rows) is tuple and all(isinstance(row, cls) for row in rows),
                    "NORMALIZED_RECORDS_MUST_BE_IMMUTABLE:" + name)
        require(type(self.sessions) is tuple, "CALENDAR_MUST_BE_IMMUTABLE")
        require(bool(self.sessions) and all(type(d) is date for d in self.sessions)
                and tuple(sorted(set(self.sessions))) == self.sessions, "TRADING_CALENDAR_INVALID")
        require(type(self.scoring_dates) is tuple and bool(self.scoring_dates)
                and all(type(d) is date for d in self.scoring_dates)
                and tuple(sorted(set(self.scoring_dates))) == self.scoring_dates,
                "SCORING_DATES_INVALID")
        require(self.data_kind in ("SYNTHETIC", "REAL_DATA"), "DATA_KIND_INVALID")
        require(self.price_basis == "TOTAL_RETURN_REFERENCE", "EXPLICIT_RETURN_BASIS_REQUIRED")
        require(len({(b.symbol, b.session) for b in self.bars}) == len(self.bars), "DUPLICATE_MARKET_BAR")
        calendar = set(self.sessions)
        require(all(b.session in calendar for b in self.bars), "BAR_OUTSIDE_CALENDAR")
        days = tuple(session_of(u.as_of) for u in self.universes)
        require(len(set(days)) == len(days), "MULTIPLE_UNIVERSES_PER_TRADING_DATE")
        require(set(days) <= calendar, "UNIVERSE_OUTSIDE_CALENDAR")
        require(set(self.scoring_dates) <= set(days), "SCORING_UNIVERSE_MISSING")
