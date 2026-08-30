"""Validated user configuration for tablet-auto-rotate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib
from typing import Any


@dataclass(frozen=True)
class HardwareConfig:
    """Machine-specific identifiers and orientation policy."""

    output: str = "eDP-1"
    touch_device: str = "elan9004:00-04f3:4110"
    switch_name: str = "Intel HID switches"
    preferred_switch_path: str = "/dev/input/by-path/platform-INTC1070:00-event"
    desktop_integration: str = "omarchy"
    axis_order: tuple[str, str, str] = ("x", "y", "z")
    axis_signs: tuple[int, int, int] = (1, 1, 1)
    orientation_transforms: tuple[int, int, int, int] = (1, 2, 3, 0)


def default_config_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    if root:
        return Path(root) / "tablet-auto-rotate" / "config.toml"
    return Path.home() / ".config" / "tablet-auto-rotate" / "config.toml"


def _required_string(table: dict[str, Any], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"hardware.{key} must be a non-empty string")
    return value.strip()


def load_config(path: Path | None = None, *, required: bool = False) -> HardwareConfig:
    """Load a TOML configuration, falling back to the calibrated prototype."""

    config_path = path or default_config_path()
    if not config_path.exists():
        if required:
            raise FileNotFoundError(config_path)
        return HardwareConfig()
    with config_path.open("rb") as stream:
        payload = tomllib.load(stream)
    hardware = payload.get("hardware", {})
    if not isinstance(hardware, dict):
        raise ValueError("hardware must be a TOML table")
    defaults = HardwareConfig()
    integration = _required_string(
        hardware, "desktop_integration", defaults.desktop_integration
    )
    if integration not in {"none", "omarchy"}:
        raise ValueError("hardware.desktop_integration must be 'none' or 'omarchy'")
    sensor = payload.get("sensor", {})
    if not isinstance(sensor, dict):
        raise ValueError("sensor must be a TOML table")
    axis_order_value = sensor.get("axis_order", list(defaults.axis_order))
    if (
        not isinstance(axis_order_value, list)
        or len(axis_order_value) != 3
        or set(axis_order_value) != {"x", "y", "z"}
    ):
        raise ValueError("sensor.axis_order must be a permutation of ['x', 'y', 'z']")
    axis_signs_value = sensor.get("axis_signs", list(defaults.axis_signs))
    if (
        not isinstance(axis_signs_value, list)
        or len(axis_signs_value) != 3
        or any(isinstance(value, bool) or value not in {-1, 1} for value in axis_signs_value)
    ):
        raise ValueError("sensor.axis_signs must contain exactly three values of -1 or 1")
    transforms_value = sensor.get(
        "orientation_transforms", list(defaults.orientation_transforms)
    )
    if (
        not isinstance(transforms_value, list)
        or len(transforms_value) != 4
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value not in range(4)
            for value in transforms_value
        )
    ):
        raise ValueError(
            "sensor.orientation_transforms must contain four transforms from 0 through 3"
        )
    return HardwareConfig(
        output=_required_string(hardware, "output", defaults.output),
        touch_device=_required_string(
            hardware, "touch_device", defaults.touch_device
        ),
        switch_name=_required_string(hardware, "switch_name", defaults.switch_name),
        preferred_switch_path=_required_string(
            hardware, "preferred_switch_path", defaults.preferred_switch_path
        ),
        desktop_integration=integration,
        axis_order=tuple(axis_order_value),  # type: ignore[arg-type]
        axis_signs=tuple(axis_signs_value),  # type: ignore[arg-type]
        orientation_transforms=tuple(transforms_value),  # type: ignore[arg-type]
    )
