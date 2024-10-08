# schema-compat-ci

Fail the build when an Avro schema change would break Kafka consumers or producers, and say exactly which field is the problem.

**Status:** working first version for **Avro**. Offline checks (two files, or your working tree against a git revision) work with BACKWARD, FORWARD and FULL rules and field-level explanations. A read-only Schema Registry mode works against the registry REST API and has been tested with a fake registry server, not yet against a real one. Protobuf and JSON Schema are not analysed locally. A composite GitHub Action is included but has not yet run on a GitHub runner.

## Install

Python 3.10 or newer, no dependencies.

```bash
git clone https://github.com/narayana006/schema-compat-ci
cd schema-compat-ci
pip install .
```

## Quick start

`examples/` has an `Order` schema and two candidate changes. Check the breaking one under FULL compatibility:

```bash
schema-compat-ci check --old examples/order-v1.avsc --new examples/order-v2-breaking.avsc --mode FULL
```

```text
FAIL  examples/order-v2-breaking.avsc (FULL)
      [BACKWARD] Order.amount_cents: The old schema writes long, which cannot be read as int by the new schema (Avro promotes int to long, not the other way round).
      [BACKWARD] Order.customer_id: Field 'customer_id' is in the new schema without a default but is not in the old schema, so there is no value for it when reading data written with the old schema. Give the new field a default value.
      [FORWARD] Order.status: Enum symbol(s) REFUNDED can be written with the new schema but are unknown to the old schema, whose enum has no default symbol to fall back on. Old consumers need a default symbol in their enum before new symbols can be added.
      [FORWARD] Order.legacy_ref: Field 'legacy_ref' is in the old schema without a default but is not in the new schema, so there is no value for it when reading data written with the new schema. Keep the field, or give it a default in a version that every consumer has adopted before removing it.

1 incompatible, 0 error(s), 1 schema(s) checked.
```

The exit status is 1. The compatible change (a new field with a default) passes with exit status 0:

```bash
schema-compat-ci check --old examples/order-v1.avsc --new examples/order-v2-compatible.avsc --mode FULL
```

```text
PASS  examples/order-v2-compatible.avsc (FULL)

All 1 schema(s) compatible.
```

## Three ways to run it

### 1. Two files

```bash
schema-compat-ci check --old OLD.avsc --new NEW.avsc [--mode MODE]
```

### 2. Working tree against a git revision

Every `.avsc` file under the given paths is compared with its content at the revision. Files that do not exist there are reported as `NEW` and pass.

```bash
schema-compat-ci check --base-ref HEAD examples/
```

```text
PASS  examples/order-v1.avsc (BACKWARD)
PASS  examples/order-v2-breaking.avsc (BACKWARD)
PASS  examples/order-v2-compatible.avsc (BACKWARD)

All 3 schema(s) compatible.
```

In a pull request build you would use `--base-ref origin/main`; the revision has to be present in the clone.

### 3. Against Schema Registry

```bash
schema-compat-ci check --registry https://registry:8081 --subject-template "{name}-value" schemas/
```

Each file is sent to `POST /compatibility/subjects/{subject}/versions?verbose=true`. The registry applies the subject's own compatibility level: against the latest version, or against all versions for the `_TRANSITIVE` levels. That is why `--mode` is not accepted here. The registry's verdict decides pass or fail and its messages are printed. For Avro files the tool additionally reads the subject's level (`GET /config/{subject}?defaultToGlobal=true`) and latest version (`GET /subjects/{subject}/versions/latest`) and adds its own field-level explanation.

- The subject is the file name without its extension, passed through `--subject-template` (default `{name}`, so `orders-value.avsc` maps to subject `orders-value`).
- A subject the registry does not know (error code 40401) is reported as `NEW` and passes.
- Nothing is registered or changed; only the three requests above are made.
- For HTTP basic auth set `SCHEMA_REGISTRY_BASIC_AUTH_USER_INFO=user:password` in the environment.
- `.proto` files (and `.json` files named explicitly) are forwarded with `schemaType` `PROTOBUF` / `JSON` and the registry's answer is reported as is. Schema references are not sent, so Protobuf files with imports of other registered schemas will not check correctly.

### Exit status

| Code | Meaning |
| --- | --- |
| 0 | every schema is compatible (or new) |
| 1 | at least one incompatible schema |
| 2 | invalid schema, unreachable registry, bad arguments |

When `GITHUB_ACTIONS=true` is set, each finding is also printed as an `::error file=...::` workflow command so it shows up as an annotation on the file.

## Compatibility modes

| Mode | Question asked | Typical breaking change |
| --- | --- | --- |
| `BACKWARD` (default) | Can consumers on the **new** schema read data written with the **old** one? | adding a field without a default |
| `FORWARD` | Can consumers on the **old** schema read data written with the **new** one? | removing a field that has no default |
| `FULL` | Both | either of the above |
| `NONE` | Nothing is checked | |

`BACKWARD_TRANSITIVE`, `FORWARD_TRANSITIVE` and `FULL_TRANSITIVE` are accepted too. Offline there is only one earlier version to compare with, so they behave like the non-transitive modes; only the registry mode checks all earlier versions.

## Avro rules that are checked

Each direction is one reader/writer resolution check as described in the Avro specification:

- **Record fields.** A reader field missing from the writer needs a default (`"default": null` counts; a nullable type without a default does not). Writer fields the reader does not have are ignored. Field order is irrelevant. A reader field can match a writer field through its `aliases`.
- **Type promotion.** `int` to `long`, `float` or `double`; `long` to `float` or `double`; `float` to `double`; `string` to `bytes` and `bytes` to `string`. Nothing in the other direction.
- **Unions.** Every branch the writer may write must be readable by some branch of the reader (or by the reader's plain type). Making a nullable field required therefore fails; widening a union passes.
- **Enums.** Every writer symbol must exist in the reader's enum unless that enum declares a default symbol.
- **Fixed.** Sizes must be equal.
- **Named types.** Records, enums and fixed types must have the same name, or the reader must list the writer's full name in `aliases`. As in Avro's own compatibility checker, only the unqualified name is compared, so a namespace change alone is not flagged.
- **Arrays, maps, nested and recursive records** are followed, and findings carry the path, such as `Customer.addresses[].zip`.

## GitHub Action

`action.yml` is a composite action that installs this package and runs the git or registry check.

```yaml
jobs:
  schemas:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0            # so origin/<base branch> exists locally
      - uses: narayana006/schema-compat-ci@main
        with:
          paths: schemas
          base-ref: origin/${{ github.base_ref }}
          mode: FULL
```

Inputs: `paths` (space separated, default `.`), `base-ref`, `mode` (default `BACKWARD`), `registry` (when set, replaces the git comparison), `subject-template` (default `{name}`), `python-version` (default `3.12`).

This repository's CI workflow runs the action against `examples/`. Until that has passed on GitHub, treat the action as untested; the plain CLI in a `run:` step is the safe choice.

## Features

- [x] Command-line tool
- [x] Offline mode against schema files in git
- [x] Explains which field broke compatibility and why
- [x] Avro
- [x] Checks schema files against the registered versions (registry mode; tested against a fake server only)
- [x] Respects each subject's compatibility mode (the registry applies it; offline you pass `--mode`)
- [ ] Protobuf: local analysis and explanations
- [ ] JSON Schema: local analysis and explanations
- [ ] GitHub Action verified on a real runner
- [ ] Schema references and Protobuf imports in registry mode
- [ ] Detecting only the files changed in a pull request (today every schema under the paths is checked)

## Limitations

- Local analysis is Avro only. A `.proto` or `.json` file given to an offline check is an error, not a silent pass.
- Default values are not validated against the field type, and logical types are compared by their underlying type only (a change of decimal precision or scale is not detected).
- A field without a default whose type is an enum with a default symbol is still reported as missing a default.
- The git mode does not notice deleted or renamed schema files.
- Offline, the transitive modes compare with a single earlier version.
- In registry mode the tool's own Avro explanation is based on the latest registered version; for transitive levels the registry may reject a schema because of an older version, in which case only the registry's messages explain it.
- Registry support covers basic auth only (no mTLS or OAuth options), and has been exercised against a fake HTTP server that follows the documented API, not a live Schema Registry.

## Development

```bash
pip install -e ".[test]"
pytest
```

## Contributing

Bug reports with a pair of schemas where the verdict differs from Schema Registry are the most valuable contribution. Pull requests are welcome.

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
