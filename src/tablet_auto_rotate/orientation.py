"""Pure accelerometer mapping, orientation classification, and filtering."""

from __future__ import annotations

import math
from typing import Optional, Sequence

from .config import HardwareConfig, RuntimeConfig


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

_DEFAULT_RUNTIME = RuntimeConfig.from_hardware(HardwareConfig())


def _vector_magnitude(values: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def classify_orientation(
    values: Sequence[float], runtime: Optional[RuntimeConfig] = None
) -> Optional[int]:
    """Classify a stable, cardinal, in-plane gravity vector."""

    runtime = runtime or _DEFAULT_RUNTIME
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        return None
    x, y, _z = values
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
        return (
            runtime.orientation_transforms[1]
            if y > 0.0
            else runtime.orientation_transforms[3]
        )
    return (
        runtime.orientation_transforms[0]
        if x > 0.0
        else runtime.orientation_transforms[2]
    )


def map_sensor_values(
    values: Sequence[float], runtime: Optional[RuntimeConfig] = None
) -> tuple[float, float, float]:
    """Map physical IIO axes into the configured logical screen axes."""

    runtime = runtime or _DEFAULT_RUNTIME
    if len(values) != 3:
        raise ValueError("accelerometer sample must contain three axes")
    return tuple(
        values[index] * sign
        for index, sign in zip(runtime.axis_order, runtime.axis_signs)
    )  # type: ignore[return-value]


def _unit_vector(
    values: Sequence[float], magnitude: float
) -> tuple[float, float, float]:
    return (values[0] / magnitude, values[1] / magnitude, values[2] / magnitude)


def _stable_sample(
    reference: tuple[float, float, float],
    current: tuple[float, float, float],
    reference_magnitude: float,
    current_magnitude: float,
) -> bool:
    dot = sum(a * b for a, b in zip(reference, current))
    relative_magnitude_change = (
        abs(current_magnitude - reference_magnitude) / reference_magnitude
        if reference_magnitude > 0.0
        else math.inf
    )
    return (
        dot >= STABILITY_DOT
        and relative_magnitude_change <= STABILITY_MAGNITUDE_RATIO
    )


class OrientationFilter:
    """Reject movement/diagonals and hold a candidate before accepting it."""

    def __init__(self, runtime: Optional[RuntimeConfig] = None) -> None:
        self.runtime = runtime or _DEFAULT_RUNTIME
        self.reset()

    def reset(self) -> None:
        self.candidate: Optional[int] = None
        self.candidate_since: Optional[float] = None
        self.reference_unit: Optional[tuple[float, float, float]] = None
        self.reference_magnitude: Optional[float] = None
        self.last_unit: Optional[tuple[float, float, float]] = None
        self.last_magnitude: Optional[float] = None
        self.last_sample_time: Optional[float] = None
        self.accepted = False

    def _begin_candidate(
        self,
        orientation: int,
        now: float,
        unit: tuple[float, float, float],
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
        """Return a transform only once a candidate has held for about 350 ms."""

        orientation = classify_orientation(values, self.runtime)
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
