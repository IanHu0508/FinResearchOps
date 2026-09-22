"""Outcome-only reference prices; never supply feature activity or eligibility.

Unknown activity remains unknown. A narrow, unchanged-price halted observation
may extend a tentative price chain, but is withheld until a later complete,
positive-activity trading observation confirms that same chain. The confirming
time becomes an outcome dependency, never a historical feature or universe fact.
Exact positive-activity alias price consensus is a separate same-day source;
the conflicting amounts remain unselected and unresolved.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from quant.contracts import aware, finite, fingerprint, identifier, market_time, require
from .identity import IdentityMap, QuoteObservation


PRICE_FIELDS = ("open", "high", "low", "close", "preclose")
SPARSE_ISSUES = {"RAW_NUMERIC_INVALID", "IDENTITY_NO_COMPLETE_QUOTE"}
AMOUNT_PRICE_CONSENSUS_METHOD = "exact-outcome-price-projection-amount-conflict/v1"
CONSENSUS_FIELDS = ("date", *PRICE_FIELDS, "volume", "tradestatus", "adjustflag", "pctChg", "isST")


@dataclass(frozen=True)
class NormalizedOutcome:
    """price_only identifies the sparse, confirmation-delayed activity branch."""
    security_id: str
    session: date
    available_at: datetime
    open: float
    high: float
    low: float
    close: float
    preclose: float
    factor: float
    return_open: float
    return_close: float
    chain_start: str
    source_id: str
    price_only: bool
    confirmation_at: datetime | None

    def __post_init__(self):
        require(identifier(self.security_id) and identifier(self.source_id), "OUTCOME_SOURCE_REQUIRED")
        require(type(self.session) is date and aware(self.available_at) >= market_time(self.session, 15),
                "OUTCOME_AVAILABILITY_INVALID")
        require(all(finite(getattr(self, f)) and getattr(self, f) > 0
                    for f in (*PRICE_FIELDS, "factor", "return_open", "return_close")),
                "OUTCOME_PRICE_INVALID")
        require(self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high,
                "OUTCOME_PRICE_RANGE_INVALID")
        require(date.fromisoformat(self.chain_start) <= self.session, "OUTCOME_CHAIN_START_INVALID")
        require(type(self.price_only) is bool, "OUTCOME_PRICE_ONLY_FLAG_INVALID")
        if self.confirmation_at is not None:
            require(market_time(self.session, 15) <= aware(self.confirmation_at) <= self.available_at,
                    "OUTCOME_CONFIRMATION_TIME_INVALID")


@dataclass(frozen=True)
class _Previous:
    ordinal: int
    close: float
    factor: float
    chain_start: str
    available_at: datetime
    source_id: str


class OutcomePriceNormalizer:
    """Forward-only price normalization, separate from complete feature bars.

    ``day`` may emit earlier pending sessions when the current full feature row
    confirms them. Their original raw prices are unchanged and availability is
    delayed. ``last_issues`` contains (session, security_id, reason) tuples.
    ``finish`` discards every unconfirmed pending observation and emits nothing.
    """

    def __init__(self, identity_map=None):
        self.identity_map = identity_map if identity_map is not None else IdentityMap()
        self.previous = {}
        self.pending = {}
        self.last_issues = ()
        self.discarded_pending_count = 0
        self._last_day = None
        self._last_ordinal = None
        self._finished = False

    @property
    def pending_count(self):
        return sum(len(rows) for rows in self.pending.values())

    def _break(self, sid, session, reason, problems):
        self.previous.pop(sid, None)
        discarded = self.pending.pop(sid, ())
        self.discarded_pending_count += len(discarded)
        problems.extend((row.session, sid, "OUTCOME_PENDING_DISCARDED:" + reason) for row in discarded)
        problems.append((session, sid, reason))

    @staticmethod
    def _price_view(symbol, row):
        values = {}
        for field in (*PRICE_FIELDS, "adjustflag", "tradestatus"):
            value = row.get(field)
            if field in PRICE_FIELDS:
                try:
                    value = float(value)
                    value = value if finite(value) else None
                except (TypeError, ValueError, OverflowError):
                    value = None
            elif value == "":
                value = None
            values[field] = value
        return QuoteObservation(symbol, tuple(sorted(values.items())))

    @staticmethod
    def _record(sid, session, prices, previous, available, source, price_only):
        factor = previous.factor * previous.close / prices["preclose"] if previous else 1.0
        source += ":price-chain:" + fingerprint({
            "method": "baostock-ex-reference-chain/v1", "security_id": sid,
            "session": session, "raw_prices": prices, "current_source_id": source,
            "previous": None if previous is None else {
                "source_id": previous.source_id, "close": previous.close,
                "factor": previous.factor, "chain_start": previous.chain_start,
                "available_at": previous.available_at,
            },
        })
        return NormalizedOutcome(
            sid, session, max(available, previous.available_at) if previous else available,
            *(prices[f] for f in PRICE_FIELDS), factor,
            prices["open"] * factor, prices["close"] * factor,
            previous.chain_start if previous else session.isoformat(), source, price_only, None,
        )

    def _full(self, sid, session, row, previous):
        bar, preclose, *_ = row
        require(bar.symbol == sid and bar.session == session, "OUTCOME_FEATURE_ROW_MISMATCH")
        prices = {f: getattr(bar, f) for f in PRICE_FIELDS if f != "preclose"}
        prices["preclose"] = preclose
        require(finite(preclose) and preclose > 0, "OUTCOME_PRECLOSE_INVALID")
        if not bar.tradable:
            require(bar.volume == bar.amount == 0, "OUTCOME_SUSPENDED_ACTIVITY_UNRESOLVED")
            require(bar.open == bar.high == bar.low == bar.close, "OUTCOME_SUSPENDED_RANGE_UNRESOLVED")
            if previous:
                unchanged = abs(previous.close - preclose) < 1e-8
                coherent = all(abs(prices[f] - preclose) < 1e-8 for f in PRICE_FIELDS[:-1])
                require(unchanged or coherent, "OUTCOME_SUSPENDED_REFERENCE_CHANGE_UNRESOLVED")
        source = bar.source_id + ":independent-outcome-price"
        return self._record(sid, session, prices, previous, bar.available_at, source, False)

    def _sparse(self, sid, session, decision, raw_by_symbol, previous, source_id):
        require(previous is not None, "OUTCOME_SPARSE_WITHOUT_CONTIGUOUS_ANCHOR")
        require(not decision.conflicting_fields and decision.selected_symbol is not None,
                "OUTCOME_PRICE_IDENTITY_UNRESOLVED")
        row = raw_by_symbol[decision.selected_symbol]
        require(row.get("date") == session.isoformat(), "OUTCOME_RAW_DATE_MISMATCH")
        require(row.get("adjustflag") == "3", "OUTCOME_PRICES_MUST_BE_UNADJUSTED")
        require(row.get("tradestatus") == "0", "OUTCOME_SPARSE_REQUIRES_REPORTED_HALT")
        # Explicitly missing is different from invalid, positive or negative.
        activity = [row.get(f) for f in ("volume", "amount")]
        require(any(v == "" for v in activity), "OUTCOME_ACTIVITY_NOT_EXPLICITLY_MISSING")
        # A partial alias can contradict a reported halt even when the selected
        # price-complete alias has no activity values. Do not discard that fact.
        for symbol in decision.provider_symbols:
            for field in ("volume", "amount"):
                value = raw_by_symbol[symbol].get(field)
                if value != "":
                    numeric = float(value)
                    require(finite(numeric) and numeric == 0, "OUTCOME_KNOWN_ACTIVITY_NOT_ZERO")
        prices = {f: float(row[f]) for f in PRICE_FIELDS}
        require(all(finite(v) and v > 0 for v in prices.values()), "OUTCOME_SPARSE_PRICE_INVALID")
        require(len(set(prices.values())) == 1, "OUTCOME_SPARSE_PRICE_NOT_FLAT")
        require(abs(prices["preclose"] - previous.close) < 1e-8,
                "OUTCOME_SPARSE_REFERENCE_CHANGE_UNRESOLVED")
        source = source_id + ":price-only:" + fingerprint({
            "identity_map_id": self.identity_map.map_id, "decision": decision, "raw_row": row,
        })
        return self._record(sid, session, prices, previous, market_time(session, 21), source, True)

    def _amount_consensus(self, sid, session, decision, raw_by_symbol, previous, source_id):
        """Project identical prices, retaining rather than resolving amount disagreement.

        Every alias must independently show positive trading activity. This is
        not the unknown-activity/price_only branch and needs no future price.
        """
        require(sid in self.identity_map.by_id and decision.mapped
                and decision.conflicting_fields == ("amount",)
                and not decision.incomplete_symbols
                and len(decision.provider_symbols) >= 2, "OUTCOME_AMOUNT_CONSENSUS_IDENTITY_INVALID")
        sources = []
        for symbol in sorted(decision.provider_symbols):
            require(self.identity_map.security_id(symbol) == sid, "OUTCOME_AMOUNT_CONSENSUS_IDENTITY_INVALID")
            row = raw_by_symbol[symbol]
            require(row.get("code") == symbol[-2:].lower() + "." + symbol[:6],
                    "OUTCOME_AMOUNT_CONSENSUS_RAW_SYMBOL_MISMATCH")
            require(all(isinstance(row.get(f), str) and row[f] != "" for f in CONSENSUS_FIELDS),
                    "OUTCOME_AMOUNT_CONSENSUS_FIELD_MISSING")
            sources.append((symbol, row))
        projected = {field: sources[0][1][field] for field in CONSENSUS_FIELDS}
        require(all(all(row[field] == projected[field] for field in CONSENSUS_FIELDS)
                    for _, row in sources), "OUTCOME_AMOUNT_CONSENSUS_NOT_EXACT")
        require(projected["date"] == session.isoformat() and projected["tradestatus"] == "1"
                and projected["adjustflag"] == "3" and projected["isST"] in ("0", "1"),
                "OUTCOME_AMOUNT_CONSENSUS_STATUS_INVALID")
        try:
            amounts = [Decimal(row["amount"]) for _, row in sources]
            volume, pct_change = Decimal(projected["volume"]), Decimal(projected["pctChg"])
        except (InvalidOperation, TypeError, ValueError, KeyError) as error:
            raise ValueError("OUTCOME_AMOUNT_CONSENSUS_NUMERIC_INVALID") from error
        require(all(value.is_finite() and value > 0 for value in amounts)
                and volume.is_finite() and volume > 0 and pct_change.is_finite(),
                "OUTCOME_AMOUNT_CONSENSUS_ACTIVITY_INVALID")
        # Match the complete-bar numeric domain too: a positive Decimal can
        # overflow to infinity or underflow to zero in the existing float view.
        # This checks representability, not agreement or tolerance of amounts.
        native_activity = [float(projected["volume"]), *(float(row["amount"]) for _, row in sources)]
        require(all(finite(value) and value > 0 for value in native_activity),
                "OUTCOME_AMOUNT_CONSENSUS_ACTIVITY_NOT_REPRESENTABLE")
        prices = {field: float(projected[field]) for field in PRICE_FIELDS}
        source = source_id + ":amount-conflict-price-consensus:" + fingerprint({
            "method": AMOUNT_PRICE_CONSENSUS_METHOD, "identity_map_id": self.identity_map.map_id,
            "identity_decision": decision, "projected_fields": CONSENSUS_FIELDS,
            "projected_values": projected, "all_original_alias_rows": sources,
        })
        return self._record(sid, session, prices, previous, market_time(session, 21), source, False)

    def day(self, session, ordinal, raw_by_symbol, feature_rows, identity_decisions, issues, source_id):
        require(not self._finished, "OUTCOME_NORMALIZER_FINISHED")
        require(type(session) is date and type(ordinal) is int and ordinal >= 0,
                "OUTCOME_SESSION_INVALID")
        require(self._last_ordinal is None or (ordinal > self._last_ordinal and session > self._last_day),
                "OUTCOME_DAYS_NOT_STRICTLY_INCREASING")
        require(identifier(source_id), "OUTCOME_SOURCE_REQUIRED")
        self._last_day, self._last_ordinal = session, ordinal
        full = {row[0].symbol: row for row in feature_rows}
        require(len(full) == len(feature_rows), "OUTCOME_DUPLICATE_FEATURE_SECURITY")
        original = {d.security_id: d for d in identity_decisions}
        reasons = {}
        for sid, reason in issues:
            reasons.setdefault(sid, []).append(reason)
        observations = [self._price_view(symbol, row) for symbol, row in raw_by_symbol.items()]
        decisions = {d.security_id: d for d in self.identity_map.reconcile(session, observations)}
        seen = set(decisions) | set(full)
        problems, emitted = [], []
        for sid in sorted(set(self.previous) - seen):
            self._break(sid, session, "OUTCOME_RAW_PRICE_OBSERVATION_MISSING", problems)
        for sid in sorted(seen):
            previous = self.previous.get(sid)
            if previous is not None and previous.ordinal != ordinal - 1:
                self._break(sid, session, "OUTCOME_CALENDAR_GAP", problems)
                previous = None
            try:
                decision = original.get(sid)
                consensus = (sid not in full and decision is not None and decision.mapped
                             and decision.conflicting_fields == ("amount",))
                if consensus:
                    require(all(reason == "IDENTITY_QUOTE_CONFLICT:amount" for reason in reasons.get(sid, ())),
                            "OUTCOME_UNRESOLVED_FEATURE_SEMANTICS")
                    record = self._amount_consensus(sid, session, decision, raw_by_symbol, previous, source_id)
                    confirms, confirmation = True, record.available_at
                else:
                    require(decision is None or not decision.conflicting_fields,
                            "OUTCOME_FULL_IDENTITY_CONFLICT")
                    require(all(reason in SPARSE_ISSUES for reason in reasons.get(sid, ())),
                            "OUTCOME_UNRESOLVED_FEATURE_SEMANTICS")
                if not consensus and sid in full:
                    record = self._full(sid, session, full[sid], previous)
                    bar = full[sid][0]
                    confirms = bar.tradable and bar.volume > 0 and bar.amount > 0
                    confirmation = bar.available_at
                elif not consensus:
                    require(sid in decisions, "OUTCOME_PRICE_IDENTITY_MISSING")
                    record = self._sparse(sid, session, decisions[sid], raw_by_symbol, previous, source_id)
                    confirms, confirmation = False, None
                self.previous[sid] = _Previous(ordinal, record.close, record.factor,
                                               record.chain_start, record.available_at, record.source_id)
                if sid in self.pending and confirms:
                    for pending in self.pending.pop(sid):
                        emitted.append(replace(pending,
                            available_at=max(pending.available_at, record.available_at),
                            confirmation_at=confirmation,
                            source_id=pending.source_id + ":confirmed-by:" + record.source_id))
                    emitted.append(record)
                elif record.price_only or sid in self.pending:
                    self.pending.setdefault(sid, []).append(record)
                else:
                    emitted.append(record)
            except (ValueError, TypeError, OverflowError, ZeroDivisionError, KeyError) as error:
                reason = str(error) if isinstance(error, ValueError) and str(error).startswith("OUTCOME_") else "OUTCOME_PRICE_INVALID"
                self._break(sid, session, reason, problems)
        self.last_issues = tuple(problems)
        return sorted(emitted, key=lambda r: (r.session, r.security_id))

    def finish(self):
        problems = []
        for sid, rows in sorted(self.pending.items()):
            problems.extend((row.session, sid, "OUTCOME_UNCONFIRMED_TAIL") for row in rows)
            self.discarded_pending_count += len(rows)
        self.pending.clear()
        self.previous.clear()
        self._finished = True
        self.last_issues = tuple(problems)
        return []
