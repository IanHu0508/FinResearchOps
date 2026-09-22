"""Disk-backed market inputs, consumed through the existing Quant contracts.

The explicit reference method is a theoretical ex-price chain, not an account
cash-flow return. Unknown observations break a series; they are never filled.
"""

from dataclasses import asdict
import argparse
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3

from quant.artifacts.store import _private_root
from quant.contracts import ContractError, canonical, finite, fingerprint, market_time, require
from .acquisition import read_cached
from .identity import IdentityMap, QuoteObservation
from .quote_choices import ReviewedQuoteChoices
from .suspension_repairs import ReviewedSuspensionZeros
from .records import ExitReference, MarketBar, OutcomePrice, ResearchData, UniverseSnapshot
from .outcome_prices import OutcomePriceNormalizer
from .share_conversions import build_exit_references, validate_conversions
from .eligibility import LiquidationStageExclusions, UNIVERSE_ID as LIQUIDATION_UNIVERSE_ID

REFERENCE_METHOD = "baostock-ex-reference-chain/v1"
UNIVERSE_ID = "cn-a-sh-sz-observed-tradable-61-session-history/v3"
STORE_VERSION = "quant.canonical-market-store/v9"
OUTCOME_REFERENCE_METHOD = "observed-outcome-price-chain/v2"
AVAILABILITY_BASIS = "HISTORICAL_POST_CLOSE_RECONSTRUCTION_NOT_VENDOR_VINTAGE"


def is_a_share(code):
    # STAR ordinary shares use 688; 689 is the depositary-receipt range.
    # Code classification is not evidence of historical listing eligibility.
    return bool(re.fullmatch(r"(?:sh\.(?:60\d{4}|688\d{3})|sz\.(?:00|30)\d{4})", code))


def canonical_symbol(code):
    require(is_a_share(code), "NON_A_SHARE_SYMBOL")
    return code[3:] + "." + code[:2].upper()


class ReferenceNormalizer:
    """Forward-only reconstruction keyed internally by stable/provisional ID.

    Intermediate MarketBar.symbol holds that ID; MarketStore projects the
    scoring-date ticker when crossing into the research-input contract.
    """

    def __init__(self, identity_map=None, quote_choices=None, suspension_repairs=None):
        self.previous = {}
        self.identity_map = identity_map if identity_map is not None else IdentityMap()
        self.identity_map_id = self.identity_map.map_id
        self.identity_decisions = ()
        self.quote_choices = quote_choices
        if quote_choices is not None:
            require(quote_choices.document["identity_map_id"] == self.identity_map_id,
                    "QUOTE_CHOICES_IDENTITY_MISMATCH")
        self.applied_quote_choices = ()
        self.suspension_repairs = suspension_repairs
        self.applied_suspension_repairs = ()
        self.raw_by_symbol = {}

    def day(self, session, ordinal, table, source_id):
        columns = table.fields
        required = {"date", "code", "open", "high", "low", "close", "preclose",
                    "volume", "amount", "adjustflag", "tradestatus", "isST"}
        require(required <= set(columns), "RAW_MARKET_COLUMNS_MISSING")
        values, issues = [], []
        raw_by_symbol, observations = {}, []
        repaired_sources, repaired = {}, []
        seen = set()
        for raw in table.rows:
            row = dict(zip(columns, raw))
            provider_code = row["code"]
            require(row["date"] == session.isoformat(), "RAW_MARKET_DATE_MISMATCH")
            require(provider_code not in seen, "RAW_MARKET_DUPLICATE_SYMBOL")
            seen.add(provider_code)
            if not is_a_share(provider_code):
                continue
            symbol = canonical_symbol(provider_code)
            if self.suspension_repairs is not None:
                row, receipt = self.suspension_repairs.apply(session, symbol, row, source_id)
                if receipt is not None:
                    repaired_sources[symbol] = fingerprint(receipt)
                    repaired.append(receipt)
            raw_by_symbol[symbol] = row
            comparison = {}
            no_quote = all(not row[name] for name in ("open", "high", "low", "close", "preclose", "volume", "amount"))
            for name in sorted(required - {"date", "code"}):
                value = row[name] if row[name] and not no_quote else None
                if name in {"open", "high", "low", "close", "preclose", "volume", "amount"}:
                    try:
                        number = float(value)
                        value = number if finite(number) else None
                    except (ValueError, TypeError, OverflowError):
                        pass
                comparison[name] = value
            observations.append(QuoteObservation(symbol, tuple(comparison.items())))
        decisions = self.identity_map.reconcile(session, observations)
        self.raw_by_symbol = raw_by_symbol
        self.applied_suspension_repairs = tuple(repaired)
        applied = []
        if self.quote_choices is not None:
            resolved = []
            for decision in decisions:
                decision, receipt = self.quote_choices.apply(session, decision, raw_by_symbol, source_id)
                resolved.append(decision)
                if receipt is not None:
                    applied.append(receipt)
            decisions = tuple(resolved)
        self.applied_quote_choices = tuple(applied)
        self.identity_decisions = tuple(d for d in decisions if d.mapped)
        for decision in decisions:
            symbol = decision.security_id
            if decision.conflicting_fields:
                issues.append((symbol, "IDENTITY_QUOTE_CONFLICT:" + ",".join(decision.conflicting_fields)))
                self.previous.pop(symbol, None)
                continue
            if decision.selected_symbol is None:
                issues.append((symbol, "IDENTITY_NO_COMPLETE_QUOTE"))
                self.previous.pop(symbol, None)
                continue
            row = raw_by_symbol[decision.selected_symbol]
            provenance = source_id + ":identity:" + self.identity_map_id
            if decision.selected_symbol in repaired_sources:
                provenance += ":suspension-reconstruction:" + repaired_sources[decision.selected_symbol]
            if decision.mapped:
                provenance += ":selection:" + fingerprint(decision)
                if self.quote_choices is not None:
                    provenance += ":review:" + self.quote_choices.manifest_id
            try:
                require(row["adjustflag"] == "3", "RAW_PRICES_MUST_BE_UNADJUSTED")
                require(row["tradestatus"] in ("0", "1"), "TRADE_STATUS_UNKNOWN")
                numeric = {name: float(row[name]) for name in
                           ("open", "high", "low", "close", "preclose", "volume", "amount")}
                previous = self.previous.get(symbol)
                continuous = previous is not None and previous[0] == ordinal - 1
                require(finite(numeric["preclose"]) and numeric["preclose"] > 0, "PRECLOSE_INVALID")
                # A carried old quote plus a changed ex-reference while halted
                # cannot be certified from daily OHLC alone.
                if continuous and row["tradestatus"] == "0":
                    reference_unchanged = abs(previous[1] - numeric["preclose"]) < 1e-8
                    coherent_zero_trade_adjustment = (
                        numeric["volume"] == numeric["amount"] == 0
                        and all(abs(numeric[name] - numeric["preclose"]) < 1e-8
                                for name in ("open", "high", "low", "close")))
                    require(reference_unchanged or coherent_zero_trade_adjustment,
                            "SUSPENDED_REFERENCE_CHANGE_UNRESOLVED")
                factor = previous[2] * previous[1] / numeric["preclose"] if continuous else 1.0
                chain_start = previous[3] if continuous else session.isoformat()
                streak = previous[4] + 1 if continuous else 1
                bar = MarketBar(
                    symbol, session, market_time(session, 21),
                    numeric["open"], numeric["high"], numeric["low"], numeric["close"],
                    numeric["volume"], numeric["amount"], numeric["open"] * factor,
                    numeric["close"] * factor, row["tradestatus"] == "1", provenance,
                )
                if not bar.tradable:
                    # A status/quote contradiction may be a partial halt or a
                    # vendor error. Neither authorizes flipping the flag,
                    # erasing activity, or supplying a reference observation.
                    require(bar.volume == bar.amount == 0, "SUSPENDED_ACTIVITY_UNRESOLVED")
                    require(bar.open == bar.high == bar.low == bar.close,
                            "SUSPENDED_RANGE_UNRESOLVED")
                self.previous[symbol] = (ordinal, bar.close, factor, chain_start, streak)
                eligible = streak >= 61 and bar.tradable and bar.volume > 0 and bar.amount > 0
                values.append((bar, numeric["preclose"], factor, chain_start, streak, eligible, row["isST"]))
            except (ValueError, OverflowError, ZeroDivisionError) as error:
                # Missing amount is unknown, including a terminal stub; never 0.
                issues.append((symbol, str(error) if isinstance(error, ContractError) else "RAW_NUMERIC_INVALID"))
                self.previous.pop(symbol, None)
        return values, issues


BAR_COLUMNS = ("symbol", "session", "available_at", "open", "high", "low", "close",
               "volume", "amount", "return_open", "return_close", "tradable", "source_id")
STORE_BAR_COLUMNS = ("security_id",) + BAR_COLUMNS[1:]
OUTCOME_COLUMNS = ("security_id", "session", "available_at", "open", "high", "low", "close",
                   "preclose", "factor", "return_open", "return_close", "chain_start", "source_id",
                   "price_only", "confirmation_at")


def outcome_basis_id(security_id, chain_start):
    return fingerprint({"method": OUTCOME_REFERENCE_METHOD, "security_id": security_id,
                        "chain_start": str(chain_start)})


def build_store(raw_root, destination, *, identity_map=None, quote_choices_document=None, suspension_repairs_document=None,
                share_conversions_document=None, liquidation_exclusions_document=None):
    """Consume a complete immutable acquisition; never build an incomplete cache."""
    raw_root, destination = _private_root(raw_root), _private_root(destination)
    identity_map = identity_map if identity_map is not None else IdentityMap()
    quote_choices = (ReviewedQuoteChoices(quote_choices_document, identity_map)
                     if quote_choices_document is not None else None)
    suspension_repairs = ReviewedSuspensionZeros(suspension_repairs_document) if suspension_repairs_document is not None else None
    conversions = validate_conversions(share_conversions_document, identity_map) if share_conversions_document is not None else None
    exclusions = (LiquidationStageExclusions(liquidation_exclusions_document, identity_map)
                  if liquidation_exclusions_document is not None else None)
    require(not destination.exists(), "CANONICAL_STORE_ALREADY_EXISTS")
    plan = json.loads((raw_root / "plan.json").read_text())
    require(plan["schema_version"] == "quant.baostock-acquisition/v1", "RAW_PLAN_VERSION_INVALID")
    calendar_rows = []
    for path in sorted(raw_root.glob("calendar-p*.json"), key=lambda p: int(p.stem.split("p")[-1])):
        table = read_cached(raw_root, path.stem, method="query_trade_dates", field_index=9)
        calendar_rows.extend(table.rows)
    start, end = date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])
    require([row[0] for row in calendar_rows] == [(start + timedelta(days=i)).isoformat()
                for i in range((end - start).days + 1)]
            and all(row[1] in ("0", "1") for row in calendar_rows), "STORE_CALENDAR_INCOMPLETE")
    sessions = [date.fromisoformat(day) for day, trading in calendar_rows if trading == "1"]
    require(sessions and sessions == sorted(set(sessions)), "STORE_CALENDAR_INVALID")
    if exclusions is not None:
        require(exclusions.start <= sessions[0] and sessions[-1] <= exclusions.end, "LIQUIDATION_COVERAGE_MISSING")
    require(all((raw_root / ("daily-" + day.isoformat() + ".json")).is_file() for day in sessions),
            "RAW_ACQUISITION_INCOMPLETE")
    raw_hashes = {}
    for day in sessions:
        name = "daily-" + day.isoformat()
        raw_hashes[name] = json.loads((raw_root / (name + ".json")).read_text())["sha256"]
    metadata = {
        "schema_version": STORE_VERSION, "data_kind": "REAL_DATA",
        "source_plan": plan, "source_hashes": raw_hashes,
        "universe_id": LIQUIDATION_UNIVERSE_ID if exclusions else UNIVERSE_ID,
        "liquidation_exclusions": exclusions.document if exclusions else None,
        "liquidation_exclusions_id": exclusions.manifest_id if exclusions else None,
        "symbol_convention": "Store security_id/universe keys are stable or provisional IDs; bars.trading_symbol is the session-effective ticker. ResearchData projects every row to the scoring-date ticker.",
        "identity_map": identity_map.document, "identity_map_id": identity_map.map_id,
        "reviewed_quote_choices": quote_choices.document if quote_choices else None,
        "reviewed_quote_choices_id": quote_choices.manifest_id if quote_choices else None,
        "reviewed_suspension_repairs": suspension_repairs.document if suspension_repairs else None,
        "reviewed_suspension_repairs_id": suspension_repairs.manifest_id if suspension_repairs else None,
        "share_conversions": conversions,
        "share_conversions_id": fingerprint(conversions) if conversions is not None else None,
        "identity_policy": "EXPLICIT_ALIASES_ONLY; UNKNOWN_CODES_PROVISIONAL; COMPLETE_OBSERVATION_REQUIRED; OBSERVED_CONFLICT_BREAKS_CHAIN; NO_FIELD_FILL",
        "all_security_identities_verified": False,
        "instrument_scope": "SH 60xxxx/688xxx and SZ 00xxxx/30xxxx ordinary-share code ranges; exclude 689xxx CDRs. Historical instrument/lifecycle coverage requires separate audit.",
        "halted_observation_policy": "Explicit zero activity and flat raw OHLC required; ambiguous activity/range breaks the chain without rewriting prices or status.",
        "outcome_reference_method": OUTCOME_REFERENCE_METHOD,
        "outcome_policy": "Feature bars and 61-session qualification stay unchanged. Label prices have a separate basis; sparse constant halted quotes require later same-chain traded confirmation, with delayed availability. Certified same-security aliases may supply an exact price projection only when their sole feature conflict is amount, both report positive traded activity and all price/reference/volume/state fields agree exactly; no amount is selected or repaired. Unconfirmed tails and unresolved price/identity/action conflicts stay unavailable.",
        "price_basis": "TOTAL_RETURN_REFERENCE", "return_reference_method": REFERENCE_METHOD,
        "availability_basis": AVAILABILITY_BASIS, "historical_vendor_vintage_verified": False,
        "assumed_available_local": "21:00 Asia/Shanghai", "scoring_local": "21:30 Asia/Shanghai",
        "formula": "F[t]=F[t-1]*raw_close[t-1]/preclose[t]; reference_OHLC=raw_OHLC*F[t]",
        "gap_policy": "UNKNOWN_GAP_STARTS_NEW_CHAIN; NO_TARGET_ACROSS_CHAIN_BREAK",
        "universe_policy": "Contemporaneously observed SH/SZ A shares, 61 consecutive valid session bars, currently trading with positive volume and amount; ST included; no current-listing filter."
                           + (" Exclude explicitly reviewed liquidation stages only after publication and on/after effectiveness; prior history stays present." if exclusions else ""),
        "limitations": ["Latest-vintage historical market reconstruction, not verified historical vendor PIT.",
                        "Theoretical ex-price reference, not cash-flow, tax or execution returns.",
                        "No terminal-value fill; unknown outcomes retain the full pool and use the separately versioned interval-label contract.",
                        "Daily provider coverage is assessed separately; observed bars are not proof of all-market coverage.",
                        "Only supplied evidence-backed aliases are resolved; other ticker identities are provisional.",
                        "Conflicting alias observations are preserved as issues and not silently selected or averaged."],
    }
    metadata["source_snapshot_id"] = fingerprint(metadata)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects an original store even on a failed build.
    with destination.open("xb"):
        pass
    connection = sqlite3.connect(destination)
    try:
        connection.executescript("""
            CREATE TABLE metadata (document TEXT NOT NULL);
            CREATE TABLE sessions (ordinal INTEGER PRIMARY KEY, session TEXT UNIQUE NOT NULL);
            CREATE TABLE bars (
                security_id TEXT, session TEXT, available_at TEXT, open REAL, high REAL, low REAL,
                close REAL, volume REAL, amount REAL, return_open REAL, return_close REAL,
                tradable INTEGER, source_id TEXT, preclose REAL, factor REAL,
                chain_start TEXT, consecutive_sessions INTEGER, eligible INTEGER, is_st TEXT,
                trading_symbol TEXT NOT NULL,
                PRIMARY KEY(session,security_id));
            CREATE INDEX bars_security_session ON bars(security_id,session);
            CREATE TABLE universes (session TEXT PRIMARY KEY, symbols TEXT NOT NULL, source_id TEXT NOT NULL);
            CREATE TABLE issues (session TEXT, security_id TEXT, reason TEXT);
            CREATE TABLE identity_decisions (session TEXT, security_id TEXT, document TEXT NOT NULL,
                                            PRIMARY KEY(session,security_id));
            CREATE TABLE reviewed_quote_choices (session TEXT, security_id TEXT, document TEXT NOT NULL,
                                                PRIMARY KEY(session,security_id));
            CREATE TABLE suspension_repairs (session TEXT, symbol TEXT, document TEXT NOT NULL,
                                            PRIMARY KEY(session,symbol));
            CREATE TABLE exit_references (security_id TEXT, session TEXT, available_at TEXT, return_close REAL,
                                          source_id TEXT, anchor_chain_start TEXT, method_id TEXT,
                                          PRIMARY KEY(session,security_id));
            CREATE TABLE outcome_prices (
                security_id TEXT, session TEXT, available_at TEXT, open REAL, high REAL, low REAL, close REAL,
                preclose REAL, factor REAL, return_open REAL, return_close REAL, chain_start TEXT,
                source_id TEXT, price_only INTEGER, confirmation_at TEXT,
                PRIMARY KEY(session,security_id));
            CREATE INDEX outcome_security_session ON outcome_prices(security_id,session);
            CREATE TABLE outcome_exit_references (
                security_id TEXT, session TEXT, available_at TEXT, return_close REAL,
                source_id TEXT, anchor_chain_start TEXT, method_id TEXT, PRIMARY KEY(session,security_id));
            CREATE TABLE outcome_price_issues (session TEXT, security_id TEXT, reason TEXT);
            CREATE TABLE eligibility_exclusions (session TEXT, security_id TEXT, manifest_id TEXT,
                                               PRIMARY KEY(session,security_id));
            CREATE TABLE day_counts (session TEXT PRIMARY KEY, raw_rows INTEGER, valid_rows INTEGER,
                                     eligible_rows INTEGER, issue_rows INTEGER);
        """)
        connection.executemany("INSERT INTO sessions VALUES (?,?)",
                               [(i, day.isoformat()) for i, day in enumerate(sessions)])
        normalizer = ReferenceNormalizer(identity_map, quote_choices, suspension_repairs)
        outcome_normalizer = OutcomePriceNormalizer(identity_map)
        for index, day in enumerate(sessions):
            name = "daily-" + day.isoformat()
            table = read_cached(raw_root, name, method="query_daily_history_k_AStock", row_index=4, field_index=5)
            require(bool(table.rows), "RAW_MARKET_EMPTY")
            source = "baostock:" + raw_hashes[name]
            rows, issues = normalizer.day(day, index, table, source)
            outcome_rows = outcome_normalizer.day(day, index, normalizer.raw_by_symbol,
                rows, normalizer.identity_decisions, issues, source)
            outcome_records = []
            for outcome in outcome_rows:
                value = asdict(outcome)
                for field in ("session", "available_at", "chain_start", "confirmation_at"):
                    if value[field] is not None:
                        value[field] = value[field].isoformat() if hasattr(value[field], "isoformat") else str(value[field])
                outcome_records.append(tuple(value[field] for field in OUTCOME_COLUMNS))
            connection.executemany("INSERT INTO outcome_prices VALUES (" + ",".join("?"*15) + ")", outcome_records)
            connection.executemany("INSERT INTO outcome_price_issues VALUES (?,?,?)",
                [(str(d), sid, why) for d, sid, why in outcome_normalizer.last_issues])
            removed = []
            if exclusions is not None:
                filtered = []
                for bar, preclose, factor, chain, streak, eligible, is_st in rows:
                    if eligible and exclusions.excludes(bar.symbol, day):
                        removed.append((str(day), bar.symbol, exclusions.manifest_id))
                        eligible = False
                    filtered.append((bar, preclose, factor, chain, streak, eligible, is_st))
                rows = filtered
            connection.executemany("INSERT INTO eligibility_exclusions VALUES (?,?,?)", removed)
            records = []
            for bar, preclose, factor, chain, streak, eligible, is_st in rows:
                values = asdict(bar)
                values["session"], values["available_at"] = bar.session.isoformat(), bar.available_at.isoformat()
                records.append(tuple(values[n] for n in BAR_COLUMNS) + (preclose, factor, chain, streak, eligible, is_st,
                               identity_map.symbol_at(bar.symbol, day)))
            connection.executemany("INSERT INTO bars VALUES (" + ",".join("?" * 20) + ")", records)
            connection.executemany("INSERT INTO identity_decisions VALUES (?,?,?)",
                                   [(day.isoformat(), d.security_id, canonical(d)) for d in normalizer.identity_decisions])
            connection.executemany("INSERT INTO reviewed_quote_choices VALUES (?,?,?)",
                                   [(d["session"], d["security_id"], canonical(d)) for d in normalizer.applied_quote_choices])
            connection.executemany("INSERT INTO suspension_repairs VALUES (?,?,?)",
                                   [(d["session"], d["symbol"], canonical(d)) for d in normalizer.applied_suspension_repairs])
            symbols = tuple(sorted(bar.symbol for bar, _, _, _, _, eligible, _ in rows if eligible))
            if len(symbols) >= 2:
                pool_source = source + (":eligibility:" + exclusions.manifest_id if exclusions else "")
                connection.execute("INSERT INTO universes VALUES (?,?,?)", (day.isoformat(), canonical(symbols), pool_source))
            connection.executemany("INSERT INTO issues VALUES (?,?,?)", [(day.isoformat(), s, reason) for s, reason in issues])
            connection.execute("INSERT INTO day_counts VALUES (?,?,?,?,?)",
                               (day.isoformat(), len(table.rows), len(rows), len(symbols), len(issues)))
            if index % 25 == 0:
                connection.commit()
                print(canonical({"normalized_days": index + 1, "total_days": len(sessions), "date": day}), flush=True)
        if quote_choices is not None:
            quote_choices.require_all_used()
        if suspension_repairs is not None:
            suspension_repairs.require_all_used()
        outcome_normalizer.finish()
        connection.executemany("INSERT INTO outcome_price_issues VALUES (?,?,?)",
            [(str(d), sid, why) for d, sid, why in outcome_normalizer.last_issues])
        if conversions is not None:
            build_exit_references(connection, conversions, identity_map)
            build_exit_references(connection, conversions, identity_map,
                                  bar_table="outcome_prices", target_table="outcome_exit_references")
        connection.execute("INSERT INTO metadata VALUES (?)", (canonical(metadata),))
        connection.commit()
    finally:
        connection.close()
    return metadata


class MarketStore:
    """Read only. Materialize a bounded date block using the shared definitions."""

    def __init__(self, path):
        path = Path(path).resolve()
        self.connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            records = self.connection.execute("SELECT document FROM metadata").fetchall()
            require(len(records) == 1, "CANONICAL_STORE_INCOMPLETE")
            self.metadata = json.loads(records[0][0])
            require(self.metadata["schema_version"] == STORE_VERSION, "STORE_VERSION_INVALID")
            require(self.metadata["outcome_reference_method"] == OUTCOME_REFERENCE_METHOD,
                    "STORE_OUTCOME_METHOD_INVALID")
            self.identity_map = IdentityMap.from_document(self.metadata["identity_map"])
            require(self.identity_map.map_id == self.metadata["identity_map_id"], "STORE_IDENTITY_MAP_MISMATCH")
            exclusions_doc = self.metadata["liquidation_exclusions"]
            exclusions = LiquidationStageExclusions(exclusions_doc, self.identity_map) if exclusions_doc is not None else None
            require((exclusions.manifest_id if exclusions else None) == self.metadata["liquidation_exclusions_id"],
                    "STORE_LIQUIDATION_EXCLUSIONS_MISMATCH")
            self.universe_id = LIQUIDATION_UNIVERSE_ID if exclusions else UNIVERSE_ID
            require(self.metadata["universe_id"] == self.universe_id, "STORE_UNIVERSE_RULE_MISMATCH")
            choices_doc = self.metadata["reviewed_quote_choices"]
            choice_id = ReviewedQuoteChoices(choices_doc, self.identity_map).manifest_id if choices_doc else None
            require(choice_id == self.metadata["reviewed_quote_choices_id"], "STORE_QUOTE_CHOICES_MISMATCH")
            repairs_doc = self.metadata["reviewed_suspension_repairs"]
            repairs_id = ReviewedSuspensionZeros(repairs_doc).manifest_id if repairs_doc else None
            require(repairs_id == self.metadata["reviewed_suspension_repairs_id"], "STORE_SUSPENSION_REPAIRS_MISMATCH")
            conversions = self.metadata["share_conversions"]
            if conversions is not None:
                validate_conversions(conversions, self.identity_map)
            require((fingerprint(conversions) if conversions is not None else None) == self.metadata["share_conversions_id"],
                    "STORE_CONVERSIONS_MISMATCH")
            require(fingerprint({k: v for k, v in self.metadata.items() if k != "source_snapshot_id"})
                    == self.metadata["source_snapshot_id"], "STORE_METADATA_HASH_MISMATCH")
            self.sessions = tuple(date.fromisoformat(r[0]) for r in self.connection.execute("SELECT session FROM sessions ORDER BY ordinal"))
            self.positions = {day: index for index, day in enumerate(self.sessions)}
        except Exception:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def read_day(self, day):
        """All qualifying members stay present, even with an unknown future outcome.

        A new chain after an unknown gap has an arbitrary base. Future values
        from that chain must not be divided by today's old chain values.
        """
        pos = self.positions[day]
        require(pos >= 79, "STORE_WARMUP_INCOMPLETE")
        start = self.sessions[pos - 60]
        end = self.sessions[min(pos + 20, len(self.sessions) - 1)]
        day_text = day.isoformat()
        # A block has one scoring date. Use its effective tickers consistently
        # across history, future outcomes and past universe snapshots; internal
        # stable IDs carry reference continuity across the code change itself.
        symbols_at_score = {}
        def projected_symbol(security_id):
            if security_id not in symbols_at_score:
                symbols_at_score[security_id] = self.identity_map.symbol_at(security_id, day)
            return symbols_at_score[security_id]
        current_chains = dict(self.connection.execute("SELECT security_id,chain_start FROM bars WHERE session=?", (day_text,)))
        bars = []
        query = "SELECT " + ",".join(STORE_BAR_COLUMNS) + ",chain_start FROM bars WHERE session BETWEEN ? AND ? ORDER BY session,security_id"
        for raw in self.connection.execute(query, (start.isoformat(), end.isoformat())):
            values = dict(zip(BAR_COLUMNS, raw[:-1]))
            values["session"] = date.fromisoformat(values["session"])
            if values["session"] > day and current_chains.get(values["symbol"]) != raw[-1]:
                continue
            values["symbol"] = projected_symbol(values["symbol"])
            values["available_at"] = datetime.fromisoformat(values["available_at"])
            values["tradable"] = bool(values["tradable"])
            bars.append(MarketBar(**values))
        snapshots = []
        for text, symbols, source in self.connection.execute(
                "SELECT session,symbols,source_id FROM universes WHERE session BETWEEN ? AND ? ORDER BY session",
                (self.sessions[pos - 19].isoformat(), day_text)):
            session = date.fromisoformat(text)
            snapshots.append(UniverseSnapshot(self.universe_id, market_time(session, 21, 30),
                             market_time(session, 21), tuple(sorted(projected_symbol(s) for s in json.loads(symbols))), source))
        outcome_quotes,references=self._outcome_slice(day,end,projected_symbol)
        return ResearchData(self.sessions, tuple(bars), tuple(snapshots), (day,), "REAL_DATA",
            exit_references=tuple(references), outcome_prices=tuple(outcome_quotes), outcome_prices_enabled=True)

    def _outcome_slice(self,day,end,projected_symbol,*,sessions_filter=None):
        day_text=day.isoformat()
        references = []
        outcome_anchors = dict(self.connection.execute(
            "SELECT security_id,chain_start FROM outcome_prices WHERE session=?", (day_text,)))
        outcome_quotes = []
        where='session>=? AND session<=?';arguments=[day_text,str(end)]
        if sessions_filter is not None:
            where+=' AND session IN ('+','.join('?' for _ in sessions_filter)+')'
            arguments.extend(sorted(sessions_filter))
        for sid, text, ready, ropen, rclose, source, chain in self.connection.execute(
                "SELECT security_id,session,available_at,return_open,return_close,source_id,chain_start "
                "FROM outcome_prices WHERE "+where+" ORDER BY session,security_id", arguments):
            if (sessions_filter is None or text in sessions_filter) and outcome_anchors.get(sid) == chain:
                outcome_quotes.append(OutcomePrice(projected_symbol(sid), date.fromisoformat(text),
                    datetime.fromisoformat(ready), ropen, rclose, source, outcome_basis_id(sid, chain)))
        for sid, text, ready, value, source, chain, method in self.connection.execute(
                "SELECT * FROM outcome_exit_references WHERE session>? AND session<=? ORDER BY session,security_id", (day_text, str(end))):
            if (sessions_filter is None or text in sessions_filter) and outcome_anchors.get(sid) == chain:
                references.append(ExitReference(projected_symbol(sid), date.fromisoformat(text), datetime.fromisoformat(ready),
                                                value, source, method, outcome_basis_id(sid, chain)))
        return outcome_quotes,references

    def read_label_day(self,day):
        """Read only the full declared pool and three label price positions.

        Uses the same price/basis reader as read_day; it cannot create features
        or serve as an alternative price normalization or eligibility path.
        """
        pos=self.positions[day]
        require(pos>=79,'STORE_WARMUP_INCOMPLETE')
        record=self.connection.execute('SELECT symbols,source_id FROM universes WHERE session=?',(str(day),)).fetchone()
        require(record is not None,'SCORING_UNIVERSE_MISSING')
        projected=lambda sid:self.identity_map.symbol_at(sid,day)
        pool=UniverseSnapshot(self.universe_id,market_time(day,21,30),market_time(day,21),
            tuple(sorted(projected(sid) for sid in json.loads(record[0]))),record[1])
        end=self.sessions[min(pos+20,len(self.sessions)-1)]
        wanted={str(day),str(end)}
        if pos+1<len(self.sessions):wanted.add(str(self.sessions[pos+1]))
        prices,refs=self._outcome_slice(day,end,projected,sessions_filter=wanted)
        return ResearchData(self.sessions,(),(pool,),(day,),"REAL_DATA",
            exit_references=tuple(refs),outcome_prices=tuple(prices),outcome_prices_enabled=True)



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--identity-map", help="Private, evidence-backed quant.security-identity-map/v1 JSON")
    parser.add_argument("--quote-choices", help="Private, reviewed exact-row choices; no raw price rewriting")
    parser.add_argument("--suspension-repairs", help="Private, reviewed exact-day suspension zero-trade reconstructions")
    parser.add_argument("--share-conversions", help="Private, verified conversion ratios for exit reference values only")
    parser.add_argument("--liquidation-exclusions", help="Private, reviewed publication/effectiveness dates for universe eligibility")
    args = parser.parse_args()
    identity_map = IdentityMap.from_document(json.loads(Path(args.identity_map).read_text())) if args.identity_map else None
    choices = json.loads(Path(args.quote_choices).read_text()) if args.quote_choices else None
    repairs = json.loads(Path(args.suspension_repairs).read_text()) if args.suspension_repairs else None
    conversions = json.loads(Path(args.share_conversions).read_text()) if args.share_conversions else None
    exclusions = json.loads(Path(args.liquidation_exclusions).read_text()) if args.liquidation_exclusions else None
    metadata = build_store(args.raw_root, args.output, identity_map=identity_map, quote_choices_document=choices,
                           suspension_repairs_document=repairs, share_conversions_document=conversions,
                           liquidation_exclusions_document=exclusions)
    print(canonical({"source_snapshot_id": metadata["source_snapshot_id"]}), flush=True)


if __name__ == "__main__":
    main()
