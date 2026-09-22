"""Exact, reviewed source-row choices; never a tolerance or price rewrite."""

from dataclasses import replace
from datetime import date
import re

from quant.contracts import canonical, fingerprint, identifier, require
import json

SCHEMA_VERSION = "quant.reviewed-quote-choices/v1"


class ReviewedQuoteChoices:
    def __init__(self, document, identity_map):
        require(isinstance(document, dict) and set(document) == {
            "schema_version", "identity_map_id", "row_fields", "choices"}
            and document["schema_version"] == SCHEMA_VERSION, "QUOTE_CHOICES_SCHEMA_INVALID")
        require(document["identity_map_id"] == identity_map.map_id, "QUOTE_CHOICES_IDENTITY_MISMATCH")
        fields = document["row_fields"]
        require(isinstance(fields, list) and fields and all(identifier(x) for x in fields)
                and fields == sorted(set(fields)), "QUOTE_CHOICE_FIELDS_INVALID")
        require(isinstance(document["choices"], list), "QUOTE_CHOICES_LIST_INVALID")
        self._document = json.loads(canonical(document))
        self._manifest_id = fingerprint(self._document)
        self.row_fields = tuple(fields)
        self.by_key = {}
        for choice in self._document["choices"]:
            require(isinstance(choice, dict) and set(choice) == {
                "session", "security_id", "source_id", "selected_symbol", "expected_row_sha256",
                "expected_conflicting_fields", "reason", "evidence_ids"}, "QUOTE_CHOICE_RECORD_INVALID")
            session = date.fromisoformat(choice["session"])
            sid, selected = choice["security_id"], choice["selected_symbol"]
            require(sid in identity_map.by_id and identity_map.security_id(selected) == sid
                    and identity_map.symbol_at(sid, session) == selected, "QUOTE_CHOICE_NOT_EFFECTIVE_CODE")
            require(identifier(choice["source_id"]) and identifier(choice["reason"])
                    and isinstance(choice["evidence_ids"], list) and choice["evidence_ids"]
                    and all(identifier(x) for x in choice["evidence_ids"]), "QUOTE_CHOICE_EVIDENCE_REQUIRED")
            expected = choice["expected_row_sha256"]
            require(isinstance(expected, dict) and selected in expected and len(expected) >= 2
                    and all(identity_map.security_id(s) == sid and isinstance(h, str)
                            and re.fullmatch(r"[0-9a-f]{64}", h) for s, h in expected.items()),
                    "QUOTE_CHOICE_ROW_BINDING_INVALID")
            conflicts = choice["expected_conflicting_fields"]
            require(isinstance(conflicts, list) and conflicts and all(identifier(x) for x in conflicts)
                    and conflicts == sorted(set(conflicts)), "QUOTE_CHOICE_CONFLICTS_INVALID")
            key = (session, sid)
            require(key not in self.by_key, "DUPLICATE_QUOTE_CHOICE")
            self.by_key[key] = choice
        self.used = set()

    @property
    def document(self):
        return json.loads(canonical(self._document))

    @property
    def manifest_id(self):
        return self._manifest_id

    def apply(self, session, decision, raw_by_symbol, source_id):
        key = (session, decision.security_id)
        choice = self.by_key.get(key)
        if choice is None:
            return decision, None
        require(key not in self.used, "QUOTE_CHOICE_ALREADY_USED")
        require(source_id == choice["source_id"], "QUOTE_CHOICE_SOURCE_CHANGED")
        require(set(decision.provider_symbols) == set(choice["expected_row_sha256"]),
                "QUOTE_CHOICE_ALIASES_CHANGED")
        require(tuple(choice["expected_conflicting_fields"]) == decision.conflicting_fields,
                "QUOTE_CHOICE_CONFLICT_CHANGED")
        for symbol, expected in choice["expected_row_sha256"].items():
            raw = raw_by_symbol[symbol]
            require(all(f in raw for f in self.row_fields), "QUOTE_CHOICE_RAW_FIELDS_MISSING")
            require(fingerprint({f: raw[f] for f in self.row_fields}) == expected,
                    "QUOTE_CHOICE_RAW_ROW_CHANGED")
        require(choice["selected_symbol"] not in decision.incomplete_symbols,
                "QUOTE_CHOICE_SELECTED_ROW_INCOMPLETE")
        self.used.add(key)
        receipt = {**choice, "choice_id": fingerprint(choice), "manifest_id": self.manifest_id}
        return replace(decision, selected_symbol=choice["selected_symbol"], conflicting_fields=()), receipt

    def require_all_used(self):
        require(self.used == set(self.by_key), "QUOTE_CHOICES_NOT_ALL_APPLIED")
