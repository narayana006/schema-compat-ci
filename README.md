# schema-compat-ci

Fail the build when an Avro, Protobuf or JSON schema change would break Kafka consumers, checked against Schema Registry.

> **Project status: planning. There is no working code in this repository yet.**
> This README describes what the project will do. Nothing below can be installed or run today.
> Features are ticked off in the checklist as they are built.

## What it is

A command-line tool and GitHub Action.

**The problem it solves:** Incompatible schema changes are usually discovered in production, when consumers start failing.

## Who it is for

Teams that share Kafka topics between services.

## How it will work

1. You add the check to your pipeline.
2. On each change it compares the new schemas with the registered ones.
3. It fails with a clear explanation if a consumer would break.

## Planned features

Unticked items are not built yet.

- [ ] Checks changed schema files against the registered versions
- [ ] Avro, Protobuf and JSON Schema
- [ ] Respects each subject's compatibility mode
- [ ] Explains which field broke compatibility and why
- [ ] GitHub Action and command-line tool
- [ ] Offline mode against schema files in git

## Planned usage

This is the intended interface, shown so you can judge the design. **It does not work yet**, and names may change before the first release.

```bash
schema-compat-ci check --registry https://registry:8081 schemas/
```

## Contributing

Feedback on the design is the most useful contribution right now. Open an issue to suggest a feature, question an approach or describe your use case. Code contributions are welcome once the first version lands.

## Author

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
