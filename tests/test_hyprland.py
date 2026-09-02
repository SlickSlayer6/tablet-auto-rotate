from __future__ import annotations

import subprocess

import pytest

from tablet_auto_rotate import core


class RecordingLogger:
    def __init__(self):
        self.debugs = []
        self.errors = []

    def debug(self, message):
        self.debugs.append(message)

    def error(self, key, message, **_kwargs):
        self.errors.append((key, message))


class EventSocket:
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


def status(transform=2, x=17, y=-4, active=1):
    return core.MonitorStatus(
        True,
        True,
        transform,
        x,
        y,
        active_monitor_count=active,
    )


def test_verify_monitor_transform_returns_the_fresh_matching_status():
    expected = status()
    statuses = iter((status(transform=1), None, expected))
    sleeps = []

    result = core.verify_monitor_transform(
        2,
        RecordingLogger(),
        query=lambda: next(statuses),
        sleep=sleeps.append,
    )

    assert result is expected
    assert sleeps == [
        core.POST_APPLY_VERIFY_DELAY,
        core.POST_APPLY_VERIFY_DELAY,
    ]


@pytest.mark.parametrize("coordinates", [(True, 0), (0, False), (1.5, 0), (0, "1")])
def test_position_command_rejects_non_integer_coordinates(coordinates):
    with pytest.raises(ValueError):
        core.build_position_eval_command(*coordinates)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"touch": "not-a-list"},
        {"touch": [{"name": 3}, None]},
        {"devices": [{"name": core.TOUCH_DEVICE_NAME}]},
    ],
)
def test_touch_parser_rejects_malformed_or_non_touch_payloads(payload):
    assert not core.has_touch_device(payload)


def test_monitor_geometry_refresh_requires_a_confirmed_transform_change():
    assert core.should_refresh_geometry(status(transform=0), 2)
    assert not core.should_refresh_geometry(status(transform=2), 2)
    assert not core.should_refresh_geometry(None, 2)


def test_malformed_touch_json_prevents_transform_mutation(monkeypatch):
    calls = []

    def run_command(argv, **_kwargs):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="not-json", stderr="")

    monkeypatch.setattr(core, "_run_command", run_command)

    assert core.apply_eval(2, RecordingLogger()) is None
    assert calls == [("hyprctl", "-j", "devices")]


def test_fast_layer_remap_reuses_initial_status_and_restores_position():
    initial = status()
    statuses = iter((status(y=-3), status()))
    events = []

    def query():
        events.append(("query",))
        return next(statuses)

    def set_position(x, y):
        events.append(("set", x, y))
        return True

    assert core.fast_layer_remap(
        2,
        RecordingLogger(),
        initial_status=initial,
        query=query,
        set_position=set_position,
        sleep=lambda delay: events.append(("sleep", delay)),
    )
    assert events == [
        ("set", 17, -3),
        ("sleep", core.LAYER_REMAP_NUDGE_DELAY),
        ("query",),
        ("set", 17, -4),
        ("sleep", core.LAYER_REMAP_SETTLE_DELAY),
        ("query",),
    ]


def test_fast_layer_remap_restores_after_unconfirmed_nudge():
    positions = []

    assert not core.fast_layer_remap(
        2,
        RecordingLogger(),
        initial_status=status(),
        query=lambda: status(),
        set_position=lambda x, y: positions.append((x, y)) or True,
        sleep=lambda _delay: None,
    )
    assert positions == [(17, -3), (17, -4)]


def test_fast_layer_remap_rejects_multiple_active_monitors_without_mutation():
    positions = []

    assert not core.fast_layer_remap(
        2,
        RecordingLogger(),
        initial_status=status(active=2),
        set_position=lambda x, y: positions.append((x, y)) or True,
        sleep=lambda _delay: None,
    )
    assert positions == []


def test_event_reader_drops_oversized_input_and_returns_to_polling():
    logger = RecordingLogger()
    reader = core.HyprlandEventReader(logger)
    event_socket = EventSocket([b"x" * (core.HYPRLAND_EVENT_BUFFER_LIMIT + 1)])
    reader.socket = event_socket

    assert reader.poll(10.0) is False
    assert event_socket.closed
    assert not reader.connected
    assert reader.next_deadline() == 10.0 + core.HYPRLAND_EVENT_RETRY_INTERVAL
    assert logger.errors[-1][0] == "hyprland-events"


def test_event_reader_rejects_an_oversized_partial_line_immediately():
    reader = core.HyprlandEventReader(RecordingLogger())
    event_socket = EventSocket(
        [b"x" * (core.HYPRLAND_EVENT_LINE_LIMIT + 1)]
    )
    reader.socket = event_socket

    assert reader.poll(8.0) is False
    assert event_socket.closed
    assert not reader.connected


def test_event_reader_preserves_a_relevant_event_when_peer_disconnects():
    reader = core.HyprlandEventReader(RecordingLogger())
    reader.socket = EventSocket([b"configreloaded>>\n", b""])

    assert reader.poll(3.0) is True
    assert not reader.connected
    assert reader.next_deadline() == 3.0 + core.HYPRLAND_EVENT_RETRY_INTERVAL


def test_event_socket_path_rejects_relative_runtime_and_signature_traversal(
    monkeypatch,
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative")
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "instance")
    assert core._hyprland_socket_path(".socket2.sock") is None

    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "../instance")
    assert core._hyprland_socket_path(".socket2.sock") is None

    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "instance")
    assert core._hyprland_socket_path(".socket2.sock") == (
        "/run/user/1000/hypr/instance/.socket2.sock"
    )
