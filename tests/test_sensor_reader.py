import threading
import time
from dataclasses import replace

from tablet_auto_rotate import core
from tablet_auto_rotate.config import HardwareConfig, RuntimeConfig


def runtime(**changes):
    return RuntimeConfig.from_hardware(replace(HardwareConfig(), **changes))


class RecordingLogger:
    def __init__(self):
        self.errors = []
        self.infos = []

    def error(self, key, message, **_kwargs):
        self.errors.append((key, message))

    def info(self, message):
        self.infos.append(message)


def device():
    return core.AccelDevice(
        iio_path="/sys/test/iio:device0",
        hinge_path="",
        hid_hub="test",
        name_path="name",
        raw_paths=("x", "y", "z"),
        scale_paths=("sx", "sy", "sz"),
    )


def test_blocked_sensor_read_does_not_block_caller(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def blocked_read(_device, _runtime=None):
        started.set()
        release.wait()
        return core.AccelReading((1, 2, 3), (1.0, 1.0, 1.0), (1.0, 2.0, 3.0))

    monkeypatch.setattr(core, "discover_accel", lambda _runtime=None: device())
    class Session:
        def read(self):
            return blocked_read(None)

        def close(self):
            pass

    monkeypatch.setattr(core, "_open_accel_sample_session", lambda *_args: Session())
    logger = RecordingLogger()
    reader = core.SensorReader(logger)

    assert reader.read(0.0) is None
    assert started.wait(1.0)
    before = time.monotonic()
    assert reader.read(core.SENSOR_READ_WARNING_SECONDS + 0.1) is None
    assert time.monotonic() - before < 0.1
    assert logger.errors[0][0] == "sensor-read-blocked"
    release.set()
    reader.close()


def test_completed_sensor_read_is_returned_and_next_read_starts(monkeypatch):
    expected = core.AccelReading(
        (1, 2, 3), (1.0, 1.0, 1.0), (1.0, 2.0, 3.0)
    )
    monkeypatch.setattr(core, "discover_accel", lambda _runtime=None: device())
    class Session:
        def read(self):
            return expected

        def close(self):
            pass

    monkeypatch.setattr(core, "_open_accel_sample_session", lambda *_args: Session())
    reader = core.SensorReader(RecordingLogger())

    assert reader.read(0.0) is None
    deadline = time.monotonic() + 1.0
    result = None
    while result is None and time.monotonic() < deadline:
        result = reader.read(0.1)
        time.sleep(0.001)
    assert result == expected
    reader.close()


def test_reset_discards_result_from_previous_generation(monkeypatch):
    release = threading.Event()

    def delayed_read(_device, _runtime=None):
        release.wait()
        return core.AccelReading((1, 2, 3), (1.0, 1.0, 1.0), (1.0, 2.0, 3.0))

    monkeypatch.setattr(core, "discover_accel", lambda _runtime=None: device())
    class Session:
        def read(self):
            return delayed_read(None)

        def close(self):
            pass

    monkeypatch.setattr(core, "_open_accel_sample_session", lambda *_args: Session())
    reader = core.SensorReader(RecordingLogger())
    assert reader.read(0.0) is None
    reader.reset()
    release.set()

    deadline = time.monotonic() + 1.0
    while reader._read_pending and time.monotonic() < deadline:
        reader.read(0.1)
        time.sleep(0.001)
    assert reader.device is None
    reader.close()


def test_reset_cancels_a_request_not_yet_taken_by_worker(monkeypatch):
    class IdleWorker:
        def join(self, timeout=None):
            pass

    reader = core.SensorReader(RecordingLogger())
    reader.device = device()
    reader._worker = IdleWorker()
    monkeypatch.setattr(reader, "_ensure_worker", lambda: None)

    reader._start_read(1.0)
    assert reader._read_pending
    assert reader._request == (0, reader.device)

    reader.reset()

    assert not reader._read_pending
    assert reader._request == (1, None)
    reader.close()


def test_reset_cancels_prefetch_queued_before_worker_finishes_bookkeeping(
    monkeypatch,
):
    expected = core.AccelReading(
        (1, 2, 0), (1.0, 1.0, 1.0), (1.0, 2.0, 0.0)
    )

    class IdleWorker:
        def join(self, timeout=None):
            pass

    reader = core.SensorReader(RecordingLogger())
    reader.device = device()
    reader._worker = IdleWorker()
    # Model the reviewed interleaving: the worker has published its result but
    # has not yet cleared the old busy marker when policy queues a prefetch.
    reader._worker_busy = True
    reader._read_pending = True
    reader._results.put((0, expected, None))
    monkeypatch.setattr(reader, "_ensure_worker", lambda: None)

    assert reader.read(1.0) == expected
    assert reader._read_pending
    assert reader._request == (0, reader.device)

    reader.reset()

    assert not reader._read_pending
    assert reader._request == (1, None)
    reader.close()


def test_read_error_closes_session_and_rediscovers_before_reopening(monkeypatch):
    expected = core.AccelReading(
        (1, 2, 0), (1.0, 1.0, 1.0), (1.0, 2.0, 0.0)
    )
    sessions = []

    class Session:
        def __init__(self):
            self.closed = False
            self.number = len(sessions)
            sessions.append(self)

        def read(self):
            if self.number == 0:
                raise OSError("device disappeared")
            return expected

        def close(self):
            self.closed = True

    monkeypatch.setattr(core, "discover_accel", lambda _runtime=None: device())
    monkeypatch.setattr(
        core, "_open_accel_sample_session", lambda *_args: Session()
    )
    reader = core.SensorReader(RecordingLogger())
    try:
        reader.read(0.0)
        deadline = time.monotonic() + 1.0
        while reader.device is not None and time.monotonic() < deadline:
            reader.read(0.1)
            time.sleep(0.001)

        assert reader.device is None
        assert sessions[0].closed

        reader.read(1.1)
        deadline = time.monotonic() + 1.0
        result = None
        while result is None and time.monotonic() < deadline:
            result = reader.read(1.2)
            time.sleep(0.001)

        assert result == expected
        assert len(sessions) == 2
    finally:
        reader.close()


def test_orientation_read_skips_unused_physical_z(monkeypatch):
    selected = device()
    values = {"x": "100", "y": "-200", "sx": "0.01", "sy": "0.01"}
    reads = []

    def read_text(path):
        reads.append(path)
        return values[path]

    monkeypatch.setattr(core, "_read_text", read_text)
    reading = core.read_orientation_accel(selected, runtime())

    assert reading.values == (1.0, -2.0, 0.0)
    assert "z" not in reads and "sz" not in reads


def test_orientation_read_applies_mount_matrix_and_reads_needed_axes(monkeypatch):
    selected = core.AccelDevice(
        iio_path="/sensor",
        hinge_path="",
        hid_hub="test",
        name_path="name",
        raw_paths=("x", "y", "z"),
        scale_paths=("sx", "sy", "sz"),
        mount_matrix=((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0)),
    )
    values = {"y": "-200", "z": "100", "sy": "0.01", "sz": "0.01"}
    reads = []

    def read_text(path):
        reads.append(path)
        return values[path]

    monkeypatch.setattr(core, "_read_text", read_text)

    reading = core.read_orientation_accel(selected, runtime(mount_matrix="auto"))

    assert reading.values == (1.0, -2.0, 0.0)
    assert "x" not in reads and "sx" not in reads
