"""Provider-independent, evidence-backed ticker aliases for one security.

This is an explicit map, not a name/price-based identity inference engine.
Unknown codes retain provisional identities; an empty map proves no coverage.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from quant.contracts import fingerprint, identifier, primitive, require

SCHEMA_VERSION = "quant.security-identity-map/v1"


@dataclass(frozen=True)
class TickerPeriod:
    symbol: str
    start: date | None
    end: date | None

    def __post_init__(self):
        require(identifier(self.symbol) and ":" not in self.symbol, "TICKER_INVALID")
        require(all(x is None or type(x) is date for x in (self.start, self.end)), "TICKER_DATE_INVALID")
        require(self.start is None or self.end is None or self.start < self.end, "TICKER_INTERVAL_INVALID")

    def contains(self, session):
        return (self.start is None or self.start <= session) and (self.end is None or session < self.end)


@dataclass(frozen=True)
class SecurityIdentity:
    security_id: str
    tickers: tuple[TickerPeriod, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self):
        require(identifier(self.security_id) and self.security_id.startswith("security:")
                and len(self.security_id) > len("security:"), "SECURITY_ID_INVALID")
        require(type(self.tickers) is tuple and bool(self.tickers)
                and all(isinstance(p, TickerPeriod) for p in self.tickers), "TICKER_PERIODS_REQUIRED")
        require(type(self.evidence_ids) is tuple and bool(self.evidence_ids)
                and all(identifier(x) for x in self.evidence_ids), "IDENTITY_EVIDENCE_REQUIRED")
        require(len({p.symbol for p in self.tickers}) == len(self.tickers), "TICKER_REUSE_UNSUPPORTED")
        require(self.tickers[0].start is None and self.tickers[-1].end is None, "IDENTITY_HISTORY_MUST_BE_UNBOUNDED")
        require(all(a.end is not None and a.end == b.start for a, b in zip(self.tickers, self.tickers[1:])),
                "TICKER_INTERVALS_NOT_CONTIGUOUS")

    def symbol_at(self, session):
        return next(p.symbol for p in self.tickers if p.contains(session))


@dataclass(frozen=True)
class QuoteObservation:
    """Adapter-supplied comparable values, after its unit normalization.

    None means an unavailable field, never zero. A selected record must be
    complete; partial records may corroborate but cannot be spliced together.
    All observations in one reconciliation must use the same fields, units,
    price/reference basis and vendor. Cross-vendor mixing is not implicit.
    """
    symbol: str
    values: tuple[tuple[str, object], ...]

    def __post_init__(self):
        require(identifier(self.symbol), "OBSERVATION_SYMBOL_REQUIRED")
        require(type(self.values) is tuple and bool(self.values)
                and all(type(p) is tuple and len(p) == 2 and identifier(p[0]) for p in self.values),
                "OBSERVATION_FIELDS_INVALID")
        names = tuple(p[0] for p in self.values)
        require(names == tuple(sorted(set(names))), "OBSERVATION_FIELDS_NOT_UNIQUE_SORTED")


@dataclass(frozen=True)
class IdentityDecision:
    security_id: str
    trading_symbol: str
    provider_symbols: tuple[str, ...]
    selected_symbol: str | None
    conflicting_fields: tuple[str, ...]
    incomplete_symbols: tuple[str, ...]
    mapped: bool


class IdentityMap:
    def __init__(self, identities=()):
        require(type(identities) is tuple and all(isinstance(x, SecurityIdentity) for x in identities),
                "IDENTITY_MAP_INVALID")
        require(len({x.security_id for x in identities}) == len(identities), "DUPLICATE_SECURITY_ID")
        self.identities = tuple(sorted(identities, key=lambda x: x.security_id))
        self.by_id = {x.security_id: x for x in identities}
        self.by_symbol = {}
        for identity in self.identities:
            for period in identity.tickers:
                require(period.symbol not in self.by_symbol, "AMBIGUOUS_TICKER_IDENTITY")
                self.by_symbol[period.symbol] = identity

    @property
    def document(self):
        return {"schema_version": SCHEMA_VERSION, "identities": primitive(self.identities)}

    @property
    def map_id(self):
        return fingerprint(self.document)

    @classmethod
    def from_document(cls, document):
        require(isinstance(document, dict) and set(document) == {"schema_version", "identities"}
                and document["schema_version"] == SCHEMA_VERSION
                and isinstance(document["identities"], list), "IDENTITY_MAP_SCHEMA_INVALID")
        identities = []
        for raw in document["identities"]:
            require(isinstance(raw, dict) and set(raw) == {"security_id", "tickers", "evidence_ids"}
                    and isinstance(raw["tickers"], list) and isinstance(raw["evidence_ids"], list),
                    "IDENTITY_FIELDS_INVALID")
            periods = []
            for p in raw["tickers"]:
                require(isinstance(p, dict) and set(p) == {"symbol", "start", "end"}, "TICKER_FIELDS_INVALID")
                periods.append(TickerPeriod(p["symbol"], *(date.fromisoformat(p[k]) if p[k] is not None else None
                                                          for k in ("start", "end"))))
            identities.append(SecurityIdentity(raw["security_id"], tuple(periods), tuple(raw["evidence_ids"])))
        return cls(tuple(identities))

    def security_id(self, symbol):
        require(identifier(symbol) and ":" not in symbol, "TICKER_INVALID")
        identity = self.by_symbol.get(symbol)
        return identity.security_id if identity else "ticker:" + symbol

    def symbol_at(self, security_id, session):
        require(type(session) is date, "IDENTITY_SESSION_INVALID")
        if security_id in self.by_id:
            return self.by_id[security_id].symbol_at(session)
        require(security_id.startswith("ticker:") and len(security_id) > 7, "UNKNOWN_SECURITY_ID")
        return security_id[7:]

    def reconcile(self, session, observations):
        """Return one decision per security/date; conflicting aliases yield no quote.

        Prefer the contemporaneous code when equivalent rows exist. A single
        backfilled alias can be used under the explicit map; its actual source
        code is retained. No quote averaging, volume summing or fuzzy matching.
        """
        require(type(session) is date, "IDENTITY_SESSION_INVALID")
        observations = tuple(observations)
        require(all(isinstance(q, QuoteObservation) for q in observations), "OBSERVATIONS_INVALID")
        require(len({q.symbol for q in observations}) == len(observations), "DUPLICATE_PROVIDER_SYMBOL")
        grouped = defaultdict(list)
        for quote in observations:
            grouped[self.security_id(quote.symbol)].append(quote)
        result = []
        for security_id, quotes in sorted(grouped.items()):
            symbols = tuple(sorted(q.symbol for q in quotes))
            trading = self.symbol_at(security_id, session)
            values = [dict(q.values) for q in quotes]
            fields = sorted(set().union(*(set(v) for v in values)))
            conflicts = []
            for field in fields:
                observed = [v[field] for v in values if field in v and v[field] is not None]
                if any(field not in v for v in values) or any(v != observed[0] for v in observed[1:]):
                    conflicts.append(field)
            complete = sorted(q.symbol for q in quotes if all(v is not None for _, v in q.values))
            incomplete = tuple(sorted(set(symbols)-set(complete)))
            chosen = (trading if trading in complete else complete[0]) if complete and not conflicts else None
            # Preserve the adapter's numeric validation for provisional single
            # codes. Mapped identities cannot substitute an incomplete alias.
            if security_id not in self.by_id and not conflicts:
                chosen = symbols[0]
            result.append(IdentityDecision(security_id, trading, symbols, chosen, tuple(conflicts), incomplete,
                                           security_id in self.by_id))
        return tuple(result)
