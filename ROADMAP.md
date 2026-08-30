# Roadmap

Tablet Auto Rotate aims to provide safe, broadly compatible tablet-mode rotation
for Hyprland, with desktop-specific behavior kept in optional integrations.
Hardware support is evidence-based: ambiguous discovery must fail safely, and a
profile is considered physically verified only after its display, touch,
suspend, and folded-startup behavior has been exercised on that machine.

## Current state — 0.4.0

The following foundations are complete:

- MIT licensing, standard Python packaging, release wheels, and an importable
  application core
- Validated per-machine TOML configuration under the XDG config directory
- Conservative `SW_TABLET_MODE` capability discovery with ambiguity refusal
- Display accelerometer discovery for a unique sensor or a sensor sharing the
  hinge sensor's HID hub
- Interactive and offline read-only axis calibration
- Stable orientation filtering and synchronized Hyprland display/touch changes
- Safe laptop-mode restoration, device rediscovery, suspend/resume handling,
  and already-folded startup
- Non-blocking sensor reads that keep switch handling responsive during kernel
  sensor stalls
- Optional Omarchy layer-surface refresh with guarded fallback behavior
- Guarded systemd user-service installation and removal
- Human-readable diagnostics plus sanitized, versioned JSON reports
- Validated, opt-in IIO mount-matrix application with a strict required mode
- Fixture-driven discovery tests, community report templates, and compatibility
  evidence levels
- Maintainer verification on the Acer TravelMate B311R-33 across all four
  orientations, touchscreen alignment, suspend/resume, and folded startup

Hosted CI is intentionally not part of the release process. Local automated
tests, package builds, installed self-tests, and relevant physical checks form
the release gate.

## Next milestone — broader hardware intake

The immediate priority is making community reports straightforward to consume:

1. Collect sanitized 0.4.0 probe reports from additional convertible computers.
2. Convert reviewed reports into minimal regression fixtures and, where the
   evidence supports them, reusable profiles.
3. Record successes and failures in the compatibility matrix without claiming
   untested support.
4. Improve actionable diagnostics for ambiguous switches and sensor topologies
   found in those reports.
5. Keep hardware-specific facts in profiles and fixtures rather than adding
   model-specific policy to the daemon.

This milestone relies on community hardware reports; contributors do not need
to provide machines to the maintainer.

## Hardware discovery and calibration

- Collect fixtures containing standard IIO mount matrices from hardware that
  exposes them and physically verify the resulting hardware-frame orientation.
- Support additional dual-accelerometer and base/display sensor layouts.
- Distinguish machines where a hinge sensor is absent, hidden, or insufficient
  to identify the display sensor.
- Add automatic profile matching using non-serializing hardware identifiers.
- Improve calibration guidance and generate a complete reviewed config file.
- Preserve ambiguity refusal whenever available evidence is insufficient.

## Hyprland compatibility

- Detect Hyprland capabilities and configuration provider behavior instead of
  assuming one command sequence.
- Support additional Hyprland configuration providers where runtime transform
  behavior differs.
- Expand monitor hotplug, disabled-panel, compositor reload, and multi-monitor
  regression coverage.
- Keep display and touchscreen transforms synchronized without changing mode,
  resolution, scale, enabled state, or unrelated outputs.
- Reproduce compositor-specific failures against current Hyprland before filing
  narrowly scoped upstream reports.

## Desktop integration

- Keep the core daemon independent of Omarchy.
- Replace the Omarchy one-pixel remap workaround when a supported surface-remap
  mechanism becomes available.
- Consider an optional Omarchy setup command that installs only small,
  update-safe user-owned configuration snippets.
- Consider a rotation-lock control or status UI after the daemon control
  interface is stable.

## Installation and distribution

- Provide a safe installer for Hyprland snippets that merges or includes small
  files without replacing existing user configuration.
- Document complete removal of every installed file.
- Add release checksums alongside wheel artifacts.
- Evaluate distribution packages, including an AUR package, after configuration
  and service interfaces stabilize.

## Testing and compatibility

Automated coverage should continue expanding for:

- noisy, flat, diagonal, moving, and stalled sensor input;
- switch and sensor loss or rediscovery;
- ambiguous and multi-sensor discovery fixtures;
- monitor disable, hotplug, reload, and multiple-monitor behavior;
- compositor command generation and post-apply verification;
- configuration, calibration, diagnostics schema, and service lifecycle safety.

Physical compatibility records should state the exact model, firmware, kernel,
Hyprland version, desktop integration, project version, and results for:

- laptop/tablet transitions;
- all four display orientations;
- touchscreen and stylus alignment;
- suspend/resume and startup while folded;
- device disconnect/reconnect behavior;
- internal-only and external-monitor configurations.

Untested behavior must remain explicitly marked as untested.

## Safety principles

- Open only a validated tablet switch and never grab input devices.
- Run without root privileges and avoid system-wide configuration.
- Treat device names, paths, and diagnostic data as untrusted input.
- Use argument arrays, bounded subprocess timeouts, and a singleton runtime lock.
- Retain the last valid orientation when sensor readings are uncertain.
- Restore the configured laptop baseline whenever tablet mode ends.
- Refuse changes when hardware or compositor state cannot be identified safely.
