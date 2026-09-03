"""Lightweight console argument parsing for :mod:`tablet_auto_rotate`."""

from __future__ import annotations

import argparse
from typing import Optional, Sequence

from ._version import application_version


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely rotate the internal display and its touch device in tablet mode."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {application_version()}",
    )
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
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit a versioned JSON report with --probe or --doctor",
    )
    parser.add_argument("--verbose", action="store_true", help="log diagnostic details")
    parser.add_argument("--service-dry-run", action="store_true", help="preview a service operation")
    parser.add_argument("--replace-service", action="store_true", help="back up and replace a differing user unit")
    parser.add_argument("--service-executable", metavar="PATH", help="absolute executable path for the user unit")
    args = parser.parse_args(argv)
    if args.json_output and not (args.probe or args.doctor):
        parser.error("--json requires --probe or --doctor")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    from .core import run_args

    return run_args(args)


__all__ = ["main", "parse_args"]
