# Changelog

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
