"""Coverage diagnostics; current security-master facts never select the past pool."""

from collections import Counter
import hashlib
import json
from pathlib import Path

from quant.contracts import require
from .acquisition import read_cached
from .baostock import decode_table
from .market_store import MarketStore, canonical_symbol, is_a_share


def audit_store(store_path, raw_root, master_documents):
    """Compare raw daily coverage with dated listings, including later delistings.

    A current master is corroboration, not verified historical vendor vintage.
    This function reports differences and never removes a member or edits data.
    """
    master = {}
    pages = []
    for path in master_documents:
        doc = json.loads(Path(path).read_text())
        wire = Path(path).with_suffix(".bin").read_bytes()
        require(hashlib.sha256(wire).hexdigest() == doc["sha256"], "MASTER_HASH_MISMATCH")
        table = decode_table(wire, method="query_stock_basic", field_index=9)
        pages.append({"file": Path(path).name, "sha256": doc["sha256"], "rows": len(table.rows)})
        for values in table.rows:
            row = dict(zip(table.fields, values))
            if row["type"] == "1" and is_a_share(row["code"]):
                symbol = canonical_symbol(row["code"])
                require(symbol not in master, "DUPLICATE_MASTER_STOCK")
                master[symbol] = row
    require(bool(master), "SECURITY_MASTER_EMPTY")
    coverage = []
    with MarketStore(store_path) as store:
        for day in store.sessions:
            text = str(day)
            table = read_cached(Path(raw_root), "daily-" + text,
                                method="query_daily_history_k_AStock", row_index=4, field_index=5)
            ci = table.fields.index("code")
            symbols = {canonical_symbol(row[ci]) for row in table.rows if is_a_share(row[ci])}
            # Today's delisting metadata is used ONLY in this retrospective
            # completeness report, never in normalization or eligibility.
            expected = {s for s, r in master.items() if r["ipoDate"] and r["ipoDate"] <= text
                        and (not r["outDate"] or text <= r["outDate"])}
            coverage.append({"date": text, "raw_a_shares": len(symbols),
                "expected_from_current_master": len(expected),
                "master_expected_not_returned": sorted(expected - symbols),
                "raw_not_in_master": sorted(symbols - set(master)),
                "outside_master_listing_interval": sorted(symbols - expected),
                "later_delisted_observed": sum(bool(master[s]["outDate"]) and master[s]["outDate"] > text
                                                for s in symbols if s in master)})
        issue_counts = dict(store.connection.execute("SELECT reason,COUNT(*) FROM issues GROUP BY reason"))
        valid = store.connection.execute("SELECT COUNT(*),COUNT(DISTINCT symbol) FROM bars").fetchone()
        eligible = store.connection.execute("SELECT COUNT(*),COUNT(DISTINCT symbol) FROM bars WHERE eligible=1").fetchone()
        st_eligible = store.connection.execute("SELECT COUNT(*) FROM bars WHERE eligible=1 AND is_st='1'").fetchone()[0]
        counts = [{"date": d, "raw_rows": raw, "valid_rows": bars, "eligible_rows": pool, "issue_rows": issues}
                  for d, raw, bars, pool, issues in store.connection.execute("SELECT * FROM day_counts ORDER BY session")]
        observed = {r[0] for r in store.connection.execute("SELECT DISTINCT symbol FROM bars")}
        delisted = sorted(s for s in observed if s in master and master[s]["outDate"])
        absent = Counter(s for day in coverage for s in day["master_expected_not_returned"])
        return {"schema_version": "quant.market-quality-report/v1",
            "source_snapshot_id": store.metadata["source_snapshot_id"], "master_source_pages": pages,
            "raw_sessions": len(store.sessions), "valid_bars": valid[0], "observed_symbols": valid[1],
            "eligible_stock_dates_including_warmup_and_tail": eligible[0], "ever_eligible_symbols": eligible[1],
            "st_eligible_stock_dates": st_eligible, "later_delisted_observed_symbols": delisted,
            "normalization_issues": issue_counts, "daily_counts": counts, "listing_coverage": coverage,
            "master_expected_absent_counts": dict(absent),
            "limitations": ["Master listing dates are current-vintage retrospective corroboration.",
                            "No name or current listing status is used for sample eligibility.",
                            "Coverage differences are reported, not silently repaired or filtered.",
                            "Unverified prices, company actions, mergers and vendor revisions remain data-quality risks."]}
