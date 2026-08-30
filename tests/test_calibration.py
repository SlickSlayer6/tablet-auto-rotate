import tomllib

import pytest

from tablet_auto_rotate.calibration import (
    CalibrationError,
    classify_stable_samples,
    generate_config_toml,
    infer_axis_mapping,
)
from tablet_auto_rotate.config import HardwareConfig


def samples(vector, count=8):
    offsets = (-0.01, 0.0, 0.01, 0.005, -0.005, 0.002, -0.002, 0.0)
    return [tuple(value + offsets[i] for value in vector) for i in range(count)]


def test_classifies_stable_samples():
    direction = classify_stable_samples(samples((0.0, 9.81, 0.0)))
    assert direction[1] == pytest.approx(1.0, abs=0.001)


def test_infers_swapped_and_signed_axes():
    result = infer_axis_mapping(
        {
            "+x": samples((0.0, -9.81, 0.0)),
            "+y": samples((9.81, 0.0, 0.0)),
            "-x": samples((0.0, 9.81, 0.0)),
            "-y": samples((-9.81, 0.0, 0.0)),
        }
    )
    assert result.axis_order == ("y", "x", "z")
    assert result.axis_signs == (-1, 1, 1)


@pytest.mark.parametrize(
    "readings, message",
    [
        ({"+x": [(1.0, 0.0, 0.0)]}, "exactly"),
        (
            {
                "+x": samples((7.0, 7.0, 0.0)),
                "-x": samples((-7.0, -7.0, 0.0)),
                "+y": samples((0.0, 9.81, 0.0)),
                "-y": samples((0.0, -9.81, 0.0)),
            },
            "diagonal",
        ),
        (
            {
                "+x": samples((9.81, 0.0, 0.0)),
                "-x": samples((0.0, 9.81, 0.0)),
                "+y": samples((0.0, 9.81, 0.0)),
                "-y": samples((0.0, -9.81, 0.0)),
            },
            "opposite",
        ),
    ],
)
def test_refuses_ambiguous_mappings(readings, message):
    with pytest.raises(CalibrationError, match=message):
        infer_axis_mapping(readings)


def test_refuses_noisy_samples():
    noisy = [(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)] * 3
    with pytest.raises(CalibrationError, match="noisy"):
        classify_stable_samples(noisy)


def test_generates_parseable_complete_config():
    hardware = HardwareConfig(
        output='DSI-1',
        touch_device='touch "screen"',
        switch_name="Tablet switch",
        preferred_switch_path="/dev/input/event0",
        desktop_integration="none",
        orientation_transforms=(3, 0, 1, 2),
        mount_matrix="auto",
    )
    result = infer_axis_mapping(
        {
            "+x": samples((0.0, -9.81, 0.0)),
            "-x": samples((0.0, 9.81, 0.0)),
            "+y": samples((9.81, 0.0, 0.0)),
            "-y": samples((-9.81, 0.0, 0.0)),
        }
    )
    parsed = tomllib.loads(generate_config_toml(hardware, result))
    assert parsed["hardware"]["touch_device"] == 'touch "screen"'
    assert parsed["sensor"]["axis_order"] == ["y", "x", "z"]
    assert parsed["sensor"]["axis_signs"] == [-1, 1, 1]
    assert parsed["sensor"]["orientation_transforms"] == [3, 0, 1, 2]
    assert parsed["sensor"]["mount_matrix"] == "auto"
