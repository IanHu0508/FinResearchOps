"""Explicit, resumable BaoStock daily acquisition into the sibling private tree.

This command only preserves provider responses. It neither certifies PIT quality
nor constructs labels, and never imports a model or changes provider data.
"""

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import time

from quant.artifacts.store import _private_root
from quant.contracts import canonical, require
from .baostock import BaoStockClient, decode_table


def read_cached(root, name, *, method, row_index=6, field_index=8):
    meta = json.loads((root / (name + ".json")).read_text())
    wire = (root / (name + ".bin")).read_bytes()
    require(hashlib.sha256(wire).hexdigest() == meta["sha256"], "RAW_CACHE_HASH_MISMATCH")
    table = decode_table(wire, method=method, row_index=row_index, field_index=field_index)
    require(len(table.rows) == meta["row_count"], "RAW_CACHE_COUNT_MISMATCH")
    return table


def save_response(root, name, table, *, query, seconds):
    """Append once. An interrupted half-pair is an error, never a cache hit."""
    for suffix in (".bin", ".json"):
        require(not (root / (name + suffix)).exists(), "RAW_RESPONSE_ALREADY_EXISTS")
    with (root / (name + ".bin")).open("xb") as stream:
        stream.write(table.wire)
    metadata = {
        "schema_version": "quant.baostock-raw-response/v1",
        "captured_at": datetime.now(timezone.utc).isoformat(), "query": query,
        "row_count": len(table.rows), "fields": table.fields,
        "sha256": hashlib.sha256(table.wire).hexdigest(),
        "wire_bytes": len(table.wire), "seconds": round(seconds, 4),
        "historical_vendor_vintage_verified": False,
    }
    with (root / (name + ".json")).open("x") as stream:
        stream.write(canonical(metadata) + "\n")


def collect(root, start, end, *, pause_seconds=1.0, max_requests=2000, response_timeout=90):
    root = _private_root(root)
    require(type(start) is date and type(end) is date and start <= end, "RAW_DATES_INVALID")
    require(1 <= pause_seconds <= 60, "ACQUISITION_PACING_INVALID")
    require(type(max_requests) is int and max_requests > 0, "REQUEST_BUDGET_INVALID")
    require(type(response_timeout) in (int, float) and 1 <= response_timeout <= 120, "RESPONSE_TIMEOUT_INVALID")
    root.mkdir(parents=True, exist_ok=True)
    plan = {"schema_version": "quant.baostock-acquisition/v1", "provider": "BaoStock",
            "start": start.isoformat(), "end": end.isoformat(),
            "method": "query_daily_history_k_AStock", "adjustment": "UNADJUSTED",
            "execution": "ONE_CONNECTION_SEQUENTIAL", "minimum_pause_seconds": pause_seconds}
    plan_path = root / "plan.json"
    if plan_path.exists():
        require(json.loads(plan_path.read_text()) == plan, "RAW_PLAN_MISMATCH")
    else:
        with plan_path.open("x") as stream:
            stream.write(canonical(plan) + "\n")
    count = 0
    with BaoStockClient(pause_seconds=pause_seconds, timeout=response_timeout) as client:
        calendar_rows = []
        for page in range(1, 100):
            name = "calendar-p" + str(page)
            if (root / (name + ".json")).exists():
                table = read_cached(root, name, method="query_trade_dates", field_index=9)
            else:
                require(count < max_requests, "REQUEST_BUDGET_EXHAUSTED")
                began = time.monotonic()
                table = client.calendar(start.isoformat(), end.isoformat(), page)
                count += 1
                save_response(root, name, table, query={"method": "query_trade_dates", "page": page,
                              "start": start.isoformat(), "end": end.isoformat()},
                              seconds=time.monotonic() - began)
            require(table.fields == ("calendar_date", "is_trading_day"), "CALENDAR_FIELDS_CHANGED")
            calendar_rows.extend(table.rows)
            if len(table.rows) < 2000:
                break
        else:
            raise ValueError("CALENDAR_PAGINATION_LIMIT")
        expected_calendar = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        require([row[0] for row in calendar_rows] == expected_calendar
                and all(row[1] in ("0", "1") for row in calendar_rows), "RAW_CALENDAR_INCOMPLETE")
        days = [day for day, trading in calendar_rows if trading == "1"]
        require(days and days == sorted(set(days)), "RAW_CALENDAR_INVALID")
        require(all(start <= date.fromisoformat(day) <= end for day in days), "RAW_CALENDAR_RANGE_INVALID")
        for index, day in enumerate(days):
            name = "daily-" + day
            if (root / (name + ".json")).exists():
                table = read_cached(root, name, method="query_daily_history_k_AStock", row_index=4, field_index=5)
            else:
                require(count < max_requests, "REQUEST_BUDGET_EXHAUSTED")
                began = time.monotonic()
                table = client.daily(day)
                count += 1
                save_response(root, name, table, query={"method": "query_daily_history_k_AStock", "date": day},
                              seconds=time.monotonic() - began)
            require("date" in table.fields and "code" in table.fields, "DAILY_FIELDS_CHANGED")
            di, ci = table.fields.index("date"), table.fields.index("code")
            require(bool(table.rows) and all(row[di] == day for row in table.rows), "DAILY_DATE_OR_EMPTY_INVALID")
            require(len({row[ci] for row in table.rows}) == len(table.rows), "DAILY_DUPLICATE_SYMBOL")
            print(canonical({"completed_days": index + 1, "total_days": len(days),
                             "date": day, "rows": len(table.rows), "new_requests": count}), flush=True)
    return {"completed_days": len(days), "new_requests": count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    print(canonical(collect(args.root, args.start, args.end)), flush=True)


if __name__ == "__main__":
    main()
