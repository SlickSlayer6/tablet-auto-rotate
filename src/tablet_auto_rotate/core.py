#!/usr/bin/env python3
"""Safely rotate the internal display and its touch device in tablet mode.

This daemon intentionally uses only the Linux input/IIO sysfs interfaces and
the Hyprland and Omarchy command line interfaces.  It never grabs the switch
device or changes monitor scale, mode, or enabled state.  After a confirmed
output transform change it nudges the output origin briefly so Omarchy can
remap existing layer surfaces, with a full Omarchy shell restart as fallback.
The input and sensor devices are discovered again after they disappear so that
suspend/resume and driver reloads do not require a restart.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import glob
import json
from importlib.metadata import PackageNotFoundError, version
import math
import os
from pathlib import Path
import queue
import re
import signal
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence, Tuple

from .config import HardwareConfig, default_config_path, load_config
from .discovery import (
    SwitchCandidate as DiscoverySwitchCandidate,
    SwitchSelection,
    sanitize_report_path,
    select_tablet_switch,
)
from .lifecycle import LifecycleError, install as install_service, uninstall as uninstall_service
from .calibration import CalibrationError, generate_config_toml, infer_axis_mapping


SCRIPT_NAME = "tablet-auto-rotate"

# Device names and paths for this machine.
PREFERRED_SWITCH_PATH = "/dev/input/by-path/platform-INTC1070:00-event"
INPUT_EVENT_PATH = "/dev/input"
INPUT_SYSFS_GLOB = "/sys/class/input/event*/device/name"
SWITCH_NAME = "Intel HID switches"
OUTPUT_NAME = "eDP-1"
TOUCH_DEVICE_NAME = "elan9004:00-04f3:4110"
IIO_DEVICES_PATH = "/sys/bus/iio/devices"
DESKTOP_INTEGRATION = "omarchy"
AXIS_ORDER = (0, 1, 2)
AXIS_SIGNS = (1, 1, 1)
ORIENTATION_TRANSFORMS = (1, 2, 3, 0)  # +X, +Y, -X, -Y

# Linux input ABI constants.  The 64-bit input_event layout is timeval
# (long long, long long), u16, u16, s32: qqHHi, 24 bytes on this machine.
INPUT_EVENT_FORMAT = "qqHHi"
INPUT_EVENT = struct.Struct(INPUT_EVENT_FORMAT)
INPUT_EVENT_SIZE = INPUT_EVENT.size
EV_SYN = 0
SYN_REPORT = 0
SYN_DROPPED = 3
EV_SW = 5
SW_TABLET_MODE = 1

# SW_MAX is 0x11 in the Linux input-event-codes.h ABI currently installed on
# this machine.  EVIOCGSW takes the size of a byte bitmask, not a bit number.
SW_MAX = 0x11
SW_COUNT = SW_MAX + 1
SW_MASK_BYTES = (SW_COUNT + 7) // 8
IOC_READ = 2
IOC_NRSHIFT = 0
IOC_TYPESHIFT = 8
IOC_SIZESHIFT = 16
IOC_DIRSHIFT = 30

# The switch is sampled continuously, but the accelerometer is deliberately
# read only at this cadence and only while the switch reports tablet mode.
LOOP_INTERVAL = 0.10
SWITCH_RESYNC_INTERVAL = 5.0
DEVICE_RETRY_INTERVAL = 1.0
MONITOR_CHECK_INTERVAL = 2.0
POST_APPLY_VERIFY_ATTEMPTS = 3
POST_APPLY_VERIFY_DELAY = 0.075
HYPRCTL_TIMEOUT = 2.0
# Hyprland 0.56.2 can leave existing layer-shell surfaces with stale
# transform geometry.  A one-pixel output-origin nudge triggers Omarchy's
# ScreenMoveRemap watcher without restarting Quickshell.  Keep the complete
# shell restart below as a fallback when the fast path cannot be verified.
LAYER_REMAP_NUDGE_DELAY = 0.075
LAYER_REMAP_SETTLE_DELAY = 0.300
OMARCHY_SHELL_RESTART_TIMEOUT = 10.0
INITIAL_APPLY_RETRY = 2.0
MAX_APPLY_RETRY = 10.0

ALLOWED_TRANSFORMS = frozenset((0, 1, 2, 3))

# Accelerometer values are in m/s^2 with the scale exposed by the HID sensor.
# These limits allow normal tilt while rejecting no-gravity and movement
# samples.  The direction checks below reject a screen lying flat and
# orientations too close to a diagonal.
GRAVITY = 9.80665
MIN_ACCEL_MAGNITUDE = 0.65 * GRAVITY
MAX_ACCEL_MAGNITUDE = 1.35 * GRAVITY
MIN_PLANAR_RATIO = 0.70
MIN_CARDINAL_RATIO = 0.70
MAX_SECONDARY_RATIO = 0.55
STABILITY_DOT = 0.96
STABILITY_MAGNITUDE_RATIO = 0.20
CANDIDATE_HOLD_SECONDS = 0.35
MAX_SAMPLE_GAP_SECONDS = 0.25
SENSOR_READ_WARNING_SECONDS = 0.50


@dataclass(frozen=True)
class SwitchDevice:
    """A validated evdev switch node and the sysfs name used to validate it."""

    path: str
    event_name: str
    name_path: str
    name: str


@dataclass(frozen=True)
class AccelDevice:
    """A conservatively selected display accelerometer and optional hinge."""

    iio_path: str
    hinge_path: str
    hid_hub: str
    name_path: str
    raw_paths: Tuple[str, str, str]
    scale_paths: Tuple[str, str, str]


@dataclass(frozen=True)
class AccelReading:
    """One raw and scaled accelerometer sample."""

    raw: Tuple[int, int, int]
    scale: Tuple[float, float, float]
    values: Tuple[float, float, float]


@dataclass(frozen=True)
class MonitorStatus:
    """The small part of a Hyprland monitor description this daemon needs."""

    found: bool
    enabled: bool
    transform: Optional[int]
    x: Optional[int] = None
    y: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    active_monitor_count: Optional[int] = None


class Logger:
    """Small stderr logger with suppression for repeated transient failures."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._last_error: dict[str, float] = {}

    @staticmethod
    def _write(message: str) -> None:
        print(f"{SCRIPT_NAME}: {message}", file=sys.stderr, flush=True)

    def info(self, message: str) -> None:
        self._write(message)

    def debug(self, message: str) -> None:
        if self.verbose:
            self._write(message)

    def error(self, key: str, message: str, interval: float = 10.0) -> None:
        now = time.monotonic()
        previous = self._last_error.get(key)
        if previous is None or now - previous >= interval:
            self._last_error[key] = now
            self._write(f"error: {message}")


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        return stream.read().strip()


def _event_number(path: str) -> Optional[int]:
    """Return the event number represented by a device or sysfs path."""

    base = os.path.basename(os.path.realpath(path))
    match = re.fullmatch(r"event([0-9]+)", base)
    return int(match.group(1)) if match else None


def _validate_switch_path(
    path: str, expected_name: Optional[str] = None
) -> Optional[SwitchDevice]:
    """Validate an input path against its event device's sysfs name."""

    if not os.path.exists(path):
        return None
    number = _event_number(path)
    if number is None:
        return None
    event_name = f"event{number}"
    name_path = f"/sys/class/input/{event_name}/device/name"
    try:
        name = _read_text(name_path)
        if expected_name is not None and name != expected_name:
            return None
    except (OSError, UnicodeError):
        return None
    return SwitchDevice(
        path=path, event_name=event_name, name_path=name_path, name=name
    )


def _switch_codes(name_path: str) -> frozenset[int]:
    """Read advertised evdev switch codes from the event device's sysfs data."""

    capabilities_path = os.path.join(os.path.dirname(name_path), "capabilities", "sw")
    words = _read_text(capabilities_path).split()
    if not words:
        return frozenset()
    # sysfs prints the least-significant machine word last. The current tablet
    # switch code is bit 1, so no architecture-dependent word padding is needed.
    least_significant = int(words[-1], 16)
    return frozenset(
        bit for bit in range(least_significant.bit_length())
        if least_significant & (1 << bit)
    )


def _event_path_sort_key(path: str) -> Tuple[int, str]:
    number = _event_number(path)
    if number is None:
        # Fallback discovery passes /sys/class/input/eventN/device/name.
        event_name = os.path.basename(os.path.dirname(os.path.dirname(path)))
        number = _event_number(event_name)
    return (number if number is not None else 2**31, path)


def discover_switch_selection() -> tuple[SwitchSelection, dict[str, SwitchDevice]]:
    """Select a unique SW_TABLET_MODE device and retain diagnostic reasons."""

    devices: list[SwitchDevice] = []
    seen_events: set[str] = set()

    preferred = (
        None if PREFERRED_SWITCH_PATH == "auto"
        else _validate_switch_path(PREFERRED_SWITCH_PATH)
    )
    if preferred is not None:
        devices.append(preferred)
        seen_events.add(preferred.event_name)

    for name_path in sorted(glob.glob(INPUT_SYSFS_GLOB), key=_event_path_sort_key):
        event_name = os.path.basename(os.path.dirname(os.path.dirname(name_path)))
        if event_name in seen_events:
            continue
        number = _event_number(event_name)
        if number is None:
            continue
        try:
            name = _read_text(name_path)
        except (OSError, UnicodeError):
            continue
        device = SwitchDevice(
            path=os.path.join(INPUT_EVENT_PATH, f"event{number}"),
            event_name=event_name,
            name_path=name_path,
            name=name,
        )
        devices.append(device)
        seen_events.add(device.event_name)
    observations: list[DiscoverySwitchCandidate] = []
    by_path: dict[str, SwitchDevice] = {}
    for device in devices:
        try:
            codes = _switch_codes(device.name_path)
        except (OSError, UnicodeError, ValueError):
            codes = frozenset()
        observations.append(
            DiscoverySwitchCandidate.from_codes(device.path, device.name, codes)
        )
        by_path[device.path] = device
    known_paths = {device.path for device in devices}
    selection = select_tablet_switch(
        observations,
        configured_path=(
            PREFERRED_SWITCH_PATH if PREFERRED_SWITCH_PATH in known_paths else None
        ),
        configured_name=None if SWITCH_NAME == "auto" else SWITCH_NAME,
    )
    return selection, by_path


def discover_switch_candidates() -> list[SwitchDevice]:
    """Return only a conservatively selected tablet-mode switch."""

    selection, by_path = discover_switch_selection()
    if selection.selected is None:
        return []
    selected = by_path.get(selection.selected.path)
    return [selected] if selected is not None else []


def discover_switch() -> Optional[SwitchDevice]:
    """Return the first validated switch candidate, if one exists."""

    candidates = discover_switch_candidates()
    return candidates[0] if candidates else None


def _ioc(direction: int, type_number: int, number: int, size: int) -> int:
    """Build a Linux _IOC request number for the generic Linux ABI."""

    if not (0 <= direction < (1 << 2)):
        raise ValueError("invalid ioctl direction")
    if not (0 <= type_number < (1 << 8)):
        raise ValueError("invalid ioctl type")
    if not (0 <= number < (1 << 8)):
        raise ValueError("invalid ioctl number")
    if not (0 <= size < (1 << 14)):
        raise ValueError("invalid ioctl size")
    return (
        (direction << IOC_DIRSHIFT)
        | (size << IOC_SIZESHIFT)
        | (type_number << IOC_TYPESHIFT)
        | (number << IOC_NRSHIFT)
    )


def evio_cgsw(size: int = SW_MASK_BYTES) -> int:
    """Build EVIOCGSW(size), the input switch-state read ioctl."""

    return _ioc(IOC_READ, ord("E"), 0x1B, size)


EVIOCGSW_REQUEST = evio_cgsw(SW_MASK_BYTES)


def read_switch_state(fd: int) -> bool:
    """Read SW_TABLET_MODE using EVIOCGSW without grabbing the device."""

    state = bytearray(SW_MASK_BYTES)
    # Passing a mutable buffer and mutate_flag=True lets the kernel fill it.
    fcntl.ioctl(fd, EVIOCGSW_REQUEST, state, True)
    byte_index, bit_index = divmod(SW_TABLET_MODE, 8)
    return bool(state[byte_index] & (1 << bit_index))


def open_switch_device(device: SwitchDevice) -> Tuple[int, bool]:
    """Open one switch node nonblocking and obtain its initial state."""

    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(device.path, flags)
    try:
        state = read_switch_state(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd, state


def _sysfs_device_path(iio_path: str) -> str:
    child = os.path.join(iio_path, "device")
    return os.path.realpath(child if os.path.exists(child) else iio_path)


def find_hid_hub_ancestor(path: str) -> Optional[str]:
    """Find the 001F:* HID hub component in a resolved sysfs ancestry."""

    resolved = os.path.realpath(path)
    matches = [component for component in resolved.split(os.sep)
               if component.startswith("001F:") and len(component) > len("001F:")]
    return matches[-1] if matches else None


def _iio_device_dirs() -> list[str]:
    return sorted(
        (path for path in glob.glob(os.path.join(IIO_DEVICES_PATH, "iio:device*"))
         if os.path.isdir(path)),
        key=lambda path: (
            int(re.search(r"iio:device([0-9]+)$", path).group(1))
            if re.search(r"iio:device([0-9]+)$", path)
            else 2**31,
            path,
        ),
    )


def _make_accel_device(
    iio_path: str, hinge_path: str, hid_hub: str
) -> Optional[AccelDevice]:
    name_path = os.path.join(iio_path, "name")
    raw_paths = tuple(
        os.path.join(iio_path, f"in_accel_{axis}_raw") for axis in ("x", "y", "z")
    )
    common_scale = os.path.join(iio_path, "in_accel_scale")
    if os.path.isfile(common_scale):
        scale_paths = (common_scale, common_scale, common_scale)
    else:
        scale_paths = tuple(
            os.path.join(iio_path, f"in_accel_{axis}_scale")
            for axis in ("x", "y", "z")
        )

    if not all(os.path.isfile(path) and os.access(path, os.R_OK)
               for path in raw_paths + scale_paths):
        return None
    return AccelDevice(
        iio_path=iio_path,
        hinge_path=hinge_path,
        hid_hub=hid_hub,
        name_path=name_path,
        raw_paths=raw_paths,  # type: ignore[arg-type]
        scale_paths=scale_paths,  # type: ignore[arg-type]
    )


def discover_accel() -> Optional[AccelDevice]:
    """Conservatively select the display accelerometer.

    Prefer a unique readable accel sharing a HID hub with a hinge sensor. If
    no such topology exists, accept only one readable accel_3d system-wide.
    Multiple equally plausible devices are intentionally left unresolved.
    """

    hinges: list[Tuple[str, str]] = []
    accels: list[Tuple[str, str]] = []
    for iio_path in _iio_device_dirs():
        name_path = os.path.join(iio_path, "name")
        try:
            name = _read_text(name_path)
        except (OSError, UnicodeError):
            continue
        hub = find_hid_hub_ancestor(_sysfs_device_path(iio_path))
        if name == "hinge" and hub is not None:
            hinges.append((iio_path, hub))
        elif name == "accel_3d":
            accels.append((iio_path, hub or ""))

    matched: list[AccelDevice] = []
    for hinge_path, hinge_hub in hinges:
        for accel_path, accel_hub in accels:
            if hinge_hub != accel_hub:
                continue
            device = _make_accel_device(accel_path, hinge_path, accel_hub)
            if device is not None:
                matched.append(device)
    unique_matched = {device.iio_path: device for device in matched}
    if len(unique_matched) == 1:
        return next(iter(unique_matched.values()))
    if len(unique_matched) > 1:
        return None

    readable: list[AccelDevice] = []
    for accel_path, accel_hub in accels:
        device = _make_accel_device(accel_path, "", accel_hub)
        if device is not None:
            readable.append(device)
    return readable[0] if len(readable) == 1 else None


def read_accel(device: AccelDevice) -> AccelReading:
    """Read raw x/y/z and their world-unit scale from IIO sysfs."""

    raw = tuple(int(_read_text(path), 10) for path in device.raw_paths)
    scale = tuple(float(_read_text(path)) for path in device.scale_paths)
    if not all(math.isfinite(value) and value != 0.0 for value in scale):
        raise ValueError("invalid accelerometer scale")
    values = tuple(raw_value * scale_value
                   for raw_value, scale_value in zip(raw, scale))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("invalid accelerometer sample")
    return AccelReading(raw=raw, scale=scale, values=values)  # type: ignore[arg-type]


def read_orientation_accel(device: AccelDevice) -> AccelReading:
    """Read only physical axes mapped to logical screen X/Y.

    Screen orientation does not need the logical Z component: a flat screen is
    rejected because planar gravity is below ``MIN_ACCEL_MAGNITUDE``. Avoiding
    the unused axis also avoids known HID sensor-driver stalls on some hardware.
    Full three-axis reads remain available to probe and calibration commands.
    """

    required = set(AXIS_ORDER[:2])
    raw_values = [0, 0, 0]
    scale_values = [1.0, 1.0, 1.0]
    for index in required:
        raw_values[index] = int(_read_text(device.raw_paths[index]), 10)
        scale_values[index] = float(_read_text(device.scale_paths[index]))
        if not math.isfinite(scale_values[index]) or scale_values[index] == 0.0:
            raise ValueError("invalid accelerometer scale")
    values = tuple(
        raw_value * scale_value
        for raw_value, scale_value in zip(raw_values, scale_values)
    )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("invalid accelerometer sample")
    return AccelReading(
        raw=tuple(raw_values),  # type: ignore[arg-type]
        scale=tuple(scale_values),  # type: ignore[arg-type]
        values=values,  # type: ignore[arg-type]
    )


def _vector_magnitude(values: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def classify_orientation(values: Sequence[float]) -> Optional[int]:
    """Classify a stable, cardinal, in-plane gravity vector.

    Transform mapping is intentionally explicit: +Y=2, +X=1, -Y=0, -X=3.
    A None result means that the current transform must be retained.
    """

    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        return None
    x, y, z = values
    magnitude = _vector_magnitude(values)
    if not (MIN_ACCEL_MAGNITUDE <= magnitude <= MAX_ACCEL_MAGNITUDE):
        return None

    planar = math.hypot(x, y)
    if planar < MIN_PLANAR_RATIO * magnitude:
        return None

    absolute_x, absolute_y = abs(x), abs(y)
    dominant = max(absolute_x, absolute_y)
    secondary = min(absolute_x, absolute_y)
    if dominant < MIN_CARDINAL_RATIO * magnitude:
        return None
    if dominant <= 0.0 or secondary > MAX_SECONDARY_RATIO * dominant:
        return None

    if absolute_y > absolute_x:
        return ORIENTATION_TRANSFORMS[1] if y > 0.0 else ORIENTATION_TRANSFORMS[3]
    return ORIENTATION_TRANSFORMS[0] if x > 0.0 else ORIENTATION_TRANSFORMS[2]


def map_sensor_values(values: Sequence[float]) -> Tuple[float, float, float]:
    """Map physical IIO axes into the configured logical screen axes."""

    if len(values) != 3:
        raise ValueError("accelerometer sample must contain three axes")
    return tuple(
        values[index] * sign for index, sign in zip(AXIS_ORDER, AXIS_SIGNS)
    )  # type: ignore[return-value]


def _unit_vector(values: Sequence[float], magnitude: float) -> Tuple[float, float, float]:
    return (values[0] / magnitude, values[1] / magnitude, values[2] / magnitude)


def _stable_sample(
    reference: Tuple[float, float, float],
    current: Tuple[float, float, float],
    reference_magnitude: float,
    current_magnitude: float,
) -> bool:
    dot = sum(a * b for a, b in zip(reference, current))
    relative_magnitude_change = (
        abs(current_magnitude - reference_magnitude) / reference_magnitude
        if reference_magnitude > 0.0 else math.inf
    )
    return dot >= STABILITY_DOT and relative_magnitude_change <= STABILITY_MAGNITUDE_RATIO


class OrientationFilter:
    """Reject movement/diagonals and hold a candidate before accepting it."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.candidate: Optional[int] = None
        self.candidate_since: Optional[float] = None
        self.reference_unit: Optional[Tuple[float, float, float]] = None
        self.reference_magnitude: Optional[float] = None
        self.last_unit: Optional[Tuple[float, float, float]] = None
        self.last_magnitude: Optional[float] = None
        self.last_sample_time: Optional[float] = None
        self.accepted = False

    def _begin_candidate(
        self,
        orientation: int,
        now: float,
        unit: Tuple[float, float, float],
        magnitude: float,
    ) -> None:
        self.candidate = orientation
        self.candidate_since = now
        self.reference_unit = unit
        self.reference_magnitude = magnitude
        self.last_unit = unit
        self.last_magnitude = magnitude
        self.last_sample_time = now
        self.accepted = False

    def update(self, values: Sequence[float], now: float) -> Optional[int]:
        """Return a transform only once a candidate has held for ~350 ms."""

        orientation = classify_orientation(values)
        if orientation is None:
            self.reset()
            return None
        magnitude = _vector_magnitude(values)
        unit = _unit_vector(values, magnitude)

        if (
            self.candidate != orientation
            or self.candidate_since is None
            or self.reference_unit is None
            or self.reference_magnitude is None
            or self.last_unit is None
            or self.last_magnitude is None
            or self.last_sample_time is None
            or now < self.last_sample_time
            or now - self.last_sample_time > MAX_SAMPLE_GAP_SECONDS
        ):
            self._begin_candidate(orientation, now, unit, magnitude)
            return None

        if not _stable_sample(
            self.reference_unit, unit, self.reference_magnitude, magnitude
        ) or not _stable_sample(
            self.last_unit, unit, self.last_magnitude, magnitude
        ):
            self._begin_candidate(orientation, now, unit, magnitude)
            return None

        self.last_unit = unit
        self.last_magnitude = magnitude
        self.last_sample_time = now
        if not self.accepted and now - self.candidate_since >= CANDIDATE_HOLD_SECONDS:
            self.accepted = True
            return orientation
        return None


def build_eval_command(transform: int) -> str:
    """Build the Hyprland monitor/device transform mutation."""

    if (
        isinstance(transform, bool)
        or not isinstance(transform, int)
        or transform not in ALLOWED_TRANSFORMS
    ):
        raise ValueError(f"invalid transform: {transform}")
    output = lua_string(OUTPUT_NAME)
    touch = lua_string(TOUCH_DEVICE_NAME)
    return (
        f'hl.monitor({{ output = {output}, transform = {transform} }}); '
        f'hl.device({{ name = {touch}, output = {output}, '
        f'transform = {transform} }}); '
        'hl.dispatch(hl.dsp.force_renderer_reload())'
    )


def lua_string(value: str) -> str:
    """Encode an untrusted configuration value as a Lua string literal."""

    if not isinstance(value, str):
        raise ValueError("Lua command values must be strings")
    # JSON quoted-string escaping matches the Lua escapes used by Hyprland's
    # provider. Literal Unicode avoids JSON-only ``\\u`` escape sequences.
    return json.dumps(value, ensure_ascii=False)


def _validate_position_coordinate(value: Any, name: str) -> int:
    """Validate a coordinate before interpolating it into a Hyprland eval."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid {name} coordinate: {value!r}")
    return value


def build_position_eval_command(x: int, y: int) -> str:
    """Build an eval that changes only eDP-1's position and reloads rendering."""

    x = _validate_position_coordinate(x, "x")
    y = _validate_position_coordinate(y, "y")
    output = lua_string(OUTPUT_NAME)
    return (
        f'hl.monitor({{ output = {output}, position = "{x}x{y}" }}); '
        'hl.dispatch(hl.dsp.force_renderer_reload())'
    )


def apply_monitor_position(x: int, y: int, logger: Logger) -> bool:
    """Run the restricted position eval without invoking a shell."""

    try:
        command = build_position_eval_command(x, y)
    except ValueError as exc:
        logger.error("hyprctl-position", f"refusing monitor position: {exc}")
        return False

    try:
        result = subprocess.run(
            ["hyprctl", "eval", command],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=HYPRCTL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        logger.error("hyprctl-position", f"cannot set monitor position: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        if len(detail) > 160:
            detail = detail[:157] + "..."
        logger.error(
            "hyprctl-position",
            f"monitor position {x}x{y} failed ({result.returncode})"
            f"{': ' + detail if detail else ''}",
        )
        return False
    return True


def _json_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
    return default


def _monitor_geometry_int(value: Any) -> Optional[int]:
    """Return a JSON integer without coercing booleans or fractional values."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse_monitor_status(
    payload: Any, output_name: Optional[str] = None
) -> MonitorStatus:
    """Extract monitor state, transform, and live geometry from hyprctl JSON."""

    output_name = OUTPUT_NAME if output_name is None else output_name
    monitors: Any = payload.get("monitors", []) if isinstance(payload, dict) else payload
    if not isinstance(monitors, list):
        return MonitorStatus(found=False, enabled=False, transform=None)
    active_monitor_count = sum(
        1
        for monitor in monitors
        if isinstance(monitor, dict)
        and not _json_bool(monitor.get("disabled"), False)
    )
    for monitor in monitors:
        if not isinstance(monitor, dict) or monitor.get("name") != output_name:
            continue
        if "disabled" in monitor:
            enabled = not _json_bool(monitor.get("disabled"), False)
        elif "enabled" in monitor:
            enabled = _json_bool(monitor.get("enabled"), True)
        else:
            enabled = True
        transform_value = monitor.get("transform")
        transform: Optional[int]
        if isinstance(transform_value, bool):
            transform = None
        else:
            try:
                transform = int(transform_value)
            except (TypeError, ValueError):
                transform = None
        return MonitorStatus(
            found=True,
            enabled=enabled,
            transform=transform,
            x=_monitor_geometry_int(monitor.get("x")),
            y=_monitor_geometry_int(monitor.get("y")),
            width=_monitor_geometry_int(monitor.get("width")),
            height=_monitor_geometry_int(monitor.get("height")),
            active_monitor_count=active_monitor_count,
        )
    return MonitorStatus(
        found=False,
        enabled=False,
        transform=None,
        active_monitor_count=active_monitor_count,
    )


def _touch_entries(payload: Any) -> Optional[list[Any]]:
    """Extract the touch-device list from the hyprctl devices JSON."""

    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None

    touch = payload.get("touch")
    if isinstance(touch, list):
        return touch
    if isinstance(touch, dict):
        entries = _touch_entries(touch)
        if entries is not None:
            return entries

    # Accept a harmless wrapper around the normal devices object as well.
    for wrapper in ("devices", "data"):
        nested = payload.get(wrapper)
        if isinstance(nested, dict):
            entries = _touch_entries(nested)
            if entries is not None:
                return entries
    return None


def parse_touch_device_names(payload: Any) -> set[str]:
    """Return exact names from the touch list in hyprctl devices JSON."""

    entries = _touch_entries(payload)
    if entries is None:
        return set()
    return {
        entry["name"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }


def has_touch_device(payload: Any, device_name: Optional[str] = None) -> bool:
    """Return whether the devices JSON contains the exact touch name."""

    return (TOUCH_DEVICE_NAME if device_name is None else device_name) in parse_touch_device_names(payload)


def query_touch_device(logger: Logger) -> Optional[bool]:
    """Confirm the required touch device exists without changing anything."""

    try:
        result = subprocess.run(
            ["hyprctl", "-j", "devices"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=HYPRCTL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        logger.error("hyprctl-devices", f"cannot query input devices: {exc}")
        return None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        if len(detail) > 160:
            detail = detail[:157] + "..."
        logger.error(
            "hyprctl-devices",
            f"device query failed ({result.returncode}){': ' + detail if detail else ''}",
        )
        return None
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        logger.error("hyprctl-devices-json", f"invalid device JSON: {exc}")
        return None
    if not has_touch_device(payload):
        logger.error(
            "touch-device-missing",
            f"required touch device {TOUCH_DEVICE_NAME} not found; waiting before applying transform",
        )
        return False
    return True


def query_monitor_status(
    logger: Logger, *, report_errors: bool = True
) -> Optional[MonitorStatus]:
    """Ask Hyprland for monitor state without invoking a shell."""

    try:
        result = subprocess.run(
            ["hyprctl", "-j", "monitors", "all"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=HYPRCTL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        if report_errors:
            logger.error("hyprctl-monitors", f"cannot query monitors: {exc}")
        return None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        if len(detail) > 160:
            detail = detail[:157] + "..."
        if report_errors:
            logger.error(
                "hyprctl-monitors",
                f"monitor query failed ({result.returncode}){': ' + detail if detail else ''}",
            )
        return None
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        if report_errors:
            logger.error("hyprctl-monitors-json", f"invalid monitor JSON: {exc}")
        return None
    status = parse_monitor_status(payload)
    if not status.found and report_errors:
        logger.error("monitor-missing", f"monitor {OUTPUT_NAME} not found")
    return status


def monitor_status_matches(
    status: Optional[MonitorStatus], transform: int
) -> bool:
    """Return whether status confirms the requested enabled monitor transform."""

    return (
        status is not None
        and status.found
        and status.enabled
        and isinstance(status.transform, int)
        and not isinstance(status.transform, bool)
        and status.transform == transform
    )


def _monitor_status_has_integer_position(status: Optional[MonitorStatus]) -> bool:
    return (
        status is not None
        and isinstance(status.x, int)
        and not isinstance(status.x, bool)
        and isinstance(status.y, int)
        and not isinstance(status.y, bool)
    )


def _monitor_status_is_single_active(status: Optional[MonitorStatus]) -> bool:
    return (
        status is not None
        and isinstance(status.active_monitor_count, int)
        and not isinstance(status.active_monitor_count, bool)
        and status.active_monitor_count == 1
    )


def _monitor_status_matches_position(
    status: Optional[MonitorStatus], transform: int, x: int, y: int
) -> bool:
    return (
        monitor_status_matches(status, transform)
        and _monitor_status_is_single_active(status)
        and _monitor_status_has_integer_position(status)
        and status is not None
        and status.x == x
        and status.y == y
    )


def should_refresh_geometry(
    status: Optional[MonitorStatus], transform: int
) -> bool:
    """Return whether a confirmed output transform change needs fresh layers."""

    previous_transform = status.transform if status is not None else None
    return (
        isinstance(previous_transform, int)
        and not isinstance(previous_transform, bool)
        and previous_transform != transform
    )


def verify_monitor_transform(
    transform: int,
    logger: Logger,
    *,
    query: Optional[Callable[[], Optional[MonitorStatus]]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll briefly until Hyprland reports the requested monitor transform."""

    if query is None:
        query_status = lambda: query_monitor_status(logger, report_errors=False)
    else:
        query_status = query

    for attempt in range(POST_APPLY_VERIFY_ATTEMPTS):
        if attempt:
            sleep(POST_APPLY_VERIFY_DELAY)
        status = query_status()
        if monitor_status_matches(status, transform):
            return True

    logger.error(
        "hyprctl-verify",
        f"monitor {OUTPUT_NAME} did not confirm enabled transform {transform}; retrying",
    )
    return False


def fast_layer_remap(
    transform: int,
    logger: Logger,
    *,
    query: Optional[Callable[[], Optional[MonitorStatus]]] = None,
    set_position: Optional[Callable[[int, int], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Nudge and restore the output origin to remap Omarchy layer surfaces."""

    if (
        isinstance(transform, bool)
        or not isinstance(transform, int)
        or transform not in ALLOWED_TRANSFORMS
    ):
        logger.error("layer-remap", f"refusing invalid transform {transform}")
        return False

    if query is None:
        query_status = lambda: query_monitor_status(logger, report_errors=False)
    else:
        query_status = query
    if set_position is None:
        position_setter = lambda x, y: apply_monitor_position(x, y, logger)
    else:
        position_setter = set_position

    try:
        status = query_status()
    except Exception as exc:
        logger.error("layer-remap-query", f"cannot query post-transform monitor: {exc}")
        return False
    if (
        not monitor_status_matches(status, transform)
        or not _monitor_status_is_single_active(status)
        or not _monitor_status_has_integer_position(status)
    ):
        logger.error(
            "layer-remap-status",
            f"post-transform monitor {OUTPUT_NAME} lacks the requested transform, integer position, or single active monitor",
        )
        return False

    # The checks above establish that status is present and both coordinates
    # are integers; keep the explicit locals for the restore path.
    original_x = status.x
    original_y = status.y

    def restore_position() -> bool:
        try:
            restored = position_setter(original_x, original_y)
        except Exception as exc:
            logger.error("layer-remap-restore", f"cannot restore monitor position: {exc}")
            return False
        if not restored:
            logger.error(
                "layer-remap-restore",
                f"monitor {OUTPUT_NAME} position restore to {original_x}x{original_y} failed",
            )
            return False
        return True

    def failed(message: str) -> bool:
        logger.error("layer-remap", message)
        # This is deliberately best effort: the caller will use the complete
        # shell restart fallback if the fast path cannot be made reliable.
        restore_position()
        return False

    try:
        nudged = position_setter(original_x, original_y + 1)
    except Exception as exc:
        return failed(f"cannot nudge monitor position: {exc}")
    if not nudged:
        return failed(
            f"monitor {OUTPUT_NAME} position nudge to {original_x}x{original_y + 1} failed"
        )

    try:
        sleep(LAYER_REMAP_NUDGE_DELAY)
    except Exception as exc:
        return failed(f"layer-remap nudge wait failed: {exc}")

    try:
        midpoint_status = query_status()
    except Exception as exc:
        return failed(f"cannot verify nudged monitor position: {exc}")
    if not _monitor_status_matches_position(
        midpoint_status, transform, original_x, original_y + 1
    ):
        return failed(
            f"monitor {OUTPUT_NAME} nudge verification failed at {original_x}x{original_y + 1}"
        )

    try:
        restored = position_setter(original_x, original_y)
    except Exception as exc:
        return failed(f"cannot restore monitor position: {exc}")
    if not restored:
        return failed(
            f"monitor {OUTPUT_NAME} position restore to {original_x}x{original_y} failed"
        )

    try:
        sleep(LAYER_REMAP_SETTLE_DELAY)
    except Exception as exc:
        return failed(f"layer-remap settle wait failed: {exc}")

    try:
        final_status = query_status()
    except Exception as exc:
        return failed(f"cannot verify restored monitor position: {exc}")
    if not _monitor_status_matches_position(
        final_status, transform, original_x, original_y
    ):
        return failed(
            f"monitor {OUTPUT_NAME} position/transform verification failed after fast layer remap"
        )

    logger.debug(
        f"fast layer remap verified at {OUTPUT_NAME} position {original_x}x{original_y}"
    )
    return True


def apply_eval(transform: int, logger: Logger) -> bool:
    """Apply transforms once, then verify the live monitor state."""

    command = build_eval_command(transform)
    # Do this immediately before the mutation so a missing touch device can
    # never result in a monitor-only transform.
    if query_touch_device(logger) is not True:
        return False
    try:
        result = subprocess.run(
            ["hyprctl", "eval", command],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=HYPRCTL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        logger.error("hyprctl-eval", f"cannot apply transform {transform}: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        if len(detail) > 160:
            detail = detail[:157] + "..."
        logger.error(
            "hyprctl-eval",
            f"transform {transform} failed ({result.returncode})"
            f"{': ' + detail if detail else ''}",
        )
        return False
    return verify_monitor_transform(transform, logger)


def restart_omarchy_shell(logger: Logger) -> bool:
    """Restart the Omarchy shell without invoking a shell command interpreter."""

    try:
        result = subprocess.run(
            ["omarchy", "restart", "shell"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=OMARCHY_SHELL_RESTART_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        logger.error("omarchy-shell-restart", f"cannot restart Omarchy shell: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        if len(detail) > 160:
            detail = detail[:157] + "..."
        logger.error(
            "omarchy-shell-restart",
            f"Omarchy shell restart failed ({result.returncode})"
            f"{': ' + detail if detail else ''}",
        )
        return False
    logger.debug("Omarchy shell restarted")
    return True


class SwitchReader:
    """Nonblocking evdev reader with ioctl and SYN_DROPPED recovery."""

    def __init__(self, logger: Logger) -> None:
        self.logger = logger
        self.fd: Optional[int] = None
        self.device: Optional[SwitchDevice] = None
        self.state: Optional[bool] = None
        self._buffer = bytearray()
        self._next_open = 0.0
        self._next_resync = 0.0
        self._discard_after_drop = False

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        self.device = None
        self.state = None
        self._buffer.clear()
        self._discard_after_drop = False

    def _lose_device(self, now: float, reason: str) -> None:
        had_device = self.fd is not None or self.device is not None
        self.close()
        self._next_open = now + DEVICE_RETRY_INTERVAL
        if had_device:
            self.logger.error(
                "switch-device",
                f"switch device lost ({reason}); retrying",
            )

    def _open_if_needed(self, now: float) -> None:
        if self.fd is not None or now < self._next_open:
            return
        try:
            selection, devices = discover_switch_selection()
            candidates = (
                [devices[selection.selected.path]]
                if selection.selected is not None
                and selection.selected.path in devices
                else []
            )
        except Exception as exc:
            self._next_open = now + DEVICE_RETRY_INTERVAL
            self.logger.error("switch-discovery", f"switch discovery failed: {exc}")
            return
        if not candidates:
            self._next_open = now + DEVICE_RETRY_INTERVAL
            self.logger.error(
                "switch-discovery",
                f"tablet switch not selected ({selection.summary}); retrying",
            )
            return

        last_error: Optional[BaseException] = None
        for candidate in candidates:
            try:
                fd, state = open_switch_device(candidate)
            except Exception as exc:
                last_error = exc
                self.logger.debug(f"cannot open switch {candidate.path}: {exc}")
                continue
            self.fd = fd
            self.device = candidate
            self.state = state
            self._buffer.clear()
            self._discard_after_drop = False
            self._next_resync = now + SWITCH_RESYNC_INTERVAL
            self.logger.info(
                f"switch {candidate.path} opened ({'tablet' if state else 'laptop'})"
            )
            return

        self._next_open = now + DEVICE_RETRY_INTERVAL
        if last_error is not None:
            self.logger.error(
                "switch-open",
                f"cannot open tablet switch: {last_error}; retrying",
            )

    def _resync(self, now: float, reason: str, clear_drop: bool = True) -> bool:
        if self.fd is None:
            return False
        try:
            state = read_switch_state(self.fd)
        except Exception as exc:
            self._lose_device(now, f"{reason}: {exc}")
            return False
        self.state = state
        self._next_resync = now + SWITCH_RESYNC_INTERVAL
        # If no SYN_REPORT has arrived yet, continue discarding stale events
        # after SYN_DROPPED even though the ioctl has refreshed the state.
        if clear_drop:
            self._discard_after_drop = False
        self.logger.debug(f"switch state resynced ({reason})")
        return True

    def _drain(self, now: float) -> None:
        if self.fd is None:
            return
        need_resync = False
        for _ in range(32):
            try:
                chunk = os.read(self.fd, INPUT_EVENT_SIZE * 64)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                self._lose_device(now, f"read error: {exc}")
                return
            if not chunk:
                self._lose_device(now, "end of device")
                return
            self._buffer.extend(chunk)
            if len(self._buffer) > INPUT_EVENT_SIZE * 256:
                self._buffer.clear()
                need_resync = True
                self.logger.error("switch-buffer", "switch event buffer overflow; resyncing")

            offset = 0
            while len(self._buffer) - offset >= INPUT_EVENT_SIZE:
                _, _, event_type, code, value = INPUT_EVENT.unpack_from(self._buffer, offset)
                offset += INPUT_EVENT_SIZE

                if self._discard_after_drop:
                    if event_type == EV_SYN and code == SYN_REPORT:
                        self._discard_after_drop = False
                        need_resync = True
                    continue
                if event_type == EV_SYN and code == SYN_DROPPED:
                    self._discard_after_drop = True
                    need_resync = True
                    self.logger.debug("SYN_DROPPED from tablet switch")
                    continue
                if event_type == EV_SW and code == SW_TABLET_MODE:
                    if value in (0, 1):
                        self.state = bool(value)
                    else:
                        need_resync = True
            if offset:
                del self._buffer[:offset]

        if need_resync:
            self._resync(
                now,
                "SYN_DROPPED or malformed stream",
                clear_drop=not self._discard_after_drop,
            )

    def poll(self, now: float) -> Optional[bool]:
        self._open_if_needed(now)
        if self.fd is None:
            return self.state
        self._drain(now)
        if self.fd is not None and now >= self._next_resync:
            self._resync(now, "periodic")
        return self.state


class SensorReader:
    """Read IIO asynchronously so a blocked kernel attribute cannot freeze policy."""

    def __init__(self, logger: Logger) -> None:
        self.logger = logger
        self.device: Optional[AccelDevice] = None
        self._next_discovery = 0.0
        self._generation = 0
        self._worker: Optional[threading.Thread] = None
        self._worker_started = 0.0
        self._results: queue.SimpleQueue[
            tuple[int, Optional[AccelReading], Optional[BaseException]]
        ] = queue.SimpleQueue()

    def reset(self) -> None:
        self._generation += 1
        self.device = None
        self._next_discovery = 0.0

    def _start_read(self, now: float) -> None:
        if self.device is None or self._worker is not None:
            return
        device = self.device
        generation = self._generation

        def worker() -> None:
            try:
                reading = read_orientation_accel(device)
            except BaseException as exc:
                self._results.put((generation, None, exc))
            else:
                self._results.put((generation, reading, None))

        self._worker_started = now
        self._worker = threading.Thread(
            target=worker,
            name="tablet-auto-rotate-sensor-read",
            daemon=True,
        )
        self._worker.start()

    def read(self, now: float) -> Optional[AccelReading]:
        if self._worker is not None:
            try:
                generation, reading, error = self._results.get_nowait()
            except queue.Empty:
                if now - self._worker_started >= SENSOR_READ_WARNING_SECONDS:
                    self.logger.error(
                        "sensor-read-blocked",
                        "accelerometer kernel read is blocked; switch handling remains active",
                    )
                return None
            self._worker = None
            if generation != self._generation:
                return None
            if error is not None:
                self.logger.error(
                    "sensor-read",
                    f"accelerometer read failed: {error}; rediscovering",
                )
                self.reset()
                self._next_discovery = now + DEVICE_RETRY_INTERVAL
                return None
            if reading is not None:
                self._start_read(now)
                return reading

        if self.device is None:
            if now < self._next_discovery:
                return None
            try:
                device = discover_accel()
            except Exception as exc:
                device = None
                self.logger.error("sensor-discovery", f"sensor discovery failed: {exc}")
            if device is None:
                self._next_discovery = now + DEVICE_RETRY_INTERVAL
                self.logger.error(
                    "sensor-missing",
                    "display accelerometer not found; retrying",
                )
                return None
            self.device = device
            self.logger.info(
                f"accelerometer {device.iio_path} selected (hub {device.hid_hub})"
            )
        self._start_read(now)
        return None


class RotationDaemon:
    """Coordinate switch state, filtered samples, and safe display/shell updates."""

    def __init__(self, logger: Logger, dry_run: bool = False) -> None:
        self.logger = logger
        self.dry_run = dry_run
        self.stop = threading.Event()
        self.switch = SwitchReader(logger)
        self.sensor = SensorReader(logger)
        self.filter = OrientationFilter()
        self.tablet_mode: Optional[bool] = None
        self.desired_transform: Optional[int] = None
        self.last_applied_transform: Optional[int] = None
        self.next_sensor_sample = 0.0
        # Reconcile once immediately, then every MONITOR_CHECK_INTERVAL.
        self.next_monitor_check = 0.0
        self.next_apply_retry = 0.0
        self.apply_retry_delay = INITIAL_APPLY_RETRY

    def request_stop(self) -> None:
        self.stop.set()

    def _schedule_apply_retry(self, now: float) -> None:
        self.next_apply_retry = now + self.apply_retry_delay
        self.apply_retry_delay = min(self.apply_retry_delay * 2.0, MAX_APPLY_RETRY)

    def _apply_if_needed(
        self,
        now: float,
        reason: str,
        status: Optional[MonitorStatus] = None,
    ) -> None:
        transform = self.desired_transform
        if transform is None:
            return
        if (
            isinstance(transform, bool)
            or not isinstance(transform, int)
            or transform not in ALLOWED_TRANSFORMS
        ):
            self.logger.error("transform", f"refusing invalid transform {transform}")
            self.desired_transform = None
            return

        if self.dry_run:
            if self.last_applied_transform != transform:
                self.logger.info(
                    f"dry-run: would apply transform {transform} ({reason})"
                )
                self.last_applied_transform = transform
            self.next_apply_retry = math.inf
            return

        if status is None:
            status = query_monitor_status(self.logger)
        if status is None or not status.found:
            self._schedule_apply_retry(now)
            return
        if not status.enabled:
            self.logger.error(
                "monitor-disabled",
                f"monitor {OUTPUT_NAME} is disabled; waiting before applying transform",
            )
            self._schedule_apply_retry(now)
            return

        if (
            status.transform == transform
            and self.last_applied_transform == transform
        ):
            self.next_apply_retry = math.inf
            self.apply_retry_delay = INITIAL_APPLY_RETRY
            return

        previous_status = status
        if apply_eval(transform, self.logger):
            # Record the transform before the geometry refresh so a fallback
            # failure cannot cause the monitor update to be retried.
            self.last_applied_transform = transform
            self.next_apply_retry = math.inf
            self.apply_retry_delay = INITIAL_APPLY_RETRY
            if should_refresh_geometry(previous_status, transform) and DESKTOP_INTEGRATION == "omarchy":
                if not fast_layer_remap(transform, self.logger):
                    self.logger.error(
                        "layer-remap-fallback",
                        "fast layer remap failed; falling back to Omarchy shell restart",
                    )
                    restart_omarchy_shell(self.logger)
            self.logger.info(f"transform -> {transform}")
        else:
            self._schedule_apply_retry(now)

    def _set_desired(self, transform: int, now: float, reason: str) -> None:
        if (
            isinstance(transform, bool)
            or not isinstance(transform, int)
            or transform not in ALLOWED_TRANSFORMS
        ):
            self.logger.error("transform", f"ignoring invalid requested transform {transform}")
            return
        self.desired_transform = transform
        self.next_apply_retry = now
        self.apply_retry_delay = INITIAL_APPLY_RETRY
        self._apply_if_needed(now, reason)

    def _handle_switch_state(self, now: float) -> None:
        state = self.switch.state
        if state is None:
            if self.tablet_mode is not None:
                self.logger.error("switch-state", "tablet mode state unavailable; waiting for switch")
                self.tablet_mode = None
                self.desired_transform = None
                self.filter.reset()
                self.sensor.reset()
                self.next_sensor_sample = now
            return

        if self.tablet_mode is not None and state == self.tablet_mode:
            return

        self.tablet_mode = state
        self.filter.reset()
        self.sensor.reset()
        self.next_sensor_sample = now
        if state:
            # Do not change an existing transform until a tablet orientation has
            # passed the filter.  This is also the safe startup behavior.
            self.desired_transform = None
            self.logger.info("tablet mode on; waiting for stable orientation")
        else:
            self.logger.info("tablet mode off; forcing transform 0")
            self._set_desired(0, now, "tablet mode off")

    def _sample_sensor(self, now: float) -> None:
        reading = self.sensor.read(now)
        if reading is None:
            return
        transform = self.filter.update(map_sensor_values(reading.values), now)
        if transform is not None:
            self._set_desired(transform, now, "stable orientation")

    def _reconcile_monitor(self, now: float) -> None:
        if self.dry_run or self.desired_transform is None:
            return
        status = query_monitor_status(self.logger)
        if status is None:
            self._schedule_apply_retry(now)
            return
        if not status.found or not status.enabled:
            self._apply_if_needed(now, "monitor check", status)
            return
        if (
            status.transform != self.desired_transform
            or self.last_applied_transform != self.desired_transform
        ):
            self._apply_if_needed(now, "monitor check", status)

    def run(self) -> int:
        self.logger.info("started" + (" (dry-run)" if self.dry_run else ""))
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                try:
                    self.switch.poll(now)
                    self._handle_switch_state(now)
                    if self.tablet_mode is True and now >= self.next_sensor_sample:
                        self._sample_sensor(now)
                        self.next_sensor_sample = now + LOOP_INTERVAL
                    if (
                        self.desired_transform is not None
                        and now >= self.next_apply_retry
                    ):
                        self._apply_if_needed(now, "retry")
                    if now >= self.next_monitor_check:
                        self._reconcile_monitor(now)
                        self.next_monitor_check = now + MONITOR_CHECK_INTERVAL
                except Exception as exc:  # Keep transient sysfs/IPC failures nonfatal.
                    self.logger.error("loop", f"temporary loop failure: {exc}")
                self.stop.wait(LOOP_INTERVAL)
        except KeyboardInterrupt:
            self.stop.set()
        finally:
            self.switch.close()
        self.logger.info("stopped")
        return 0


def _runtime_lock_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        runtime_dir = f"/tmp/{SCRIPT_NAME}-{os.getuid()}"
    return os.path.join(runtime_dir, f"{SCRIPT_NAME}.lock")


def apply_config(config: HardwareConfig) -> None:
    """Apply validated machine-specific values before device discovery starts."""

    global OUTPUT_NAME, TOUCH_DEVICE_NAME, SWITCH_NAME, PREFERRED_SWITCH_PATH
    global DESKTOP_INTEGRATION, AXIS_ORDER, AXIS_SIGNS, ORIENTATION_TRANSFORMS
    OUTPUT_NAME = config.output
    TOUCH_DEVICE_NAME = config.touch_device
    SWITCH_NAME = config.switch_name
    PREFERRED_SWITCH_PATH = config.preferred_switch_path
    DESKTOP_INTEGRATION = config.desktop_integration
    axis_indexes = {"x": 0, "y": 1, "z": 2}
    AXIS_ORDER = tuple(axis_indexes[axis] for axis in config.axis_order)
    AXIS_SIGNS = config.axis_signs
    ORIENTATION_TRANSFORMS = config.orientation_transforms


def acquire_lock(logger: Logger) -> Optional[int]:
    """Acquire a nonblocking process lock kept open for the daemon lifetime."""

    path = _runtime_lock_path()
    try:
        parent = os.path.dirname(path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        parent_status = os.stat(parent, follow_symlinks=False)
        if not stat.S_ISDIR(parent_status.st_mode) or parent_status.st_uid != os.getuid():
            raise OSError(f"unsafe runtime directory ownership: {parent}")
        if parent_status.st_mode & 0o022:
            raise OSError(f"unsafe writable runtime directory: {parent}")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        return fd
    except BlockingIOError:
        logger.error("lock", "another tablet-auto-rotate daemon is already running", interval=0.0)
        return None
    except OSError as exc:
        logger.error("lock", f"cannot acquire runtime lock {path}: {exc}", interval=0.0)
        return None


def _format_values(values: Iterable[Any]) -> str:
    return " ".join(str(value) for value in values)


def run_probe(verbose: bool = False) -> int:
    """Print discovery and current read-only state without running rotation."""

    logger = Logger(verbose)
    try:
        selection, switch_devices = discover_switch_selection()
        candidates = (
            [switch_devices[selection.selected.path]]
            if selection.selected is not None
            and selection.selected.path in switch_devices
            else []
        )
    except Exception as exc:
        selection = None
        candidates = []
        logger.error("probe-switch-discovery", f"switch discovery failed: {exc}", interval=0.0)
    if selection is not None:
        print(f"switch_selection: {selection.status}: {selection.summary}")
        for candidate in selection.candidates:
            if not verbose and not candidate.capable and candidate.rank == 0:
                continue
            print(
                "switch_candidate: "
                f"{candidate.path} name={candidate.name!r} capable={candidate.capable} "
                f"rank={candidate.rank} reasons={'; '.join(candidate.reasons)}"
            )
    switch_ok = False
    if candidates:
        print("switch_candidates: " + ", ".join(device.path for device in candidates))
    else:
        print("switch_candidates: unavailable")

    selected_switch: Optional[SwitchDevice] = None
    fd: Optional[int] = None
    for candidate in candidates:
        try:
            fd, state = open_switch_device(candidate)
            selected_switch = candidate
            switch_ok = True
            print(f"switch_path: {candidate.path}")
            print(f"switch_sysfs: {candidate.name_path}")
            print(f"switch_name: {_read_text(candidate.name_path)}")
            print(f"switch_state: {'tablet' if state else 'laptop'}")
            break
        except Exception as exc:
            logger.error("probe-switch", f"cannot read {candidate.path}: {exc}", interval=0.0)
    if selected_switch is None:
        print("switch_state: unavailable")
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass

    sensor_ok = False
    try:
        sensor = discover_accel()
    except Exception as exc:
        sensor = None
        logger.error("probe-sensor", f"sensor discovery failed: {exc}", interval=0.0)
    if sensor is None:
        print("sensor_iio: unavailable")
    else:
        print(f"sensor_hinge_iio: {sanitize_report_path(sensor.hinge_path) if sensor.hinge_path else 'none'}")
        print(f"sensor_iio: {sanitize_report_path(sensor.iio_path)}")
        print(f"sensor_hid_hub: {sensor.hid_hub}")
        print(f"sensor_raw_paths: {_format_values(sensor.raw_paths)}")
        print(f"sensor_scale_paths: {_format_values(sensor.scale_paths)}")
        try:
            reading = read_accel(sensor)
        except Exception as exc:
            logger.error("probe-sensor-read", f"cannot read sensor: {exc}", interval=0.0)
            print("sensor_raw: unavailable")
        else:
            sensor_ok = True
            print(f"sensor_raw: {_format_values(reading.raw)}")
            print(f"sensor_scale: {_format_values(reading.scale)}")
            print(f"sensor_world: {_format_values(f'{value:.6g}' for value in reading.values)}")

    return 0 if switch_ok and sensor_ok else 1


def _assert_self_test(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_self_test() -> int:
    """Exercise pure classification, parsing, commands, verification, and ioctl construction."""

    try:
        _assert_self_test(INPUT_EVENT_SIZE == 24, "64-bit input_event size is not 24")
        packed = struct.pack(INPUT_EVENT_FORMAT, 1, 2, EV_SW, SW_TABLET_MODE, 1)
        unpacked = INPUT_EVENT.unpack(packed)
        _assert_self_test(unpacked[2:] == (EV_SW, SW_TABLET_MODE, 1), "input_event unpack")

        expected_ioctl = (
            (IOC_READ << IOC_DIRSHIFT)
            | (SW_MASK_BYTES << IOC_SIZESHIFT)
            | (ord("E") << IOC_TYPESHIFT)
            | 0x1B
        )
        _assert_self_test(EVIOCGSW_REQUEST == expected_ioctl, "EVIOCGSW request construction")
        _assert_self_test(evio_cgsw(1) != evio_cgsw(2), "EVIOCGSW size encoding")

        for values, expected in (
            ((0.0, GRAVITY, 0.0), 2),
            ((GRAVITY, 0.0, 0.0), 1),
            ((0.0, -GRAVITY, 0.0), 0),
            ((-GRAVITY, 0.0, 0.0), 3),
        ):
            _assert_self_test(classify_orientation(values) == expected, f"orientation {values}")
        for values in (
            (0.0, 0.0, GRAVITY),       # flat/normal to the screen
            (GRAVITY, GRAVITY, 0.0),   # diagonal
            (0.0, 2.0, 0.0),           # movement/no gravity
            (float("nan"), 0.0, 0.0),
            (0.0, 0.0, 20.0),
        ):
            _assert_self_test(classify_orientation(values) is None, f"rejected vector {values}")

        orientation_filter = OrientationFilter()
        _assert_self_test(orientation_filter.update((0.0, GRAVITY, 0.0), 0.0) is None,
                          "candidate accepted too early")
        _assert_self_test(orientation_filter.update((0.0, GRAVITY, 0.0), 0.10) is None,
                          "candidate accepted too early 2")
        _assert_self_test(orientation_filter.update((0.0, GRAVITY, 0.0), 0.20) is None,
                          "candidate accepted too early 3")
        _assert_self_test(orientation_filter.update((0.0, GRAVITY, 0.0), 0.36) == 2,
                          "candidate hold")
        _assert_self_test(orientation_filter.update((0.0, 0.0, GRAVITY), 0.46) is None,
                          "flat sample not rejected")

        _assert_self_test(
            build_eval_command(2)
            == 'hl.monitor({ output = "eDP-1", transform = 2 }); '
               'hl.device({ name = "elan9004:00-04f3:4110", output = "eDP-1", transform = 2 }); '
               'hl.dispatch(hl.dsp.force_renderer_reload())',
            "Hyprland command construction",
        )
        try:
            build_eval_command(4)
        except ValueError:
            pass
        else:
            raise AssertionError("transform allowlist")

        _assert_self_test(
            build_position_eval_command(-1920, 37)
            == 'hl.monitor({ output = "eDP-1", position = "-1920x37" }); '
               'hl.dispatch(hl.dsp.force_renderer_reload())',
            "monitor position command construction",
        )
        for invalid_position in ((True, 0), (0, False), (1.5, 0), (0, "1")):
            try:
                build_position_eval_command(*invalid_position)
            except ValueError:
                pass
            else:
                raise AssertionError(f"position validation: {invalid_position}")

        touch_payload = {
            "touch": [
                {"name": TOUCH_DEVICE_NAME},
                {"name": "unrelated-touch-device"},
            ]
        }
        _assert_self_test(
            parse_touch_device_names(touch_payload)
            == {TOUCH_DEVICE_NAME, "unrelated-touch-device"},
            "touch device parsing",
        )
        _assert_self_test(has_touch_device(touch_payload), "touch device presence")
        _assert_self_test(
            has_touch_device({"devices": touch_payload}),
            "wrapped touch device parsing",
        )
        _assert_self_test(
            not has_touch_device({"touch": [{"name": TOUCH_DEVICE_NAME + "-other"}]}),
            "exact touch device name matching",
        )
        _assert_self_test(
            not has_touch_device({"devices": [{"name": TOUCH_DEVICE_NAME}]}),
            "touch list is required",
        )

        status = parse_monitor_status(
            [{"name": OUTPUT_NAME, "disabled": False, "transform": 3}]
        )
        _assert_self_test(
            status == MonitorStatus(True, True, 3, active_monitor_count=1),
            "monitor parsing",
        )
        geometry_status = parse_monitor_status(
            {
                "monitors": [
                    {
                        "name": OUTPUT_NAME,
                        "disabled": False,
                        "transform": 1,
                        "x": -1920,
                        "y": 37,
                        "width": 2880,
                        "height": 1800,
                    }
                ]
            }
        )
        _assert_self_test(
            geometry_status
            == MonitorStatus(
                True,
                True,
                1,
                -1920,
                37,
                2880,
                1800,
                active_monitor_count=1,
            ),
            "monitor geometry parsing",
        )
        malformed_geometry = parse_monitor_status(
            [
                {
                    "name": OUTPUT_NAME,
                    "transform": 1,
                    "x": True,
                    "y": 1.5,
                    "width": "2880",
                    "height": None,
                }
            ]
        )
        _assert_self_test(
            malformed_geometry
            == MonitorStatus(True, True, 1, active_monitor_count=1),
            "invalid monitor geometry parsing",
        )
        disabled = parse_monitor_status(
            {"monitors": [{"name": OUTPUT_NAME, "disabled": True, "transform": 0}]}
        )
        _assert_self_test(not disabled.enabled, "disabled monitor parsing")
        _assert_self_test(
            disabled.active_monitor_count == 0,
            "disabled monitor excluded from active count",
        )
        monitor_count_status = parse_monitor_status(
            [
                {"name": OUTPUT_NAME, "disabled": False, "transform": 2},
                {"name": "HDMI-A-1", "disabled": False, "transform": 0},
                {"name": "DP-1", "disabled": True, "transform": 1},
            ]
        )
        _assert_self_test(
            monitor_count_status.active_monitor_count == 2,
            "active monitor count parsing",
        )
        _assert_self_test(
            monitor_status_matches(MonitorStatus(True, True, 2), 2),
            "monitor verification match",
        )
        _assert_self_test(
            should_refresh_geometry(MonitorStatus(True, True, 0), 2),
            "geometry refresh on transform change",
        )
        _assert_self_test(
            should_refresh_geometry(MonitorStatus(True, True, 2), 0),
            "geometry refresh on 180-degree transform change",
        )
        for unchanged_status in (
            None,
            MonitorStatus(True, True, 2),
            MonitorStatus(True, True, None),
            MonitorStatus(True, True, True),
        ):
            _assert_self_test(
                not should_refresh_geometry(unchanged_status, 2),
                "no geometry refresh without a prior differing integer transform",
            )
        for invalid_status in (
            None,
            MonitorStatus(False, True, 2),
            MonitorStatus(True, False, 2),
            MonitorStatus(True, True, 1),
        ):
            _assert_self_test(
                not monitor_status_matches(invalid_status, 2),
                "monitor verification rejection",
            )
        verification_statuses = iter(
            (
                MonitorStatus(True, True, 1),
                MonitorStatus(True, False, 2),
                MonitorStatus(True, True, 2),
            )
        )
        verification_sleeps: list[float] = []
        _assert_self_test(
            verify_monitor_transform(
                2,
                Logger(),
                query=lambda: next(verification_statuses),
                sleep=verification_sleeps.append,
            ),
            "monitor verification polling",
        )
        _assert_self_test(
            verification_sleeps
            == [POST_APPLY_VERIFY_DELAY, POST_APPLY_VERIFY_DELAY],
            "monitor verification delay",
        )

        class SelfTestLogger:
            def error(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def debug(self, *_args: Any, **_kwargs: Any) -> None:
                pass

        original_x, original_y = 17, -4
        happy_statuses = [
            MonitorStatus(
                True,
                True,
                2,
                original_x,
                original_y,
                active_monitor_count=1,
            ),
            MonitorStatus(
                True,
                True,
                2,
                original_x,
                original_y + 1,
                active_monitor_count=1,
            ),
            MonitorStatus(
                True,
                True,
                2,
                original_x,
                original_y,
                active_monitor_count=1,
            ),
        ]
        happy_events: list[tuple[Any, ...]] = []

        def happy_query() -> Optional[MonitorStatus]:
            happy_events.append(("query",))
            if not happy_statuses:
                raise AssertionError("fast remap queried too many times")
            return happy_statuses.pop(0)

        def happy_set_position(x: int, y: int) -> bool:
            happy_events.append(("set", x, y))
            return True

        def happy_sleep(delay: float) -> None:
            happy_events.append(("sleep", delay))

        _assert_self_test(
            fast_layer_remap(
                2,
                SelfTestLogger(),
                query=happy_query,
                set_position=happy_set_position,
                sleep=happy_sleep,
            ),
            "fast layer remap happy path",
        )
        _assert_self_test(
            happy_events
            == [
                ("query",),
                ("set", original_x, original_y + 1),
                ("sleep", LAYER_REMAP_NUDGE_DELAY),
                ("query",),
                ("set", original_x, original_y),
                ("sleep", LAYER_REMAP_SETTLE_DELAY),
                ("query",),
            ],
            "fast layer remap call/query/sleep order",
        )

        no_op_statuses = [
            MonitorStatus(
                True,
                True,
                2,
                original_x,
                original_y,
                active_monitor_count=1,
            ),
            MonitorStatus(
                True,
                True,
                2,
                original_x,
                original_y,
                active_monitor_count=1,
            ),
        ]
        no_op_events: list[tuple[Any, ...]] = []

        def no_op_query() -> Optional[MonitorStatus]:
            no_op_events.append(("query",))
            if not no_op_statuses:
                raise AssertionError("no-op remap queried too many times")
            return no_op_statuses.pop(0)

        def no_op_set_position(x: int, y: int) -> bool:
            no_op_events.append(("set", x, y))
            return True

        def no_op_sleep(delay: float) -> None:
            no_op_events.append(("sleep", delay))

        _assert_self_test(
            not fast_layer_remap(
                2,
                SelfTestLogger(),
                query=no_op_query,
                set_position=no_op_set_position,
                sleep=no_op_sleep,
            ),
            "silent no-op midpoint failure",
        )
        _assert_self_test(
            no_op_events
            == [
                ("query",),
                ("set", original_x, original_y + 1),
                ("sleep", LAYER_REMAP_NUDGE_DELAY),
                ("query",),
                ("set", original_x, original_y),
            ],
            "silent no-op midpoint restore",
        )

        multi_monitor_positions: list[tuple[int, int]] = []
        multi_monitor_sleeps: list[float] = []

        def multi_monitor_set_position(x: int, y: int) -> bool:
            multi_monitor_positions.append((x, y))
            return True

        def multi_monitor_sleep(delay: float) -> None:
            multi_monitor_sleeps.append(delay)

        _assert_self_test(
            not fast_layer_remap(
                2,
                SelfTestLogger(),
                query=lambda: MonitorStatus(
                    True,
                    True,
                    2,
                    original_x,
                    original_y,
                    active_monitor_count=2,
                ),
                set_position=multi_monitor_set_position,
                sleep=multi_monitor_sleep,
            ),
            "multi-monitor initial rejection",
        )
        _assert_self_test(
            not multi_monitor_positions and not multi_monitor_sleeps,
            "multi-monitor rejection mutated position",
        )

        _assert_self_test(
            find_hid_hub_ancestor("/sys/x/001F:8087:0AC2.0003/HID-SENSOR/iio:device0")
            == "001F:8087:0AC2.0003",
            "HID hub ancestry",
        )
    except AssertionError as exc:
        print(f"{SCRIPT_NAME}: self-test failed: {exc}", file=sys.stderr)
        return 1

    print(f"{SCRIPT_NAME}: self-test ok")
    return 0


def run_doctor(config: HardwareConfig, config_path: Optional[str]) -> int:
    """Run read-only compatibility checks and print actionable results."""

    checks: list[Tuple[str, bool, str]] = []
    checks.append((
        "input ABI",
        INPUT_EVENT_SIZE == 24,
        f"input_event size is {INPUT_EVENT_SIZE} bytes (24 required)",
    ))
    checks.append((
        "hyprctl",
        shutil.which("hyprctl") is not None,
        "found on PATH" if shutil.which("hyprctl") else "not found on PATH",
    ))
    if config.desktop_integration == "omarchy":
        checks.append((
            "Omarchy",
            shutil.which("omarchy") is not None,
            "found on PATH" if shutil.which("omarchy") else "not found on PATH",
        ))
    checks.append((
        "configuration",
        True,
        config_path or f"defaults (expected user path: {default_config_path()})",
    ))
    checks.append(("target output", bool(config.output), config.output))
    checks.append(("touch device", bool(config.touch_device), config.touch_device))
    checks.append(("tablet switch", bool(config.switch_name), config.switch_name))

    try:
        selection, _ = discover_switch_selection()
    except Exception as exc:
        checks.append(("switch discovery", False, f"failed: {exc}"))
    else:
        checks.append((
            "switch discovery",
            selection.status == "selected",
            f"{selection.status}: {selection.summary}",
        ))
    try:
        sensor = discover_accel()
    except Exception as exc:
        checks.append(("accelerometer discovery", False, f"failed: {exc}"))
    else:
        checks.append((
            "accelerometer discovery",
            sensor is not None,
            sanitize_report_path(sensor.iio_path) if sensor is not None else "no unique readable accel_3d",
        ))

    if shutil.which("hyprctl") is not None:
        logger = Logger(False)
        monitor = query_monitor_status(logger, report_errors=False)
        checks.append((
            "Hyprland output",
            monitor is not None and monitor.found and monitor.enabled,
            (
                f"{config.output} is enabled"
                if monitor is not None and monitor.found and monitor.enabled
                else f"{config.output} is not available and enabled"
            ),
        ))
        touch = query_touch_device(logger)
        checks.append((
            "Hyprland touch device",
            touch is True,
            f"{config.touch_device} {'found' if touch is True else 'not confirmed'}",
        ))

    for name, passed, detail in checks:
        print(f"{'ok' if passed else 'FAIL'}: {name}: {detail}")
    return 0 if all(passed for _, passed, _ in checks) else 1


def run_service_lifecycle(args: argparse.Namespace, *, remove: bool) -> int:
    """Install or remove only the generated user service; never call systemctl."""

    template = Path(__file__).with_name("data") / "tablet-auto-rotate.service.in"
    executable_text = args.service_executable or shutil.which(SCRIPT_NAME)
    if not executable_text:
        print(
            f"{SCRIPT_NAME}: cannot locate an installed executable; use --service-executable",
            file=sys.stderr,
        )
        return 2
    executable = Path(executable_text).resolve()
    try:
        if remove:
            plan = uninstall_service(
                template, executable, dry_run=args.service_dry_run
            )
        else:
            plan = install_service(
                template,
                executable,
                replace=args.replace_service,
                dry_run=args.service_dry_run,
            )
    except (OSError, LifecycleError) as exc:
        print(f"{SCRIPT_NAME}: service operation refused: {exc}", file=sys.stderr)
        return 1
    for action in plan.actions:
        prefix = "would " if args.service_dry_run else ""
        print(f"{prefix}{action.operation}: {action.path}{': ' + action.detail if action.detail else ''}")
    print("Run 'systemctl --user daemon-reload' after changing the unit.")
    if remove:
        print("Disable it separately with 'systemctl --user disable --now tablet-auto-rotate.service'.")
    else:
        print("Enable/start it explicitly with 'systemctl --user enable --now tablet-auto-rotate.service'.")
    return 0


def run_calibration_file(path: Path, config: HardwareConfig) -> int:
    """Infer calibration from a reviewed JSON sample set and print TOML."""

    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if not isinstance(payload, dict):
            raise CalibrationError("sample file must contain a JSON object")
        result = infer_axis_mapping(payload)
        rendered = generate_config_toml(config, result)
    except (OSError, TypeError, ValueError, CalibrationError) as exc:
        print(f"{SCRIPT_NAME}: calibration refused: {exc}", file=sys.stderr)
        return 1
    print(rendered, end="")
    return 0


def run_interactive_calibration(config: HardwareConfig) -> int:
    """Collect four read-only sensor positions and print proposed TOML."""

    if not sys.stdin.isatty():
        print(
            f"{SCRIPT_NAME}: interactive calibration requires a terminal",
            file=sys.stderr,
        )
        return 2
    sensor = discover_accel()
    if sensor is None:
        print(f"{SCRIPT_NAME}: display accelerometer not found", file=sys.stderr)
        return 1
    instructions = {
        "+x": "hold the screen upright with its RIGHT edge pointing down",
        "+y": "hold the screen upright with its BOTTOM edge pointing down",
        "-x": "hold the screen upright with its LEFT edge pointing down",
        "-y": "hold the screen upright with its TOP edge pointing down",
    }
    samples: dict[str, list[Tuple[float, float, float]]] = {}
    print("Calibration is read-only and will not rotate the display or save files.")
    try:
        for label, instruction in instructions.items():
            input(f"{instruction}; press Enter when stable: ")
            readings: list[Tuple[float, float, float]] = []
            for _ in range(10):
                readings.append(read_accel(sensor).values)
                time.sleep(LOOP_INTERVAL)
            samples[label] = readings
        result = infer_axis_mapping(samples)
        rendered = generate_config_toml(config, result)
    except (EOFError, KeyboardInterrupt):
        print(f"\n{SCRIPT_NAME}: calibration cancelled", file=sys.stderr)
        return 130
    except (OSError, ValueError, CalibrationError) as exc:
        print(f"{SCRIPT_NAME}: calibration refused: {exc}", file=sys.stderr)
        return 1
    print("\nProposed configuration (review before saving):\n")
    print(rendered, end="")
    return 0


def _install_signal_handlers(stop: threading.Event) -> None:
    def handle_signal(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    try:
        release = version("tablet-auto-rotate")
    except PackageNotFoundError:
        release = "0.2.1+source"
    parser.add_argument("--version", action="version", version=f"%(prog)s {release}")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--probe", action="store_true", help="print devices and current read-only state")
    modes.add_argument("--dry-run", action="store_true", help="run classification without hyprctl changes")
    modes.add_argument("--self-test", action="store_true", help="run pure logic/ioctl-construction tests")
    modes.add_argument("--doctor", action="store_true", help="run read-only compatibility checks")
    modes.add_argument("--install-service", action="store_true", help="install the systemd user unit")
    modes.add_argument("--uninstall-service", action="store_true", help="remove an unmodified generated user unit")
    modes.add_argument(
        "--calibrate-from",
        metavar="SAMPLES.json",
        help="infer sensor mapping from reviewed labeled samples and print TOML",
    )
    modes.add_argument("--calibrate", action="store_true", help="interactively infer sensor axes without rotating")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="load machine settings from a TOML file",
    )
    parser.add_argument("--verbose", action="store_true", help="log diagnostic details")
    parser.add_argument("--service-dry-run", action="store_true", help="preview a service operation")
    parser.add_argument("--replace-service", action="store_true", help="back up and replace a differing user unit")
    parser.add_argument("--service-executable", metavar="PATH", help="absolute executable path for the user unit")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return run_self_test()
    if args.install_service or args.uninstall_service:
        return run_service_lifecycle(args, remove=args.uninstall_service)
    try:
        config = load_config(
            Path(args.config) if args.config else None,
            required=bool(args.config),
        )
    except (OSError, ValueError) as exc:
        print(f"{SCRIPT_NAME}: invalid configuration: {exc}", file=sys.stderr)
        return 2
    apply_config(config)
    if args.calibrate_from:
        return run_calibration_file(Path(args.calibrate_from), config)
    if args.calibrate:
        return run_interactive_calibration(config)
    if args.doctor:
        return run_doctor(config, args.config)
    if args.probe:
        return run_probe(args.verbose)

    if INPUT_EVENT_SIZE != 24:
        print(
            f"{SCRIPT_NAME}: unsupported Linux input ABI: input_event size "
            f"is {INPUT_EVENT_SIZE}, expected 24; run --doctor for details",
            file=sys.stderr,
        )
        return 1

    logger = Logger(args.verbose)
    lock_fd = acquire_lock(logger)
    if lock_fd is None:
        return 1

    daemon = RotationDaemon(logger, dry_run=args.dry_run)
    _install_signal_handlers(daemon.stop)
    try:
        return daemon.run()
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
