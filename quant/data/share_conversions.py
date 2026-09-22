"""Narrow exit-reference bridges for verified public-share conversions.

Default passive conversion only: no cash-option election, pre-conversion
valuation, invented IPO price, or raw OHLC/volume reconstruction.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import json

from quant.contracts import canonical, finite, fingerprint, identifier, market_time, require

SCHEMA_VERSION = "quant.verified-share-conversions/v1"
METHOD_ID = "verified-share-conversion-exit-reference/v1"
HOLDER_CONVENTION = "PASSIVE_PUBLIC_HOLDER_NO_CASH_ELECTION_FRACTIONAL_REFERENCE_UNITS"


def validate_conversions(document, identity_map):
    require(isinstance(document, dict) and set(document) == {"schema_version", "events", "holder_convention"}
            and document["schema_version"] == SCHEMA_VERSION and isinstance(document["events"], list),
            "CONVERSION_SCHEMA_INVALID")
    require(document["holder_convention"] == HOLDER_CONVENTION, "CONVERSION_HOLDER_CONVENTION_REQUIRED")
    result = json.loads(canonical(document))
    keys = set()
    for event in result["events"]:
        require(isinstance(event, dict) and set(event) == {
            "old_symbol", "successor_symbol", "effective_date", "first_reference_date", "last_reference_date",
            "old_anchor_date", "successor_anchor_date", "ratio", "confirmation_date", "evidence_ids"},
            "CONVERSION_FIELDS_INVALID")
        dates = {k: date.fromisoformat(event[k]) for k in event if k.endswith("_date")}
        require(dates["old_anchor_date"] <= dates["effective_date"] <= dates["first_reference_date"] <= dates["last_reference_date"]
                and dates["successor_anchor_date"] == dates["first_reference_date"]
                and dates["confirmation_date"] >= dates["effective_date"], "CONVERSION_DATE_ORDER_INVALID")
        old, new = identity_map.security_id(event["old_symbol"]), identity_map.security_id(event["successor_symbol"])
        require(old != new, "CONVERSION_IS_NOT_TICKER_ALIAS")
        require((old, dates["effective_date"]) not in keys, "DUPLICATE_CONVERSION")
        keys.add((old, dates["effective_date"]))
        try:
            ratio = Decimal(event["ratio"])
        except (InvalidOperation, TypeError):
            ratio = Decimal("NaN")
        require(isinstance(event["ratio"], str) and ratio.is_finite() and ratio > 0, "CONVERSION_RATIO_INVALID")
        require(isinstance(event["evidence_ids"], list) and event["evidence_ids"]
                and all(identifier(x) for x in event["evidence_ids"]), "CONVERSION_EVIDENCE_REQUIRED")
    return result


def build_exit_references(connection, document, identity_map, *,
                          bar_table="bars", target_table="exit_references"):
    require((bar_table, target_table) in {("bars", "exit_references"),
            ("outcome_prices", "outcome_exit_references")}, "CONVERSION_TABLE_PAIR_INVALID")
    for event in document["events"]:
        old_id = identity_map.security_id(event["old_symbol"])
        new_id = identity_map.security_id(event["successor_symbol"])
        old = connection.execute(f"SELECT factor,chain_start,source_id FROM {bar_table} WHERE security_id=? AND session=?",
                                 (old_id, event["old_anchor_date"])).fetchone()
        new = connection.execute(f"SELECT factor,chain_start,source_id FROM {bar_table} WHERE security_id=? AND session=?",
                                 (new_id, event["successor_anchor_date"])).fetchone()
        require(old is not None and new is not None, "CONVERSION_ANCHOR_MISSING")
        if event["first_reference_date"] > event["effective_date"]:
            first_new = connection.execute(f"SELECT MIN(session) FROM {bar_table} WHERE security_id=?", (new_id,)).fetchone()[0]
            require(first_new == event["first_reference_date"] == new[1], "CONVERSION_DELAYED_ANCHOR_NOT_INITIAL_SERIES")
        last_old = connection.execute(f"SELECT MAX(session) FROM {bar_table} WHERE security_id=? AND session<=?",
                                      (old_id, event["effective_date"])).fetchone()[0]
        require(last_old == event["old_anchor_date"], "CONVERSION_OLD_ANCHOR_NOT_LAST_OBSERVATION")
        require(old[0] > 0 and new[0] > 0, "CONVERSION_FACTOR_INVALID")
        sessions = [r[0] for r in connection.execute("SELECT session FROM sessions WHERE session BETWEEN ? AND ? ORDER BY ordinal",
                    (event["first_reference_date"], event["last_reference_date"]))]
        require(sessions and sessions[0] == event["first_reference_date"] and sessions[-1] == event["last_reference_date"],
                "CONVERSION_REFERENCE_CALENDAR_MISSING")
        for session in sessions:
            require(connection.execute(f"SELECT 1 FROM {bar_table} WHERE security_id=? AND session=?", (old_id, session)).fetchone() is None,
                    "CONVERSION_CANNOT_OVERRIDE_RAW_BAR")
            quote = connection.execute(f"SELECT return_close,chain_start,available_at,source_id FROM {bar_table} WHERE security_id=? AND session=?",
                                       (new_id, session)).fetchone()
            require(quote is not None and quote[1] == new[1], "CONVERSION_SUCCESSOR_REFERENCE_GAP")
            value = old[0] * float(Decimal(event["ratio"])) * quote[0] / new[0]
            require(finite(value) and value > 0, "CONVERSION_REFERENCE_INVALID")
            confirmed = market_time(date.fromisoformat(event["confirmation_date"]), 21).isoformat()
            # Both strings are emitted in the same +08:00 convention by this
            # store. Confirmation can make a historical label available later.
            ready = max(quote[2], confirmed)
            if bar_table == "outcome_prices":
                anchor_ready = [connection.execute(
                    f"SELECT available_at FROM {bar_table} WHERE security_id=? AND session=?",
                    (sid, event[key])).fetchone()[0]
                    for sid, key in ((old_id, "old_anchor_date"), (new_id, "successor_anchor_date"))]
                ready = max(ready, *anchor_ready)
            source = "share-conversion:" + fingerprint({"event": event, "old_anchor": old,
                                                        "successor_anchor": new, "quote": quote, "session": session})
            connection.execute(f"INSERT INTO {target_table} VALUES (?,?,?,?,?,?,?)",
                               (old_id, session, ready, value, source, old[1], METHOD_ID))
