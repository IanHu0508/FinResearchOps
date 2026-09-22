"""Contemporaneously disclosed liquidation-stage exclusions, never future survival."""
from datetime import date
import json

from quant.contracts import canonical, fingerprint, identifier, require

SCHEMA_VERSION = "quant.liquidation-stage-exclusions/v1"
UNIVERSE_ID = "cn-a-sh-sz-observed-tradable-61-session-history-reviewed-exclusions/v5"


class LiquidationStageExclusions:
    def __init__(self, document, identity_map):
        require(isinstance(document, dict) and set(document) == {
            "schema_version", "coverage_start", "coverage_end", "evidence_ids", "events"}
            and document["schema_version"] == SCHEMA_VERSION, "LIQUIDATION_SCHEMA_INVALID")
        self.start = date.fromisoformat(document["coverage_start"])
        self.end = date.fromisoformat(document["coverage_end"])
        require(self.start <= self.end, "LIQUIDATION_COVERAGE_INVALID")
        require(isinstance(document["events"], list), "LIQUIDATION_EVENTS_REQUIRED")
        self._evidence(document["evidence_ids"])
        self._document = json.loads(canonical(document))
        self.manifest_id = fingerprint(self._document)
        self.events = {}
        for event in self._document["events"]:
            require(isinstance(event, dict) and set(event) == {
                "symbol", "published_date", "effective_date", "evidence_ids"}, "LIQUIDATION_EVENT_INVALID")
            require(identifier(event["symbol"]), "LIQUIDATION_SYMBOL_REQUIRED")
            published = date.fromisoformat(event["published_date"])
            effective = date.fromisoformat(event["effective_date"])
            self._evidence(event["evidence_ids"])
            sid = identity_map.security_id(event["symbol"])
            require(sid not in self.events, "DUPLICATE_LIQUIDATION_SECURITY")
            self.events[sid] = (published, effective)

    @staticmethod
    def _evidence(value):
        require(isinstance(value, list) and value and all(identifier(v) for v in value),
                "LIQUIDATION_EVIDENCE_REQUIRED")

    @property
    def document(self):
        return json.loads(canonical(self._document))

    def excludes(self, security_id, session):
        require(self.start <= session <= self.end, "LIQUIDATION_COVERAGE_MISSING")
        event = self.events.get(security_id)
        # A date-only publication has no verified intraday timestamp. Apply it
        # from the following session, even if effectiveness is already stated.
        return event is not None and event[0] < session and event[1] <= session
