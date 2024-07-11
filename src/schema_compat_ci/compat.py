"""Compatibility modes, with the meaning Schema Registry gives them.

BACKWARD  consumers using the new schema can read data produced with the old one
FORWARD   consumers using the old schema can read data produced with the new one
FULL      both
NONE      no check

The *_TRANSITIVE modes apply the same rule against every earlier version
instead of only the latest one. Offline only one earlier version is known, so
they are checked against that single version.
"""

from __future__ import annotations

from dataclasses import dataclass

from .avro import Schema, can_read

MODES = ("BACKWARD", "BACKWARD_TRANSITIVE", "FORWARD", "FORWARD_TRANSITIVE", "FULL", "FULL_TRANSITIVE", "NONE")

_HINTS = {
    ("BACKWARD", "missing_default"): "Give the new field a default value.",
    ("FORWARD", "missing_default"): "Keep the field, or give it a default in a version that every consumer has adopted before removing it.",
    ("BACKWARD", "missing_symbols"): "Keep the symbol(s), or give the enum a default symbol.",
    ("FORWARD", "missing_symbols"): "Old consumers need a default symbol in their enum before new symbols can be added.",
}


@dataclass(frozen=True)
class Finding:
    direction: str  # BACKWARD or FORWARD
    path: str
    kind: str
    message: str

    def __str__(self) -> str:
        return f"[{self.direction}] {self.path}: {self.message}"


def directions(mode: str) -> tuple[str, ...]:
    mode = mode.upper()
    if mode not in MODES:
        raise ValueError(f"unknown compatibility mode {mode!r}; expected one of {', '.join(MODES)}")
    base = mode.removesuffix("_TRANSITIVE")
    return {"BACKWARD": ("BACKWARD",), "FORWARD": ("FORWARD",), "FULL": ("BACKWARD", "FORWARD"), "NONE": ()}[base]


def check(old: Schema, new: Schema, mode: str) -> list[Finding]:
    """Findings that make `new` incompatible with `old` under `mode` (empty: compatible)."""
    findings = []
    for direction in directions(mode):
        if direction == "BACKWARD":
            issues = can_read(reader=new, writer=old)
            reader, writer = "new schema", "old schema"
        else:
            issues = can_read(reader=old, writer=new)
            reader, writer = "old schema", "new schema"
        for issue in issues:
            message = issue.render(reader, writer)
            message = message[0].upper() + message[1:] + "."
            hint = _HINTS.get((direction, issue.kind))
            if hint:
                message += " " + hint
            findings.append(Finding(direction, issue.path, issue.kind, message))
    return findings
