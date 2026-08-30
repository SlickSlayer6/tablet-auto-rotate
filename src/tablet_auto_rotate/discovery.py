"""Pure, conservative hardware candidate selection.

This module deliberately does not enumerate devices.  Callers translate their
evdev/sysfs observations into :class:`SwitchCandidate` records, then pass those
records to :func:`select_tablet_switch`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Iterable


SW_TABLET_MODE = 0x01


@dataclass(frozen=True, slots=True)
class SwitchCandidate:
    """The small portion of an input device record needed for selection."""

    path: str
    name: str
    switch_codes: frozenset[int]

    @classmethod
    def from_codes(
        cls, path: str, name: str, switch_codes: Iterable[int]
    ) -> "SwitchCandidate":
        return cls(path=path, name=name, switch_codes=frozenset(switch_codes))


@dataclass(frozen=True, slots=True)
class CandidateReason:
    path: str
    name: str
    capable: bool
    rank: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SwitchSelection:
    """Selection result suitable for both machine use and diagnostic output."""

    status: str
    selected: SwitchCandidate | None
    summary: str
    candidates: tuple[CandidateReason, ...]


def select_tablet_switch(
    candidates: Iterable[SwitchCandidate],
    *,
    configured_path: str | None = None,
    configured_name: str | None = None,
) -> SwitchSelection:
    """Select one tablet switch, refusing ties and incapable configured devices.

    An exact configured path is strongest, followed by an exact configured
    name.  ``SW_TABLET_MODE`` support is always required.  When no preference
    is configured, discovery succeeds only if exactly one capable device exists.
    """

    records = tuple(candidates)
    assessments: list[tuple[SwitchCandidate, CandidateReason]] = []
    for candidate in records:
        capable = SW_TABLET_MODE in candidate.switch_codes
        path_match = configured_path is not None and candidate.path == configured_path
        name_match = configured_name is not None and candidate.name == configured_name
        rank = (100 if path_match else 0) + (10 if name_match else 0)
        reasons: list[str] = []
        reasons.append(
            "advertises SW_TABLET_MODE" if capable else "does not advertise SW_TABLET_MODE"
        )
        if path_match:
            reasons.append("exact configured path match")
        if name_match:
            reasons.append("exact configured name match")
        assessments.append(
            (
                candidate,
                CandidateReason(
                    path=sanitize_report_path(candidate.path),
                    name=sanitize_report_name(candidate.name),
                    capable=capable,
                    rank=rank,
                    reasons=tuple(reasons),
                ),
            )
        )

    public = tuple(reason for _, reason in assessments)
    capable = [(candidate, reason) for candidate, reason in assessments if reason.capable]

    if configured_path is not None:
        exact = [(candidate, reason) for candidate, reason in assessments if candidate.path == configured_path]
        if exact and not exact[0][1].capable:
            return SwitchSelection(
                "unavailable", None, "configured switch lacks SW_TABLET_MODE", public
            )
        if not exact:
            return SwitchSelection(
                "unavailable", None, "configured switch path was not found", public
            )

    if configured_path is None and configured_name is not None:
        named = [
            (candidate, reason)
            for candidate, reason in assessments
            if candidate.name == configured_name
        ]
        if not named:
            return SwitchSelection(
                "unavailable", None, "configured switch name was not found", public
            )
        if not any(reason.capable for _, reason in named):
            return SwitchSelection(
                "unavailable", None, "configured switch lacks SW_TABLET_MODE", public
            )

    if not capable:
        return SwitchSelection(
            "unavailable", None, "no device advertises SW_TABLET_MODE", public
        )

    best_rank = max(reason.rank for _, reason in capable)
    best = [(candidate, reason) for candidate, reason in capable if reason.rank == best_rank]
    if len(best) != 1:
        return SwitchSelection(
            "ambiguous",
            None,
            f"{len(best)} equally ranked devices advertise SW_TABLET_MODE",
            public,
        )

    selected = best[0][0]
    return SwitchSelection("selected", selected, "selected unique best candidate", public)


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_HOME_PATH = re.compile(r"^/(?:home|Users)/[^/]+(?=/|$)")
_RUNTIME_USER_PATH = re.compile(r"^/run/user/\d+(?=/|$)")


def sanitize_report_name(name: str, *, limit: int = 160) -> str:
    """Make a device label safe and compact for a shared text report."""

    clean = _CONTROL_CHARACTERS.sub("?", name).strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def sanitize_report_path(path: str, *, limit: int = 240) -> str:
    """Redact common per-user path components without hiding device identity."""

    clean = _CONTROL_CHARACTERS.sub("?", path).strip()
    clean = _HOME_PATH.sub("/home/<user>", clean)
    clean = _RUNTIME_USER_PATH.sub("/run/user/<uid>", clean)
    # Normalize harmless repeated separators without resolving symlinks or I/O.
    if clean.startswith("/"):
        clean = str(PurePosixPath(clean))
    return clean if len(clean) <= limit else "…" + clean[-(limit - 1) :]
