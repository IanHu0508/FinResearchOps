"""Read a normalized, versioned snapshot; no acquisition or silent coercion."""

from dataclasses import fields
from datetime import date, datetime

from quant.contracts import primitive, require
from .records import MarketBar, ResearchData, UniverseSnapshot

SCHEMA_VERSION = "quant.research-input/v2"
TABLES = {
    "bars": (MarketBar, ("session",), ("available_at",)),
    "universes": (UniverseSnapshot, (), ("as_of", "available_at")),
}


def to_document(data):
    return {"schema_version": SCHEMA_VERSION, **primitive(data)}


def from_document(document):
    require(isinstance(document, dict) and document.get("schema_version") == SCHEMA_VERSION,
            "INPUT_SCHEMA_INVALID")
    require(set(document) == {f.name for f in fields(ResearchData)} | {"schema_version"},
            "INPUT_FIELDS_INVALID")
    values = {name: document[name] for name in ("data_kind", "price_basis")}
    values["sessions"] = tuple(date.fromisoformat(d) for d in document["sessions"])
    values["scoring_dates"] = tuple(date.fromisoformat(d) for d in document["scoring_dates"])
    for name, (cls, dates, times) in TABLES.items():
        rows = []
        for raw in document[name]:
            require(set(raw) == {f.name for f in fields(cls)}, "INPUT_RECORD_FIELDS_INVALID:" + name)
            row = dict(raw)
            for key in dates:
                row[key] = date.fromisoformat(row[key]) if row[key] is not None else None
            for key in times:
                row[key] = datetime.fromisoformat(row[key])
            if "symbols" in row:
                row["symbols"] = tuple(row["symbols"])
            rows.append(cls(**row))
        values[name] = tuple(rows)
    return ResearchData(**values)
