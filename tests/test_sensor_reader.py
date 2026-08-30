import threading
import time

from tablet_auto_rotate import core


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

    def blocked_read(_device):
        started.set()
        release.wait()
        return core.AccelReading((1, 2, 3), (1.0, 1.0, 1.0), (1.0, 2.0, 3.0))

    monkeypatch.setattr(core, "discover_accel", device)
    monkeypatch.setattr(core, "read_orientation_accel", blocked_read)
    logger = RecordingLogger()
    reader = core.SensorReader(logger)

    assert reader.read(0.0) is None
    assert started.wait(1.0)
    before = time.monotonic()
    assert reader.read(core.SENSOR_READ_WARNING_SECONDS + 0.1) is None
    assert time.monotonic() - before < 0.1
    assert logger.errors[0][0] == "sensor-read-blocked"
    release.set()


def test_completed_sensor_read_is_returned_and_next_read_starts(monkeypatch):
    expected = core.AccelReading(
        (1, 2, 3), (1.0, 1.0, 1.0), (1.0, 2.0, 3.0)
    )
    monkeypatch.setattr(core, "discover_accel", device)
    monkeypatch.setattr(core, "read_orientation_accel", lambda _device: expected)
    reader = core.SensorReader(RecordingLogger())

    assert reader.read(0.0) is None
    deadline = time.monotonic() + 1.0
    result = None
    while result is None and time.monotonic() < deadline:
        result = reader.read(0.1)
        time.sleep(0.001)
    assert result == expected


def test_reset_discards_result_from_previous_generation(monkeypatch):
    release = threading.Event()

    def delayed_read(_device):
        release.wait()
        return core.AccelReading((1, 2, 3), (1.0, 1.0, 1.0), (1.0, 2.0, 3.0))

    monkeypatch.setattr(core, "discover_accel", device)
    monkeypatch.setattr(core, "read_orientation_accel", delayed_read)
    reader = core.SensorReader(RecordingLogger())
    assert reader.read(0.0) is None
    reader.reset()
    release.set()

    deadline = time.monotonic() + 1.0
    while reader._worker is not None and time.monotonic() < deadline:
        reader.read(0.1)
        time.sleep(0.001)
    assert reader.device is None


def test_orientation_read_skips_unused_physical_z(monkeypatch):
    selected = device()
    values = {"x": "100", "y": "-200", "sx": "0.01", "sy": "0.01"}
    reads = []

    def read_text(path):
        reads.append(path)
        return values[path]

    monkeypatch.setattr(core, "AXIS_ORDER", (0, 1, 2))
    monkeypatch.setattr(core, "_read_text", read_text)
    reading = core.read_orientation_accel(selected)

    assert reading.values == (1.0, -2.0, 0.0)
    assert "z" not in reads and "sz" not in reads
