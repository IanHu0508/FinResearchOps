"""Reviewed full-day suspension facts can establish zero trading activity.

This does not fill arbitrary missing data, create prices, or value a delisted
security. Each reconstruction binds an exact original quote and dated evidence.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re

from quant.contracts import canonical, fingerprint, identifier, require

SCHEMA_VERSION = "quant.reviewed-suspension-zeros/v1"


class ReviewedSuspensionZeros:
    def __init__(self, document):
        require(isinstance(document, dict) and set(document) == {"schema_version", "row_fields", "repairs"}
                and document["schema_version"] == SCHEMA_VERSION, "SUSPENSION_REPAIRS_SCHEMA_INVALID")
        fields = document["row_fields"]
        require(isinstance(fields, list) and fields == sorted(set(fields)) and fields
                and all(identifier(f) for f in fields), "SUSPENSION_REPAIR_FIELDS_INVALID")
        require({"date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "tradestatus"}
                <= set(fields), "SUSPENSION_REPAIR_FIELDS_INCOMPLETE")
        require(isinstance(document["repairs"], list), "SUSPENSION_REPAIRS_LIST_INVALID")
        self._document = json.loads(canonical(document))
        self.manifest_id = fingerprint(self._document)
        self.row_fields = tuple(fields)
        self.by_key = {}
        for item in self._document["repairs"]:
            require(isinstance(item, dict) and set(item) == {
                "session", "symbol", "source_id", "original_row_sha256", "announcement_date", "evidence_ids", "reason"},
                "SUSPENSION_REPAIR_RECORD_INVALID")
            session = date.fromisoformat(item["session"])
            require(date.fromisoformat(item["announcement_date"]) <= session, "SUSPENSION_EVIDENCE_AFTER_SESSION")
            require(all(identifier(item[k]) for k in ("symbol", "source_id", "reason"))
                    and item["reason"] == "VERIFIED_FULL_DAY_SUSPENSION_ZERO_TRADES", "SUSPENSION_REPAIR_REASON_INVALID")
            require(isinstance(item["original_row_sha256"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", item["original_row_sha256"]), "SUSPENSION_REPAIR_HASH_INVALID")
            require(isinstance(item["evidence_ids"], list) and item["evidence_ids"]
                    and all(identifier(x) for x in item["evidence_ids"]), "SUSPENSION_EVIDENCE_REQUIRED")
            key = (session, item["symbol"])
            require(key not in self.by_key, "DUPLICATE_SUSPENSION_REPAIR")
            self.by_key[key] = item
        self.used = set()

    @property
    def document(self):
        return json.loads(canonical(self._document))

    def apply(self, session, symbol, row, source_id):
        key = (session, symbol)
        item = self.by_key.get(key)
        if item is None:
            return row, None
        require(key not in self.used, "SUSPENSION_REPAIR_ALREADY_USED")
        require(source_id == item["source_id"], "SUSPENSION_SOURCE_CHANGED")
        require(all(f in row for f in self.row_fields)
                and fingerprint({f: row[f] for f in self.row_fields}) == item["original_row_sha256"],
                "SUSPENSION_RAW_ROW_CHANGED")
        require(row["tradestatus"] == "0", "SUSPENSION_REPAIR_REQUIRES_HALTED_QUOTE")
        try:
            prices = [Decimal(row[f]) for f in ("open", "high", "low", "close", "preclose")]
            coherent = all(p.is_finite() and p > 0 and p == prices[0] for p in prices)
            amount_empty_or_zero = row["amount"] == "" or Decimal(row["amount"]) == 0
        except InvalidOperation:
            coherent = amount_empty_or_zero = False
        require(coherent and amount_empty_or_zero, "SUSPENSION_REPAIR_NOT_ZERO_TRADE_REFERENCE")
        corrected = {**row, "volume": "0", "amount": "0.0000"}
        self.used.add(key)
        receipt = {**item, "manifest_id": self.manifest_id,
                   "original_fields": {f: row[f] for f in ("volume", "amount")},
                   "reconstructed_fields": {f: corrected[f] for f in ("volume", "amount")},
                   "price_fields_unchanged": True}
        return corrected, receipt

    def require_all_used(self):
        require(self.used == set(self.by_key), "SUSPENSION_REPAIRS_NOT_ALL_APPLIED")
