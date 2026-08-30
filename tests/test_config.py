from pathlib import Path

import pytest

from tablet_auto_rotate import config
from tablet_auto_rotate import core


def write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_load_and_apply_machine_mapping(tmp_path, monkeypatch):
    for name in (
        "OUTPUT_NAME",
        "TOUCH_DEVICE_NAME",
        "SWITCH_NAME",
        "PREFERRED_SWITCH_PATH",
        "DESKTOP_INTEGRATION",
        "AXIS_ORDER",
        "AXIS_SIGNS",
        "ORIENTATION_TRANSFORMS",
    ):
        monkeypatch.setattr(core, name, getattr(core, name))
    path = write_config(
        tmp_path / "machine.toml",
        """
[hardware]
output = "DSI-1"
touch_device = "example-touch"
switch_name = "Example switches"
preferred_switch_path = "/dev/input/by-path/example-event"
desktop_integration = "none"

[sensor]
axis_order = ["y", "x", "z"]
axis_signs = [-1, 1, 1]
orientation_transforms = [3, 0, 1, 2]
""",
    )

    loaded = config.load_config(path, required=True)
    core.apply_config(loaded)

    assert core.OUTPUT_NAME == "DSI-1"
    assert core.TOUCH_DEVICE_NAME == "example-touch"
    assert core.DESKTOP_INTEGRATION == "none"
    assert core.map_sensor_values((10.0, 20.0, 30.0)) == (-20.0, 10.0, 30.0)
    assert core.classify_orientation((core.GRAVITY, 0.0, 0.0)) == 3


@pytest.mark.parametrize(
    "body, message",
    [
        ("[sensor]\naxis_order = ['x', 'x', 'z']\n", "axis_order"),
        ("[sensor]\naxis_signs = [1, 0, 1]\n", "axis_signs"),
        (
            "[sensor]\norientation_transforms = [0, 1, 2, 7]\n",
            "orientation_transforms",
        ),
        ("[hardware]\ndesktop_integration = 'unknown'\n", "desktop_integration"),
    ],
)
def test_invalid_configuration_is_rejected(tmp_path, body, message):
    path = write_config(tmp_path / "invalid.toml", body)
    with pytest.raises(ValueError, match=message):
        config.load_config(path, required=True)


def test_explicit_missing_config_is_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.load_config(tmp_path / "missing.toml", required=True)
