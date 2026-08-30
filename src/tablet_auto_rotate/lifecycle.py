"""Safe, user-scoped installation planning for the systemd service.

This module deliberately does not call ``systemctl``.  Callers can display the
returned actions and decide separately whether to reload or enable the unit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import tempfile
from typing import Mapping


UNIT_NAME = "tablet-auto-rotate.service"


class LifecycleError(RuntimeError):
    """Raised when an operation cannot be performed without risking user data."""


@dataclass(frozen=True)
class Action:
    operation: str
    path: Path
    detail: str = ""


@dataclass(frozen=True)
class Plan:
    target: Path
    content: str
    actions: tuple[Action, ...]


def _config_home(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    raw = values.get("XDG_CONFIG_HOME")
    if raw:
        root = Path(raw)
    else:
        home = values.get("HOME")
        if not home:
            raise LifecycleError("HOME or XDG_CONFIG_HOME is required")
        root = Path(home) / ".config"
    if not root.is_absolute() or ".." in root.parts:
        raise LifecycleError("XDG_CONFIG_HOME must be an absolute, normalized path")
    return root


def service_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the sole path this module is permitted to manage."""
    return _config_home(env) / "systemd" / "user" / UNIT_NAME


def _render(template_path: Path, executable: Path) -> str:
    executable = Path(executable)
    if not executable.is_absolute():
        raise LifecycleError("the service executable must be an absolute path")
    if any(ch in str(executable) for ch in "\n\r"):
        raise LifecycleError("the service executable contains an invalid newline")
    template = Path(template_path).read_text(encoding="utf-8")
    if template.count("@EXECUTABLE@") != 1:
        raise LifecycleError("service template must contain one @EXECUTABLE@ marker")
    # systemd accepts C-style quoting; reject the only characters that would
    # make direct substitution ambiguous instead of attempting clever escaping.
    if any(ch in str(executable) for ch in '"\\'):
        raise LifecycleError("the service executable path cannot contain quotes or backslashes")
    return template.replace("@EXECUTABLE@", f'"{executable}"')


def _assert_safe_target(target: Path, root: Path) -> None:
    if target != root / "systemd" / "user" / UNIT_NAME:
        raise LifecycleError("refusing to manage a path outside the XDG user service location")
    current = root
    while True:
        if current.is_symlink():
            raise LifecycleError(f"refusing to follow symlink: {current}")
        if current == target.parent or not current.exists():
            if current == target.parent:
                break
        current = current / target.relative_to(current).parts[0]
    if target.is_symlink():
        raise LifecycleError(f"refusing to follow symlink: {target}")


def plan_install(
    template_path: Path,
    executable: Path,
    *,
    env: Mapping[str, str] | None = None,
    replace: bool = False,
) -> Plan:
    root = _config_home(env)
    target = service_path(env)
    _assert_safe_target(target, root)
    content = _render(Path(template_path), Path(executable))
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        if existing == content:
            actions = (Action("unchanged", target, "already installed"),)
        elif not replace:
            raise LifecycleError(f"service already exists and differs: {target}")
        else:
            backup = target.with_name(target.name + ".bak")
            if backup.exists() or backup.is_symlink():
                raise LifecycleError(f"refusing to overwrite existing backup: {backup}")
            actions = (Action("backup", backup, f"copy of {target}"), Action("write", target))
    else:
        actions = (Action("mkdir", target.parent), Action("write", target))
    return Plan(target, content, actions)


def install(
    template_path: Path,
    executable: Path,
    *,
    env: Mapping[str, str] | None = None,
    replace: bool = False,
    dry_run: bool = False,
) -> Plan:
    """Install a user unit atomically; replacing requires an explicit backup."""
    plan = plan_install(template_path, executable, env=env, replace=replace)
    if dry_run or plan.actions[0].operation == "unchanged":
        return plan
    target = plan.target
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_safe_target(target, _config_home(env))
    if plan.actions[0].operation == "backup":
        shutil.copy2(target, plan.actions[0].path, follow_symlinks=False)
    fd, temporary = tempfile.mkstemp(prefix=f".{UNIT_NAME}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(plan.content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        if plan.actions[0].operation == "mkdir":
            # An exclusive hard link makes a concurrent creator a safe failure
            # instead of allowing the final rename to overwrite their file.
            try:
                os.link(temporary, target)
            except FileExistsError as error:
                raise LifecycleError(f"service appeared during installation: {target}") from error
            os.unlink(temporary)
        else:
            os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return plan


def plan_uninstall(
    template_path: Path,
    executable: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> Plan:
    root = _config_home(env)
    target = service_path(env)
    _assert_safe_target(target, root)
    content = _render(Path(template_path), Path(executable))
    if not target.exists():
        actions = (Action("absent", target),)
    elif target.read_text(encoding="utf-8") != content:
        raise LifecycleError(f"refusing to remove modified service: {target}")
    else:
        actions = (Action("remove", target),)
    return Plan(target, content, actions)


def uninstall(
    template_path: Path,
    executable: Path,
    *,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> Plan:
    """Remove only a byte-for-byte matching generated unit."""
    plan = plan_uninstall(template_path, executable, env=env)
    if not dry_run and plan.actions[0].operation == "remove":
        plan.target.unlink()
    return plan
