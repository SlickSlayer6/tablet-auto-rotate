# Structured diagnostics

`--probe` and `--doctor` are read-only. Add `--json` to produce a machine-readable
report suitable for an issue attachment or the starting point for a hardware
fixture:

```console
tablet-auto-rotate --doctor --json > doctor.json
tablet-auto-rotate --probe --json > probe.json
```

The command exits successfully only when the report's top-level `ok` value is
`true`. A failing command still writes valid JSON. Diagnostic logging, when
enabled, goes to stderr so redirected stdout remains parseable.

## Contract

Every report contains:

- `schema_version`, currently `1`;
- `report_type`, either `doctor` or `probe`;
- application name and version;
- the effective sanitized configuration;
- a top-level `ok` result.

A doctor report contains checks with stable `id` and `status` fields. A probe
report contains the switch selection decision, relevant switch candidates, the
selected switch state, the selected sensor topology, one sensor reading, and
structured errors. By default, unrelated incapable input devices are represented
only by `assessed_candidate_count`; `--verbose` explicitly includes them. Object
keys are sorted when serialized to make reports easy to compare. Array order is
meaningful and remains discovery or axis order.

Additive fields may appear without changing `schema_version`. A schema version
change indicates an incompatible structural or semantic change. Consumers
should ignore unknown fields and reject unsupported schema versions.

## Privacy

Reports automatically replace common home-directory usernames and runtime UIDs,
remove control characters from labels, bound free-text values, and redact the
per-boot HID instance suffix. They intentionally omit hostnames, monitor serials,
DMI serials, and complete udev data.

Hardware and software can expose identifiers in unexpected places. Always read
the complete file before publishing it. Sensor samples and device model names
are included because they are necessary for diagnosis; remove anything you do
not want to share.

## Fixtures

The probe shape is designed to be converted into a minimal test fixture without
re-running ad-hoc shell commands. Do not commit the report unchanged: retain
only the fields needed by the test, add the reported machine and evidence level,
replace identity-free topology instances with `<instance>`, and record the
expected selection result. See [hardware fixtures](hardware-fixtures.md).
