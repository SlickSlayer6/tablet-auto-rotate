from __future__ import annotations

import math
import struct

import pytest

from tablet_auto_rotate import core


@pytest.mark.parametrize(
    ("sample", "transform"),
    [
        ((0.0, core.GRAVITY, 0.0), 2),
        ((core.GRAVITY, 0.0, 0.0), 1),
        ((0.0, -core.GRAVITY, 0.0), 0),
        ((-core.GRAVITY, 0.0, 0.0), 3),
    ],
)
def test_classify_cardinal_orientation(sample, transform):
    assert core.classify_orientation(sample) == transform


@pytest.mark.parametrize(
    "sample",
    [
        (),
        (0.0, 0.0),
        (0.0, 0.0, core.GRAVITY, 0.0),
        (math.nan, 0.0, 0.0),
        (math.inf, 0.0, 0.0),
        (0.0, 0.0, core.GRAVITY),
        (core.GRAVITY, core.GRAVITY, 0.0),
        (0.0, 2.0, 0.0),
    ],
)
def test_classify_rejects_unsafe_samples(sample):
    assert core.classify_orientation(sample) is None


def test_orientation_filter_requires_contiguous_stable_hold():
    filt = core.OrientationFilter()
    upright = (0.0, core.GRAVITY, 0.0)

    assert filt.update(upright, 0.0) is None
    assert filt.update(upright, 0.2) is None
    # A long sampling gap restarts the hold period.
    assert filt.update(upright, 0.5) is None
    assert filt.update(upright, 0.7) is None
    assert filt.update(upright, 0.86) == 2
    # A held orientation is emitted only once.
    assert filt.update(upright, 0.9) is None


@pytest.mark.parametrize("invalid", [True, False, -1, 4, 1.0, "1", None])
def test_eval_command_rejects_non_allowlisted_transforms(invalid):
    with pytest.raises(ValueError):
        core.build_eval_command(invalid)


def test_eval_command_quotes_configured_names(monkeypatch):
    monkeypatch.setattr(core, "OUTPUT_NAME", 'panel"; error("no") --')
    monkeypatch.setattr(core, "TOUCH_DEVICE_NAME", "touch\\device\nname")
    command = core.build_eval_command(1)
    assert 'output = "panel\\\"; error(\\\"no\\\") --"' in command
    assert 'name = "touch\\\\device\\nname"' in command
    assert command.count("hl.monitor(") == 1
    assert command.count("hl.device(") == 1


def test_monitor_parser_counts_only_active_outputs():
    status = core.parse_monitor_status(
        {
            "monitors": [
                {"name": core.OUTPUT_NAME, "disabled": False, "transform": 2},
                {"name": "HDMI-A-1", "disabled": False, "transform": 0},
                {"name": "DP-1", "disabled": True, "transform": 0},
            ]
        }
    )
    assert status == core.MonitorStatus(
        found=True, enabled=True, transform=2, active_monitor_count=2
    )


def test_input_event_layout_matches_native_linux_abi():
    """Catch use of a fixed-width layout on an incompatible native ABI."""

    native_size = struct.calcsize("@llHHi")
    assert core.INPUT_EVENT_SIZE == native_size
    assert core.INPUT_EVENT.unpack(
        core.INPUT_EVENT.pack(1, 2, core.EV_SW, core.SW_TABLET_MODE, 1)
    )[2:] == (core.EV_SW, core.SW_TABLET_MODE, 1)


def test_eviocgsw_request_encodes_caller_buffer_size():
    one = core.evio_cgsw(1)
    two = core.evio_cgsw(2)
    assert one != two
    assert (two >> core.IOC_SIZESHIFT) & ((1 << 14) - 1) == 2
