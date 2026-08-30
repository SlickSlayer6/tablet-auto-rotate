from __future__ import annotations

import math

from tablet_auto_rotate import core


class RecordingLogger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def info(self, message):
        self.infos.append(message)

    def debug(self, _message):
        pass

    def error(self, key, message, **_kwargs):
        self.errors.append((key, message))


def test_entering_tablet_mode_waits_for_orientation():
    daemon = core.RotationDaemon(RecordingLogger(), dry_run=True)
    daemon.switch.state = True
    daemon.desired_transform = 3

    daemon._handle_switch_state(10.0)

    assert daemon.tablet_mode is True
    assert daemon.desired_transform is None
    assert daemon.next_sensor_sample == 10.0


def test_leaving_tablet_mode_forces_landscape_in_dry_run():
    logger = RecordingLogger()
    daemon = core.RotationDaemon(logger, dry_run=True)
    daemon.tablet_mode = True
    daemon.switch.state = False

    daemon._handle_switch_state(12.0)

    assert daemon.tablet_mode is False
    assert daemon.desired_transform == 0
    assert daemon.last_applied_transform == 0
    assert daemon.next_apply_retry == math.inf


def test_lost_switch_state_clears_pending_rotation():
    daemon = core.RotationDaemon(RecordingLogger(), dry_run=True)
    daemon.tablet_mode = True
    daemon.desired_transform = 2
    daemon.switch.state = None

    daemon._handle_switch_state(4.0)

    assert daemon.tablet_mode is None
    assert daemon.desired_transform is None
    assert daemon.next_sensor_sample == 4.0


def test_disabled_monitor_schedules_backoff(monkeypatch):
    daemon = core.RotationDaemon(RecordingLogger())
    daemon.desired_transform = 2
    disabled = core.MonitorStatus(True, False, 0)

    daemon._apply_if_needed(5.0, "test", disabled)

    assert daemon.last_applied_transform is None
    assert daemon.next_apply_retry == 5.0 + core.INITIAL_APPLY_RETRY
    assert daemon.apply_retry_delay == core.INITIAL_APPLY_RETRY * 2


def test_invalid_desired_transform_is_cleared():
    logger = RecordingLogger()
    daemon = core.RotationDaemon(logger)
    daemon.desired_transform = True

    daemon._apply_if_needed(1.0, "test")

    assert daemon.desired_transform is None
    assert logger.errors[0][0] == "transform"


def test_pending_async_sensor_read_preserves_filter_candidate(monkeypatch):
    logger = RecordingLogger()
    daemon = core.RotationDaemon(logger)
    daemon.filter.candidate = 1
    daemon.filter.candidate_since = 10.0
    monkeypatch.setattr(daemon.sensor, "read", lambda _now: None)

    daemon._sample_sensor(10.1)

    assert daemon.filter.candidate == 1
    assert daemon.filter.candidate_since == 10.0
