"""Automatic tablet-mode rotation for Linux convertible computers."""

from __future__ import annotations

from typing import Optional, Sequence

from ._version import __version__


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Load the command implementation only when the entry point is invoked."""

    from .cli import main as cli_main

    return cli_main(argv)

__all__ = ["__version__", "main"]
