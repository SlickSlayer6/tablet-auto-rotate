from __future__ import annotations

import os
import subprocess
import sys


def test_version_command_does_not_import_daemon_core():
    script = (
        "import sys\n"
        "from tablet_auto_rotate.cli import parse_args\n"
        "try:\n"
        "    parse_args(['--version'])\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0\n"
        "assert 'tablet_auto_rotate.core' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=5.0,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("-c ")
