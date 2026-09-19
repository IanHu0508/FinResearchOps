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
from .records import MarketBar, ResearchData, UniverseSnapshot

REFERENCE_METHOD = "baostock-ex-reference-chain/v1"
UNIVERSE_ID = "cn-a-sh-sz-observed-tradable-61-session-history/v1"
AVAILABILITY_BASIS = "HISTORICAL_POST_CLOSE_RECONSTRUCTION_NOT_VENDOR_VINTAGE"


def is_a_share(code):
    return bool(re.fullmatch(r"(?:sh\.(?:60|68)\d{4}|sz\.(?:00|30)\d{4})", code))


def canonical_symbol(code):
    require(is_a_share(code), "NON_A_SHARE_SYMBOL")
    return code[3:] + "." + code[:2].upper()


class ReferenceNormalizer:
    """Forward-only reconstruction, with explicit reset after an unknown gap."""

    def __init__(self):
        self.previous = {}

    def day(self, session, ordinal, table, source_id):
        columns = table.fields
        required = {"date", "code", "open", "high", "low", "close", "preclose",
                    "volume", "amount", "adjustflag", "tradestatus", "isST"}
        require(required <= set(columns), "RAW_MARKET_COLUMNS_MISSING")
        values, issues = [], []
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
                    numeric["close"] * factor, row["tradestatus"] == "1", source_id,
                )
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


def build_store(raw_root, destination):
    """Consume a complete immutable acquisition; never build an incomplete cache."""
    raw_root, destination = _private_root(raw_root), _private_root(destination)
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
    require(all((raw_root / ("daily-" + day.isoformat() + ".json")).is_file() for day in sessions),
            "RAW_ACQUISITION_INCOMPLETE")
    raw_hashes = {}
    for day in sessions:
        name = "daily-" + day.isoformat()
        raw_hashes[name] = json.loads((raw_root / (name + ".json")).read_text())["sha256"]
    metadata = {
        "schema_version": "quant.canonical-market-store/v1", "data_kind": "REAL_DATA",
        "source_plan": plan, "source_hashes": raw_hashes, "universe_id": UNIVERSE_ID,
        "symbol_convention": "Six-digit exchange code plus .SH or .SZ; listing names are not identifiers.",
        "price_basis": "TOTAL_RETURN_REFERENCE", "return_reference_method": REFERENCE_METHOD,
        "availability_basis": AVAILABILITY_BASIS, "historical_vendor_vintage_verified": False,
        "assumed_available_local": "21:00 Asia/Shanghai", "scoring_local": "21:30 Asia/Shanghai",
        "formula": "F[t]=F[t-1]*raw_close[t-1]/preclose[t]; reference_OHLC=raw_OHLC*F[t]",
        "gap_policy": "UNKNOWN_GAP_STARTS_NEW_CHAIN; NO_TARGET_ACROSS_CHAIN_BREAK",
        "universe_policy": "Contemporaneously observed SH/SZ A shares, 61 consecutive valid session bars, currently trading with positive volume and amount; ST included; no current-listing filter.",
        "limitations": ["Latest-vintage historical market reconstruction, not verified historical vendor PIT.",
                        "Theoretical ex-price reference, not cash-flow, tax or execution returns.",
                        "No terminal-value fill; unavailable member outcome invalidates the whole target date.",
                        "Daily provider coverage is assessed separately; observed bars are not proof of all-market coverage."],
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
                symbol TEXT, session TEXT, available_at TEXT, open REAL, high REAL, low REAL,
                close REAL, volume REAL, amount REAL, return_open REAL, return_close REAL,
                tradable INTEGER, source_id TEXT, preclose REAL, factor REAL,
                chain_start TEXT, consecutive_sessions INTEGER, eligible INTEGER, is_st TEXT,
                PRIMARY KEY(session,symbol));
            CREATE INDEX bars_symbol_session ON bars(symbol,session);
            CREATE TABLE universes (session TEXT PRIMARY KEY, symbols TEXT NOT NULL, source_id TEXT NOT NULL);
            CREATE TABLE issues (session TEXT, symbol TEXT, reason TEXT);
            CREATE TABLE day_counts (session TEXT PRIMARY KEY, raw_rows INTEGER, valid_rows INTEGER,
                                     eligible_rows INTEGER, issue_rows INTEGER);
        """)
        connection.executemany("INSERT INTO sessions VALUES (?,?)",
                               [(i, day.isoformat()) for i, day in enumerate(sessions)])
        normalizer = ReferenceNormalizer()
        for index, day in enumerate(sessions):
            name = "daily-" + day.isoformat()
            table = read_cached(raw_root, name, method="query_daily_history_k_AStock", row_index=4, field_index=5)
            require(bool(table.rows), "RAW_MARKET_EMPTY")
            source = "baostock:" + raw_hashes[name]
            rows, issues = normalizer.day(day, index, table, source)
            records = []
            for bar, preclose, factor, chain, streak, eligible, is_st in rows:
                values = asdict(bar)
                values["session"], values["available_at"] = bar.session.isoformat(), bar.available_at.isoformat()
                records.append(tuple(values[n] for n in BAR_COLUMNS) + (preclose, factor, chain, streak, eligible, is_st))
            connection.executemany("INSERT INTO bars VALUES (" + ",".join("?" * 19) + ")", records)
            symbols = tuple(sorted(bar.symbol for bar, _, _, _, _, eligible, _ in rows if eligible))
            if len(symbols) >= 2:
                connection.execute("INSERT INTO universes VALUES (?,?,?)", (day.isoformat(), canonical(symbols), source))
            connection.executemany("INSERT INTO issues VALUES (?,?,?)", [(day.isoformat(), s, reason) for s, reason in issues])
            connection.execute("INSERT INTO day_counts VALUES (?,?,?,?,?)",
                               (day.isoformat(), len(table.rows), len(rows), len(symbols), len(issues)))
            if index % 25 == 0:
                connection.commit()
                print(canonical({"normalized_days": index + 1, "total_days": len(sessions), "date": day}), flush=True)
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
            require(self.metadata["schema_version"] == "quant.canonical-market-store/v1", "STORE_VERSION_INVALID")
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
        current_chains = dict(self.connection.execute("SELECT symbol,chain_start FROM bars WHERE session=?", (day_text,)))
        bars = []
        query = "SELECT " + ",".join(BAR_COLUMNS) + ",chain_start FROM bars WHERE session BETWEEN ? AND ? ORDER BY session,symbol"
        for raw in self.connection.execute(query, (start.isoformat(), end.isoformat())):
            values = dict(zip(BAR_COLUMNS, raw[:-1]))
            values["session"] = date.fromisoformat(values["session"])
            if values["session"] > day and current_chains.get(values["symbol"]) != raw[-1]:
                continue
            values["available_at"] = datetime.fromisoformat(values["available_at"])
            values["tradable"] = bool(values["tradable"])
            bars.append(MarketBar(**values))
        snapshots = []
        for text, symbols, source in self.connection.execute(
                "SELECT session,symbols,source_id FROM universes WHERE session BETWEEN ? AND ? ORDER BY session",
                (self.sessions[pos - 19].isoformat(), day_text)):
            session = date.fromisoformat(text)
            snapshots.append(UniverseSnapshot(UNIVERSE_ID, market_time(session, 21, 30),
                             market_time(session, 21), tuple(json.loads(symbols)), source))
        return ResearchData(self.sessions, tuple(bars), tuple(snapshots), (day,), "REAL_DATA")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    metadata = build_store(args.raw_root, args.output)
    print(canonical({"source_snapshot_id": metadata["source_snapshot_id"]}), flush=True)


if __name__ == "__main__":
    main()
