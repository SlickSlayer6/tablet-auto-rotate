from pathlib import Path

import pytest

from tablet_auto_rotate.lifecycle import (
    LifecycleError,
    install,
    plan_install,
    service_path,
    uninstall,
)


TEMPLATE = Path(__file__).parents[1] / "packaging/systemd/tablet-auto-rotate.service.in"
PACKAGED_TEMPLATE = (
    Path(__file__).parents[1]
    / "src/tablet_auto_rotate/data/tablet-auto-rotate.service.in"
)
EXECUTABLE = Path("/opt/tablet-auto-rotate/bin/tablet-auto-rotate")


def test_packaging_and_runtime_service_templates_match():
    assert TEMPLATE.read_bytes() == PACKAGED_TEMPLATE.read_bytes()


def environment(tmp_path):
    return {"XDG_CONFIG_HOME": str(tmp_path / "config")}


def test_service_path_is_xdg_user_unit(tmp_path):
    assert service_path(environment(tmp_path)) == (
        tmp_path / "config/systemd/user/tablet-auto-rotate.service"
    )


def test_install_and_uninstall_round_trip(tmp_path):
    env = environment(tmp_path)
    installed = install(TEMPLATE, EXECUTABLE, env=env)
    assert installed.target.read_text() == installed.content
    assert 'ExecStart="/opt/tablet-auto-rotate/bin/tablet-auto-rotate"' in installed.content
    removed = uninstall(TEMPLATE, EXECUTABLE, env=env)
    assert removed.actions[0].operation == "remove"
    assert not installed.target.exists()


def test_dry_run_does_not_create_directories(tmp_path):
    env = environment(tmp_path)
    plan = install(TEMPLATE, EXECUTABLE, env=env, dry_run=True)
    assert [action.operation for action in plan.actions] == ["mkdir", "write"]
    assert not plan.target.parent.exists()


def test_install_is_idempotent(tmp_path):
    env = environment(tmp_path)
    install(TEMPLATE, EXECUTABLE, env=env)
    plan = install(TEMPLATE, EXECUTABLE, env=env)
    assert plan.actions[0].operation == "unchanged"


def test_different_existing_unit_is_not_overwritten(tmp_path):
    env = environment(tmp_path)
    target = service_path(env)
    target.parent.mkdir(parents=True)
    target.write_text("user content\n")
    with pytest.raises(LifecycleError, match="already exists"):
        install(TEMPLATE, EXECUTABLE, env=env)
    assert target.read_text() == "user content\n"


def test_explicit_replace_makes_non_overwriting_backup(tmp_path):
    env = environment(tmp_path)
    target = service_path(env)
    target.parent.mkdir(parents=True)
    target.write_text("old content\n")
    install(TEMPLATE, EXECUTABLE, env=env, replace=True)
    assert target.with_name(target.name + ".bak").read_text() == "old content\n"
    target.write_text("new user content\n")
    with pytest.raises(LifecycleError, match="existing backup"):
        install(TEMPLATE, EXECUTABLE, env=env, replace=True)


def test_uninstall_refuses_modified_unit(tmp_path):
    env = environment(tmp_path)
    target = install(TEMPLATE, EXECUTABLE, env=env).target
    target.write_text(target.read_text() + "# local edit\n")
    with pytest.raises(LifecycleError, match="modified service"):
        uninstall(TEMPLATE, EXECUTABLE, env=env)
    assert target.exists()


def test_rejects_relative_xdg_path():
    with pytest.raises(LifecycleError, match="absolute"):
        plan_install(TEMPLATE, EXECUTABLE, env={"XDG_CONFIG_HOME": "relative"})


def test_rejects_symlink_in_managed_path(tmp_path):
    env = environment(tmp_path)
    config = Path(env["XDG_CONFIG_HOME"])
    config.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (config / "systemd").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(LifecycleError, match="symlink"):
        install(TEMPLATE, EXECUTABLE, env=env)


def test_requires_absolute_executable(tmp_path):
    with pytest.raises(LifecycleError, match="absolute"):
        plan_install(TEMPLATE, Path("bin/tablet-auto-rotate"), env=environment(tmp_path))
