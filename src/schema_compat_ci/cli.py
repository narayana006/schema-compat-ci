"""Command-line entry point: `schema-compat-ci check ...`."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .avro import SchemaError, parse
from .compat import MODES, check
from .registry import RegistryClient, RegistryError

SCHEMA_TYPES = {".avsc": "AVRO", ".proto": "PROTOBUF", ".json": "JSON"}
AUTH_ENV = "SCHEMA_REGISTRY_BASIC_AUTH_USER_INFO"


class UsageError(Exception):
    pass


@dataclass
class Result:
    name: str  # what the user sees: a path
    status: str  # PASS, FAIL, NEW or ERROR
    detail: str = ""  # mode, subject, or the error text
    lines: list[str] = field(default_factory=list)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise UsageError("git is not installed") from exc


def schema_at_ref(path: Path, ref: str) -> str | None:
    """Content of `path` at git revision `ref`, or None if the file did not exist there."""
    path = path.resolve()
    top = _git(path.parent, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise UsageError(f"{path} is not inside a git repository")
    root = Path(top.stdout.strip()).resolve()
    if _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise UsageError(f"git revision {ref!r} not found (in CI, fetch it first; a shallow clone may not have it)")
    spec = f"{ref}:{path.relative_to(root).as_posix()}"
    if _git(root, "cat-file", "-e", spec).returncode != 0:
        return None
    shown = _git(root, "show", spec)
    if shown.returncode != 0:
        raise UsageError(f"git show {spec} failed: {shown.stderr.strip()}")
    return shown.stdout


def collect(paths: list[str], extensions: tuple[str, ...]) -> list[Path]:
    """Explicit files are taken as they are; directories are searched for the extensions."""
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix in extensions)
        elif p.is_file():
            found.append(p)
        else:
            raise UsageError(f"no such file or directory: {raw}")
    return found


def compare(name: str, old_text: str | None, new_text: str, mode: str) -> Result:
    if Path(name).suffix != ".avsc":
        return Result(name, "ERROR", "offline checks support Avro (.avsc) only")
    try:
        new = parse(new_text)
    except SchemaError as exc:
        return Result(name, "ERROR", f"new schema is invalid: {exc}")
    if old_text is None:
        return Result(name, "NEW", "no earlier version to compare with")
    try:
        old = parse(old_text)
    except SchemaError as exc:
        return Result(name, "ERROR", f"old schema is invalid: {exc}")
    findings = check(old, new, mode)
    return Result(name, "FAIL" if findings else "PASS", mode, [str(f) for f in findings])


def check_registry(client: RegistryClient, path: Path, subject: str) -> Result:
    schema_type = SCHEMA_TYPES.get(path.suffix)
    if schema_type is None:
        return Result(str(path), "ERROR", "unknown schema file type (expected .avsc, .proto or .json)")
    text = path.read_text()
    if schema_type == "AVRO":
        try:
            parse(text)
        except SchemaError as exc:
            return Result(str(path), "ERROR", f"schema is invalid: {exc}")
    try:
        verdict = client.test_compatibility(subject, text, schema_type)
    except RegistryError as exc:
        return Result(str(path), "ERROR", str(exc))
    if not verdict.subject_exists:
        return Result(str(path), "NEW", f"subject {subject} is not registered yet")
    if verdict.is_compatible:
        return Result(str(path), "PASS", f"subject {subject}")
    result = Result(str(path), "FAIL", f"subject {subject}", [f"registry: {m}" for m in verdict.messages])
    if schema_type == "AVRO":
        # Add our own field-level explanation against the latest registered version.
        try:
            level = client.compatibility_level(subject)
            old_text, old_type = client.latest_schema(subject)
            if old_type == "AVRO":
                result.detail += f", level {level}"
                result.lines += [str(f) for f in check(parse(old_text), parse(text), level)]
        except (RegistryError, SchemaError, ValueError):
            pass  # the registry's verdict stands on its own
    return result


def _annotation(result: Result, line: str) -> str:
    """A GitHub Actions workflow command that attaches the message to the file."""
    escape = lambda s: s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")  # noqa: E731
    name = escape(result.name).replace(":", "%3A").replace(",", "%2C")
    return f"::error file={name},title=Incompatible schema change::{escape(line)}"


def report(results: list[Result], out) -> int:
    for r in results:
        print(f"{r.status:<5} {r.name}" + (f" ({r.detail})" if r.detail else ""), file=out)
        for line in r.lines:
            print(f"      {line}", file=out)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            for line in r.lines or ([r.detail] if r.status == "ERROR" else []):
                print(_annotation(r, line), file=out)
    failed = sum(r.status == "FAIL" for r in results)
    errors = sum(r.status == "ERROR" for r in results)
    print(file=out)
    if not results:
        print("No schema files found.", file=out)
    elif failed or errors:
        print(f"{failed} incompatible, {errors} error(s), {len(results)} schema(s) checked.", file=out)
    else:
        print(f"All {len(results)} schema(s) compatible.", file=out)
    return 2 if errors else 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="schema-compat-ci", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser(
        "check",
        help="check schema changes for compatibility",
        description="Exit status: 0 compatible, 1 incompatible, 2 invalid input or other error.",
    )
    c.add_argument("paths", nargs="*", metavar="PATH", help="schema files, or directories searched for *.avsc")
    c.add_argument("--old", metavar="FILE", help="earlier version of one schema (use with --new)")
    c.add_argument("--new", metavar="FILE", help="changed version of that schema")
    c.add_argument("--base-ref", metavar="REV", help="compare each PATH with its content at this git revision")
    c.add_argument("--registry", metavar="URL", help="ask this Schema Registry instead of comparing files")
    c.add_argument("--mode", type=str.upper, choices=MODES, metavar="MODE",
                   help="offline compatibility mode (default BACKWARD): " + ", ".join(MODES))
    c.add_argument("--subject-template", default="{name}", metavar="TEMPLATE",
                   help="registry subject for a file; {name} is the file name without extension "
                        "(default '{name}', e.g. '{name}-value')")
    return parser


def run(argv=None, out=sys.stdout) -> int:
    args = build_parser().parse_args(argv)
    chosen = [bool(args.old or args.new), bool(args.base_ref), bool(args.registry)]
    if sum(chosen) != 1:
        raise UsageError("choose exactly one of: --old/--new, --base-ref REV, --registry URL")

    if args.registry:
        if args.mode:
            raise UsageError("--mode is not used with --registry: the registry applies each subject's own level")
        if "{name}" not in args.subject_template:
            raise UsageError("--subject-template must contain {name}")
        if not args.paths:
            raise UsageError("give at least one schema file or directory")
        client = RegistryClient(args.registry, basic_auth=os.environ.get(AUTH_ENV))
        files = collect(args.paths, (".avsc", ".proto"))
        results = [check_registry(client, f, args.subject_template.replace("{name}", f.stem)) for f in files]
        return report(results, out)

    mode = args.mode or "BACKWARD"
    if args.base_ref:
        if not args.paths:
            raise UsageError("give at least one schema file or directory")
        results = []
        for f in collect(args.paths, (".avsc",)):
            results.append(compare(str(f), schema_at_ref(f, args.base_ref), f.read_text(), mode))
        return report(results, out)

    if not (args.old and args.new) or args.paths:
        raise UsageError("--old and --new are used together, without PATH arguments")
    result = compare(args.new, Path(args.old).read_text(), Path(args.new).read_text(), mode)
    return report([result], out)


def main(argv=None) -> int:
    try:
        return run(argv)
    except UsageError as exc:
        print(f"schema-compat-ci: error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"schema-compat-ci: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
