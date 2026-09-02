# Potential optimizations

Explore-only notes from a pass over the current tree. Nothing in this document is a commitment to change behavior, and several items below would be unsafe if applied without keeping the existing switch, sensor, and compositor guards.

Tablet Auto Rotate is a small, zero-dependency Python 3.11 daemon. Almost all runtime behavior lives in `src/tablet_auto_rotate/core.py` (~2,777 lines): evdev switch I/O, IIO sysfs reads, orientation filtering, Hyprland/`hyprctl` mutation, Omarchy remap, CLI, diagnostics, and an in-process self-test. `config.py`, `discovery.py`, `calibration.py`, and `lifecycle.py` are already factored as pure helpers.

The hot path is not CPU-bound math. It is wakeups, sysfs open/read/close, one-shot threads, and `hyprctl` process spawns. The highest-value work is likely idle-power and I/O, not micro-optimizing `classify_orientation`. Treat the ordering below as hypotheses until wakeups, subprocess counts, and IIO latency have been measured on the target hardware.

## How the runtime actually spends time

`RotationDaemon.run()` is a fixed 10 Hz poll:

```python
while not self.stop.is_set():
    now = time.monotonic()
    try:
        self.switch.poll(now)
        self._handle_switch_state(now)
        if self.tablet_mode is True and now >= self.next_sensor_sample:
            self._sample_sensor(now)
            self.next_sensor_sample = now + LOOP_INTERVAL
        ...
        if now >= self.next_monitor_check:
            self._reconcile_monitor(now)
            self.next_monitor_check = now + MONITOR_CHECK_INTERVAL
    ...
    self.stop.wait(LOOP_INTERVAL)
```

In laptop mode that still wakes 10 times a second. In tablet mode each sample also opens several IIO sysfs files on a newly created thread. After a confirmed rotation, Omarchy remap adds more `hyprctl` calls and **375 ms of blocking `sleep`** on the same thread (`LAYER_REMAP_NUDGE_DELAY` + `LAYER_REMAP_SETTLE_DELAY`).

`MAX_SAMPLE_GAP_SECONDS` is 0.25 s and `LOOP_INTERVAL` is 0.10 s. One skipped sampling opportunity normally leaves about 200 ms between accepted readings and does not reset the filter; two skipped opportunities, sufficient scheduler jitter, or a slow IIO read can push the gap beyond 250 ms and restart the 350 ms hold. Any change that lowers the sampling frequency or makes sampling less regular has to revisit that gap.

## Highest-impact runtime opportunities

### 1. Sleep on the switch fd instead of 10 Hz polling

`SwitchReader` already opens the node `O_NONBLOCK` and drains `input_event` records, but the daemon never `select`/`poll`s that fd. It always `Event.wait(0.10)`.

A deadline-driven loop would:

- `poll()` the switch fd
- wake immediately on `SW_TABLET_MODE`
- otherwise sleep until the next of: sensor sample, monitor reconcile, switch ioctl resync (5 s), apply retry, device rediscovery

In stable laptop mode this removes the fixed 100 ms wakeup and makes switch events react immediately. With the current reconciliation policy the next deadline is still normally the 2 s monitor check, not the 5 s switch resync; reaching a 5 s or longer idle interval also requires adaptive or event-assisted monitor reconciliation. The size of the power win should be measured rather than assumed.

Keep the existing ioctl resync and `SYN_DROPPED` handling; only change *when* the loop wakes.

A blocking fd wait also needs an explicit wake fd, such as a nonblocking pipe or socketpair registered with the poller. `threading.Event.set()` does not by itself wake `poll()`, so signal handling and `request_stop()` must notify that fd to preserve prompt shutdown.

### 2. Stop creating a thread per accelerometer sample

`SensorReader` starts a **new daemon `threading.Thread` for every IIO read**, then immediately prefetches the next one after a success. In tablet mode that is ~10 thread create/teardown cycles per second.

The isolation is the right idea (HID sysfs can block in the kernel; changelog 0.2.1). A **single long-lived worker** with a generation counter and at most one outstanding request would keep that isolation and drop the spawn cost.

This does not solve an IIO syscall that never returns: Python cannot safely cancel that worker, and closing an fd from another thread is not a reliable cancellation mechanism. Define the wedged-worker behavior explicitly and do not queue requests behind a stuck read. The current implementation already becomes unable to start another sensor read while its one worker is stuck, so the redesign must at least preserve switch responsiveness and avoid making recovery worse.

Also: `RotationDaemon._handle_switch_state()` calls `self.sensor.reset()` on every fold, which drops the device and re-runs full IIO discovery. Close live sample fds when leaving tablet mode. A cached IIO path must not bypass conservative selection: validating that the same device still exists does not prove that a second equally plausible accelerometer has not appeared. Reuse cached metadata only after a topology census still selects it uniquely; otherwise retain full discovery on entry to tablet mode.

### 3. Cache IIO sysfs file descriptors and scale

`_read_text()` does open/read/close on every attribute. `read_orientation_accel()` does that for each required raw axis **and** its scale. When the device exposes a shared `in_accel_scale`, the same file is opened twice per sample.

IIO scale is effectively constant for the life of one validated device instance. Practical steps:

- Read scale once after discovery; refresh on revalidation, rediscovery, or a read error
- Keep raw FDs open, `lseek(0)` + `read`, and close them on reset or device loss
- Deduplicate reads when `scale_paths` repeats the same path

Do **not** jump to IIO buffers without hardware proof. The code already skips logical Z because some HID drivers stall; a buffer enable could reintroduce that.

### 4. Reduce `hyprctl` process spawns, optionally with direct socket requests

There are four near-identical `subprocess.run(["hyprctl", ...])` sites (devices query, monitors query, eval, and position eval), plus a fifth subprocess site for `omarchy restart shell`. A successful rotation can look like:

| Step | Process |
| --- | --- |
| Pre-check | `hyprctl -j monitors all` |
| Touch guard | `hyprctl -j devices` |
| Apply | `hyprctl eval ...` |
| Verify | up to 3× `hyprctl -j monitors all` |
| Omarchy remap | 2× position eval + 3× monitor query, with 375 ms sleep |

Then `_reconcile_monitor()` still runs `hyprctl -j monitors all` **every 2 s whenever `desired_transform` is set**, including laptop mode with transform 0.

`hyprctl` is a short-lived client of `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock`. A small direct Unix-socket helper (still no third-party dependencies) could remove fork/exec from the apply and reconcile paths, but **the control socket must not be persistent**. Hyprland documents this interface as synchronous and warns that an unclosed connection can freeze the compositor until its five-second timeout. Open, write, read, and close a fresh connection for every request; use strict timeouts and response-size limits. See the [Hyprland IPC documentation](https://wiki.hypr.land/0.56.0/IPC/).

Even without the socket:

- One shared runner for argv, timeout, stderr truncation
- Keep the positive touch-device check immediately before every transform apply. It is not an idle-path query, and caching it would weaken the monitor-plus-touch safety invariant.
- Have post-apply verification return its fresh `MonitorStatus`, then pass that result into the first remap stage. Never reuse the pre-apply status as post-apply verification.
- Subscribe persistently to Hyprland’s event socket (`.socket2.sock`) so monitor add/remove and config reload events trigger an immediate reconciliation.
- Retain a slower periodic reconciliation as a safety net. The documented event list does not provide a general event for every external monitor-transform change.

### 5. Do not block the policy loop during layer remap

`fast_layer_remap()` calls `time.sleep` twice on the daemon thread. During that window switch events are not drained. A rapid tablet→laptop fold can wait ~375 ms plus remaining `hyprctl` time.

A small state machine (nudge → wait → verify → restore → wait → verify) driven by the same deadline loop would keep switch handling live during the waits. The 375 ms still has to elapse; it just should not stall evdev. The synchronous Hyprland queries and mutations can still block up to their own timeouts.

Tag each remap with the desired-transform generation and define cancellation carefully. If tablet mode turns off while the output is nudged, restore the original position before abandoning the operation or applying a new transform. Errors and daemon shutdown need the same best-effort restore invariant.

### 6. Make device rediscovery cheaper before adding hotplug IPC

While the switch or accel is missing, discovery re-globs `/sys/class/input/event*/device/name` and `/sys/bus/iio/devices/iio:device*` every `DEVICE_RETRY_INTERVAL` (1 s). `SwitchReader._open_if_needed` always calls `discover_switch_selection()` even when the previous node still exists.

Retry an explicitly configured, fully revalidated switch path first, then fall back to a full scan with bounded or exponential backoff. Accelerometer selection has no equivalent configured path, so it still needs enough topology enumeration to preserve ambiguity refusal. This makes the common switch suspend/resume case cheaper without silently weakening sensor selection.

Do not plan on inotify for `/sys/bus/iio/devices`: Linux documents `/sys` among the pseudo-filesystems that inotify cannot monitor. Watching `/dev/input` alone would cover only part of the problem. If measurements show that fallback scans still matter, consume kernel device uevents through a carefully bounded netlink listener, or keep the simpler periodic fallback. See [`inotify(7)` limitations](https://man7.org/linux/man-pages/man7/inotify.7.html).

## Code-structure opportunities

These are not CPU wins by themselves. They make the runtime changes above safer and remove duplicated work.

### Split `core.py`

Natural modules, following the existing `discovery.py` / `calibration.py` pattern:

| Module | Contents |
| --- | --- |
| `switch.py` | evdev ABI, `SwitchReader`, switch discovery I/O |
| `sensor.py` | IIO discovery, mount matrix, `SensorReader` |
| `orientation.py` | `classify_orientation`, `OrientationFilter` |
| `hyprland.py` | eval builders, queries, verify, remap |
| `daemon.py` | `RotationDaemon`, lock, main loop |
| `diagnostics.py` | probe/doctor reports |
| `cli.py` | argparse + mode dispatch (today `cli.py` only re-exports `main`) |

`run_self_test()` is ~400 lines that largely duplicate `tests/test_core.py`. Either generate it from the pytest suite or keep a thin packaged smoke test and drop the copy.

### Stop using module globals as live config

`HardwareConfig` is validated, then `apply_config()` writes `OUTPUT_NAME`, `TOUCH_DEVICE_NAME`, `AXIS_ORDER`, etc. as process-wide globals. Tests monkeypatch those names. Every helper reads the global.

Pass a config (or a small `Runtime` object) into the daemon, readers, and Hyprland helpers. Defaults already exist on `HardwareConfig`; the parallel constants at the top of `core.py` (lines 54–65) are a second copy of the Acer profile.

### One source of version and defaults

Version appears in `pyproject.toml`, `SOURCE_VERSION` in `core.py`, and the `PackageNotFoundError` fallback in `__init__.py`. Hardware defaults appear in `HardwareConfig`, `core.py` globals, and `profiles/acer-travelmate-b311r-33.toml`.

### Lazy-import CLI modes

`tablet-auto-rotate --version` / `--help` currently import the full module containing the daemon, IIO, evdev, and diagnostics code (`cli.py` → `core.main`), although import itself does not perform device I/O. Moving argument parsing into `cli.py` and lazy-importing mode implementations would reduce startup and coupling. This is a low-priority cleanup unless measurement shows meaningful latency.

### Small, local cleanups (only worth doing with a split)

- `_iio_device_dirs()` runs the same `re.search` twice per path; compile once.
- `classify_orientation()` already computes magnitude; `OrientationFilter.update()` computes it again.
- Frozen dataclasses in `core.py` omit `slots=True` (discovery dataclasses already use it). Negligible at runtime.
- Tests copy `RecordingLogger` in `test_daemon.py`, `test_sensor_reader.py`, and `test_runtime_io.py`.
- `_read_text()` uses UTF-8 text mode for numeric sysfs; bytes + `int()`/`float()` is enough on the sample path.

## What not to optimize

- **Do not add runtime dependencies.** The project is explicit about this.
- **Do not rewrite in C/Rust.** The bottleneck is I/O and process spawns, not Python.
- **Do not grab the switch device** or drop `EVIOCGSW` / `SYN_DROPPED` recovery.
- **Do not enable IIO buffers or read Z “for completeness”** without re-running the Acer stall case.
- **Do not lower the sampling frequency materially below 10 Hz, or make sampling substantially less regular,** without revisiting `MAX_SAMPLE_GAP_SECONDS`; the filter may repeatedly reset.
- **Do not remove post-apply verification or the touch-device guard.** Those are safety, not waste. Reuse fresh results within the same operation only where their meaning remains valid.
- **Do not cache a positive touch-device result across transform attempts.** Keep that guard immediately before the mutation.

## Suggested order if implemented later

1. **Add lightweight measurements** — count loop wakeups, sensor reads and latency, worker creations, subprocesses, and reconciliation outcomes on the target hardware.
2. **Extract the daemon/switch boundary and pass configuration explicitly**, then implement the deadline/`poll` loop with a stop wake fd and adaptive reconciliation.
3. **Extract the sensor boundary**, then add the persistent single-request worker, validated device reuse, cached scale, and cached raw fds.
4. **Extract the Hyprland boundary and add one shared subprocess runner.** Make post-apply verification return the fresh status needed by remap.
5. **If measurements justify it, add fresh per-request control-socket IPC** and persistent event-socket reconciliation, while retaining a slow safety heartbeat.
6. **Convert layer remap to a generation-aware state machine** with guaranteed best-effort position restoration.
7. **Optimize missing-device recovery** with validated last-path retries and backoff; add netlink uevents only if scans remain material.

Avoid both a giant up-front split and behavioral changes inside the current monolith. Extract the smallest relevant boundary immediately before changing it, preserve existing tests at each step, and measure again after every optimization. The likely runtime wins are the event-driven loop, cheaper stable-state reconciliation, persistent sensor worker, and reduced subprocess use; the structure work is what makes those changes safe to test and maintain.

See [`optimization_implementation_plan.md`](optimization_implementation_plan.md) for the phased implementation, test requirements, fallbacks, and hardware acceptance criteria.
