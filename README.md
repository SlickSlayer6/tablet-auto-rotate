# Tablet Auto Rotate

Automatic display and touchscreen rotation for Linux convertible computers
running Hyprland. Tablet Auto Rotate reads the laptop's tablet-mode switch and
display accelerometer directly through Linux evdev and IIO, then keeps the
configured internal display and touchscreen aligned.

Omarchy is supported through an optional integration that refreshes shell
surfaces after a rotation. The rotation daemon itself can run without Omarchy
and has no third-party Python runtime dependencies.

## Features

- Rotates only while the hardware tablet-mode switch is active
- Restores the configured landscape transform in laptop mode
- Keeps the configured display and touchscreen transforms synchronized
- Rejects flat, diagonal, moving, and unstable sensor readings
- Rediscovers input and sensor devices after suspend or driver reload
- Handles startup when the computer is already folded
- Refuses ambiguous switch or accelerometer discovery instead of guessing
- Never changes display mode, resolution, scale, or enabled state
- Supports per-machine TOML configuration and read-only calibration
- Provides human-readable and sanitized JSON diagnostics
- Includes guarded systemd user-service installation

## Requirements

- Linux with a tablet-mode switch that advertises `SW_TABLET_MODE`
- A readable IIO `accel_3d` display sensor
- Hyprland
- Python 3.11 or newer
- `hyprctl` available in the graphical session
- Omarchy only when `desktop_integration = "omarchy"` is configured

Hardware layouts vary. The currently verified configuration is an Acer
TravelMate B311R-33 / TravelMate B3 Spin 11 running Hyprland 0.56.2 and Omarchy
4.0.1. See [hardware compatibility](docs/compatibility.md) for the exact test
record and evidence levels.

## Install

From a source checkout:

```console
python3 -m pip install --user .
tablet-auto-rotate --version
```

If your distribution prevents user-level `pip` installs, install the release
wheel in a virtual environment or with your preferred isolated Python package
manager.

The Acer TravelMate profile is used when no configuration file exists. Other
computers should create
`~/.config/tablet-auto-rotate/config.toml` before starting the daemon. Begin by
copying the example profile and replacing only values confirmed on your machine:

```console
mkdir -p ~/.config/tablet-auto-rotate
cp profiles/acer-travelmate-b311r-33.toml ~/.config/tablet-auto-rotate/config.toml
tablet-auto-rotate --doctor
tablet-auto-rotate --probe
```

For unknown tablet switches, set `switch_name` and `preferred_switch_path` to
`"auto"`. Discovery succeeds only when a unique capable switch is available.
Use the read-only calibration command to determine sensor axes and transforms:

```console
tablet-auto-rotate --calibrate
```

Review the proposed configuration and test touchscreen corners in every
orientation before enabling automatic startup. See
[configuration](docs/configuration.md) and [calibration](docs/calibration.md)
for the complete procedure.

## Run

Test the daemon without changing display or input transforms:

```console
tablet-auto-rotate --dry-run --verbose
```

Press `Ctrl+C` to stop it. Once discovery, folding, and orientation changes look
correct, run it normally:

```console
tablet-auto-rotate
```

For an immediate temporary launch under the systemd user manager:

```console
systemd-run --user --unit=tablet-auto-rotate-now --collect tablet-auto-rotate
```

For persistent startup outside Omarchy, use the guarded
[systemd user-service installer](docs/service.md). Omarchy users can add the
small UWSM-aware rule in `examples/hypr/autostart.lua`. The examples in
`examples/hypr/` must be merged into existing Hyprland files; do not overwrite
an existing configuration wholesale.

## Diagnostics

These commands are read-only and do not rotate the display:

```console
tablet-auto-rotate --self-test
tablet-auto-rotate --doctor
tablet-auto-rotate --probe
tablet-auto-rotate --doctor --json
tablet-auto-rotate --probe --json
```

JSON reports use a deterministic, versioned schema and redact common user-path
and device-instance identifiers. Always review the complete report before
sharing it. See [structured diagnostics](docs/diagnostics.md).

## How it works

1. Reads the initial and subsequent tablet-mode state through evdev.
2. Selects the display accelerometer from its IIO topology.
3. Filters acceleration into one of four calibrated orientations.
4. Applies matching Hyprland display and touchscreen transforms.
5. Verifies the display transform before reporting success.
6. When using Omarchy, refreshes layer surfaces and falls back to a guarded
   shell restart if the fast remap cannot be verified.

When multiple monitors are active, the Omarchy integration skips the temporary
one-pixel position nudge and uses the safer shell-restart path. External displays
are not rotated, enabled, disabled, or repositioned.

## Limitations

- Only transforms 0 through 3 are supported.
- Automatic sensor selection does not yet use IIO mount matrices.
- Machines with ambiguous or unusual multi-accelerometer topologies may require
  further discovery support.
- Effective touchscreen transform cannot be queried from Hyprland, so physical
  corner-touch testing remains required after calibration.
- The fast Omarchy surface refresh depends on current Omarchy behavior; a full
  shell restart is used when it cannot be verified.
- Physical verification currently covers only the Acer model listed above.

Hardware reports from other convertible computers are welcome, including
partial failures. See [contributing](CONTRIBUTING.md),
[hardware compatibility](docs/compatibility.md), and the
[project roadmap](ROADMAP.md).

## License

Tablet Auto Rotate is licensed under the [MIT License](LICENSE).
