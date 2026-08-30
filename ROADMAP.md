# Roadmap: Universal Tablet Support for Hyprland and Omarchy

## Current implementation status

The first generalization milestone is now present:

- MIT licensing and standard Python packaging.
- An importable core with a compatibility launcher and extracted test suite.
- Validated TOML configuration for output, touch device, tablet switch, sensor
  axis order/signs, transform mapping, and optional Omarchy integration.
- A physically verified Acer profile plus community hardware-report and
  compatibility-evidence templates.
- A read-only `--doctor` command, input ABI guard, and hardened runtime lock.
- Capability-based tablet-switch selection with ambiguity refusal and a unique
  accelerometer fallback for systems without the Acer hinge topology.
- Read-only interactive/offline calibration foundations and fixture-driven
  community discovery tests.
- A guarded systemd user-service installer/uninstaller that never overwrites a
  differing unit implicitly or invokes `systemctl` itself.

Mount-matrix-aware sensor selection, richer multi-accelerometer calibration,
additional Hyprland backends, and automatic profile matching remain future
milestones below.

## Direction

Turn the working Acer-specific prototype into a safe, configurable tablet-mode and auto-rotation utility for convertible laptops running Hyprland, with optional first-class Omarchy integration.

The core should remain usable without Omarchy. Omarchy-specific behavior—autostart, shell surface refresh, setup commands, and packaging—should live in a thin integration layer.

## Where to share it

### Standalone project

Publish the generalized daemon as its own repository first. This provides a place to support different laptops, compositors/config providers, distributions, calibration data, and release schedules without coupling the core hardware logic to Omarchy.

### Omarchy

Propose optional tablet support through an Omarchy Suggestion. The likely long-term interface is a setup command such as:

```text
omarchy setup tablet
```

This is not naturally an Omarchy shell plugin: shell plugins are QML widgets and panels, while this utility reads kernel devices and controls Hyprland outputs and input devices.

Submit a separate, focused Omarchy fix so its layer surfaces remap when screen geometry or transform changes—not only when the screen origin changes. That could eliminate the current one-pixel remap workaround.

### Hyprland

Do not propose the complete auto-rotation daemon as part of Hyprland. Instead, report narrowly reproducible compositor behavior if it still exists on the latest development version:

- Runtime Lua `hl.monitor()` transform changes are not committed without a forced renderer reload.
- Existing layer-shell surfaces may not immediately receive the updated output transform.

## Generalization roadmap

### Phase 1 — Separate policy from hardware

- Move output name, touchscreen name, switch identity, sensor selection, axis mapping, thresholds, and timing into configuration.
- Keep safe defaults: never enable a disabled monitor or alter mode, resolution, scale, or unrelated outputs.
- Separate modules for device discovery, orientation filtering, compositor control, and desktop integration.
- Preserve `--probe`, `--dry-run`, `--verbose`, and `--self-test` modes.

### Phase 2 — Hardware discovery and calibration

- Discover tablet-mode switches by capabilities rather than one fixed device name.
- Prefer standard sensor metadata and mount matrices when available.
- Support systems with one accelerometer, separate base/display accelerometers, or no hinge sensor.
- Add an interactive calibration flow for the four orientations.
- Save generated per-machine configuration under an XDG user configuration directory.
- Detect ambiguous hardware and fail safely instead of guessing.

A possible interface:

```text
tablet-auto-rotate probe
tablet-auto-rotate calibrate
tablet-auto-rotate run
tablet-auto-rotate doctor
```

### Phase 3 — Hyprland compatibility

- Support both Hyprland Lua and legacy/Hyprlang configuration providers where practical.
- Detect Hyprland capabilities and version instead of assuming one API sequence.
- Keep output and touchscreen transforms synchronized.
- Handle compositor reloads, monitor hotplug, disabled internal panels, suspend/resume, and already-folded startup.
- Avoid disturbing external displays; use the safe shell-restart path when multiple monitors are active until a better verified mechanism exists.

### Phase 4 — Desktop integration

Core daemon:

- No Omarchy dependency.
- Standard logging and user-service lifecycle.
- Optional adapters for desktop-specific behavior.

Omarchy adapter:

- UWSM-aware autostart.
- Fast layer-surface refresh with a supported fallback.
- Optional setup/remove commands.
- Update-safe configuration changes only under user-owned paths.

Potential future UI plugin:

- Status indicator or rotation lock toggle only.
- The plugin would control the daemon; it would not contain the sensor engine.

### Phase 5 — Installation and removal

- Add a safe installer that merges small config snippets rather than replacing user files.
- Add a complete uninstaller and document every file it creates.
- Consider a systemd user service for non-Omarchy environments.
- Add release archives and checksums.
- Consider an AUR package after the interfaces stabilize.

### Phase 6 — Testing

Automated tests:

- Sensor classification and debounce behavior.
- Device discovery fixtures from multiple laptops.
- Switch loss and rediscovery.
- Flat, diagonal, moving, and noisy sensor input.
- Monitor disabled/hotplug/reload scenarios.
- Command generation and failure handling for each supported Hyprland provider.

Physical compatibility matrix:

- Laptop model and firmware version.
- Tablet switch behavior.
- Sensor topology and mount matrix.
- Touchscreen/stylus mapping.
- Landscape and portrait directions.
- Suspend/resume and cold startup while folded.
- Internal-only and external-monitor configurations.

### Phase 7 — Release and upstream work

1. Choose a license, likely a permissive license such as MIT or Apache-2.0.
2. Add contribution and hardware-report templates.
3. Publish the standalone prototype.
4. Collect probe output and calibration reports from other convertible owners.
5. Open an Omarchy Suggestion for optional tablet setup.
6. Submit the focused Omarchy shell geometry/remap improvement.
7. Reproduce relevant compositor issues against current Hyprland main before filing upstream reports.

## Security and safety principles

- Open only the validated tablet switch, never arbitrary keyboards or pointer devices.
- Never grab input devices.
- Avoid root privileges and system-wide configuration where possible.
- Treat device names and sensor data as untrusted input.
- Use argument arrays rather than shell interpolation.
- Keep a singleton lock and bounded subprocess timeouts.
- Retain the last valid orientation when sensor readings are uncertain.
- Always restore the configured laptop baseline when tablet mode ends.

## Suggested project structure

```text
bin/
  tablet-auto-rotate
src/
  discovery.py
  sensors.py
  orientation.py
  hyprland.py
  integrations/
    omarchy.py
examples/
  hypr/
tests/
docs/
  hardware-support.md
  calibration.md
  architecture.md
```

The current single-file implementation should remain intact until tests cover the behavior being extracted.

## Immediate next steps

1. Keep using the current implementation and record any reliability issues.
2. Choose the project name and license.
3. Design the user configuration format.
4. Add `probe` output suitable for sharing without unique machine identifiers.
5. Implement interactive calibration before asking others to test.
6. Refactor only after recording tests for the current working machine.
