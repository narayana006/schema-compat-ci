from pathlib import Path

import pytest

from schema_compat_ci.avro import parse
from schema_compat_ci.compat import MODES, check, directions

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

V1 = parse('{"type":"record","name":"R","fields":[{"name":"id","type":"string"}]}')
ADD_REQUIRED = parse('{"type":"record","name":"R","fields":[{"name":"id","type":"string"},{"name":"n","type":"int"}]}')
ADD_OPTIONAL = parse('{"type":"record","name":"R","fields":[{"name":"id","type":"string"},{"name":"n","type":"int","default":0}]}')


def dirs(old, new, mode):
    return sorted({f.direction for f in check(old, new, mode)})


def test_adding_a_required_field_breaks_backward_only():
    assert dirs(V1, ADD_REQUIRED, "BACKWARD") == ["BACKWARD"]
    assert dirs(V1, ADD_REQUIRED, "FORWARD") == []
    assert dirs(V1, ADD_REQUIRED, "FULL") == ["BACKWARD"]
    assert dirs(V1, ADD_REQUIRED, "NONE") == []


def test_removing_a_required_field_breaks_forward_only():
    assert dirs(ADD_REQUIRED, V1, "BACKWARD") == []
    assert dirs(ADD_REQUIRED, V1, "FORWARD") == ["FORWARD"]
    assert dirs(ADD_REQUIRED, V1, "FULL") == ["FORWARD"]


def test_optional_fields_can_come_and_go_under_full():
    assert check(V1, ADD_OPTIONAL, "FULL") == []
    assert check(ADD_OPTIONAL, V1, "FULL") == []


def test_messages_name_the_field_and_both_sides():
    (finding,) = check(V1, ADD_REQUIRED, "BACKWARD")
    assert finding.path == "R.n" and finding.kind == "missing_default"
    assert "'n'" in finding.message and "new schema without a default" in finding.message
    assert "Give the new field a default" in finding.message
    (finding,) = check(ADD_REQUIRED, V1, "FORWARD")
    assert "old schema without a default" in finding.message and str(finding).startswith("[FORWARD] R.n:")


def test_transitive_modes_use_the_same_rule_and_modes_are_validated():
    for mode in MODES:
        assert directions(mode) == directions(mode.removesuffix("_TRANSITIVE"))
    assert directions("full_transitive") == ("BACKWARD", "FORWARD")
    with pytest.raises(ValueError):
        directions("SIDEWAYS")


def test_example_schemas():
    v1 = parse((EXAMPLES / "order-v1.avsc").read_text())
    good = parse((EXAMPLES / "order-v2-compatible.avsc").read_text())
    bad = parse((EXAMPLES / "order-v2-breaking.avsc").read_text())
    assert check(v1, good, "FULL") == []
    assert [(f.direction, f.path, f.kind) for f in check(v1, bad, "FULL")] == [
        ("BACKWARD", "Order.amount_cents", "type_mismatch"),
        ("BACKWARD", "Order.customer_id", "missing_default"),
        ("FORWARD", "Order.status", "missing_symbols"),
        ("FORWARD", "Order.legacy_ref", "missing_default"),
    ]
