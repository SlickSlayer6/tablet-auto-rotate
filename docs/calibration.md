# Calibration

Calibration is deliberately read-only: it samples the accelerometer and prints
a proposed configuration to standard output. It does not rotate the display or
write into `~/.config`.

Run it from an interactive terminal while the machine is in tablet mode:

```console
tablet-auto-rotate --config my-machine.toml --calibrate
```

The prompts collect ten samples with each screen edge pointing downward. The
result is rejected if samples move, are diagonal, do not form opposite pairs,
or do not identify two distinct physical axes. Review the printed TOML before
saving it as described in [configuration.md](configuration.md).

Maintainers and hardware reporters can also infer a mapping from a reviewed
JSON fixture:

```console
tablet-auto-rotate --config my-machine.toml --calibrate-from samples.json
```

The JSON object must contain `+x`, `+y`, `-x`, and `-y` arrays. Each array must
contain at least five three-number acceleration samples. Do not publish a raw
fixture before checking it for unrelated machine or user information.

Calibration determines sensor axes and signs. It cannot verify the effective
touchscreen transform, so every contributed profile still needs the corner
touch test in [compatibility.md](compatibility.md).
