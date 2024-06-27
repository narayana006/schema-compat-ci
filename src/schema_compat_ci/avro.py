"""Avro schema parsing and the reader/writer resolution check.

`can_read(reader, writer)` answers: can data written with `writer` be decoded
by a consumer using `reader`? The rules are the schema resolution rules of the
Avro specification, applied the way Avro's own compatibility checker applies
them (which is what Schema Registry runs for Avro subjects).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

PRIMITIVES = ("null", "boolean", "int", "long", "float", "double", "bytes", "string")
NAMED = ("record", "enum", "fixed")

# reader type -> writer types it can also decode (Avro type promotion)
PROMOTIONS = {
    "long": {"int"},
    "float": {"int", "long"},
    "double": {"int", "long", "float"},
    "string": {"bytes"},
    "bytes": {"string"},
}


class SchemaError(ValueError):
    """The text is not a valid Avro schema."""


@dataclass(eq=False)
class Field:
    name: str
    type: "Schema"
    has_default: bool
    aliases: tuple[str, ...] = ()


@dataclass(eq=False)
class Schema:
    type: str  # a primitive, or record / enum / fixed / array / map / union
    name: str | None = None  # full name of a named type
    aliases: frozenset = frozenset()  # full names
    fields: list[Field] = field(default_factory=list)  # record
    symbols: tuple[str, ...] = ()  # enum
    has_enum_default: bool = False  # enum
    size: int = 0  # fixed
    items: "Schema | None" = None  # array items / map values
    branches: list["Schema"] = field(default_factory=list)  # union

    @property
    def short_name(self) -> str:
        return (self.name or "").rpartition(".")[2]

    def describe(self) -> str:
        if self.type in NAMED:
            return f"{self.type} {self.name}"
        if self.type == "array":
            return f"array<{self.items.describe()}>"
        if self.type == "map":
            return f"map<{self.items.describe()}>"
        if self.type == "union":
            return "[" + ", ".join(b.describe() for b in self.branches) + "]"
        return self.type


def parse(text: str) -> Schema:
    """Parse the JSON text of an Avro schema (the content of an .avsc file)."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"not valid JSON: {exc}") from exc
    return _build(doc, None, {})


def _full_name(name: str, namespace: str | None) -> str:
    return name if "." in name or not namespace else f"{namespace}.{name}"


def _build(doc, namespace: str | None, names: dict[str, Schema]) -> Schema:
    if isinstance(doc, str):
        if doc in PRIMITIVES:
            return Schema(doc)
        full = _full_name(doc, namespace)
        if full not in names:
            raise SchemaError(f"unknown type {doc!r}")
        return names[full]
    if isinstance(doc, list):
        branches = [_build(b, namespace, names) for b in doc]
        seen = set()
        for branch in branches:
            if branch.type == "union":
                raise SchemaError("a union may not directly contain another union")
            key = branch.name if branch.type in NAMED else branch.type
            if key in seen:
                raise SchemaError(f"union contains {branch.describe()} more than once")
            seen.add(key)
        return Schema("union", branches=branches)
    if not isinstance(doc, dict) or "type" not in doc:
        raise SchemaError(f"expected a schema, got {json.dumps(doc)[:80]}")

    kind = doc["type"]
    if isinstance(kind, (dict, list)) or kind in PRIMITIVES:
        return _build(kind, namespace, names)  # {"type": "string", "logicalType": ...} and similar
    if kind == "array":
        if "items" not in doc:
            raise SchemaError("array schema without items")
        return Schema("array", items=_build(doc["items"], namespace, names))
    if kind == "map":
        if "values" not in doc:
            raise SchemaError("map schema without values")
        return Schema("map", items=_build(doc["values"], namespace, names))
    if kind == "error":
        kind = "record"
    if kind not in NAMED:
        return _build(kind, namespace, names)  # reference to a named type, or unknown

    name = doc.get("name")
    if not isinstance(name, str) or not name:
        raise SchemaError(f"{kind} schema without a name")
    if "." in name:
        own_namespace = name.rpartition(".")[0]
    else:
        own_namespace = doc.get("namespace", namespace) or None
    full = _full_name(name, own_namespace)
    if full in names:
        raise SchemaError(f"type {full!r} is defined twice")
    aliases = frozenset(_full_name(a, own_namespace) for a in doc.get("aliases", []))
    schema = Schema(kind, name=full, aliases=aliases)
    names[full] = schema  # registered before the fields so recursive records resolve

    if kind == "enum":
        symbols = doc.get("symbols")
        if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
            raise SchemaError(f"enum {full} needs a list of symbols")
        schema.symbols = tuple(symbols)
        schema.has_enum_default = doc.get("default") in symbols
    elif kind == "fixed":
        if not isinstance(doc.get("size"), int):
            raise SchemaError(f"fixed {full} needs an integer size")
        schema.size = doc["size"]
    else:
        fields = doc.get("fields")
        if not isinstance(fields, list):
            raise SchemaError(f"record {full} needs a list of fields")
        seen = set()
        for f in fields:
            if not isinstance(f, dict) or "name" not in f or "type" not in f:
                raise SchemaError(f"record {full} has a field without name or type")
            if f["name"] in seen:
                raise SchemaError(f"record {full} defines field {f['name']!r} twice")
            seen.add(f["name"])
            schema.fields.append(
                Field(
                    name=f["name"],
                    type=_build(f["type"], own_namespace, names),
                    has_default="default" in f,
                    aliases=tuple(f.get("aliases", [])),
                )
            )
    return schema


@dataclass(frozen=True)
class Issue:
    path: str  # where in the schema, e.g. Order.customer.zip
    kind: str  # missing_default, type_mismatch, name_mismatch, missing_symbols, size_mismatch, missing_branch
    # Text with {R} / {W} standing for the reader's and writer's schema.
    template: str
    field: str | None = None

    def render(self, reader: str, writer: str) -> str:
        return self.template.replace("{R}", reader).replace("{W}", writer)


def can_read(reader: Schema, writer: Schema) -> list[Issue]:
    """Every reason `reader` cannot decode data written with `writer` (empty: compatible)."""
    root = reader.short_name or writer.short_name or "(root)"
    return _check(reader, writer, root, frozenset())


def _names_match(reader: Schema, writer: Schema) -> bool:
    # Same rule as Avro's checker: the unqualified names are equal, or the
    # reader lists the writer's full name as an alias.
    return reader.short_name == writer.short_name or writer.name in reader.aliases


def _same_kind(reader: Schema, writer: Schema) -> bool:
    """Would the reader pick this type for the writer's data, before looking inside?"""
    if reader.type != writer.type:
        return writer.type in PROMOTIONS.get(reader.type, ())
    return reader.type not in NAMED or _names_match(reader, writer)


def _check(reader: Schema, writer: Schema, path: str, active: frozenset) -> list[Issue]:
    pair = (id(reader), id(writer))
    if pair in active:
        return []  # recursive type already being checked further up
    active = active | {pair}

    if writer.type == "union":
        # Any branch may turn up in the data, so the reader must handle each one.
        issues: list[Issue] = []
        for branch in writer.branches:
            issues += _check_against_reader(reader, branch, path, active, writer_is_union=True)
        return issues
    return _check_against_reader(reader, writer, path, active, writer_is_union=False)


def _check_against_reader(reader, writer, path, active, writer_is_union) -> list[Issue]:
    if reader.type == "union":
        results = [_check(branch, writer, path, active) for branch in reader.branches]
        if any(not r for r in results):
            return []
        # No branch works. If exactly one is the obvious counterpart, explain that one.
        close = [r for b, r in zip(reader.branches, results) if _same_kind(b, writer)]
        if len(close) == 1:
            return close[0]
        return [Issue(path, "missing_branch",
                      f"the {{W}} can write {writer.describe()} here, but the union "
                      f"{reader.describe()} in the {{R}} has no branch that can read it")]

    if reader.type != writer.type:
        if writer.type in PROMOTIONS.get(reader.type, ()):
            return []
        note = ""
        if reader.type in PROMOTIONS.get(writer.type, ()):
            note = f" (Avro promotes {reader.type} to {writer.type}, not the other way round)"
        where = "a union branch of the {W}" if writer_is_union else "the {W}"
        return [Issue(path, "type_mismatch",
                      f"{where} writes {writer.describe()}, which cannot be read as "
                      f"{reader.describe()} by the {{R}}{note}")]

    if reader.type in NAMED and not _names_match(reader, writer):
        return [Issue(path, "name_mismatch",
                      f"the {reader.type} is named {writer.name} in the {{W}} but {reader.name} in the "
                      f"{{R}}, and the {{R}} does not list {writer.name} as an alias")]
    if reader.type in ("array", "map"):
        return _check(reader.items, writer.items, path + ("[]" if reader.type == "array" else "{}"), active)
    if reader.type == "fixed":
        if reader.size != writer.size:
            return [Issue(path, "size_mismatch",
                          f"fixed size is {writer.size} in the {{W}} but {reader.size} in the {{R}}")]
        return []
    if reader.type == "enum":
        missing = [s for s in writer.symbols if s not in reader.symbols]
        if missing and not reader.has_enum_default:
            return [Issue(path, "missing_symbols",
                          f"enum symbol(s) {', '.join(missing)} can be written with the {{W}} but are unknown "
                          f"to the {{R}}, whose enum has no default symbol to fall back on")]
        return []
    if reader.type == "record":
        issues = []
        writer_fields = {f.name: f for f in writer.fields}
        for f in reader.fields:
            match = writer_fields.get(f.name)
            if match is None:
                match = next((writer_fields[a] for a in f.aliases if a in writer_fields), None)
            if match is not None:
                issues += _check(f.type, match.type, f"{path}.{f.name}", active)
            elif not f.has_default:
                issues.append(Issue(f"{path}.{f.name}", "missing_default",
                                    f"field '{f.name}' is in the {{R}} without a default but is not in the "
                                    f"{{W}}, so there is no value for it when reading data written with the {{W}}",
                                    field=f.name))
        return issues
    return []  # identical primitives
