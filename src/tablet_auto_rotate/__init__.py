"""Automatic tablet-mode rotation for Linux convertible computers."""

from .core import main

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("tablet-auto-rotate")
except PackageNotFoundError:
    __version__ = "0.4.0+source"

__all__ = ["__version__", "main"]
