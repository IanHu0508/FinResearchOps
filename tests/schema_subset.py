"""Check an instance against the JSON Schema 2020-12 keywords the published thesis schemas use.

A test helper, not a general validator: an unknown keyword raises, so a schema that needs more
than this subset fails loudly instead of passing unchecked. format is an annotation, as it is by
default in 2020-12. Returns (path, keyword) pairs for every failed assertion.
"""

import json
import re


_ANNOTATIONS = {"$schema", "$id", "$defs", "title", "description", "default", "format"}
_ASSERTIONS = {"$ref", "type", "const", "enum", "not", "oneOf", "anyOf", "allOf", "if", "then", "properties",
               "required", "additionalProperties", "items", "minItems", "maxItems", "uniqueItems", "minLength",
               "maxLength", "pattern", "minimum", "maximum"}
_TYPES = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
          "string": lambda v: isinstance(v, str), "null": lambda v: v is None,
          "boolean": lambda v: isinstance(v, bool),
          "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
          "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)}


def _same(value, expected):
    """JSON equality: numbers compare by value, but true and false are not numbers."""
    return value == expected and isinstance(value, bool) == isinstance(expected, bool)


def schema_errors(value, schema, root=None, path=()):
    root = schema if root is None else root
    if schema is True:
        return []
    if schema is False:
        return [(path, "false")]
    unknown = set(schema) - _ANNOTATIONS - _ASSERTIONS
    if unknown:
        raise ValueError(f"UNSUPPORTED_SCHEMA_KEYWORDS {sorted(unknown)} at {path}")
    errors = []
    if "$ref" in schema:
        if not schema["$ref"].startswith("#/$defs/"):
            raise ValueError(f"UNSUPPORTED_SCHEMA_REF {schema['$ref']}")
        errors += schema_errors(value, root["$defs"][schema["$ref"][len("#/$defs/"):]], root, path)
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[kind](value) for kind in kinds):
            return errors + [(path, "type")]
    if "const" in schema and not _same(value, schema["const"]):
        errors.append((path, "const"))
    if "enum" in schema and not any(_same(value, e) for e in schema["enum"]):
        errors.append((path, "enum"))
    if "not" in schema and not schema_errors(value, schema["not"], root, path):
        errors.append((path, "not"))
    if "oneOf" in schema and sum(not schema_errors(value, branch, root, path) for branch in schema["oneOf"]) != 1:
        errors.append((path, "oneOf"))
    if "anyOf" in schema and all(schema_errors(value, branch, root, path) for branch in schema["anyOf"]):
        errors.append((path, "anyOf"))
    for branch in schema.get("allOf", []):
        errors += schema_errors(value, branch, root, path)
    if "if" in schema and "then" in schema and not schema_errors(value, schema["if"], root, path):
        errors += schema_errors(value, schema["then"], root, path)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        errors += [(path + (key,), "required") for key in schema.get("required", []) if key not in value]
        for key, item in value.items():
            if key in properties:
                errors += schema_errors(item, properties[key], root, path + (key,))
            elif "additionalProperties" in schema:
                errors += schema_errors(item, schema["additionalProperties"], root, path + (key,))
    if isinstance(value, list):
        for i, item in enumerate(value):
            if "items" in schema:
                errors += schema_errors(item, schema["items"], root, path + (i,))
        if len(value) < schema.get("minItems", 0) or ("maxItems" in schema and len(value) > schema["maxItems"]):
            errors.append((path, "items"))
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            errors.append((path, "uniqueItems"))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or ("maxLength" in schema and len(value) > schema["maxLength"]):
            errors.append((path, "length"))
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append((path, "pattern"))
    if _TYPES["number"](value):
        if ("minimum" in schema and value < schema["minimum"]) or ("maximum" in schema and value > schema["maximum"]):
            errors.append((path, "range"))
    return errors
