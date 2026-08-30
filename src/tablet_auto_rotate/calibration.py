"""Pure, side-effect-free foundations for guided sensor calibration.

Calibration labels describe the logical direction in which gravity should point.
Only the X/Y mapping used for screen rotation is observable from the four edge
positions.  The unused Z sign is therefore deliberately canonicalized to +1.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
import tomllib
from typing import Iterable, Mapping, Sequence

from .config import HardwareConfig


LABELS = ("+x", "+y", "-x", "-y")
_AXES = ("x", "y", "z")


class CalibrationError(ValueError):
    """Raised when samples cannot produce a safe, unique calibration."""


@dataclass(frozen=True)
class CalibrationResult:
    axis_order: tuple[str, str, str]
    axis_signs: tuple[int, int, int]

    def __post_init__(self) -> None:
        if set(self.axis_order) != set(_AXES) or len(self.axis_order) != 3:
            raise ValueError("axis_order must be a permutation of x, y, z")
        if len(self.axis_signs) != 3 or any(
            sign not in (-1, 1) for sign in self.axis_signs
        ):
            raise ValueError("axis_signs must contain three values of -1 or 1")


def _unit(sample: Sequence[float]) -> tuple[float, float, float]:
    if len(sample) != 3 or any(not math.isfinite(value) for value in sample):
        raise CalibrationError("each sample must contain three finite values")
    magnitude = math.sqrt(sum(value * value for value in sample))
    if magnitude <= 1e-9:
        raise CalibrationError("zero-magnitude sensor sample")
    return tuple(value / magnitude for value in sample)  # type: ignore[return-value]


def classify_stable_samples(
    samples: Iterable[Sequence[float]],
    *,
    min_samples: int = 5,
    min_resultant: float = 0.97,
) -> tuple[float, float, float]:
    """Return the mean unit direction, rejecting sparse or noisy samples."""

    if min_samples < 1 or not 0.0 < min_resultant <= 1.0:
        raise ValueError("invalid stability thresholds")
    units = [_unit(sample) for sample in samples]
    if len(units) < min_samples:
        raise CalibrationError(f"need at least {min_samples} samples per position")
    mean = tuple(sum(sample[i] for sample in units) / len(units) for i in range(3))
    resultant = math.sqrt(sum(value * value for value in mean))
    if resultant < min_resultant:
        raise CalibrationError("samples are too noisy or changed direction")
    return tuple(value / resultant for value in mean)  # type: ignore[return-value]


def infer_axis_mapping(
    labeled_samples: Mapping[str, Iterable[Sequence[float]]],
    *,
    min_samples: int = 5,
    min_resultant: float = 0.97,
    min_dominance: float = 0.80,
    max_opposite_error: float = 0.20,
    max_orthogonal_dot: float = 0.25,
) -> CalibrationResult:
    """Infer physical axis order/signs from four stable edge positions.

    ``labeled_samples`` must contain exactly ``+x``, ``+y``, ``-x``, and
    ``-y``.  Ambiguous diagonal, non-opposite, or non-orthogonal readings are
    refused.  Z is selected by elimination and its unobservable sign is +1.
    """

    if set(labeled_samples) != set(LABELS):
        raise CalibrationError(f"samples must contain exactly: {', '.join(LABELS)}")
    directions = {
        label: classify_stable_samples(
            labeled_samples[label], min_samples=min_samples, min_resultant=min_resultant
        )
        for label in LABELS
    }

    def dominant(label: str) -> tuple[int, int]:
        vector = directions[label]
        index = max(range(3), key=lambda i: abs(vector[i]))
        ordered = sorted((abs(value) for value in vector), reverse=True)
        if ordered[0] < min_dominance or ordered[0] - ordered[1] < 0.35:
            raise CalibrationError(f"{label} is diagonal or has no dominant axis")
        return index, 1 if vector[index] > 0 else -1

    plus_x_axis, plus_x_sign = dominant("+x")
    minus_x_axis, minus_x_sign = dominant("-x")
    plus_y_axis, plus_y_sign = dominant("+y")
    minus_y_axis, minus_y_sign = dominant("-y")
    if plus_x_axis != minus_x_axis or plus_x_sign != -minus_x_sign:
        raise CalibrationError("+x and -x do not identify opposite directions")
    if plus_y_axis != minus_y_axis or plus_y_sign != -minus_y_sign:
        raise CalibrationError("+y and -y do not identify opposite directions")
    if plus_x_axis == plus_y_axis:
        raise CalibrationError("logical x and y map to the same physical axis")

    def dot(left: str, right: str) -> float:
        return sum(a * b for a, b in zip(directions[left], directions[right]))

    if dot("+x", "-x") > -1.0 + max_opposite_error:
        raise CalibrationError("x samples are not sufficiently opposite")
    if dot("+y", "-y") > -1.0 + max_opposite_error:
        raise CalibrationError("y samples are not sufficiently opposite")
    if abs(dot("+x", "+y")) > max_orthogonal_dot:
        raise CalibrationError("x and y samples are not sufficiently orthogonal")

    z_axis = ({0, 1, 2} - {plus_x_axis, plus_y_axis}).pop()
    return CalibrationResult(
        axis_order=(_AXES[plus_x_axis], _AXES[plus_y_axis], _AXES[z_axis]),
        axis_signs=(plus_x_sign, plus_y_sign, 1),
    )


def generate_config_toml(hardware: HardwareConfig, result: CalibrationResult) -> str:
    """Render a complete validated TOML configuration without writing it."""

    calibrated = replace(
        hardware, axis_order=result.axis_order, axis_signs=result.axis_signs
    )

    def quoted(value: str) -> str:
        # JSON strings are valid TOML basic strings and handle control characters.
        return json.dumps(value, ensure_ascii=False)

    order = ", ".join(quoted(value) for value in calibrated.axis_order)
    signs = ", ".join(str(value) for value in calibrated.axis_signs)
    transforms = ", ".join(str(value) for value in calibrated.orientation_transforms)
    text = (
        "[hardware]\n"
        f"output = {quoted(calibrated.output)}\n"
        f"touch_device = {quoted(calibrated.touch_device)}\n"
        f"switch_name = {quoted(calibrated.switch_name)}\n"
        f"preferred_switch_path = {quoted(calibrated.preferred_switch_path)}\n"
        f"desktop_integration = {quoted(calibrated.desktop_integration)}\n\n"
        "[sensor]\n"
        f"axis_order = [{order}]\n"
        f"axis_signs = [{signs}]\n"
        f"orientation_transforms = [{transforms}]\n"
    )
    # Guard against accidental generation of malformed or structurally incomplete TOML.
    parsed = tomllib.loads(text)
    if set(parsed) != {"hardware", "sensor"}:
        raise AssertionError("generated an invalid configuration structure")
    return text
