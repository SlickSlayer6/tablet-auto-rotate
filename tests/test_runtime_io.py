from __future__ import annotations

import os

from tablet_auto_rotate import core


class RecordingLogger:
    def __init__(self):
        self.errors = []

    def error(self, key, message, **_kwargs):
        self.errors.append((key, message))


def test_runtime_lock_prefers_xdg_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert core._runtime_lock_path() == str(tmp_path / "tablet-auto-rotate.lock")


def test_runtime_lock_has_uid_scoped_fallback(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(core.os, "getuid", lambda: 1234)
    assert core._runtime_lock_path() == "/tmp/tablet-auto-rotate-1234/tablet-auto-rotate.lock"


def test_acquire_lock_excludes_second_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    first_logger = RecordingLogger()
    second_logger = RecordingLogger()
    first_fd = core.acquire_lock(first_logger)
    try:
        assert first_fd is not None
        second_fd = core.acquire_lock(second_logger)
        assert second_fd is None
        assert second_logger.errors[0][0] == "lock"
    finally:
        if first_fd is not None:
            os.close(first_fd)
