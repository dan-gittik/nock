"""Payload validation against the subset of JSON Schema nock's records and
generators use: type, required, properties, additionalProperties, items, enum,
const, pattern, bounds on numbers, strings and arrays, uniqueItems, anyOf and
oneOf. A record that does not fit its schema is a usage error, naming every problem."""

from __future__ import annotations

import difflib
import re
from typing import Any

from nock.errors import UsageError

TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}
ANNOTATIONS = {
    "$schema",
    "$id",
    "$comment",
    "title",
    "description",
    "default",
    "examples",
    "format",
}
KEYWORDS = {
    "type", "required", "properties", "additionalProperties", "items", "enum", "const",
    "pattern", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength",
    "maxLength", "minItems", "maxItems", "uniqueItems", "anyOf", "oneOf",
}  # fmt: skip


def validate(record: Any, schema: dict[str, Any]) -> None:
    """Refuse a payload that does not fit `schema`, naming every problem."""
    problems = Validator().problems(record, schema)
    if problems:
        raise UsageError(f"invalid record: {'; '.join(problems)}")


def check_schema(schema: Any) -> None:
    """Refuse a schema that uses what this validator does not understand, so a
    rule is never silently skipped."""
    problems = Validator().schema_problems(schema, "(root)")
    if problems:
        raise UsageError(f"not a schema nock can check: {'; '.join(problems)}")


class Validator:
    def problems(self, value: Any, schema: dict[str, Any], path: str = "") -> list[str]:
        where = path or "(root)"
        problems: list[str] = []
        expected = schema.get("type")
        if expected is not None:
            names = expected if isinstance(expected, list) else [expected]
            if not any(self.is_type(value, name) for name in names):
                return [f"{where}: {value!r} is not of type {' or '.join(map(repr, names))}"]
        if "const" in schema and value != schema["const"]:
            problems.append(f"{where}: {value!r} should be {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            problems.append(f"{where}: {value!r} is not one of {schema['enum']!r}")
        if isinstance(value, str):
            problems += self.string_problems(value, schema, where)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            problems += self.number_problems(value, schema, where)
        if isinstance(value, list):
            problems += self.array_problems(value, schema, path)
        if isinstance(value, dict):
            problems += self.object_problems(value, schema, path)
        if "anyOf" in schema and not any(
            not self.problems(value, option, path) for option in schema["anyOf"]
        ):
            problems.append(f"{where}: {value!r} fits none of the allowed shapes")
        if "oneOf" in schema:
            fits = sum(not self.problems(value, option, path) for option in schema["oneOf"])
            if fits != 1:
                problems.append(f"{where}: {value!r} fits {fits} of the shapes, not exactly one")
        return problems

    def is_type(self, value: Any, name: str) -> bool:
        if name in ("integer", "number") and isinstance(value, bool):
            return False
        if name == "integer" and isinstance(value, float):
            return value.is_integer()
        return isinstance(value, TYPES.get(name, ()))

    def string_problems(self, value: str, schema: dict[str, Any], where: str) -> list[str]:
        problems = []
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{where}: {value!r} is shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{where}: {value!r} is longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            problems.append(f"{where}: {value!r} does not match {schema['pattern']!r}")
        return problems

    def number_problems(self, value: float, schema: dict[str, Any], where: str) -> list[str]:
        bounds = [
            ("minimum", lambda bound: value < bound, "less than"),
            ("maximum", lambda bound: value > bound, "greater than"),
            ("exclusiveMinimum", lambda bound: value <= bound, "at most"),
            ("exclusiveMaximum", lambda bound: value >= bound, "at least"),
        ]
        return [
            f"{where}: {value!r} is {words} {schema[keyword]!r}"
            for keyword, broken, words in bounds
            if keyword in schema and broken(schema[keyword])
        ]

    def array_problems(self, value: list[Any], schema: dict[str, Any], path: str) -> list[str]:
        where = path or "(root)"
        problems = []
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{where}: has fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{where}: has more than {schema['maxItems']} items")
        if schema.get("uniqueItems") and any(
            item in value[:index] for index, item in enumerate(value)
        ):
            problems.append(f"{where}: has repeated items")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                problems += self.problems(item, schema["items"], f"{path}/{index}".lstrip("/"))
        return problems

    def object_problems(
        self, value: dict[str, Any], schema: dict[str, Any], path: str
    ) -> list[str]:
        where = path or "(root)"
        properties: dict[str, Any] = schema.get("properties") or {}
        problems = [
            f"{where}: {name!r} is a required property"
            for name in schema.get("required") or []
            if name not in value
        ]
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            inner = f"{path}/{name}".lstrip("/")
            if name in properties:
                problems += self.problems(item, properties[name], inner)
            elif extra is False:
                close = difflib.get_close_matches(name, list(properties), 1, 0.6)
                hint = f" — did you mean {close[0]!r}?" if close else ""
                problems.append(f"{where}: unexpected property {name!r}{hint}")
            elif isinstance(extra, dict):
                problems += self.problems(item, extra, inner)
        return problems

    def schema_problems(self, schema: Any, where: str) -> list[str]:
        if not isinstance(schema, dict):
            return [f"{where}: a schema is an object"]
        problems = [
            f"{where}: {keyword!r} is not supported"
            for keyword in schema
            if keyword not in KEYWORDS and keyword not in ANNOTATIONS
        ]
        names = schema.get("type", [])
        for name in names if isinstance(names, list) else [names]:
            if name not in TYPES:
                problems.append(f"{where}: unknown type {name!r}")
        for name, inner in (schema.get("properties") or {}).items():
            problems += self.schema_problems(inner, f"{where}/{name}")
        for keyword in ("items", "additionalProperties"):
            if isinstance(schema.get(keyword), dict):
                problems += self.schema_problems(schema[keyword], f"{where}/{keyword}")
        for keyword in ("anyOf", "oneOf"):
            for index, inner in enumerate(schema.get(keyword) or []):
                problems += self.schema_problems(inner, f"{where}/{keyword}/{index}")
        if "pattern" in schema:
            try:
                re.compile(schema["pattern"])
            except re.error as error:
                problems.append(f"{where}: pattern does not compile: {error}")
        return problems
