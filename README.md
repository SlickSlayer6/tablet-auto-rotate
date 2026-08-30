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

## AI-assisted setup

An AI coding agent with terminal access can inspect the local hardware, install
the package, prepare a machine-specific configuration, and guide the physical
checks. Give the agent the repository checkout as its working directory and use
the following prompt:

```text
Install and configure Tablet Auto Rotate from this repository for this computer.

Read README.md, docs/configuration.md, docs/calibration.md, docs/service.md,
docs/diagnostics.md, and docs/compatibility.md before making changes.

Safety requirements:
- Start with read-only inspection. Do not assume the bundled Acer defaults match
  this computer.
- Do not use sudo, change system-wide files, overwrite existing Hyprland files,
  or replace an existing service/configuration without showing me the proposed
  change and obtaining approval.
- Preserve unrelated user changes. Merge only the minimum required snippets.
- Treat ambiguous switch or accelerometer discovery as a blocker; do not guess.
- Do not enable persistent startup until dry-run and physical validation pass.

Workflow:
1. Inspect the repository, current installation, Hyprland session, existing
   configuration, and any running tablet-auto-rotate process or user service.
2. Run the repository's local tests and packaged self-test. Install it in an
   isolated user environment appropriate for this system.
3. Run `tablet-auto-rotate --doctor --json` and
   `tablet-auto-rotate --probe --json`. Review the reports for private data
   before proposing that I share them.
4. Create or update a machine-specific TOML configuration using only discovered
   values. Back up an existing file before changing it.
5. Use read-only calibration if the sensor mapping is not already physically
   verified. Ask me to perform each required fold/orientation action.
6. Run `tablet-auto-rotate --dry-run --verbose` and have me verify tablet/laptop
   transitions and all four orientation classifications.
7. Before live rotation, warn me to save work and ensure a recovery path. Have
   me verify display orientation and touchscreen corners in all four directions,
   laptop-mode restoration, and suspend/resume.
8. Only after those checks pass, configure persistent user startup using the
   documented Omarchy snippet or guarded systemd user-service installer.
9. Report exactly what was installed or changed, how it was verified, remaining
   untested behavior, and how to disable or uninstall it.
```

The agent cannot observe whether the physical screen and touch coordinates are
actually aligned. The person at the computer must perform and confirm those
steps; successful commands or automated tests are not a substitute.

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
- IIO mount matrices are supported conservatively but remain opt-in so existing
  calibrated profiles do not change behavior during an upgrade.
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
