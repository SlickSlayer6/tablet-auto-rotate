# Tablet Auto Rotate

Safe automatic tablet-mode display and touchscreen rotation for Linux
convertibles running Hyprland. The first physically verified profile targets
an Acer TravelMate B311R-33 with Omarchy and Hyprland's Lua provider.

This is an early, configurable release built from a working hardware-calibrated
prototype. It directly reads the Linux tablet-mode switch and display
accelerometer without additional runtime Python packages.

See [`ROADMAP.md`](ROADMAP.md) for the plan to turn this into universal tablet support for Hyprland with optional Omarchy integration.

## Features

- Rotates only while the hardware tablet-mode switch is active
- Restores normal landscape when returning to laptop mode
- Rotates `eDP-1` and the ELAN touchscreen together
- Rejects flat, diagonal, and moving sensor readings
- Rediscovers input/IIO devices after suspend or driver reload
- Uses a fast Omarchy layer-surface remap after rotation
- Falls back to restarting the Omarchy shell if the fast remap cannot be verified
- Does not enable/disable displays or change their mode, scale, or resolution
- Loads machine identifiers, sensor axes, and transform mappings from TOML
- Can run without the Omarchy-specific layer refresh adapter

## Tested environment

- Acer TravelMate B311R-33 / TravelMate B3 Spin 11
- Omarchy 4.0.1
- Hyprland 0.56.2 with the Lua config provider
- Internal display: `eDP-1`
- Touchscreen: `elan9004:00-04f3:4110`
- Tablet switch: `Intel HID switches`, `SW_TABLET_MODE`
- Display accelerometer identified by the HID hub shared with the hinge sensor

The bundled Acer values are defaults for compatibility and are also recorded in
[`profiles/acer-travelmate-b311r-33.toml`](profiles/acer-travelmate-b311r-33.toml).
Other machines should use an explicit configuration; see
[`docs/configuration.md`](docs/configuration.md).

## Diagnostics

These commands do not rotate the display:

```bash
bin/tablet-auto-rotate --self-test
bin/tablet-auto-rotate --doctor
bin/tablet-auto-rotate --probe
bin/tablet-auto-rotate --dry-run --verbose
```

`--dry-run` remains active until interrupted and listens to the real tablet switch and accelerometer.

For a new machine, use capability-based switch discovery by setting both
switch fields to `"auto"`, run `--probe`, and then use the read-only
`--calibrate` flow. See [`docs/configuration.md`](docs/configuration.md) and
[`docs/calibration.md`](docs/calibration.md).

## Manual installation

Install the package from a source checkout:

```bash
python3 -m pip install --user .
```

For development, `bin/tablet-auto-rotate` remains a source-tree launcher.

Merge the small rules in `examples/hypr/` into the corresponding files under `~/.config/hypr/`. Do not overwrite an existing Hyprland configuration wholesale.

Then validate:

```bash
hyprctl reload
hyprctl configerrors
```

The autostart rule takes effect on the next Hyprland session. For an immediate temporary launch under the current user manager:

```bash
systemd-run --user --unit=tablet-auto-rotate-now --collect tablet-auto-rotate
```

A guarded systemd user-service installer is also available for non-Omarchy
sessions; see [`docs/service.md`](docs/service.md). It never invokes `systemctl`
or overwrites a differing unit implicitly.

## How it works

1. Reads the initial and subsequent `SW_TABLET_MODE` state through evdev.
2. Finds the display accelerometer by matching it to the HID sensor hub that owns the hinge sensor.
3. Filters raw IIO acceleration into one of four calibrated orientations.
4. Applies matching Hyprland output and touch transforms through `hyprctl eval`.
5. Forces Hyprland to commit the runtime monitor rule.
6. Nudges and restores the monitor origin by one pixel, triggering Omarchy's existing layer-surface remap guard. With multiple active monitors, it skips the nudge and uses the safer full shell-restart fallback.

## Current limitations

- Hardware selection is configurable but automatic capability-based discovery
  and interactive calibration are not implemented yet.
- The fast shell remap depends on Omarchy's current `ScreenMoveRemap.qml` behavior.
- Effective touchscreen transform cannot be queried from Hyprland, so corner-touch testing is still required after changing hardware mappings.
- It has only been physically tested on the machine listed above.

Community testing is the intended path to broader support. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`docs/compatibility.md`](docs/compatibility.md) for sanitized hardware reports,
evidence levels, and the physical test checklist.

## Before broader release

The next useful steps are:

1. Add capability-based discovery with sanitized sysfs fixtures.
2. Add an interactive calibration command and corner-touch validation.
3. Collect community reports from other convertible sensor topologies.
4. Support additional Hyprland configuration providers.
5. Add an installer/uninstaller that safely merges—not replaces—user configuration.
