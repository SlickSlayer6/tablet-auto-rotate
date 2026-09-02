#!/usr/bin/env python3
"""Safely rotate the internal display and its touch device in tablet mode.

This daemon intentionally uses only the Linux input/IIO sysfs interfaces,
Hyprland's read-only event socket, and the Hyprland and Omarchy command line
interfaces.  It never grabs the switch device or changes monitor scale, mode,
or enabled state.  After a confirmed output transform change it nudges the
output origin briefly so Omarchy can remap existing layer surfaces, with a
full Omarchy shell restart as fallback.  The input and sensor devices are
discovered again after they disappear so suspend/resume and driver reloads do
not require a restart.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import glob
import json
import math
import os
from pathlib import Path
import queue
import re
import select
import signal
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence, Tuple

from ._version import application_version
from .config import HardwareConfig, RuntimeConfig, default_config_path, load_config
from .discovery import (
    SwitchCandidate as DiscoverySwitchCandidate,
    SwitchSelection,
    sanitize_report_name,
    sanitize_report_path,
    select_tablet_switch,
)
from .lifecycle import LifecycleError, install as install_service, uninstall as uninstall_service
from .calibration import CalibrationError, generate_config_toml, infer_axis_mapping
from .orientation import (
    CANDIDATE_HOLD_SECONDS,
    GRAVITY,
    MAX_ACCEL_MAGNITUDE,
    MAX_SAMPLE_GAP_SECONDS,
    MIN_ACCEL_MAGNITUDE,
    MIN_CARDINAL_RATIO,
    MIN_PLANAR_RATIO,
    MAX_SECONDARY_RATIO,
    STABILITY_DOT,
    STABILITY_MAGNITUDE_RATIO,
    OrientationFilter,
    classify_orientation,
    map_sensor_values,
)


SCRIPT_NAME = "tablet-auto-rotate"
REPORT_SCHEMA_VERSION = 1
# Legacy facade constants are derived from the sole hardware-default source.
_DEFAULT_HARDWARE = HardwareConfig()
PREFERRED_SWITCH_PATH = _DEFAULT_HARDWARE.preferred_switch_path
INPUT_EVENT_PATH = "/dev/input"
INPUT_SYSFS_GLOB = "/sys/class/input/event*/device/name"
SWITCH_NAME = _DEFAULT_HARDWARE.switch_name
OUTPUT_NAME = _DEFAULT_HARDWARE.output
TOUCH_DEVICE_NAME = _DEFAULT_HARDWARE.touch_device
IIO_DEVICES_PATH = "/sys/bus/iio/devices"
DESKTOP_INTEGRATION = _DEFAULT_HARDWARE.desktop_integration
AXIS_ORDER = RuntimeConfig.from_hardware(_DEFAULT_HARDWARE).axis_order
AXIS_SIGNS = _DEFAULT_HARDWARE.axis_signs
ORIENTATION_TRANSFORMS = _DEFAULT_HARDWARE.orientation_transforms
MOUNT_MATRIX_MODE = _DEFAULT_HARDWARE.mount_matrix


def _runtime_from_globals() -> RuntimeConfig:
    """Build compatibility settings for direct calls to legacy core helpers."""

    return RuntimeConfig(
        output=OUTPUT_NAME,
        touch_device=TOUCH_DEVICE_NAME,
        switch_name=SWITCH_NAME,
        preferred_switch_path=PREFERRED_SWITCH_PATH,
        desktop_integration=DESKTOP_INTEGRATION,
        axis_order=AXIS_ORDER,
        axis_signs=AXIS_SIGNS,
        orientation_transforms=ORIENTATION_TRANSFORMS,
        mount_matrix=MOUNT_MATRIX_MODE,
    )

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
MAX_DEVICE_RETRY_INTERVAL = 10.0
MONITOR_CHECK_INTERVAL = 2.0
EVENT_MONITOR_CHECK_INTERVAL = 5.0
HYPRLAND_EVENT_RETRY_INTERVAL = 2.0
HYPRLAND_EVENT_BUFFER_LIMIT = 64 * 1024
HYPRLAND_EVENT_LINE_LIMIT = 4096
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

SENSOR_READ_WARNING_SECONDS = 0.50
_DEFAULT_COMPONENT = object()


@dataclass(frozen=True, slots=True)
class SwitchDevice:
    """A validated evdev switch node and the sysfs name used to validate it."""

    path: str
    event_name: str
    name_path: str
    name: str


@dataclass(frozen=True, slots=True)
class AccelDevice:
    """A conservatively selected display accelerometer and optional hinge."""

    iio_path: str
    hinge_path: str
    hid_hub: str
    name_path: str
    raw_paths: Tuple[str, str, str]
    scale_paths: Tuple[str, str, str]
    mount_matrix_path: str = ""
    mount_matrix: Optional[Tuple[Tuple[float, float, float], ...]] = None
    mount_matrix_error: str = ""


@dataclass(frozen=True, slots=True)
class AccelReading:
    """One raw and scaled accelerometer sample."""

    raw: Tuple[int, int, int]
    scale: Tuple[float, float, float]
    values: Tuple[float, float, float]


@dataclass(frozen=True, slots=True)
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


def _run_command(
    argv: Sequence[str],
    *,
    timeout: float,
    logger: Logger,
    error_key: str,
    action: str,
    report_errors: bool = True,
) -> Optional[subprocess.CompletedProcess[str]]:
    """Run one bounded command and normalize failure reporting."""

    try:
        result = subprocess.run(
            list(argv),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        if report_errors:
            logger.error(error_key, f"{action}: {exc}")
        return None
    if result.returncode == 0:
        return result
    detail = (result.stderr or result.stdout).strip().replace("\n", " ")
    if len(detail) > 160:
        detail = detail[:157] + "..."
    if report_errors:
        logger.error(
            error_key,
            f"{action} failed ({result.returncode}){': ' + detail if detail else ''}",
        )
    return None


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


def discover_switch_selection(
    runtime: Optional[RuntimeConfig] = None,
) -> tuple[SwitchSelection, dict[str, SwitchDevice]]:
    """Select a unique SW_TABLET_MODE device and retain diagnostic reasons."""

    runtime = runtime or _runtime_from_globals()
    devices: list[SwitchDevice] = []
    seen_events: set[str] = set()

    preferred = (
        None if runtime.preferred_switch_path == "auto"
        else _validate_switch_path(runtime.preferred_switch_path)
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
            runtime.preferred_switch_path
            if runtime.preferred_switch_path in known_paths
            else None
        ),
        configured_name=None if runtime.switch_name == "auto" else runtime.switch_name,
    )
    return selection, by_path


def discover_switch_candidates(
    runtime: Optional[RuntimeConfig] = None,
) -> list[SwitchDevice]:
    """Return only a conservatively selected tablet-mode switch."""

    selection, by_path = discover_switch_selection(runtime)
    if selection.selected is None:
        return []
    selected = by_path.get(selection.selected.path)
    return [selected] if selected is not None else []


def discover_switch(runtime: Optional[RuntimeConfig] = None) -> Optional[SwitchDevice]:
    """Return the first validated switch candidate, if one exists."""

    candidates = discover_switch_candidates(runtime)
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


_IIO_DEVICE_NUMBER = re.compile(r"iio:device([0-9]+)$")


def _iio_device_sort_key(path: str) -> tuple[int, str]:
    match = _IIO_DEVICE_NUMBER.search(path)
    return (int(match.group(1)) if match else 2**31, path)


def _iio_device_dirs() -> list[str]:
    return sorted(
        (path for path in glob.glob(os.path.join(IIO_DEVICES_PATH, "iio:device*"))
         if os.path.isdir(path)),
        key=_iio_device_sort_key,
    )


def parse_mount_matrix(text: str) -> Tuple[Tuple[float, float, float], ...]:
    """Parse and validate the Linux IIO 3x3 unitary mount-matrix ABI."""

    clean = text.strip().translate(str.maketrans({"[": " ", "]": " "}))
    tokens = [token for token in re.split(r"[,;\s]+", clean) if token]
    if len(tokens) != 9:
        raise ValueError("mount matrix must contain exactly nine numbers")
    try:
        values = tuple(float(token) for token in tokens)
    except ValueError as exc:
        raise ValueError("mount matrix contains a non-numeric value") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError("mount matrix contains a non-finite value")
    matrix = tuple(
        tuple(values[row * 3 + column] for column in range(3))
        for row in range(3)
    )
    tolerance = 1e-3
    for row, vector in enumerate(matrix):
        if abs(sum(value * value for value in vector) - 1.0) > tolerance:
            raise ValueError(f"mount matrix row {row + 1} is not unit length")
    for first in range(3):
        for second in range(first + 1, 3):
            dot = sum(
                matrix[first][column] * matrix[second][column]
                for column in range(3)
            )
            if abs(dot) > tolerance:
                raise ValueError("mount matrix rows are not orthogonal")
    return matrix  # type: ignore[return-value]


def apply_mount_matrix(
    matrix: Sequence[Sequence[float]], values: Sequence[float]
) -> Tuple[float, float, float]:
    """Rotate one physical sensor sample into the main-hardware frame."""

    if len(matrix) != 3 or any(len(row) != 3 for row in matrix) or len(values) != 3:
        raise ValueError("mount matrix and sample must both have three axes")
    transformed = tuple(
        sum(matrix[row][column] * values[column] for column in range(3))
        for row in range(3)
    )
    if not all(math.isfinite(value) for value in transformed):
        raise ValueError("mount matrix produced a non-finite sample")
    return transformed  # type: ignore[return-value]


def _mounted_values(
    device: AccelDevice,
    values: Sequence[float],
    runtime: Optional[RuntimeConfig] = None,
) -> Tuple[float, float, float]:
    runtime = runtime or _runtime_from_globals()
    if runtime.mount_matrix in {"auto", "require"} and device.mount_matrix is not None:
        return apply_mount_matrix(device.mount_matrix, values)
    return tuple(values)  # type: ignore[return-value]


def _make_accel_device(
    iio_path: str,
    hinge_path: str,
    hid_hub: str,
    runtime: Optional[RuntimeConfig] = None,
) -> Optional[AccelDevice]:
    runtime = runtime or _runtime_from_globals()
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
    mount_matrix_path = ""
    mount_matrix: Optional[Tuple[Tuple[float, float, float], ...]] = None
    mount_matrix_error = ""
    for filename in ("in_accel_mount_matrix", "in_mount_matrix", "mount_matrix"):
        candidate = os.path.join(iio_path, filename)
        if not os.path.isfile(candidate) or not os.access(candidate, os.R_OK):
            continue
        mount_matrix_path = candidate
        try:
            mount_matrix = parse_mount_matrix(_read_text(candidate))
        except (OSError, UnicodeError, ValueError) as exc:
            mount_matrix_error = str(exc)
        break
    if runtime.mount_matrix == "require" and mount_matrix is None:
        return None
    return AccelDevice(
        iio_path=iio_path,
        hinge_path=hinge_path,
        hid_hub=hid_hub,
        name_path=name_path,
        raw_paths=raw_paths,  # type: ignore[arg-type]
        scale_paths=scale_paths,  # type: ignore[arg-type]
        mount_matrix_path=mount_matrix_path,
        mount_matrix=mount_matrix,
        mount_matrix_error=mount_matrix_error,
    )


def discover_accel(runtime: Optional[RuntimeConfig] = None) -> Optional[AccelDevice]:
    """Conservatively select the display accelerometer.

    Prefer a unique readable accel sharing a HID hub with a hinge sensor. If
    no such topology exists, accept only one readable accel_3d system-wide.
    Multiple equally plausible devices are intentionally left unresolved.
    """

    runtime = runtime or _runtime_from_globals()
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
            device = _make_accel_device(accel_path, hinge_path, accel_hub, runtime)
            if device is not None:
                matched.append(device)
    unique_matched = {device.iio_path: device for device in matched}
    if len(unique_matched) == 1:
        return next(iter(unique_matched.values()))
    if len(unique_matched) > 1:
        return None

    readable: list[AccelDevice] = []
    for accel_path, accel_hub in accels:
        device = _make_accel_device(accel_path, "", accel_hub, runtime)
        if device is not None:
            readable.append(device)
    return readable[0] if len(readable) == 1 else None


def read_accel(
    device: AccelDevice, runtime: Optional[RuntimeConfig] = None
) -> AccelReading:
    """Read raw x/y/z and their world-unit scale from IIO sysfs."""

    raw = tuple(int(_read_text(path), 10) for path in device.raw_paths)
    scale = tuple(float(_read_text(path)) for path in device.scale_paths)
    if not all(math.isfinite(value) and value != 0.0 for value in scale):
        raise ValueError("invalid accelerometer scale")
    physical_values = tuple(raw_value * scale_value
                            for raw_value, scale_value in zip(raw, scale))
    values = _mounted_values(device, physical_values, runtime)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("invalid accelerometer sample")
    return AccelReading(raw=raw, scale=scale, values=values)  # type: ignore[arg-type]


def read_orientation_accel(
    device: AccelDevice, runtime: Optional[RuntimeConfig] = None
) -> AccelReading:
    """Read only physical axes mapped to logical screen X/Y.

    Screen orientation does not need the logical Z component: a flat screen is
    rejected because planar gravity is below ``MIN_ACCEL_MAGNITUDE``. Avoiding
    the unused axis also avoids known HID sensor-driver stalls on some hardware.
    Full three-axis reads remain available to probe and calibration commands.
    """

    runtime = runtime or _runtime_from_globals()
    if runtime.mount_matrix in {"auto", "require"} and device.mount_matrix is not None:
        required = {
            physical_axis
            for mounted_axis in runtime.axis_order[:2]
            for physical_axis, coefficient in enumerate(device.mount_matrix[mounted_axis])
            if abs(coefficient) > 1e-12
        }
    else:
        required = set(runtime.axis_order[:2])
    raw_values = [0, 0, 0]
    scale_values = [1.0, 1.0, 1.0]
    for index in required:
        raw_values[index] = int(_read_text(device.raw_paths[index]), 10)
        scale_values[index] = float(_read_text(device.scale_paths[index]))
        if not math.isfinite(scale_values[index]) or scale_values[index] == 0.0:
            raise ValueError("invalid accelerometer scale")
    physical_values = tuple(
        raw_value * scale_value
        for raw_value, scale_value in zip(raw_values, scale_values)
    )
    values = _mounted_values(device, physical_values, runtime)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("invalid accelerometer sample")
    return AccelReading(
        raw=tuple(raw_values),  # type: ignore[arg-type]
        scale=tuple(scale_values),  # type: ignore[arg-type]
        values=values,  # type: ignore[arg-type]
    )


def _required_orientation_axes(
    device: AccelDevice, runtime: RuntimeConfig
) -> tuple[int, ...]:
    if runtime.mount_matrix in {"auto", "require"} and device.mount_matrix is not None:
        required = {
            physical_axis
            for mounted_axis in runtime.axis_order[:2]
            for physical_axis, coefficient in enumerate(
                device.mount_matrix[mounted_axis]
            )
            if abs(coefficient) > 1e-12
        }
    else:
        required = set(runtime.axis_order[:2])
    return tuple(sorted(required))


def _read_fd_text(fd: int, limit: int = 128) -> str:
    """Read one small sysfs attribute from an already-open descriptor."""

    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, limit)
    if len(data) == limit and os.read(fd, 1):
        raise ValueError("sysfs attribute exceeds read limit")
    return data.decode("ascii").strip()


class AccelSampleSession:
    """Cached raw descriptors and scale values for one validated IIO device."""

    def __init__(self, device: AccelDevice, runtime: RuntimeConfig) -> None:
        self.device = device
        self.runtime = runtime
        self.required_axes = _required_orientation_axes(device, runtime)
        self.raw_fds: dict[int, int] = {}
        scale_by_path: dict[str, float] = {}
        scale_values = [1.0, 1.0, 1.0]
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        try:
            for index in self.required_axes:
                scale_path = device.scale_paths[index]
                if scale_path not in scale_by_path:
                    scale = float(_read_text(scale_path))
                    if not math.isfinite(scale) or scale == 0.0:
                        raise ValueError("invalid accelerometer scale")
                    scale_by_path[scale_path] = scale
                scale_values[index] = scale_by_path[scale_path]
                self.raw_fds[index] = os.open(device.raw_paths[index], flags)
        except BaseException:
            self.close()
            raise
        self.scale = tuple(scale_values)

    def close(self) -> None:
        raw_fds, self.raw_fds = self.raw_fds, {}
        for fd in raw_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass

    def read(self) -> AccelReading:
        raw_values = [0, 0, 0]
        for index in self.required_axes:
            raw_values[index] = int(_read_fd_text(self.raw_fds[index]), 10)
        physical_values = tuple(
            raw_value * scale_value
            for raw_value, scale_value in zip(raw_values, self.scale)
        )
        values = _mounted_values(self.device, physical_values, self.runtime)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("invalid accelerometer sample")
        return AccelReading(
            raw=tuple(raw_values),  # type: ignore[arg-type]
            scale=self.scale,  # type: ignore[arg-type]
            values=values,
        )


def _open_accel_sample_session(
    device: AccelDevice, runtime: RuntimeConfig
) -> AccelSampleSession:
    return AccelSampleSession(device, runtime)


def build_eval_command(
    transform: int, runtime: Optional[RuntimeConfig] = None
) -> str:
    """Build the Hyprland monitor/device transform mutation."""

    if (
        isinstance(transform, bool)
        or not isinstance(transform, int)
        or transform not in ALLOWED_TRANSFORMS
    ):
        raise ValueError(f"invalid transform: {transform}")
    runtime = runtime or _runtime_from_globals()
    output = lua_string(runtime.output)
    touch = lua_string(runtime.touch_device)
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


def build_position_eval_command(
    x: int, y: int, runtime: Optional[RuntimeConfig] = None
) -> str:
    """Build an eval that changes only eDP-1's position and reloads rendering."""

    x = _validate_position_coordinate(x, "x")
    y = _validate_position_coordinate(y, "y")
    runtime = runtime or _runtime_from_globals()
    output = lua_string(runtime.output)
    return (
        f'hl.monitor({{ output = {output}, position = "{x}x{y}" }}); '
        'hl.dispatch(hl.dsp.force_renderer_reload())'
    )


def apply_monitor_position(
    x: int,
    y: int,
    logger: Logger,
    runtime: Optional[RuntimeConfig] = None,
) -> bool:
    """Run the restricted position eval without invoking a shell."""

    try:
        command = build_position_eval_command(x, y, runtime)
    except ValueError as exc:
        logger.error("hyprctl-position", f"refusing monitor position: {exc}")
        return False

    return _run_command(
        ["hyprctl", "eval", command],
        timeout=HYPRCTL_TIMEOUT,
        logger=logger,
        error_key="hyprctl-position",
        action=f"set monitor position {x}x{y}",
    ) is not None


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


def query_touch_device(
    logger: Logger, runtime: Optional[RuntimeConfig] = None
) -> Optional[bool]:
    """Confirm the required touch device exists without changing anything."""

    runtime = runtime or _runtime_from_globals()
    result = _run_command(
        ["hyprctl", "-j", "devices"],
        timeout=HYPRCTL_TIMEOUT,
        logger=logger,
        error_key="hyprctl-devices",
        action="query input devices",
    )
    if result is None:
        return None
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        logger.error("hyprctl-devices-json", f"invalid device JSON: {exc}")
        return None
    return _query_touch_payload(payload, logger, runtime)


def _query_touch_payload(
    payload: Any, logger: Logger, runtime: RuntimeConfig
) -> bool:
    if not has_touch_device(payload, runtime.touch_device):
        logger.error(
            "touch-device-missing",
            f"required touch device {runtime.touch_device} not found; waiting before applying transform",
        )
        return False
    return True


def query_monitor_status(
    logger: Logger,
    *,
    report_errors: bool = True,
    runtime: Optional[RuntimeConfig] = None,
) -> Optional[MonitorStatus]:
    """Ask Hyprland for monitor state without invoking a shell."""

    runtime = runtime or _runtime_from_globals()
    result = _run_command(
        ["hyprctl", "-j", "monitors", "all"],
        timeout=HYPRCTL_TIMEOUT,
        logger=logger,
        error_key="hyprctl-monitors",
        action="query monitors",
        report_errors=report_errors,
    )
    if result is None:
        return None
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        if report_errors:
            logger.error("hyprctl-monitors-json", f"invalid monitor JSON: {exc}")
        return None
    status = parse_monitor_status(payload, runtime.output)
    if not status.found and report_errors:
        logger.error("monitor-missing", f"monitor {runtime.output} not found")
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
    runtime: Optional[RuntimeConfig] = None,
) -> Optional[MonitorStatus]:
    """Poll briefly until Hyprland reports the requested monitor transform."""

    runtime = runtime or _runtime_from_globals()
    if query is None:
        query_status = lambda: query_monitor_status(
            logger, report_errors=False, runtime=runtime
        )
    else:
        query_status = query

    for attempt in range(POST_APPLY_VERIFY_ATTEMPTS):
        if attempt:
            sleep(POST_APPLY_VERIFY_DELAY)
        status = query_status()
        if monitor_status_matches(status, transform):
            return status

    logger.error(
        "hyprctl-verify",
        f"monitor {runtime.output} did not confirm enabled transform {transform}; retrying",
    )
    return None


def fast_layer_remap(
    transform: int,
    logger: Logger,
    *,
    query: Optional[Callable[[], Optional[MonitorStatus]]] = None,
    set_position: Optional[Callable[[int, int], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
    initial_status: Optional[MonitorStatus] = None,
    runtime: Optional[RuntimeConfig] = None,
) -> bool:
    """Nudge and restore the output origin to remap Omarchy layer surfaces."""

    runtime = runtime or _runtime_from_globals()
    if (
        isinstance(transform, bool)
        or not isinstance(transform, int)
        or transform not in ALLOWED_TRANSFORMS
    ):
        logger.error("layer-remap", f"refusing invalid transform {transform}")
        return False

    if query is None:
        query_status = lambda: query_monitor_status(
            logger, report_errors=False, runtime=runtime
        )
    else:
        query_status = query
    if set_position is None:
        position_setter = lambda x, y: apply_monitor_position(x, y, logger, runtime)
    else:
        position_setter = set_position

    status = initial_status
    if status is None:
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
            f"post-transform monitor {runtime.output} lacks the requested transform, integer position, or single active monitor",
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
                f"monitor {runtime.output} position restore to {original_x}x{original_y} failed",
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
            f"monitor {runtime.output} position nudge to {original_x}x{original_y + 1} failed"
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
            f"monitor {runtime.output} nudge verification failed at {original_x}x{original_y + 1}"
        )

    try:
        restored = position_setter(original_x, original_y)
    except Exception as exc:
        return failed(f"cannot restore monitor position: {exc}")
    if not restored:
        return failed(
            f"monitor {runtime.output} position restore to {original_x}x{original_y} failed"
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
            f"monitor {runtime.output} position/transform verification failed after fast layer remap"
        )

    logger.debug(
        f"fast layer remap verified at {runtime.output} position {original_x}x{original_y}"
    )
    return True


class LayerRemapOperation:
    """Generation-bound Omarchy remap whose waits are driven by daemon deadlines."""

    def __init__(
        self,
        transform: int,
        generation: int,
        status: MonitorStatus,
        client: Any,
        logger: Logger,
        runtime: RuntimeConfig,
    ) -> None:
        self.transform = transform
        self.generation = generation
        self.status = status
        self.client = client
        self.logger = logger
        self.runtime = runtime
        self.original_x: Optional[int] = None
        self.original_y: Optional[int] = None
        self.state = "new"
        self.deadline = math.inf

    def _restore(self) -> bool:
        if self.original_x is None or self.original_y is None:
            return True
        try:
            restored = self.client.set_position(self.original_x, self.original_y)
        except Exception as exc:
            self.logger.error(
                "layer-remap-restore", f"cannot restore monitor position: {exc}"
            )
            return False
        if not restored:
            self.logger.error(
                "layer-remap-restore",
                f"monitor {self.runtime.output} position restore to "
                f"{self.original_x}x{self.original_y} failed",
            )
        return bool(restored)

    def _fail(self, message: str, *, restore: bool) -> bool:
        self.logger.error("layer-remap", message)
        if restore:
            self._restore()
        self.state = "failed"
        self.deadline = math.inf
        return False

    def start(self, now: float) -> Optional[bool]:
        if (
            not monitor_status_matches(self.status, self.transform)
            or not _monitor_status_is_single_active(self.status)
            or not _monitor_status_has_integer_position(self.status)
        ):
            return self._fail(
                f"post-transform monitor {self.runtime.output} lacks the requested "
                "transform, integer position, or single active monitor",
                restore=False,
            )
        self.original_x = self.status.x
        self.original_y = self.status.y
        try:
            nudged = self.client.set_position(
                self.original_x, self.original_y + 1
            )
        except Exception as exc:
            return self._fail(
                f"cannot nudge monitor position: {exc}", restore=True
            )
        if not nudged:
            return self._fail(
                f"monitor {self.runtime.output} position nudge to "
                f"{self.original_x}x{self.original_y + 1} failed",
                restore=True,
            )
        self.state = "wait_nudge"
        self.deadline = now + LAYER_REMAP_NUDGE_DELAY
        return None

    def advance(self, now: float) -> Optional[bool]:
        if now < self.deadline:
            return None
        if self.state == "wait_nudge":
            try:
                midpoint = self.client.query_monitor(report_errors=False)
            except Exception as exc:
                return self._fail(
                    f"cannot verify nudged monitor position: {exc}", restore=True
                )
            if not _monitor_status_matches_position(
                midpoint,
                self.transform,
                self.original_x,
                self.original_y + 1,
            ):
                return self._fail(
                    f"monitor {self.runtime.output} nudge verification failed at "
                    f"{self.original_x}x{self.original_y + 1}",
                    restore=True,
                )
            if not self._restore():
                return self._fail(
                    f"monitor {self.runtime.output} position restore to "
                    f"{self.original_x}x{self.original_y} failed",
                    restore=False,
                )
            self.state = "wait_settle"
            self.deadline = now + LAYER_REMAP_SETTLE_DELAY
            return None
        if self.state == "wait_settle":
            try:
                final_status = self.client.query_monitor(report_errors=False)
            except Exception as exc:
                return self._fail(
                    f"cannot verify restored monitor position: {exc}", restore=True
                )
            if not _monitor_status_matches_position(
                final_status,
                self.transform,
                self.original_x,
                self.original_y,
            ):
                return self._fail(
                    f"monitor {self.runtime.output} position/transform verification "
                    "failed after fast layer remap",
                    restore=True,
                )
            self.state = "complete"
            self.deadline = math.inf
            self.logger.debug(
                f"fast layer remap verified at {self.runtime.output} position "
                f"{self.original_x}x{self.original_y}"
            )
            return True
        return self.state == "complete"

    def cancel(self) -> bool:
        needs_restore = self.state == "wait_nudge"
        restored = self._restore() if needs_restore else True
        self.state = "cancelled"
        self.deadline = math.inf
        return restored


def apply_eval(
    transform: int,
    logger: Logger,
    runtime: Optional[RuntimeConfig] = None,
) -> Optional[MonitorStatus]:
    """Apply transforms once, then verify the live monitor state."""

    runtime = runtime or _runtime_from_globals()
    command = build_eval_command(transform, runtime)
    # Do this immediately before the mutation so a missing touch device can
    # never result in a monitor-only transform.
    if query_touch_device(logger, runtime) is not True:
        return None
    if _run_command(
        ["hyprctl", "eval", command],
        timeout=HYPRCTL_TIMEOUT,
        logger=logger,
        error_key="hyprctl-eval",
        action=f"apply transform {transform}",
    ) is None:
        return None
    return verify_monitor_transform(transform, logger, runtime=runtime)


def restart_omarchy_shell(logger: Logger) -> bool:
    """Restart the Omarchy shell without invoking a shell command interpreter."""

    if _run_command(
        ["omarchy", "restart", "shell"],
        timeout=OMARCHY_SHELL_RESTART_TIMEOUT,
        logger=logger,
        error_key="omarchy-shell-restart",
        action="restart Omarchy shell",
    ) is None:
        return False
    logger.debug("Omarchy shell restarted")
    return True


class SubprocessHyprlandClient:
    """Injectable Hyprland operations backed by short-lived CLI processes."""

    def __init__(self, logger: Logger, runtime: RuntimeConfig) -> None:
        self.logger = logger
        self.runtime = runtime

    def query_monitor(self, *, report_errors: bool = True) -> Optional[MonitorStatus]:
        return query_monitor_status(
            self.logger,
            report_errors=report_errors,
            runtime=self.runtime,
        )

    def apply_transform(self, transform: int) -> Optional[MonitorStatus]:
        return apply_eval(transform, self.logger, self.runtime)

    def set_position(self, x: int, y: int) -> bool:
        return apply_monitor_position(x, y, self.logger, self.runtime)

    def restart_shell(self) -> bool:
        return restart_omarchy_shell(self.logger)


def _hyprland_socket_path(filename: str) -> Optional[str]:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if (
        not runtime_dir
        or not os.path.isabs(runtime_dir)
        or not signature
        or os.path.basename(signature) != signature
    ):
        return None
    return os.path.join(runtime_dir, "hypr", signature, filename)


class HyprlandEventReader:
    """Nonblocking reader for monitor-related Hyprland socket2 events."""

    RELEVANT_EVENTS = frozenset(
        {
            b"monitoradded",
            b"monitoraddedv2",
            b"monitorremoved",
            b"monitorremovedv2",
            b"configreloaded",
        }
    )

    def __init__(self, logger: Logger) -> None:
        self.logger = logger
        self.socket: Optional[socket.socket] = None
        self._buffer = bytearray()
        self._next_connect = 0.0

    @property
    def connected(self) -> bool:
        return self.socket is not None

    @property
    def fd(self) -> Optional[int]:
        return self.socket.fileno() if self.socket is not None else None

    def next_deadline(self) -> float:
        return math.inf if self.socket is not None else self._next_connect

    def close(self) -> None:
        current, self.socket = self.socket, None
        if current is not None:
            try:
                current.close()
            except OSError:
                pass
        self._buffer.clear()

    def _lose_connection(self, now: float, reason: str) -> None:
        had_connection = self.socket is not None
        self.close()
        self._next_connect = now + HYPRLAND_EVENT_RETRY_INTERVAL
        if had_connection:
            self.logger.error(
                "hyprland-events",
                f"Hyprland event socket lost ({reason}); using polling fallback",
            )

    def _connect(self, now: float) -> None:
        if self.socket is not None or now < self._next_connect:
            return
        path = _hyprland_socket_path(".socket2.sock")
        if path is None:
            self._next_connect = now + HYPRLAND_EVENT_RETRY_INTERVAL
            return
        candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            candidate.setblocking(False)
            connect_error = candidate.connect_ex(path)
            if connect_error:
                raise OSError(connect_error, os.strerror(connect_error))
        except OSError as exc:
            candidate.close()
            self._next_connect = now + HYPRLAND_EVENT_RETRY_INTERVAL
            self.logger.error(
                "hyprland-events-connect",
                f"cannot connect to Hyprland event socket: {exc}",
            )
            return
        self.socket = candidate
        self._buffer.clear()
        self.logger.debug("Hyprland event socket connected")

    def poll(self, now: float) -> bool:
        self._connect(now)
        current = self.socket
        if current is None:
            return False
        relevant = False
        for _ in range(16):
            try:
                chunk = current.recv(4096)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            except OSError as exc:
                self._lose_connection(now, str(exc))
                return relevant
            if not chunk:
                self._lose_connection(now, "end of stream")
                return relevant
            self._buffer.extend(chunk)
            if len(self._buffer) > HYPRLAND_EVENT_BUFFER_LIMIT:
                self._lose_connection(now, "receive buffer limit exceeded")
                return relevant
            while b"\n" in self._buffer:
                line, _, remainder = self._buffer.partition(b"\n")
                self._buffer = bytearray(remainder)
                if len(line) > HYPRLAND_EVENT_LINE_LIMIT:
                    self._lose_connection(now, "event line limit exceeded")
                    return relevant
                event_name, separator, _data = bytes(line).partition(b">>")
                if separator and event_name in self.RELEVANT_EVENTS:
                    relevant = True
            if len(self._buffer) > HYPRLAND_EVENT_LINE_LIMIT:
                self._lose_connection(now, "event line limit exceeded")
                return relevant
        return relevant


class SwitchReader:
    """Nonblocking evdev reader with ioctl and SYN_DROPPED recovery."""

    def __init__(
        self, logger: Logger, runtime: Optional[RuntimeConfig] = None
    ) -> None:
        self.logger = logger
        self.runtime = runtime or _runtime_from_globals()
        self.fd: Optional[int] = None
        self.device: Optional[SwitchDevice] = None
        self._candidate: Optional[SwitchDevice] = None
        self.state: Optional[bool] = None
        self._buffer = bytearray()
        self._next_open = 0.0
        self._open_retry_delay = DEVICE_RETRY_INTERVAL
        self._next_resync = 0.0
        self._discard_after_drop = False

    def close(self) -> None:
        if self.device is not None:
            self._candidate = self.device
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

    def _cached_configured_candidate(self) -> Optional[SwitchDevice]:
        candidate = self._candidate
        if (
            candidate is None
            or self.runtime.preferred_switch_path == "auto"
            or candidate.path != self.runtime.preferred_switch_path
        ):
            return None
        validated = _validate_switch_path(candidate.path, candidate.name)
        if validated is None:
            return None
        try:
            if SW_TABLET_MODE not in _switch_codes(validated.name_path):
                return None
        except (OSError, UnicodeError, ValueError):
            return None
        return validated

    def _lose_device(self, now: float, reason: str) -> None:
        had_device = self.fd is not None or self.device is not None
        self.close()
        self._open_retry_delay = DEVICE_RETRY_INTERVAL
        self._next_open = now + DEVICE_RETRY_INTERVAL
        if had_device:
            self.logger.error(
                "switch-device",
                f"switch device lost ({reason}); retrying",
            )

    def _schedule_open_retry(self, now: float) -> None:
        self._next_open = now + self._open_retry_delay
        self._open_retry_delay = min(
            self._open_retry_delay * 2.0, MAX_DEVICE_RETRY_INTERVAL
        )

    def _open_if_needed(self, now: float) -> None:
        if self.fd is not None or now < self._next_open:
            return
        cached = self._cached_configured_candidate()
        if cached is not None:
            try:
                fd, state = open_switch_device(cached)
            except Exception as exc:
                self.logger.debug(
                    f"cannot reopen cached switch {cached.path}: {exc}"
                )
            else:
                self.fd = fd
                self.device = cached
                self._candidate = cached
                self.state = state
                self._buffer.clear()
                self._discard_after_drop = False
                self._next_resync = now + SWITCH_RESYNC_INTERVAL
                self._open_retry_delay = DEVICE_RETRY_INTERVAL
                self.logger.info(
                    f"switch {cached.path} reopened "
                    f"({'tablet' if state else 'laptop'})"
                )
                return
        try:
            selection, devices = discover_switch_selection(self.runtime)
            candidates = (
                [devices[selection.selected.path]]
                if selection.selected is not None
                and selection.selected.path in devices
                else []
            )
        except Exception as exc:
            self._schedule_open_retry(now)
            self.logger.error("switch-discovery", f"switch discovery failed: {exc}")
            return
        if not candidates:
            self._schedule_open_retry(now)
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
            self._candidate = candidate
            self.state = state
            self._buffer.clear()
            self._discard_after_drop = False
            self._next_resync = now + SWITCH_RESYNC_INTERVAL
            self._open_retry_delay = DEVICE_RETRY_INTERVAL
            self.logger.info(
                f"switch {candidate.path} opened ({'tablet' if state else 'laptop'})"
            )
            return

        self._schedule_open_retry(now)
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

    def next_deadline(self) -> float:
        """Return the next open or ioctl-resync deadline."""

        return self._next_open if self.fd is None else self._next_resync


class SensorReader:
    """Read IIO on one persistent worker so kernel stalls cannot freeze policy."""

    def __init__(
        self, logger: Logger, runtime: Optional[RuntimeConfig] = None
    ) -> None:
        self.logger = logger
        self.runtime = runtime or _runtime_from_globals()
        self.device: Optional[AccelDevice] = None
        self._next_discovery = 0.0
        self._discovery_retry_delay = DEVICE_RETRY_INTERVAL
        self._generation = 0
        self._worker: Optional[threading.Thread] = None
        self._worker_started = 0.0
        self._read_pending = False
        self._condition = threading.Condition()
        self._request: Optional[tuple[int, Optional[AccelDevice]]] = None
        self._closing = False
        self._results: queue.Queue[
            tuple[int, Optional[AccelReading], Optional[BaseException]]
        ] = queue.Queue(maxsize=1)

    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._worker_main,
            name="tablet-auto-rotate-sensor-read",
            daemon=True,
        )
        self._worker.start()

    def _worker_main(self) -> None:
        session: Optional[AccelSampleSession] = None
        session_device: Optional[AccelDevice] = None
        session_generation: Optional[int] = None
        try:
            while True:
                with self._condition:
                    while self._request is None and not self._closing:
                        self._condition.wait()
                    if self._closing:
                        return
                    generation, device = self._request
                    self._request = None

                if device is None:
                    if session is not None:
                        session.close()
                    session = None
                    session_device = None
                    session_generation = None
                    continue

                reading: Optional[AccelReading] = None
                error: Optional[BaseException] = None
                try:
                    if (
                        session is None
                        or session_device != device
                        or session_generation != generation
                    ):
                        if session is not None:
                            session.close()
                        session = _open_accel_sample_session(device, self.runtime)
                        session_device = device
                        session_generation = generation
                    reading = session.read()
                except BaseException as exc:
                    error = exc
                    if session is not None:
                        session.close()
                    session = None
                    session_device = None
                    session_generation = None
                self._results.put((generation, reading, error))
        finally:
            if session is not None:
                session.close()

    def reset(self) -> None:
        self._generation += 1
        self.device = None
        self._next_discovery = 0.0
        self._discovery_retry_delay = DEVICE_RETRY_INTERVAL
        if self._worker is not None:
            with self._condition:
                queued_read = (
                    self._request is not None and self._request[1] is not None
                )
                self._request = (self._generation, None)
                # If the worker had not taken the request, replacing it with
                # the reset sentinel cancels that read and no result will
                # arrive.  An in-flight read still owns _read_pending until
                # its generation-tagged result can be drained and discarded.
                if queued_read:
                    self._read_pending = False
                self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closing = True
            self._request = None
            self._condition.notify()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=0.1)

    def _start_read(self, now: float) -> None:
        if self.device is None or self._read_pending or self._closing:
            return
        self._ensure_worker()
        self._read_pending = True
        self._worker_started = now
        with self._condition:
            self._request = (self._generation, self.device)
            self._condition.notify()

    def _select_device(self, now: float) -> bool:
        if now < self._next_discovery:
            return False
        device: Optional[AccelDevice] = None
        try:
            device = discover_accel(self.runtime)
        except Exception as exc:
            self.logger.error("sensor-discovery", f"sensor discovery failed: {exc}")
        if device is None:
            self._next_discovery = now + self._discovery_retry_delay
            self._discovery_retry_delay = min(
                self._discovery_retry_delay * 2.0,
                MAX_DEVICE_RETRY_INTERVAL,
            )
            self.logger.error(
                "sensor-missing",
                "display accelerometer not found; retrying",
            )
            return False
        self.device = device
        self._discovery_retry_delay = DEVICE_RETRY_INTERVAL
        self.logger.info(
            f"accelerometer {device.iio_path} selected (hub {device.hid_hub})"
        )
        return True

    def read(self, now: float) -> Optional[AccelReading]:
        if self._read_pending:
            try:
                generation, reading, error = self._results.get_nowait()
            except queue.Empty:
                if now - self._worker_started >= SENSOR_READ_WARNING_SECONDS:
                    self.logger.error(
                        "sensor-read-blocked",
                        "accelerometer kernel read is blocked; switch handling remains active",
                    )
                return None
            self._read_pending = False
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

        if self.device is None and not self._select_device(now):
            return None
        self._start_read(now)
        return None


class RotationDaemon:
    """Coordinate switch state, filtered samples, and safe display/shell updates."""

    def __init__(
        self,
        logger: Logger,
        dry_run: bool = False,
        runtime: Optional[RuntimeConfig] = None,
        hyprland: Optional[Any] = None,
        *,
        switch: Optional[Any] = None,
        sensor: Optional[Any] = None,
        hyprland_events: Any = _DEFAULT_COMPONENT,
        clock: Callable[[], float] = time.monotonic,
        poll_factory: Callable[[], Any] = select.poll,
    ) -> None:
        self.logger = logger
        self.dry_run = dry_run
        self.runtime = runtime or _runtime_from_globals()
        self.stop = threading.Event()
        pipe_flags = os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        self._wake_read_fd, self._wake_write_fd = os.pipe2(pipe_flags)
        self._wake_closed = False
        self.clock = clock
        self.poll_factory = poll_factory
        self.switch = (
            switch if switch is not None else SwitchReader(logger, self.runtime)
        )
        self.sensor = (
            sensor if sensor is not None else SensorReader(logger, self.runtime)
        )
        self.filter = OrientationFilter(self.runtime)
        self.hyprland = (
            hyprland
            if hyprland is not None
            else SubprocessHyprlandClient(logger, self.runtime)
        )
        if hyprland_events is _DEFAULT_COMPONENT:
            self.hyprland_events = None if dry_run else HyprlandEventReader(logger)
        else:
            self.hyprland_events = hyprland_events
        self.tablet_mode: Optional[bool] = None
        self.desired_transform: Optional[int] = None
        self.last_applied_transform: Optional[int] = None
        self.next_sensor_sample = 0.0
        # Reconcile once immediately, then every MONITOR_CHECK_INTERVAL.
        self.next_monitor_check = 0.0
        self.next_apply_retry = 0.0
        self.apply_retry_delay = INITIAL_APPLY_RETRY
        self._desired_generation = 0
        self._layer_remap: Optional[LayerRemapOperation] = None

    def request_stop(self) -> None:
        self.stop.set()
        if self._wake_closed:
            return
        try:
            os.write(self._wake_write_fd, b"\0")
        except BlockingIOError:
            pass
        except OSError:
            if not self._wake_closed:
                raise

    def _close_wake_pipe(self) -> None:
        if self._wake_closed:
            return
        self._wake_closed = True
        for fd in (self._wake_read_fd, self._wake_write_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def _drain_wake_pipe(self) -> None:
        while True:
            try:
                if not os.read(self._wake_read_fd, 4096):
                    return
            except BlockingIOError:
                return
            except InterruptedError:
                continue

    def _next_deadline(self) -> float:
        deadlines = [self.switch.next_deadline()]
        if self.hyprland_events is not None:
            deadlines.append(self.hyprland_events.next_deadline())
        if self.tablet_mode is True:
            deadlines.append(self.next_sensor_sample)
        if self.desired_transform is not None:
            if math.isfinite(self.next_apply_retry):
                deadlines.append(self.next_apply_retry)
            if not self.dry_run:
                deadlines.append(self.next_monitor_check)
        if self._layer_remap is not None:
            deadlines.append(self._layer_remap.deadline)
        return min(deadlines)

    def _wait_for_work(self) -> None:
        if self.stop.is_set():
            return
        poller = self.poll_factory()
        poller.register(self._wake_read_fd, select.POLLIN)
        switch_fd = self.switch.fd
        if switch_fd is not None:
            poller.register(
                switch_fd,
                select.POLLIN | select.POLLERR | select.POLLHUP | select.POLLNVAL,
            )
        event_fd = (
            self.hyprland_events.fd
            if self.hyprland_events is not None
            else None
        )
        if event_fd is not None:
            poller.register(
                event_fd,
                select.POLLIN | select.POLLERR | select.POLLHUP | select.POLLNVAL,
            )
        delay = max(0.0, self._next_deadline() - self.clock())
        timeout_ms = max(1, math.ceil(delay * 1000.0))
        try:
            events = poller.poll(timeout_ms)
        except InterruptedError:
            return
        if any(fd == self._wake_read_fd for fd, _event in events):
            self._drain_wake_pipe()

    def _schedule_apply_retry(self, now: float) -> None:
        self.next_apply_retry = now + self.apply_retry_delay
        self.apply_retry_delay = min(self.apply_retry_delay * 2.0, MAX_APPLY_RETRY)

    def _finish_layer_remap(self, success: bool) -> None:
        self._layer_remap = None
        if success:
            return
        self.logger.error(
            "layer-remap-fallback",
            "fast layer remap failed; falling back to Omarchy shell restart",
        )
        self.hyprland.restart_shell()

    def _start_layer_remap(
        self, transform: int, status: MonitorStatus, now: float
    ) -> None:
        self._cancel_layer_remap()
        operation = LayerRemapOperation(
            transform,
            self._desired_generation,
            status,
            self.hyprland,
            self.logger,
            self.runtime,
        )
        result = operation.start(now)
        if result is None:
            self._layer_remap = operation
        else:
            self._finish_layer_remap(result)

    def _advance_layer_remap(self, now: float) -> None:
        operation = self._layer_remap
        if operation is None:
            return
        if operation.generation != self._desired_generation:
            self._cancel_layer_remap()
            return
        result = operation.advance(now)
        if result is not None:
            self._finish_layer_remap(result)

    def _cancel_layer_remap(self) -> None:
        operation, self._layer_remap = self._layer_remap, None
        if operation is None:
            return
        if not operation.cancel():
            self.logger.error(
                "layer-remap-cancel",
                f"monitor {self.runtime.output} position could not be restored while cancelling remap",
            )

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
            status = self.hyprland.query_monitor()
        if status is None or not status.found:
            self._schedule_apply_retry(now)
            return
        if not status.enabled:
            self.logger.error(
                "monitor-disabled",
                f"monitor {self.runtime.output} is disabled; waiting before applying transform",
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
        self._cancel_layer_remap()
        confirmed_status = self.hyprland.apply_transform(transform)
        if confirmed_status is not None:
            # Record the transform before the geometry refresh so a fallback
            # failure cannot cause the monitor update to be retried.
            self.last_applied_transform = transform
            self.next_apply_retry = math.inf
            self.apply_retry_delay = INITIAL_APPLY_RETRY
            if (
                should_refresh_geometry(previous_status, transform)
                and self.runtime.desktop_integration == "omarchy"
            ):
                self._start_layer_remap(transform, confirmed_status, now)
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
        self._desired_generation += 1
        self._cancel_layer_remap()
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
                self._desired_generation += 1
                self._cancel_layer_remap()
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
            self._desired_generation += 1
            self._cancel_layer_remap()
            self.desired_transform = None
            self.logger.info("tablet mode on; waiting for stable orientation")
        else:
            self.logger.info("tablet mode off; forcing transform 0")
            self._set_desired(0, now, "tablet mode off")

    def _sample_sensor(self, now: float) -> None:
        reading = self.sensor.read(now)
        if reading is None:
            return
        transform = self.filter.update(
            map_sensor_values(reading.values, self.runtime), now
        )
        if transform is not None:
            self._set_desired(transform, now, "stable orientation")

    def _reconcile_monitor(self, now: float) -> None:
        if self.dry_run or self.desired_transform is None:
            return
        status = self.hyprland.query_monitor()
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

    def _monitor_check_interval(self) -> float:
        if self.hyprland_events is not None and self.hyprland_events.connected:
            return EVENT_MONITOR_CHECK_INTERVAL
        return MONITOR_CHECK_INTERVAL

    def _process_once(self, now: float) -> None:
        self.switch.poll(now)
        self._handle_switch_state(now)
        if self.hyprland_events is not None and self.hyprland_events.poll(now):
            # A config reload may preserve the monitor transform while losing
            # the touch mapping.  Forget the prior paired apply so the fresh
            # reconciliation re-establishes both in one guarded mutation.
            self.last_applied_transform = None
            self.next_monitor_check = now
        self._advance_layer_remap(now)
        if self.tablet_mode is True and now >= self.next_sensor_sample:
            self._sample_sensor(now)
            self.next_sensor_sample = now + LOOP_INTERVAL
        if self.desired_transform is not None and now >= self.next_apply_retry:
            self._apply_if_needed(now, "retry")
        if now >= self.next_monitor_check:
            self._reconcile_monitor(now)
            self.next_monitor_check = now + self._monitor_check_interval()

    def run(self) -> int:
        self.logger.info("started" + (" (dry-run)" if self.dry_run else ""))
        try:
            while not self.stop.is_set():
                now = self.clock()
                try:
                    self._process_once(now)
                except Exception as exc:  # Keep transient sysfs/IPC failures nonfatal.
                    self.logger.error("loop", f"temporary loop failure: {exc}")
                self._wait_for_work()
        except KeyboardInterrupt:
            self.request_stop()
        finally:
            self._cancel_layer_remap()
            self.switch.close()
            self.sensor.close()
            if self.hyprland_events is not None:
                self.hyprland_events.close()
            self._close_wake_pipe()
        self.logger.info("stopped")
        return 0


def _runtime_lock_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        runtime_dir = f"/tmp/{SCRIPT_NAME}-{os.getuid()}"
    return os.path.join(runtime_dir, f"{SCRIPT_NAME}.lock")


def apply_config(config: HardwareConfig) -> None:
    """Update legacy facade defaults; the live daemon uses ``RuntimeConfig``."""

    global OUTPUT_NAME, TOUCH_DEVICE_NAME, SWITCH_NAME, PREFERRED_SWITCH_PATH
    global DESKTOP_INTEGRATION, AXIS_ORDER, AXIS_SIGNS, ORIENTATION_TRANSFORMS
    global MOUNT_MATRIX_MODE
    OUTPUT_NAME = config.output
    TOUCH_DEVICE_NAME = config.touch_device
    SWITCH_NAME = config.switch_name
    PREFERRED_SWITCH_PATH = config.preferred_switch_path
    DESKTOP_INTEGRATION = config.desktop_integration
    axis_indexes = {"x": 0, "y": 1, "z": 2}
    AXIS_ORDER = tuple(axis_indexes[axis] for axis in config.axis_order)
    AXIS_SIGNS = config.axis_signs
    ORIENTATION_TRANSFORMS = config.orientation_transforms
    MOUNT_MATRIX_MODE = config.mount_matrix


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


def _configuration_report(
    config: HardwareConfig, config_path: Optional[str]
) -> dict[str, Any]:
    source = sanitize_report_path(config_path) if config_path else "defaults"
    return {
        "axis_order": list(config.axis_order),
        "axis_signs": list(config.axis_signs),
        "desktop_integration": sanitize_report_name(config.desktop_integration),
        "orientation_transforms": list(config.orientation_transforms),
        "mount_matrix": config.mount_matrix,
        "output": sanitize_report_name(config.output),
        "preferred_switch_path": sanitize_report_path(config.preferred_switch_path),
        "source": source,
        "switch_name": sanitize_report_name(config.switch_name),
        "touch_device": sanitize_report_name(config.touch_device),
    }


def _report_envelope(
    report_type: str, config: HardwareConfig, config_path: Optional[str]
) -> dict[str, Any]:
    return {
        "application": {"name": SCRIPT_NAME, "version": application_version()},
        "configuration": _configuration_report(config, config_path),
        "report_type": report_type,
        "schema_version": REPORT_SCHEMA_VERSION,
    }


def _sanitize_error(exc: BaseException) -> str:
    return sanitize_report_name(str(exc), limit=240)


def _sanitize_hid_hub(hid_hub: str) -> str:
    clean = sanitize_report_name(hid_hub)
    return re.sub(r"\.[0-9A-Fa-f]+$", ".<instance>", clean)


def _write_json_report(report: dict[str, Any]) -> None:
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()


def collect_probe_report(
    config: HardwareConfig,
    config_path: Optional[str],
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    """Collect a sanitized, versioned hardware report without writing stdout."""

    logger = Logger(verbose)
    runtime = RuntimeConfig.from_hardware(config)
    report = _report_envelope("probe", config, config_path)
    errors: list[dict[str, str]] = []
    try:
        selection, switch_devices = discover_switch_selection(runtime)
        candidates = (
            [switch_devices[selection.selected.path]]
            if selection.selected is not None
            and selection.selected.path in switch_devices
            else []
        )
    except Exception as exc:
        selection = None
        candidates = []
        errors.append({"component": "switch_discovery", "message": _sanitize_error(exc)})
        logger.error("probe-switch-discovery", f"switch discovery failed: {exc}", interval=0.0)
    selection_report: dict[str, Any]
    if selection is not None:
        relevant_candidates = [
            candidate
            for candidate in selection.candidates
            if verbose or candidate.capable or candidate.rank != 0
        ]
        selection_report = {
            "assessed_candidate_count": len(selection.candidates),
            "candidates": [
                {
                    "capable": candidate.capable,
                    "name": candidate.name,
                    "path": candidate.path,
                    "rank": candidate.rank,
                    "reasons": list(candidate.reasons),
                }
                for candidate in relevant_candidates
            ],
            "status": selection.status,
            "summary": sanitize_report_name(selection.summary),
        }
    else:
        selection_report = {
            "assessed_candidate_count": 0,
            "candidates": [],
            "status": "error",
            "summary": "switch discovery failed",
        }
    switch_ok = False
    selected_switch_report: Optional[dict[str, Any]] = None
    fd: Optional[int] = None
    for candidate in candidates:
        try:
            fd, state = open_switch_device(candidate)
            switch_ok = True
            selected_switch_report = {
                "name": sanitize_report_name(_read_text(candidate.name_path)),
                "path": sanitize_report_path(candidate.path),
                "state": "tablet" if state else "laptop",
                "sysfs_name_path": sanitize_report_path(candidate.name_path),
            }
            break
        except Exception as exc:
            errors.append({"component": "switch_read", "message": _sanitize_error(exc)})
            logger.error("probe-switch", f"cannot read {candidate.path}: {exc}", interval=0.0)
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass

    sensor_ok = False
    try:
        sensor = discover_accel(runtime)
    except Exception as exc:
        sensor = None
        errors.append({"component": "sensor_discovery", "message": _sanitize_error(exc)})
        logger.error("probe-sensor", f"sensor discovery failed: {exc}", interval=0.0)
    sensor_report: Optional[dict[str, Any]] = None
    reading_report: Optional[dict[str, Any]] = None
    if sensor is None:
        if not any(error["component"] == "sensor_discovery" for error in errors):
            errors.append({"component": "sensor_discovery", "message": "no unique readable accel_3d"})
    else:
        sensor_report = {
            "hid_hub": _sanitize_hid_hub(sensor.hid_hub),
            "hinge_path": sanitize_report_path(sensor.hinge_path) if sensor.hinge_path else None,
            "iio_path": sanitize_report_path(sensor.iio_path),
            "mount_matrix": {
                "error": sanitize_report_name(sensor.mount_matrix_error, limit=240)
                if sensor.mount_matrix_error else None,
                "path": sanitize_report_path(sensor.mount_matrix_path)
                if sensor.mount_matrix_path else None,
                "value": [list(row) for row in sensor.mount_matrix]
                if sensor.mount_matrix is not None else None,
            },
            "raw_paths": [sanitize_report_path(path) for path in sensor.raw_paths],
            "scale_paths": [sanitize_report_path(path) for path in sensor.scale_paths],
        }
        try:
            reading = read_accel(sensor, runtime)
        except Exception as exc:
            errors.append({"component": "sensor_read", "message": _sanitize_error(exc)})
            logger.error("probe-sensor-read", f"cannot read sensor: {exc}", interval=0.0)
        else:
            sensor_ok = True
            reading_report = {
                "raw": list(reading.raw),
                "scale": list(reading.scale),
                "world": list(reading.values),
            }

    report.update({
        "errors": errors,
        "ok": switch_ok and sensor_ok,
        "sensor": {"reading": reading_report, "selected": sensor_report},
        "switch": {"selected": selected_switch_report, "selection": selection_report},
    })
    return report


def _render_probe_human(report: dict[str, Any], *, verbose: bool) -> None:
    switch = report["switch"]
    selection = switch["selection"]
    print(f"switch_selection: {selection['status']}: {selection['summary']}")
    for candidate in selection["candidates"]:
        if not verbose and not candidate["capable"] and candidate["rank"] == 0:
            continue
        print(
            "switch_candidate: "
            f"{candidate['path']} name={candidate['name']!r} capable={candidate['capable']} "
            f"rank={candidate['rank']} reasons={'; '.join(candidate['reasons'])}"
        )
    selected_switch = switch["selected"]
    print(
        "switch_candidates: "
        + (selected_switch["path"] if selected_switch is not None else "unavailable")
    )
    if selected_switch is None:
        print("switch_state: unavailable")
    else:
        print(f"switch_path: {selected_switch['path']}")
        print(f"switch_sysfs: {selected_switch['sysfs_name_path']}")
        print(f"switch_name: {selected_switch['name']}")
        print(f"switch_state: {selected_switch['state']}")
    sensor = report["sensor"]
    selected_sensor = sensor["selected"]
    if selected_sensor is None:
        print("sensor_iio: unavailable")
        return
    print(f"sensor_hinge_iio: {selected_sensor['hinge_path'] or 'none'}")
    print(f"sensor_iio: {selected_sensor['iio_path']}")
    print(f"sensor_hid_hub: {selected_sensor['hid_hub']}")
    matrix = selected_sensor["mount_matrix"]
    print(f"sensor_mount_matrix_path: {matrix['path'] or 'none'}")
    print(
        "sensor_mount_matrix: "
        f"{matrix['value'] if matrix['value'] is not None else 'unavailable'}"
    )
    if matrix["error"] is not None:
        print(f"sensor_mount_matrix_error: {matrix['error']}")
    print(f"sensor_raw_paths: {_format_values(selected_sensor['raw_paths'])}")
    print(f"sensor_scale_paths: {_format_values(selected_sensor['scale_paths'])}")
    reading = sensor["reading"]
    if reading is None:
        print("sensor_raw: unavailable")
        return
    print(f"sensor_raw: {_format_values(reading['raw'])}")
    print(f"sensor_scale: {_format_values(reading['scale'])}")
    print(f"sensor_world: {_format_values(f'{value:.6g}' for value in reading['world'])}")


def run_probe(
    verbose: bool = False,
    *,
    config: Optional[HardwareConfig] = None,
    config_path: Optional[str] = None,
    json_output: bool = False,
) -> int:
    """Print discovery and current read-only state without running rotation."""

    report = collect_probe_report(config or HardwareConfig(), config_path, verbose=verbose)
    if json_output:
        _write_json_report(report)
    else:
        _render_probe_human(report, verbose=verbose)

    return 0 if report["ok"] else 1


def _assert_self_test(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_self_test() -> int:
    """Run a small installed smoke test; detailed cases live in pytest."""

    try:
        _assert_self_test(INPUT_EVENT_SIZE == 24, "64-bit input_event size is not 24")
        packed = INPUT_EVENT.pack(1, 2, EV_SW, SW_TABLET_MODE, 1)
        _assert_self_test(
            INPUT_EVENT.unpack(packed)[2:] == (EV_SW, SW_TABLET_MODE, 1),
            "input_event unpack",
        )
        expected_ioctl = (
            (IOC_READ << IOC_DIRSHIFT)
            | (SW_MASK_BYTES << IOC_SIZESHIFT)
            | (ord("E") << IOC_TYPESHIFT)
            | 0x1B
        )
        _assert_self_test(
            EVIOCGSW_REQUEST == expected_ioctl, "EVIOCGSW request construction"
        )

        for values, expected in (
            ((0.0, GRAVITY, 0.0), 2),
            ((GRAVITY, 0.0, 0.0), 1),
            ((0.0, -GRAVITY, 0.0), 0),
            ((-GRAVITY, 0.0, 0.0), 3),
        ):
            _assert_self_test(
                classify_orientation(values) == expected,
                f"orientation {values}",
            )
        _assert_self_test(
            classify_orientation((0.0, 0.0, GRAVITY)) is None,
            "flat vector rejection",
        )

        orientation_filter = OrientationFilter()
        upright = (0.0, GRAVITY, 0.0)
        for now in (0.0, 0.10, 0.20):
            _assert_self_test(
                orientation_filter.update(upright, now) is None,
                "candidate accepted too early",
            )
        _assert_self_test(
            orientation_filter.update(upright, 0.36) == 2,
            "candidate hold",
        )

        _assert_self_test(
            build_eval_command(2)
            == 'hl.monitor({ output = "eDP-1", transform = 2 }); '
            'hl.device({ name = "elan9004:00-04f3:4110", output = "eDP-1", '
            'transform = 2 }); hl.dispatch(hl.dsp.force_renderer_reload())',
            "Hyprland command construction",
        )
        _assert_self_test(
            build_position_eval_command(-1920, 37)
            == 'hl.monitor({ output = "eDP-1", position = "-1920x37" }); '
            'hl.dispatch(hl.dsp.force_renderer_reload())',
            "monitor position command construction",
        )
        _assert_self_test(
            has_touch_device({"touch": [{"name": TOUCH_DEVICE_NAME}]}),
            "touch device parsing",
        )
        _assert_self_test(
            parse_monitor_status(
                [{"name": OUTPUT_NAME, "disabled": False, "transform": 3}]
            )
            == MonitorStatus(True, True, 3, active_monitor_count=1),
            "monitor parsing",
        )
        _assert_self_test(
            find_hid_hub_ancestor(
                "/sys/x/001F:8087:0AC2.0003/HID-SENSOR/iio:device0"
            )
            == "001F:8087:0AC2.0003",
            "HID hub ancestry",
        )
    except AssertionError as exc:
        print(f"{SCRIPT_NAME}: self-test failed: {exc}", file=sys.stderr)
        return 1

    print(f"{SCRIPT_NAME}: self-test ok")
    return 0


def collect_doctor_report(
    config: HardwareConfig, config_path: Optional[str]
) -> dict[str, Any]:
    """Collect read-only compatibility checks in a stable report schema."""

    runtime = RuntimeConfig.from_hardware(config)
    checks: list[tuple[str, str, bool, str]] = []
    checks.append((
        "input_abi",
        "input ABI",
        INPUT_EVENT_SIZE == 24,
        f"input_event size is {INPUT_EVENT_SIZE} bytes (24 required)",
    ))
    hyprctl_found = shutil.which("hyprctl") is not None
    checks.append((
        "hyprctl",
        "hyprctl",
        hyprctl_found,
        "found on PATH" if hyprctl_found else "not found on PATH",
    ))
    if config.desktop_integration == "omarchy":
        omarchy_found = shutil.which("omarchy") is not None
        checks.append((
            "omarchy",
            "Omarchy",
            omarchy_found,
            "found on PATH" if omarchy_found else "not found on PATH",
        ))
    checks.append((
        "configuration",
        "configuration",
        True,
        sanitize_report_path(config_path)
        if config_path
        else f"defaults (expected user path: {sanitize_report_path(str(default_config_path()))})",
    ))
    checks.append(("target_output", "target output", bool(config.output), sanitize_report_name(config.output)))
    checks.append(("touch_device", "touch device", bool(config.touch_device), sanitize_report_name(config.touch_device)))
    checks.append(("tablet_switch", "tablet switch", bool(config.switch_name), sanitize_report_name(config.switch_name)))

    try:
        selection, _ = discover_switch_selection(runtime)
    except Exception as exc:
        checks.append(("switch_discovery", "switch discovery", False, f"failed: {_sanitize_error(exc)}"))
    else:
        checks.append((
            "switch_discovery",
            "switch discovery",
            selection.status == "selected",
            sanitize_report_name(f"{selection.status}: {selection.summary}"),
        ))
    try:
        sensor = discover_accel(runtime)
    except Exception as exc:
        checks.append(("accelerometer_discovery", "accelerometer discovery", False, f"failed: {_sanitize_error(exc)}"))
    else:
        checks.append((
            "accelerometer_discovery",
            "accelerometer discovery",
            sensor is not None,
            sanitize_report_path(sensor.iio_path) if sensor is not None else "no unique readable accel_3d",
        ))

    if hyprctl_found:
        logger = Logger(False)
        monitor = query_monitor_status(
            logger, report_errors=False, runtime=runtime
        )
        checks.append((
            "hyprland_output",
            "Hyprland output",
            monitor is not None and monitor.found and monitor.enabled,
            (
                f"{config.output} is enabled"
                if monitor is not None and monitor.found and monitor.enabled
                else f"{config.output} is not available and enabled"
            ),
        ))
        touch = query_touch_device(logger, runtime)
        checks.append((
            "hyprland_touch_device",
            "Hyprland touch device",
            touch is True,
            f"{config.touch_device} {'found' if touch is True else 'not confirmed'}",
        ))

    report = _report_envelope("doctor", config, config_path)
    report_checks = [
        {
            "detail": sanitize_report_name(detail, limit=240),
            "id": check_id,
            "name": name,
            "status": "ok" if passed else "fail",
        }
        for check_id, name, passed, detail in checks
    ]
    report.update({
        "checks": report_checks,
        "ok": all(check["status"] == "ok" for check in report_checks),
    })
    return report


def run_doctor(
    config: HardwareConfig,
    config_path: Optional[str],
    *,
    json_output: bool = False,
) -> int:
    """Run read-only compatibility checks and print actionable results."""

    report = collect_doctor_report(config, config_path)
    if json_output:
        _write_json_report(report)
    else:
        for check in report["checks"]:
            status = "ok" if check["status"] == "ok" else "FAIL"
            print(f"{status}: {check['name']}: {check['detail']}")
    return 0 if report["ok"] else 1


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
    runtime = RuntimeConfig.from_hardware(config)
    sensor = discover_accel(runtime)
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
                readings.append(read_accel(sensor, runtime).values)
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


def _install_signal_handlers(request_stop: Callable[[], None]) -> None:
    def handle_signal(_signum: int, _frame: Any) -> None:
        request_stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    from .cli import parse_args as cli_parse_args

    return cli_parse_args(argv)


def run_args(args: argparse.Namespace) -> int:
    """Dispatch already-parsed CLI arguments."""

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
    runtime = RuntimeConfig.from_hardware(config)
    if args.calibrate_from:
        return run_calibration_file(Path(args.calibrate_from), config)
    if args.calibrate:
        return run_interactive_calibration(config)
    if args.doctor:
        return run_doctor(config, args.config, json_output=args.json_output)
    if args.probe:
        return run_probe(
            args.verbose,
            config=config,
            config_path=args.config,
            json_output=args.json_output,
        )

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

    daemon = RotationDaemon(logger, dry_run=args.dry_run, runtime=runtime)
    _install_signal_handlers(daemon.request_stop)
    try:
        return daemon.run()
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run_args(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
