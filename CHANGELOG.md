# Changelog

## 0.5.0

- Replace the daemon's fixed 10 Hz wakeup loop with deadline-driven polling
  over tablet-switch, Hyprland-event, and stop-wakeup descriptors plus sensor
  and recovery deadlines.
- Keep one bounded accelerometer worker alive, reuse its sysfs descriptors and
  scale metadata, and invalidate the session safely after read failures.
- Retry an explicitly configured tablet switch directly after disconnects
  while retaining conservative full discovery for automatic selection, and
  bound repeated missing-device scans with exponential backoff.
- Make runtime configuration instance-local so multiple daemon objects cannot
  overwrite one another through module globals.
- Reconcile monitor changes from Hyprland events with periodic polling as a
  fallback, and make the post-transform layer remap non-blocking and
  generation-safe.
- Centralize bounded subprocess execution, reuse the confirmed monitor status
  after a transform, and keep the touch-device safety check immediately before
  each display mutation.
- Move orientation filtering, CLI parsing, and version lookup into focused
  modules so lightweight commands do not import the daemon core.

## 0.4.1

- Add a bounded AI-assisted setup prompt covering safe installation, discovery,
  calibration, physical validation, persistent startup, and rollback reporting.
- Correct repository links in package metadata and generated systemd units.
- Recommend isolated installation and document the current local release checks.
- Add private vulnerability-reporting instructions for public users.

## 0.4.0

- Add validated Linux IIO mount-matrix discovery and application with
  backward-compatible `ignore`, optional `auto`, and strict `require` policies.
- Include sanitized mount-matrix metadata in probe reports and hardware
  fixtures while preserving selective physical-axis reads.
- Rewrite the README as an end-user installation, configuration, operation, and
  troubleshooting guide.
- Update the roadmap to distinguish completed 0.3.0 foundations from upcoming
  hardware-support, compatibility, integration, and distribution work.

## 0.3.0

- Add deterministic, versioned JSON output for `--probe` and `--doctor`.
- Sanitize configuration paths, device labels, error text, and HID instance
  identifiers in shareable diagnostics.
- Give compatibility checks stable IDs and statuses for tooling and issue
  intake while preserving the existing human-readable commands.

## 0.2.1

- Keep tablet-switch handling and the daemon event loop responsive when a
  Linux HID/IIO sysfs sensor read blocks inside the kernel.
- Allow only one outstanding sensor reader, avoiding unbounded threads or
  subprocesses while a driver is stalled.
- Discard stale sensor results after tablet/laptop state changes.
- Add regression tests for blocked, completed, and stale asynchronous reads.

## 0.2.0

- First hardware-verified release.
- Add configurable hardware/sensor mapping, conservative discovery, read-only
  diagnostics and calibration, packaging, service lifecycle helpers, and
  community hardware fixtures.
