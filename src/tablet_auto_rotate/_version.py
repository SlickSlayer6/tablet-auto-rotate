"""Package version helpers that do not import the daemon runtime."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


SOURCE_VERSION = "0.5.0+source"


def application_version() -> str:
    """Return the installed release, or an explicit source-tree fallback."""

    try:
        return version("tablet-auto-rotate")
    except PackageNotFoundError:
        return SOURCE_VERSION


__version__ = application_version()
