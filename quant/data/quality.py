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
                "codes_with_later_master_outdate": sum(bool(master[s]["outDate"]) and master[s]["outDate"] > text
                                                for s in symbols if s in master)})
        issue_counts = dict(store.connection.execute("SELECT reason,COUNT(*) FROM issues GROUP BY reason"))
        outcome_counts = store.connection.execute(
            "SELECT COUNT(*),SUM(price_only),SUM(confirmation_at IS NOT NULL) FROM outcome_prices").fetchone()
        outcome_issues = dict(store.connection.execute(
            "SELECT reason,COUNT(*) FROM outcome_price_issues GROUP BY reason"))
        valid = store.connection.execute("SELECT COUNT(*),COUNT(DISTINCT security_id) FROM bars").fetchone()
        eligible = store.connection.execute("SELECT COUNT(*),COUNT(DISTINCT security_id) FROM bars WHERE eligible=1").fetchone()
        st_eligible = store.connection.execute("SELECT COUNT(*) FROM bars WHERE eligible=1 AND is_st='1'").fetchone()[0]
        counts = [{"date": d, "raw_rows": raw, "valid_rows": bars, "eligible_rows": pool, "issue_rows": issues}
                  for d, raw, bars, pool, issues in store.connection.execute("SELECT * FROM day_counts ORDER BY session")]
        observed = {r[0] for r in store.connection.execute("SELECT DISTINCT trading_symbol FROM bars")}
        retired_codes = sorted(s for s in observed if s in master and master[s]["outDate"])
        absent = Counter(s for day in coverage for s in day["master_expected_not_returned"])
        decisions = [json.loads(r[0]) for r in store.connection.execute("SELECT document FROM identity_decisions")]
        return {"schema_version": "quant.market-quality-report/v3",
            "source_snapshot_id": store.metadata["source_snapshot_id"], "master_source_pages": pages,
            "raw_sessions": len(store.sessions), "valid_bars": valid[0], "observed_security_ids": valid[1],
            "observed_trading_symbols": len(observed),
            "eligible_stock_dates_including_warmup_and_tail": eligible[0], "ever_eligible_security_ids": eligible[1],
            "st_eligible_stock_dates": st_eligible, "observed_codes_with_master_outdate": retired_codes,
            "identity_map_id": store.metadata["identity_map_id"],
            "all_security_identities_verified": False,
            "identity_decision_count": len(decisions),
            "identity_conflict_dates": sum(bool(d["conflicting_fields"]) for d in decisions),
            "reconciled_extra_alias_rows_removed": sum(len(d["provider_symbols"])-1 for d in decisions if d["selected_symbol"] is not None),
            "incomplete_alias_observations": sum(len(d["incomplete_symbols"]) for d in decisions),
            "normalization_issues": issue_counts, "daily_counts": counts, "listing_coverage": coverage,
            "outcome_price_rows": outcome_counts[0],
            "outcome_price_only_rows": outcome_counts[1] or 0,
            "outcome_rows_requiring_later_confirmation": outcome_counts[2] or 0,
            "outcome_price_issues": outcome_issues,
            "master_expected_absent_counts": dict(absent),
            "limitations": ["Master listing dates are current-vintage retrospective corroboration.",
                            "No name or current listing status is used for sample eligibility.",
                            "Master outDate counts are retired codes, including ticker changes, not confirmed economic delistings.",
                            "Raw code/master coverage is separate from stable-security grain; unmapped identities remain provisional.",
                            "Coverage differences are reported, not silently repaired or filtered.",
                            "Unverified prices, company actions, mergers and vendor revisions remain data-quality risks.",
                            "Outcome-only prices never restore missing feature activity; later confirmations delay label availability and do not certify listing continuity or historical vendor vintage."]}
