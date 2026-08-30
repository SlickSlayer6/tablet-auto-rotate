# Hardware fixtures

Fixtures let community hardware reports improve discovery regression coverage
without giving CI access to a physical laptop. They describe only the minimum
sanitized facts needed to select devices and predict behavior.

JSON fixtures live under `tests/fixtures`, use `schema_version: 1`, and contain:

- a non-serializing manufacturer/model label and evidence level;
- input switch candidates with a generic `/dev/input` path, kernel name, and
  advertised numeric switch codes;
- IIO function names, redacted topology identifiers, and required attribute
  availability;
- the expected conservative selection result.

Do not include hostnames, usernames, `/run/user` IDs, device serial numbers,
complete udev databases, or unrelated input devices. Replace topology instance
identifiers with `<instance>` when identity—not the literal value—is what the
test needs.

A new fixture must be accompanied by a test that consumes it and by a hardware
report stating exactly which behavior was physically exercised. Fixture-tested
support alone does not establish touch alignment or reliable suspend behavior.
