"""Format failures of v18/v19 answers, proven from the saved answer with the standard library only.

Live stages and the Case reader call the same functions, so a retry is granted
only when the saved first answer demonstrably cannot be used: it cannot be
parsed, it lacks a required reason, a basis label lies outside its vocabulary,
or (protocol 19) it violates the frozen stage schema in any other way.
Content, citation, coverage and numeric-contract failures are never format
failures. The stage schemas are identical in protocols 18 and 19.
"""

from copy import deepcopy
from datetime import date
import json
import math
import re

from finauditgate.core.artifacts import canonical_json_bytes
from .thesis_schemas_v18 import SCHEMAS


# No single object can be read. A recognized Markdown layout with an invalid
# rating or row, or valid JSON holding competing values, is content and halts.
UNPARSEABLE_CODES = frozenset({"THESIS_RESEARCH_MARKDOWN_LAYOUT_UNSUPPORTED", "THESIS_RESPONSE_FRAGMENTS_CONFLICT"})
# Provenance labels only: no rating, disposition, status, identity, metric or sign.
REPAIRABLE_ENUMS = frozenset({"basis_type", "correction_basis", "update_basis"})


def schema_errors(kind, value):
    """(type, path, allowed values) where value violates the frozen v18 schema."""
    schema = SCHEMAS[kind]
    return _errors(value, schema, schema.get("$defs", {}), ())


def _errors(value, schema, defs, path):
    while "$ref" in schema:
        schema = defs[schema["$ref"].rsplit("/", 1)[-1]]
    if "anyOf" in schema:
        branches = [_errors(value, branch, defs, path) for branch in schema["anyOf"]]
        return [] if any(not branch for branch in branches) else min(branches, key=len)
    if "const" in schema and value != schema["const"]:
        return [("const", path, None)]
    if "enum" in schema and value not in schema["enum"]:
        return [("enum", path, tuple(schema["enum"]))]
    kind = schema.get("type")
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
               "null": value is None, "boolean": isinstance(value, bool),
               "integer": isinstance(value, int) and not isinstance(value, bool),
               "number": isinstance(value, (int, float)) and not isinstance(value, bool)}
    if kind is not None and not matches[kind]:
        return [("type", path, None)]
    found = []
    if kind == "object":
        properties = schema.get("properties", {})
        found += [("missing", path + (name,), None) for name in schema.get("required", []) if name not in value]
        for name, item in value.items():
            if name in properties:
                found += _errors(item, properties[name], defs, path + (name,))
            elif schema.get("additionalProperties") is False:
                found.append(("extra", path + (name,), None))
    elif kind == "array":
        if len(value) > schema.get("maxItems", len(value)):
            found.append(("too_long", path, None))
        if len(value) < schema.get("minItems", 0):
            found.append(("too_short", path, None))
        for index, item in enumerate(value):
            found += _errors(item, schema.get("items", {}), defs, path + (index,))
    elif kind == "string":
        if len(value) > schema.get("maxLength", len(value)):
            found.append(("string_long", path, None))
        if len(value) < schema.get("minLength", 0):
            found.append(("string_short", path, None))
        if "pattern" in schema and not re.search(_anchored(schema["pattern"]), value):
            found.append(("pattern", path, None))
        if schema.get("format") == "date":
            try:
                valid = re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None and bool(date.fromisoformat(value))
            except ValueError:
                valid = False
            if not valid:
                found.append(("date", path, None))
    elif kind in ("number", "integer"):
        if isinstance(value, float) and not math.isfinite(value):
            found.append(("finite", path, None))
        elif (("minimum" in schema and value < schema["minimum"]) or ("maximum" in schema and value > schema["maximum"])
                or ("exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"])
                or ("exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"])):
            found.append(("range", path, None))
    return found


def _anchored(pattern):
    """Pydantic's regex engine ends `$` at the end of text; Python's also before a final newline."""
    return pattern[:-1] + r"\Z" if pattern.endswith("$") and not pattern.endswith(r"\$") else pattern


def _at(value, path):
    for key in path:
        value = value[key]
    return value


def _drop_notes(value, blank):
    if isinstance(value, list):
        for item in value:
            _drop_notes(item, blank)
    elif isinstance(value, dict):
        for key in [k for k in value if k.endswith("_note") and k != "_note"]:
            stem, note = key[:-len("_note")], value[key]
            if (note is None or (blank and isinstance(note, str) and not note.strip())
                    or (stem in value and canonical_json_bytes(note) == canonical_json_bytes(value[stem]))):
                del value[key]
        for child in value.values():
            _drop_notes(child, blank)


def normalize_format(value, kind, protocol_version=18):
    """Drop `*_note` fields that are null or repeat their named sibling exactly.

    Protocol 19 also drops empty or whitespace-only notes. No stage schema has
    such a property, so these fields are always extra and removing them loses
    no information. A note with any other content stays for the schema to refuse.
    """
    value = deepcopy(value)
    _drop_notes(value, blank=protocol_version >= 19)
    return value


def format_failure(call, kind, protocol_version=18):
    """Why a saved v18/v19 answer cannot be used: (reason, paths), or (None, []) for no retry."""
    from .thesis_recovery import missing_reason_paths
    from .thesis_responses import response_candidate
    if call.get("error_type") or not call.get("output") or kind not in SCHEMAS:
        return None, []
    try:
        candidate = response_candidate(call["output"], kind, protocol_version=protocol_version)
    except json.JSONDecodeError:
        return "UNPARSEABLE", []
    except ValueError as exc:
        return ("UNPARSEABLE", []) if str(exc) in UNPARSEABLE_CODES else (None, [])
    except (TypeError, KeyError, AttributeError):
        return None, []
    errors = schema_errors(kind, candidate)
    if not errors:
        return None, []
    paths = missing_reason_paths(candidate, kind)
    if paths and all(error == "missing" for error, _, _ in errors) and {p for _, p, _ in errors} == {tuple(p) for p in paths}:
        return "MISSING_REASON", paths
    paths = format_paths(candidate, kind)
    if paths:
        return "ENUM_INVALID", paths
    # Protocol 19: any other proven schema error is asked again with the unchanged prompt; the
    # recovery row keeps each (error type, path) so the reader proves exactly the same list.
    if protocol_version >= 19:
        return "SCHEMA_INVALID", [[error, *path] for error, path, _ in errors]
    return None, []


def enum_repair_messages(base, kind, candidate, paths):
    allowed = {path: list(values) for error, path, values in schema_errors(kind, candidate) if error == "enum"}
    items = [{"path": path, "value": _at(candidate, path), "allowed": allowed[tuple(path)]} for path in paths]
    prompt = deepcopy(base)
    prompt[0]["content"] += ("\n本次仅修正上一响应中不在候选值内的依据类型标签。返回同一完整JSON对象；"
        "只把下列路径改为该路径的候选值之一，其他所有字段、列表顺序、数值、评级、编号和文字逐字不变，不重新研究或改写。"
        "路径、原取值、候选值及原响应：\n"
        + canonical_json_bytes({"enum_paths": items, "previous_response": candidate}).decode())
    return prompt


def check_enum_repair(before, after, paths, kind):
    """Only the listed labels may change, and each must now be a listed value."""
    if not paths or format_paths(before, kind) != paths:
        raise ValueError("THESIS_REPAIR_PATHS_INVALID")
    restored = deepcopy(after)
    try:
        for path in paths:
            parent = _at(restored, path[:-1])
            if not isinstance(parent, dict) or not isinstance(parent.get(path[-1]), str):
                raise ValueError("THESIS_REPAIR_ENUM_REQUIRED")
            parent[path[-1]] = _at(before, path)
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("THESIS_REPAIR_ENUM_REQUIRED") from exc
    if canonical_json_bytes(restored) != canonical_json_bytes(before):
        raise ValueError("THESIS_REPAIR_CHANGED_EXISTING_CONTENT")
    targets = {tuple(path) for path in paths}
    if any(path in targets for _, path, _ in schema_errors(kind, after)):
        raise ValueError("THESIS_REPAIR_ENUM_INVALID")


def format_paths(candidate, kind):
    """Repairable label paths of a parsed candidate, in validator order."""
    errors = schema_errors(kind, candidate)
    enums = [list(path) for error, path, _ in errors if error == "enum"]
    if not errors or len(enums) != len(errors) or not all(
            p[-1] in REPAIRABLE_ENUMS and isinstance(_at(candidate, p), str) for p in enums):
        return []
    return enums
