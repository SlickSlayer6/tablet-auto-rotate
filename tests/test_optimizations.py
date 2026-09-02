from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import replace

from tablet_auto_rotate import core
from tablet_auto_rotate.config import HardwareConfig, RuntimeConfig


class RecordingLogger:
    def __init__(self):
        self.infos = []
        self.debugs = []
        self.errors = []

    def info(self, message):
        self.infos.append(message)

    def debug(self, message):
        self.debugs.append(message)

    def error(self, key, message, **_kwargs):
        self.errors.append((key, message))


def runtime(**changes):
    return RuntimeConfig.from_hardware(replace(HardwareConfig(), **changes))


def close_daemon(daemon):
    daemon.sensor.close()
    if daemon.hyprland_events is not None:
        daemon.hyprland_events.close()
    daemon._close_wake_pipe()


def test_daemons_keep_independent_runtime_configuration():
    first = core.RotationDaemon(
        RecordingLogger(), dry_run=True, runtime=runtime(output="DSI-1")
    )
    second = core.RotationDaemon(
        RecordingLogger(), dry_run=True, runtime=runtime(output="eDP-9")
    )
    try:
        assert 'output = "DSI-1"' in core.build_eval_command(1, first.runtime)
        assert 'output = "eDP-9"' in core.build_eval_command(1, second.runtime)
        assert first.runtime != second.runtime
    finally:
        close_daemon(first)
        close_daemon(second)


def test_stop_request_wakes_deadline_wait_promptly():
    daemon = core.RotationDaemon(RecordingLogger(), dry_run=True)
    daemon.switch._next_open = time.monotonic() + 10.0
    waiter = threading.Thread(target=daemon._wait_for_work)
    waiter.start()
    try:
        time.sleep(0.02)
        started = time.monotonic()
        daemon.request_stop()
        waiter.join(timeout=0.5)
        assert not waiter.is_alive()
        assert time.monotonic() - started < 0.25
    finally:
        close_daemon(daemon)


def test_idle_deadline_ignores_inactive_sensor_and_monitor_work():
    daemon = core.RotationDaemon(RecordingLogger(), dry_run=True)
    try:
        daemon.switch._next_open = 123.0
        daemon.next_sensor_sample = 0.0
        daemon.next_monitor_check = 0.0
        daemon.tablet_mode = False
        daemon.desired_transform = None

        assert daemon._next_deadline() == 123.0
    finally:
        close_daemon(daemon)


def test_wait_registers_switch_and_uses_the_next_absolute_deadline():
    class Switch:
        fd = 17

        def next_deadline(self):
            return 20.0

    class Poller:
        def __init__(self):
            self.registered = []
            self.timeout = None

        def register(self, fd, events):
            self.registered.append((fd, events))

        def poll(self, timeout):
            self.timeout = timeout
            return [(17, core.select.POLLIN)]

    poller = Poller()
    daemon = core.RotationDaemon(
        RecordingLogger(),
        dry_run=True,
        switch=Switch(),
        clock=lambda: 10.0,
        poll_factory=lambda: poller,
    )
    try:
        daemon._wait_for_work()
        assert {fd for fd, _events in poller.registered} == {
            daemon._wake_read_fd,
            17,
        }
        assert poller.timeout == 10_000
    finally:
        close_daemon(daemon)


def test_switch_reader_fast_retries_an_explicit_validated_path(monkeypatch):
    selected_runtime = runtime(preferred_switch_path="/dev/input/by-path/tablet")
    candidate = core.SwitchDevice(
        selected_runtime.preferred_switch_path,
        "event7",
        "/sys/class/input/event7/device/name",
        "Tablet switches",
    )
    reader = core.SwitchReader(RecordingLogger(), selected_runtime)
    reader._candidate = candidate
    monkeypatch.setattr(
        core,
        "_validate_switch_path",
        lambda _path, expected_name=None: (
            candidate if expected_name == candidate.name else None
        ),
    )
    monkeypatch.setattr(
        core, "_switch_codes", lambda _path: frozenset({core.SW_TABLET_MODE})
    )
    monkeypatch.setattr(core, "open_switch_device", lambda _device: (55, True))
    monkeypatch.setattr(
        core,
        "discover_switch_selection",
        lambda _runtime=None: (_ for _ in ()).throw(
            AssertionError("full scan should not run")
        ),
    )

    reader._open_if_needed(10.0)

    assert reader.fd == 55
    assert reader.state is True
    reader.close()


def test_switch_full_scan_retry_backoff_grows_caps_and_resets(monkeypatch):
    reader = core.SwitchReader(RecordingLogger(), runtime())
    monkeypatch.setattr(
        core,
        "discover_switch_selection",
        lambda _runtime=None: (
            core.SwitchSelection(
                status="none",
                selected=None,
                summary="not found",
                candidates=(),
            ),
            {},
        ),
    )

    now = 0.0
    observed = []
    for _ in range(6):
        reader._open_if_needed(now)
        observed.append(reader._next_open - now)
        now = reader._next_open

    assert observed == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
    reader._lose_device(now, "test")
    assert reader._next_open - now == core.DEVICE_RETRY_INTERVAL
    assert reader._open_retry_delay == core.DEVICE_RETRY_INTERVAL


def test_sensor_discovery_retry_backoff_resets_after_success(monkeypatch):
    selected = core.AccelDevice(
        iio_path="/sensor",
        hinge_path="",
        hid_hub="hub",
        name_path="/sensor/name",
        raw_paths=("x", "y", "z"),
        scale_paths=("s", "s", "s"),
    )
    results = iter((None, None, selected))
    monkeypatch.setattr(core, "discover_accel", lambda _runtime=None: next(results))
    reader = core.SensorReader(RecordingLogger())

    assert not reader._select_device(0.0)
    assert reader._next_discovery == 1.0
    assert not reader._select_device(1.0)
    assert reader._next_discovery == 3.0
    assert reader._select_device(3.0)
    assert reader.device == selected
    assert reader._discovery_retry_delay == core.DEVICE_RETRY_INTERVAL
    reader.close()


def test_accel_session_caches_common_scale_and_raw_descriptors(
    monkeypatch, tmp_path
):
    raw_paths = []
    for name, value in (("x", "100\n"), ("y", "-200\n"), ("z", "999\n")):
        path = tmp_path / name
        path.write_text(value, encoding="ascii")
        raw_paths.append(str(path))
    scale_path = tmp_path / "scale"
    scale_path.write_text("0.01\n", encoding="ascii")
    reads = []
    original_read_text = core._read_text

    def recording_read(path):
        reads.append(path)
        return original_read_text(path)

    monkeypatch.setattr(core, "_read_text", recording_read)
    device = core.AccelDevice(
        iio_path=str(tmp_path),
        hinge_path="",
        hid_hub="",
        name_path=str(tmp_path / "name"),
        raw_paths=tuple(raw_paths),
        scale_paths=(str(scale_path), str(scale_path), str(scale_path)),
    )
    session = core.AccelSampleSession(device, runtime())
    try:
        first = session.read()
        second = session.read()
    finally:
        session.close()

    assert first.values == second.values == (1.0, -2.0, 0.0)
    assert reads == [str(scale_path)]
    assert set(session.required_axes) == {0, 1}


def test_sensor_reader_reuses_one_worker_and_one_session(monkeypatch):
    selected = core.AccelDevice(
        iio_path="/sensor",
        hinge_path="",
        hid_hub="hub",
        name_path="/sensor/name",
        raw_paths=("x", "y", "z"),
        scale_paths=("s", "s", "s"),
    )
    expected = core.AccelReading(
        (1, 2, 0), (1.0, 1.0, 1.0), (1.0, 2.0, 0.0)
    )
    opened = []

    class Session:
        def __init__(self):
            opened.append(self)

        def read(self):
            return expected

        def close(self):
            pass

    monkeypatch.setattr(core, "discover_accel", lambda _runtime=None: selected)
    monkeypatch.setattr(
        core, "_open_accel_sample_session", lambda *_args: Session()
    )
    reader = core.SensorReader(RecordingLogger())
    results = []
    try:
        reader.read(0.0)
        deadline = time.monotonic() + 1.0
        while len(results) < 5 and time.monotonic() < deadline:
            reading = reader.read(len(results) * 0.1 + 0.1)
            if reading is not None:
                results.append(reading)
            time.sleep(0.001)
        worker = reader._worker
        assert results == [expected] * 5
        assert worker is not None and worker.is_alive()
        assert len(opened) == 1
    finally:
        reader.close()


class FakeEventSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def recv(self, _size):
        item = self.chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def fileno(self):
        return 42

    def close(self):
        self.closed = True


def test_hyprland_event_reader_handles_partial_and_relevant_lines():
    reader = core.HyprlandEventReader(RecordingLogger())
    reader.socket = FakeEventSocket(
        [
            b"monitoradd",
            b"ed>>eDP-1\nworkspace>>1\nconfigreloaded>>\n",
            BlockingIOError(),
        ]
    )

    assert reader.poll(1.0) is True
    assert reader._buffer == bytearray()
    reader.close()


class FakeHyprland:
    def __init__(self, statuses=()):
        self.statuses = list(statuses)
        self.positions = []
        self.queries = 0
        self.restarts = 0

    def query_monitor(self, *, report_errors=True):
        self.queries += 1
        return self.statuses.pop(0)

    def set_position(self, x, y):
        self.positions.append((x, y))
        return True

    def restart_shell(self):
        self.restarts += 1
        return True


def test_layer_remap_operation_uses_deadlines_without_sleeping():
    initial = core.MonitorStatus(True, True, 2, 10, 20, active_monitor_count=1)
    midpoint = core.MonitorStatus(True, True, 2, 10, 21, active_monitor_count=1)
    final = core.MonitorStatus(True, True, 2, 10, 20, active_monitor_count=1)
    client = FakeHyprland([midpoint, final])
    operation = core.LayerRemapOperation(
        2, 7, initial, client, RecordingLogger(), runtime()
    )

    assert operation.start(5.0) is None
    assert client.positions == [(10, 21)]
    assert operation.advance(5.01) is None
    assert client.queries == 0
    assert operation.advance(5.0 + core.LAYER_REMAP_NUDGE_DELAY) is None
    assert client.positions == [(10, 21), (10, 20)]
    assert operation.advance(operation.deadline) is True
    assert client.queries == 2


def test_layer_remap_cancel_restores_a_successful_nudge():
    initial = core.MonitorStatus(True, True, 2, -4, 8, active_monitor_count=1)
    client = FakeHyprland()
    operation = core.LayerRemapOperation(
        2, 3, initial, client, RecordingLogger(), runtime()
    )

    assert operation.start(1.0) is None
    assert operation.cancel() is True
    assert client.positions == [(-4, 9), (-4, 8)]


def test_daemon_remap_failure_restores_then_uses_shell_fallback_once():
    initial = core.MonitorStatus(True, True, 2, 4, 9, active_monitor_count=1)
    client = FakeHyprland(
        [core.MonitorStatus(True, True, 2, 4, 9, active_monitor_count=1)]
    )
    daemon = core.RotationDaemon(
        RecordingLogger(), hyprland=client, hyprland_events=None
    )
    try:
        daemon._start_layer_remap(2, initial, 5.0)
        assert client.positions == [(4, 10)]

        daemon._advance_layer_remap(5.0 + core.LAYER_REMAP_NUDGE_DELAY)

        assert client.positions == [(4, 10), (4, 9)]
        assert client.restarts == 1
        assert daemon._layer_remap is None
    finally:
        close_daemon(daemon)


def test_apply_starts_remap_from_confirmed_status_without_extra_query():
    before = core.MonitorStatus(True, True, 0, 2, 3, active_monitor_count=1)
    confirmed = core.MonitorStatus(True, True, 2, 2, 3, active_monitor_count=1)

    class Client(FakeHyprland):
        def apply_transform(self, transform):
            assert transform == 2
            return confirmed

    client = Client()
    daemon = core.RotationDaemon(
        RecordingLogger(), hyprland=client, hyprland_events=None
    )
    daemon.desired_transform = 2
    try:
        daemon._apply_if_needed(1.0, "test", before)

        assert client.queries == 0
        assert client.positions == [(2, 4)]
        assert daemon._layer_remap is not None
    finally:
        daemon._cancel_layer_remap()
        close_daemon(daemon)


def test_hyprland_event_reestablishes_paired_touch_transform():
    current = core.MonitorStatus(True, True, 2, 2, 3, active_monitor_count=1)

    class Client(FakeHyprland):
        def __init__(self):
            super().__init__([current])
            self.applies = []

        def apply_transform(self, transform):
            self.applies.append(transform)
            return current

    class Switch:
        fd = None
        state = None

        def poll(self, _now):
            pass

        def next_deadline(self):
            return 10.0

    class Events:
        connected = True
        fd = None

        def poll(self, _now):
            return True

        def next_deadline(self):
            return float("inf")

        def close(self):
            pass

    client = Client()
    daemon = core.RotationDaemon(
        RecordingLogger(),
        hyprland=client,
        switch=Switch(),
        hyprland_events=Events(),
    )
    daemon.desired_transform = 2
    daemon.last_applied_transform = 2
    daemon.next_apply_retry = float("inf")
    daemon.next_monitor_check = float("inf")
    try:
        daemon._process_once(1.0)

        assert client.applies == [2]
        assert daemon.last_applied_transform == 2
    finally:
        close_daemon(daemon)


def test_apply_eval_keeps_touch_check_immediately_before_mutation(monkeypatch):
    calls = []
    selected_runtime = runtime(output="DSI-1", touch_device="touch-one")

    def run_command(argv, **_kwargs):
        calls.append(tuple(argv))
        if argv[1:3] == ["-j", "devices"]:
            stdout = '{"touch":[{"name":"touch-one"}]}'
        elif argv[1:4] == ["-j", "monitors", "all"]:
            stdout = '[{"name":"DSI-1","disabled":false,"transform":2}]'
        else:
            stdout = "ok"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(core, "_run_command", run_command)
    status = core.apply_eval(2, RecordingLogger(), selected_runtime)

    assert status is not None and status.transform == 2
    assert calls[0] == ("hyprctl", "-j", "devices")
    assert calls[1][0:2] == ("hyprctl", "eval")
    assert calls[2] == ("hyprctl", "-j", "monitors", "all")
