# Optimization implementation plan

This plan turns the opportunities in [`potential_optimization.md`](potential_optimization.md) into independently reviewable changes. The goal is lower idle wakeups, less repeated I/O, and faster switch response without weakening tablet-switch recovery, sensor-stall isolation, touch/display synchronization, or post-apply verification.

## Execution status (2026-09-02)

The implementation now includes the automated portions of Phases 1–5, the
validated-path retry and bounded backoff from Phase 7, and the useful cleanup
from Phase 8. In particular, the daemon uses deadline-driven polling, one
persistent bounded sensor worker, cached IIO scale/raw descriptors, explicit
runtime configuration, Hyprland event reconciliation with a polling fallback,
fresh-status reuse, and a generation-bound nonblocking layer-remap operation.
The detailed safety cases formerly embedded in the installed self-test now
have direct pytest coverage; the installed command remains a compact smoke
test.

Phase 6 has not been activated: there is no target-hardware evidence yet that
the remaining `hyprctl` fork/exec cost justifies taking ownership of the direct
control-socket protocol. The optional netlink wake hint from Phase 7 is also
not implemented because periodic recovery is now backed off and no remaining
scan cost has been demonstrated.

Automated verification was run on Linux 7.1.9 (x86-64) with Python 3.14.7.
This environment has `hyprctl`, but no accessible live Hyprland session or
tablet hardware, and does not provide `strace`, `pidstat`, or `powertop`.
Consequently, wakeup/process/power baselines and the physical acceptance
matrix still need to be collected on the Acer; no synthetic values are being
substituted for those measurements.

The pure orientation code, CLI, and version lookup were extracted. The larger
switch/sensor/Hyprland/daemon file split remains a follow-up cleanup: the new
runtime objects already have injectable boundaries, while moving those
implementations now would add substantial review churn without changing the
measurable behavior. `core.py` therefore remains the compatibility facade for
this optimization pass.

### Live Acer validation (2026-09-02)

- `--doctor --verbose` passed the input ABI, configured switch, accelerometer,
  Hyprland output, touchscreen, and Omarchy checks.
- `--probe --verbose` uniquely selected the configured `Intel HID switches`
  node and the hinge-associated display accelerometer while unfolded.
- A live dry run detected tablet entry, committed transforms 1, 2, 3, and 0 in
  the four physical orientations, and detected the return to laptop mode.
- A live mutation run applied all four display/touch transforms. The user
  physically confirmed that display rendering and touch input remained aligned
  in every orientation.
- Intermittent IIO reads exceeded the 500 ms warning threshold, but switch
  resynchronization and the final laptop-mode transform 0 remained responsive.
- Hyprland did not confirm the one-pixel position nudge on this system, so the
  guarded Omarchy shell-restart fallback ran successfully. No output position
  was left nudged.
- The working-tree daemon stopped cleanly, and the previously installed user
  service was restored and confirmed active after the test.

Rapid repeated fold/unfold, suspend/resume, compositor restart, disabled-panel,
and external-monitor scenarios remain outstanding, as do quantitative wakeup,
process-count, latency, and power measurements.

## Non-negotiable invariants

Every phase must preserve these behaviors:

- Never grab the evdev switch device.
- Keep `EVIOCGSW`, periodic switch resynchronization, and `SYN_DROPPED` recovery.
- Read orientation only in confirmed tablet mode.
- Retain the last valid tablet transform when a sensor sample is uncertain.
- Force transform 0 when laptop mode is confirmed.
- Check for the configured touch device immediately before every transform apply.
- Apply the display and touch transforms in the same Hyprland eval.
- Verify live monitor state after every transform apply.
- Refuse ambiguous switch, accelerometer, monitor, or touch-device state.
- Keep kernel sensor reads off the policy thread and never accumulate blocked reader threads.
- Keep the daemon dependency-free at runtime and unprivileged.

## Delivery strategy

Use one focused pull request per phase. Within a phase, put behavior-preserving extraction in a separate commit from the behavioral optimization. Keep `core.py` as a temporary compatibility facade while tests and imports move to the new modules; remove facade exports only in a later cleanup.

Run the full local release gate after every phase:

```console
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
.venv/bin/tablet-auto-rotate --self-test
```

Phases that change runtime timing, device reuse, Hyprland IPC, or remapping also require the physical checks listed in their acceptance criteria.

## Phase 0: Establish measurements and test seams

### Work

1. Record a baseline on the Acer for:
   - daemon wakeups per minute in stable laptop mode;
   - `hyprctl` processes per minute in stable laptop and tablet modes;
   - sensor reads, worker-thread creations, and sysfs opens per minute in tablet mode;
   - median and worst observed IIO read latency;
   - tablet-switch-to-policy latency while idle;
   - time from `SIGTERM` to process exit.
2. Use external development tools such as `strace`, `pidstat`, and `powertop` where available. Do not add a permanent runtime dependency merely to collect the baseline.
3. Move safety-critical cases currently present only in `run_self_test()` into pytest, especially:
   - monitor-transform verification retries;
   - layer-remap preconditions, nudge verification, restoration, and fallback;
   - invalid transform and coordinate rejection;
   - touch-device presence and malformed Hyprland JSON handling.
4. Consolidate the repeated `RecordingLogger` test helper.
5. Add deterministic test doubles for a monotonic clock, wait/poll operations, switch reader, sensor reader, and Hyprland client.

### Acceptance criteria

- A checked-in or release-noted baseline records the commands, duration, hardware/software versions, and results.
- All safety paths that will be moved or rewritten have direct pytest coverage.
- Existing behavior and CLI output remain unchanged.
- The release gate passes.

## Phase 1: Introduce explicit boundaries and runtime configuration

### Work

1. Pass an immutable runtime configuration into the daemon and subsystem helpers instead of mutating `core.py` globals with `apply_config()`.
2. Derive numeric axis indexes once from `HardwareConfig`; retain the validated user-facing TOML representation.
3. Define narrow injectable protocols or duck-typed interfaces for the daemon's switch, sensor, Hyprland client, clock, and waiter. Do not introduce a framework or third-party dependency.
4. Optionally extract the already-pure classification and filtering code into `orientation.py`. Defer `switch.py`, `sensor.py`, `hyprland.py`, and `daemon.py` until the phase that changes each subsystem.
5. Preserve moved imports through `core.py` while callers and tests migrate.

### Acceptance criteria

- Two daemon instances can be constructed with different configurations without shared mutable state.
- Tests no longer need to monkeypatch hardware configuration globals.
- Extraction commits have no intended runtime behavior change.
- The release gate passes before any event-loop change begins.

## Phase 2: Replace the fixed loop with deadline-driven waiting

### Work

1. Extract the evdev ABI and `SwitchReader` into `switch.py`, and the runtime coordinator into `daemon.py`, without changing behavior.
2. Add a wait-set abstraction built on `selectors` or `select.poll()`.
3. Register:
   - the current evdev switch fd;
   - a nonblocking wake pipe or socketpair used by `request_stop()` and signal handling;
   - later, optional Hyprland event and worker-completion fds.
4. Make `SwitchReader` expose its current fd and scheduling deadlines without surrendering ownership. Safely unregister an old fd before closing or replacing it.
5. Calculate the next absolute deadline from:
   - the 10 Hz tablet sensor cadence;
   - the switch ioctl resync;
   - device rediscovery;
   - apply retry/backoff;
   - monitor reconciliation;
   - any active remap stage.
6. After every wake, drain switch events and handle a tablet-to-laptop transition before sensor or compositor work.
7. Retain the existing two-second monitor reconciliation initially. This isolates the loop change from reconciliation-policy changes.
8. Handle fd error/hangup, spurious wakeups, interrupted waits, clock movement in tests, and deadlines already in the past without creating a busy loop.

### Tests

- Switch readability wakes the daemon immediately.
- A stop request wakes an otherwise indefinite wait and exits promptly.
- Sensor work is scheduled only in tablet mode and no faster than 10 Hz.
- Resync, retry, rediscovery, and monitor deadlines fire once when due.
- Replaced or lost switch fds are unregistered and rediscovered.
- A stream containing `SYN_DROPPED` still resynchronizes through `EVIOCGSW`.
- A due laptop-mode reset wins over pending sensor or rotation work.

### Acceptance criteria

- Stable laptop mode no longer wakes on a fixed 100 ms timer.
- Idle switch response is faster than the old 0–100 ms polling delay.
- `SIGINT` and `SIGTERM` stop the daemon within 250 ms when no subprocess is active.
- Folded startup, laptop/tablet transitions, and suspend/resume pass on the Acer.
- No increase occurs in failed switch resyncs or busy-loop behavior.

## Phase 3: Rework sensor reading and cache IIO state

### Work

1. Extract IIO discovery, readings, mount-matrix handling, and `SensorReader` into `sensor.py` without changing behavior.
2. Give `SensorReader` one long-lived daemon worker with a bounded request slot and result slot. Never queue more than one read request.
3. Keep a generation on requests and results so a reading begun before reset or mode change is discarded.
4. Preserve the current stuck-read policy:
   - the policy thread remains responsive;
   - repeated warnings are rate-limited;
   - no replacement worker is created while the old kernel read is blocked;
   - leaving tablet mode invalidates the eventual result.
5. Let the worker own all sample fds so open/read/close operations cannot race the policy thread.
6. On each conservatively selected device instance:
   - read each unique required scale attribute once;
   - open only the raw physical axes required for logical screen X/Y;
   - use `lseek(0)` plus bounded `read()` for each sample;
   - close all fds on worker shutdown, invalidation, or read failure.
7. Do not let a cached IIO path bypass ambiguity detection. A matching `iio:deviceN` pathname proves neither identity nor unique selection, so entry to tablet mode must still perform enough topology enumeration to reject a newly ambiguous layout.
8. Cache metadata only after the topology census selects the same device uniquely; otherwise perform full conservative discovery.

### Tests

- Many successful samples create one worker thread, not one per sample.
- The request and result channels remain bounded under a slow read.
- A blocked read never blocks switch handling or spawns another worker.
- Results from an obsolete generation are discarded.
- A common scale path is read once per validated device instance.
- Scale and raw fds are refreshed after invalidation or a read error.
- Selection rejects a path whose device identity changed or whose topology became ambiguous.
- Logical Z remains unread unless a configured mount matrix makes its physical value necessary for logical X/Y.

### Acceptance criteria

- Tablet mode shows approximately zero thread creations per sample after worker startup.
- Sysfs opens per sample fall to zero after the validated session is initialized.
- All four orientations still commit after the stable hold.
- Flat, diagonal, moving, stale-generation, and intentionally blocked samples remain non-mutating.
- Suspend/resume and repeated fold/unfold cycles recover without restarting the daemon.

## Phase 4: Centralize Hyprland operations and reduce reconciliation cost

### Work

1. Extract command construction, parsing, queries, mutations, and `MonitorStatus` into `hyprland.py` without changing behavior.
2. Introduce a `HyprlandClient` interface whose subprocess implementation owns:
   - argv construction;
   - bounded timeouts;
   - stdout/stderr decoding and truncation;
   - JSON parsing and error reporting.
3. Preserve exact argument-array execution with `shell=False`.
4. Change transform verification to return the fresh confirmed `MonitorStatus`, not only `True` or `False`.
5. Pass that fresh post-apply status into the first layer-remap stage, eliminating its redundant initial monitor query. Never use the pre-apply status as verification.
6. Keep the touch query immediately before each transform mutation. Do not cache a positive result between attempts.
7. Add a persistent, nonblocking `.socket2.sock` event listener for `monitoradded`, `monitorremoved`, their v2 forms, and `configreloaded`:
   - cap the receive buffer and individual line length;
   - tolerate partial and duplicate events;
   - debounce an event burst into one reconciliation;
   - reconnect with backoff;
   - fall back safely when the event socket or instance path is unavailable.
8. When the event listener is healthy, start with a five-second periodic reconciliation heartbeat. When it is unavailable, retain the current two-second fallback. Revisit the interval only after measuring recovery latency and process count.

### Tests

- All client methods enforce timeouts and consistently sanitize errors.
- A transform apply always performs a fresh touch check first.
- Failed or malformed touch/monitor queries prevent mutation.
- Verification returns only a fresh, matching, enabled monitor status.
- The returned status is reused by remap without skipping any post-mutation check.
- Relevant event lines schedule reconciliation; unrelated, partial, duplicate, oversized, and malformed lines are harmless.
- Event-socket loss re-enables the two-second polling fallback.
- The periodic heartbeat repairs a silently changed transform even when no event arrives.

### Acceptance criteria

- Stable-state `hyprctl` process count is reduced from the measured baseline.
- Monitor hotplug and configuration reload trigger immediate reconciliation.
- Silent external transform changes are still corrected by the periodic heartbeat.
- Disabled-panel and multi-monitor guards behave exactly as before.
- Touch alignment is physically checked in all four orientations.

## Phase 5: Make layer remapping non-blocking between IPC operations

### Work

1. Replace `fast_layer_remap()` sleeps with daemon-scheduled states:
   - validate and capture original position;
   - nudge;
   - wait for the nudge deadline;
   - verify the nudge;
   - restore;
   - wait for the settle deadline;
   - verify the final position and transform.
2. Tag the operation with the desired-transform generation.
3. If the generation changes before the nudge, cancel without mutation.
4. If the generation changes after a successful nudge, restore the original position before applying another transform or completing shutdown.
5. On any failure after a successful nudge, attempt restoration once and then use the existing guarded Omarchy shell-restart fallback.
6. Keep the single-active-monitor, integer-position, transform, and post-restore checks.

### Tests

- Cancellation is safe at every state boundary.
- Tablet-to-laptop transition during both waits is processed immediately and leaves the original monitor position restored.
- Stop during a nudge makes a bounded best-effort restore.
- Query, nudge, restore, and verification failures take the expected fallback path once.
- Stale callbacks cannot advance a newer remap generation.
- Multiple active monitors remain a no-mutation rejection.

### Acceptance criteria

- Switch events are serviced during both the 75 ms and 300 ms waits.
- Rapid fold/unfold testing never leaves the output at the nudged coordinate.
- Omarchy layer surfaces remain correct in all four orientations.
- The shell-restart fallback remains functional and rate-limited by actual remap attempts.

## Phase 6: Evaluate direct Hyprland control-socket requests

Do this only if Phase 4 measurements show that remaining `hyprctl` fork/exec cost is material.

### Work

1. Implement a socket client with the same `HyprlandClient` contract.
2. For every request, open `.socket.sock`, write one bounded request, read one bounded response with a timeout, and close the connection. Never retain a control-socket connection.
3. Match the installed Hyprland request flags and response parsing with fixture-based protocol tests.
4. Keep the subprocess client as a compatibility fallback for unsupported or unverified behavior.
5. Treat ambiguous mutation failures carefully: if a socket disconnect or timeout occurs after sending a transform, query live status before deciding whether to retry. Do not blindly replay a mutation that may already have succeeded.
6. Resolve and validate the runtime directory and instance signature without accepting path traversal or an unowned socket endpoint.

### Tests

- Each request creates and closes exactly one connection.
- Connect, send, receive, timeout, oversized response, early EOF, and malformed response paths fail closed.
- Query failures may fall back to `hyprctl` without mutation.
- Ambiguous mutation results trigger status reconciliation rather than immediate replay.
- Configured monitor and touch names remain safely encoded in eval requests.

### Acceptance criteria

- Direct IPC matches subprocess behavior for monitor queries, device queries, transform eval, and position eval on the supported Hyprland version.
- Fork/exec count during a rotation and stable reconciliation falls as expected.
- No compositor stalls occur during fault injection, daemon termination, or Hyprland restart.
- If the measured gain is negligible, keep the shared subprocess client and do not ship this phase.

## Phase 7: Make missing-device recovery cheaper

### Work

1. Retry the last validated switch before a global glob scan only when it is the explicit configured path.
2. Re-run capability and identity validation before opening the cached evdev path. Continue enumerating enough IIO topology to preserve unique accelerometer selection.
3. Add bounded exponential backoff for repeated full-scan failures, starting at one second and capped at a value chosen from resume-latency testing.
4. Reset backoff on a successful discovery, a relevant fd hangup, or a trusted hotplug wakeup.
5. Do not use inotify for `/sys`. If missing-device scans remain measurable, add a small netlink uevent listener only as a wake hint:
   - cap datagrams and parsing work;
   - accept only relevant input/IIO subsystem actions;
   - never trust event fields as proof of device identity;
   - always run normal validation before use;
   - retain periodic retry for dropped events.

### Tests

- An explicitly configured switch candidate avoids a full scan only after capability and identity revalidation.
- Reused event and IIO numbers with changed identity are rejected.
- Backoff grows, caps, and resets at the correct events.
- Malformed or spoofed uevents cannot select a device or cause mutation.
- Dropped/no uevents still recover through periodic fallback.

### Acceptance criteria

- Missing-device operation performs materially fewer full sysfs scans.
- Suspend/resume recovery remains within the agreed latency bound.
- Switch and accelerometer replacement or renumbering is handled conservatively.

## Phase 8: Low-risk cleanup after measurements

Only do cleanups that remain useful after the new boundaries settle:

- Move argument parsing and mode dispatch into `cli.py`; make `__init__.py` lightweight.
- Lazy-import command implementations if CLI startup measurement justifies it.
- Establish one maintained source fallback for the package version and one source for hardware defaults.
- Reduce `run_self_test()` to a thin installed smoke test after its detailed cases live in pytest.
- Compile the IIO device-number regex once.
- Pass a previously computed magnitude from classification into the filter only if the API remains clear.
- Add `slots=True` to stable internal dataclasses only if it does not complicate compatibility.

## Final verification matrix

Before declaring the optimization work complete, run:

| Scenario | Required result |
| --- | --- |
| Start unfolded | Transform 0; no sensor sampling |
| Start already folded | No mutation until a stable orientation passes the filter |
| Fold and unfold rapidly | Prompt laptop reset; no stale sensor/remap action |
| Four tablet orientations | Display and touch remain aligned |
| Flat, diagonal, and moving device | Retain the previous valid orientation |
| Blocked IIO read | Switch and shutdown handling remain responsive |
| Suspend/resume in both modes | Devices revalidate or rediscover without restart |
| Switch `SYN_DROPPED` | State is recovered with `EVIOCGSW` |
| Hyprland reload/restart | Event listener reconnects or polling fallback recovers |
| Internal output disabled | No unsafe transform attempt |
| External monitor attached | Multi-monitor remap guard remains non-mutating |
| Touch device absent | No display-only transform |
| Stop during remap nudge | Original output position is restored best-effort |

Compare final measurements with Phase 0. A phase counts as an optimization only if it improves the targeted metric without a safety, correctness, recovery-latency, or maintainability regression.
