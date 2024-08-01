"""Reader/writer resolution rules from the Avro specification."""

import json

import pytest

from schema_compat_ci.avro import SchemaError, can_read, parse


def schema(doc):
    return parse(json.dumps(doc))


def record(*fields, name="R", **extra):
    return {"type": "record", "name": name, "fields": list(fields), **extra}


def f(name, type_, **extra):
    return {"name": name, "type": type_, **extra}


def kinds(reader, writer):
    return [i.kind for i in can_read(schema(reader), schema(writer))]


PROMOTABLE = {("int", "long"), ("int", "float"), ("int", "double"), ("long", "float"),
              ("long", "double"), ("float", "double"), ("string", "bytes"), ("bytes", "string")}
PRIMITIVES = ["null", "boolean", "int", "long", "float", "double", "bytes", "string"]


@pytest.mark.parametrize("writer", PRIMITIVES)
@pytest.mark.parametrize("reader", PRIMITIVES)
def test_primitive_promotion_matrix(reader, writer):
    ok = reader == writer or (writer, reader) in PROMOTABLE
    assert (kinds(reader, writer) == []) is ok


def test_promotion_inside_record_array_and_map():
    assert kinds(record(f("a", "long")), record(f("a", "int"))) == []
    assert kinds(record(f("a", "int")), record(f("a", "long"))) == ["type_mismatch"]
    assert kinds({"type": "array", "items": "double"}, {"type": "array", "items": "float"}) == []
    assert kinds({"type": "map", "values": "int"}, {"type": "map", "values": "long"}) == ["type_mismatch"]
    assert kinds({"type": "array", "items": "int"}, {"type": "map", "values": "int"}) == ["type_mismatch"]


def test_logical_type_annotations_use_the_underlying_type():
    ts = {"type": "long", "logicalType": "timestamp-millis"}
    assert kinds(record(f("at", ts)), record(f("at", "long"))) == []
    assert kinds(record(f("at", ts)), record(f("at", "int"))) == []


def test_field_added_needs_a_default_to_read_old_data():
    old = record(f("id", "string"))
    assert kinds(record(f("id", "string"), f("x", "int")), old) == ["missing_default"]
    assert kinds(record(f("id", "string"), f("x", "int", default=0)), old) == []
    assert kinds(record(f("id", "string"), f("x", ["null", "int"], default=None)), old) == []
    # a nullable type alone is not a default
    assert kinds(record(f("id", "string"), f("x", ["null", "int"])), old) == ["missing_default"]


def test_field_removed_is_ignored_by_the_reader():
    assert kinds(record(f("id", "string")), record(f("id", "string"), f("gone", "int"))) == []


def test_field_order_does_not_matter():
    assert kinds(record(f("a", "int"), f("b", "string")), record(f("b", "string"), f("a", "int"))) == []


def test_field_rename_needs_a_reader_alias():
    writer = record(f("old_name", "string"))
    assert kinds(record(f("new_name", "string")), writer) == ["missing_default"]
    assert kinds(record(f("new_name", "string", aliases=["old_name"])), writer) == []
    # the alias also has to resolve to a readable type
    assert kinds(record(f("new_name", "int", aliases=["old_name"])), writer) == ["type_mismatch"]


def test_issue_paths_point_at_the_nested_field():
    inner_old = record(f("zip", "string"), name="Address")
    inner_new = record(f("zip", "int"), name="Address")
    old = record(f("addresses", {"type": "array", "items": inner_old}), name="Customer")
    new = record(f("addresses", {"type": "array", "items": inner_new}), name="Customer")
    (issue,) = can_read(schema(new), schema(old))
    assert issue.path == "Customer.addresses[].zip" and issue.kind == "type_mismatch"
    assert "old" in issue.render("new schema", "old schema")


def test_record_name_must_match_or_be_aliased():
    writer = record(f("a", "int"), name="Old", namespace="ns")
    assert kinds(record(f("a", "int"), name="New", namespace="ns"), writer) == ["name_mismatch"]
    assert kinds(record(f("a", "int"), name="New", namespace="ns", aliases=["Old"]), writer) == []
    assert kinds(record(f("a", "int"), name="New", namespace="other", aliases=["ns.Old"]), writer) == []
    assert kinds(record(f("a", "int"), name="New", namespace="other", aliases=["Old"]), writer) == ["name_mismatch"]
    # as in Avro's own checker, only the unqualified name is compared
    assert kinds(record(f("a", "int"), name="Old", namespace="moved"), writer) == []


def test_enum_symbols():
    def enum(*symbols, **extra):
        return {"type": "enum", "name": "E", "symbols": list(symbols), **extra}

    assert kinds(enum("A", "B", "C"), enum("A", "B")) == []  # reader knows more
    assert kinds(enum("A", "B"), enum("B", "A")) == []  # order is irrelevant
    assert kinds(enum("A"), enum("A", "B")) == ["missing_symbols"]
    assert kinds(enum("A", default="A"), enum("A", "B")) == []  # unknown symbols fall back to the default
    assert kinds({"type": "enum", "name": "Other", "symbols": ["A"]}, enum("A")) == ["name_mismatch"]
    assert kinds("string", enum("A")) == ["type_mismatch"]


def test_fixed_size_and_name():
    def fixed(name, size):
        return {"type": "fixed", "name": name, "size": size}

    assert kinds(fixed("H", 16), fixed("H", 16)) == []
    assert kinds(fixed("H", 16), fixed("H", 32)) == ["size_mismatch"]
    assert kinds(fixed("G", 16), fixed("H", 16)) == ["name_mismatch"]


def test_reader_union_accepts_any_matching_branch():
    assert kinds(["null", "string"], "string") == []
    assert kinds(["null", "long"], "int") == []  # promotion into a branch
    assert kinds(["null", "string"], "int") == ["missing_branch"]
    assert kinds(["null", "string", "int"], ["null", "string"]) == []  # union widened
    assert kinds(["null", "string"], ["null", "string", "int"]) == ["missing_branch"]  # union narrowed


def test_writer_union_needs_every_branch_readable():
    assert kinds("string", ["null", "string"]) == ["type_mismatch"]  # nullable -> required
    assert kinds("string", ["string"]) == []
    assert kinds("long", ["int", "long"]) == []
    assert kinds("double", ["int", "string"]) == ["type_mismatch"]


def test_union_branch_problem_is_explained_inside_the_branch():
    old = record(f("payload", ["null", record(f("a", "int"), name="P")]))
    new = record(f("payload", ["null", record(f("a", "int"), f("b", "int"), name="P")]))
    (issue,) = can_read(schema(new), schema(old))
    assert (issue.kind, issue.path) == ("missing_default", "R.payload.b")


def test_named_type_references_and_namespaces():
    writer = record(
        f("billing", record(f("zip", "string"), name="Address")),
        f("shipping", "Address"),
        name="Order", namespace="shop",
    )
    reader = record(
        f("billing", record(f("zip", "string"), f("country", "string"), name="shop.Address")),
        f("shipping", "shop.Address"),
        name="Order", namespace="shop",
    )
    issues = can_read(schema(reader), schema(writer))
    assert [(i.path, i.kind) for i in issues] == [
        ("Order.billing.country", "missing_default"), ("Order.shipping.country", "missing_default")]


def test_recursive_schema_terminates():
    def tree(value_type):
        return record(f("value", value_type), f("children", {"type": "array", "items": "Node"}), name="Node")

    assert kinds(tree("long"), tree("int")) == []
    assert kinds(tree("int"), tree("long")) == ["type_mismatch"]
    linked = record(f("next", ["null", "L"], default=None), name="L")
    assert kinds(linked, linked) == []


@pytest.mark.parametrize(
    "bad",
    ['{"type": "record"', '{"type": "record", "name": "R"}', '"NoSuchType"', '{"type": "enum", "name": "E"}',
     '{"type": "fixed", "name": "F"}', '{"type": "array"}', '{"type": "map"}', '["int", "int"]',
     '["null", ["int", "string"]]', '{"name": "x"}', '42',
     '{"type": "record", "name": "R", "fields": [{"name": "a", "type": "int"}, {"name": "a", "type": "int"}]}',
     '{"type": "record", "name": "R", "fields": [{"name": "a"}]}'],
)
def test_invalid_schemas_are_rejected(bad):
    with pytest.raises(SchemaError):
        parse(bad)


def test_union_may_hold_several_named_types():
    a, b = record(f("x", "int"), name="A"), record(f("y", "int"), name="B")
    assert kinds([a, b], b) == []
    assert kinds([a], b) == ["missing_branch"]
