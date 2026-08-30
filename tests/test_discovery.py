from tablet_auto_rotate.discovery import (
    SW_TABLET_MODE,
    SwitchCandidate,
    sanitize_report_name,
    sanitize_report_path,
    select_tablet_switch,
)
from tablet_auto_rotate import core
import json
from pathlib import Path


def candidate(path, name, codes=()):
    return SwitchCandidate.from_codes(path, name, codes)


def test_selects_only_capable_candidate():
    result = select_tablet_switch(
        [
            candidate("/dev/input/event1", "Lid", {0}),
            candidate("/dev/input/event2", "Tablet switches", {SW_TABLET_MODE}),
        ]
    )
    assert result.status == "selected"
    assert result.selected.path == "/dev/input/event2"
    assert result.candidates[1].capable is True


def test_exact_configured_path_has_highest_rank():
    result = select_tablet_switch(
        [
            candidate("/dev/input/event1", "Same", {SW_TABLET_MODE}),
            candidate("/dev/input/by-path/platform-tablet", "Same", {SW_TABLET_MODE}),
        ],
        configured_path="/dev/input/by-path/platform-tablet",
        configured_name="Same",
    )
    assert result.status == "selected"
    assert result.selected.path.endswith("platform-tablet")
    assert result.candidates[1].rank > result.candidates[0].rank


def test_exact_name_selects_unique_capable_match():
    result = select_tablet_switch(
        [
            candidate("/dev/input/event1", "Other", {SW_TABLET_MODE}),
            candidate("/dev/input/event2", "Wanted", {SW_TABLET_MODE}),
        ],
        configured_name="Wanted",
    )
    assert result.status == "selected"
    assert result.selected.name == "Wanted"


def test_refuses_unconfigured_ambiguity():
    result = select_tablet_switch(
        [
            candidate("/dev/input/event1", "One", {SW_TABLET_MODE}),
            candidate("/dev/input/event2", "Two", {SW_TABLET_MODE}),
        ]
    )
    assert result.status == "ambiguous"
    assert result.selected is None
    assert "2 equally ranked" in result.summary


def test_refuses_duplicate_configured_name():
    result = select_tablet_switch(
        [
            candidate("/dev/input/event1", "Same", {SW_TABLET_MODE}),
            candidate("/dev/input/event2", "Same", {SW_TABLET_MODE}),
        ],
        configured_name="Same",
    )
    assert result.status == "ambiguous"


def test_explicit_missing_path_does_not_fall_back():
    result = select_tablet_switch(
        [candidate("/dev/input/event1", "Tablet", {SW_TABLET_MODE})],
        configured_path="/dev/input/missing",
    )
    assert result.status == "unavailable"
    assert result.selected is None
    assert "not found" in result.summary


def test_explicit_incapable_path_does_not_fall_back():
    result = select_tablet_switch(
        [
            candidate("/dev/input/chosen", "Lid", {0}),
            candidate("/dev/input/event2", "Tablet", {SW_TABLET_MODE}),
        ],
        configured_path="/dev/input/chosen",
    )
    assert result.status == "unavailable"
    assert "lacks SW_TABLET_MODE" in result.summary


def test_explicit_missing_name_does_not_fall_back():
    result = select_tablet_switch(
        [candidate("/dev/input/event1", "Other", {SW_TABLET_MODE})],
        configured_name="Missing",
    )
    assert result.status == "unavailable"
    assert "name was not found" in result.summary


def test_no_capable_candidates_is_explained():
    result = select_tablet_switch([candidate("/dev/input/event1", "Lid", {0})])
    assert result.status == "unavailable"
    assert result.candidates[0].reasons == ("does not advertise SW_TABLET_MODE",)


def test_report_values_are_sanitized_without_mutating_selection():
    raw = candidate("/home/alice/devices//event1", "Tablet\nSecret", {SW_TABLET_MODE})
    result = select_tablet_switch([raw])
    assert result.selected is raw
    assert result.candidates[0].path == "/home/<user>/devices/event1"
    assert result.candidates[0].name == "Tablet?Secret"


def test_report_sanitizers_redact_runtime_uid_and_bound_output():
    assert sanitize_report_path("/run/user/1000/input/event1") == "/run/user/<uid>/input/event1"
    assert sanitize_report_name("abcdef", limit=4) == "abc…"


def test_core_can_select_from_sysfs_when_dev_nodes_are_not_visible(monkeypatch):
    name_paths = [
        "/sys/class/input/event1/device/name",
        "/sys/class/input/event2/device/name",
    ]
    values = {
        name_paths[0]: "Lid Switch",
        name_paths[1]: "Convertible switches",
        "/sys/class/input/event1/device/capabilities/sw": "1",
        "/sys/class/input/event2/device/capabilities/sw": "2",
    }
    monkeypatch.setattr(core.glob, "glob", lambda _pattern: name_paths)
    monkeypatch.setattr(core, "_read_text", values.__getitem__)
    monkeypatch.setattr(core, "PREFERRED_SWITCH_PATH", "auto")
    monkeypatch.setattr(core, "SWITCH_NAME", "auto")

    selection, devices = core.discover_switch_selection()

    assert selection.status == "selected"
    assert selection.selected is not None
    assert selection.selected.path == "/dev/input/event2"
    assert devices[selection.selected.path].name == "Convertible switches"


def test_acer_hardware_fixture_selects_expected_switch():
    path = Path(__file__).parent / "fixtures" / "acer-travelmate-b311r-33.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    candidates = [
        SwitchCandidate.from_codes(
            entry["path"], entry["name"], entry["switch_codes"]
        )
        for entry in fixture["switches"]
    ]
    result = select_tablet_switch(
        candidates, configured_name=fixture["expected"]["switch_name"]
    )
    assert result.status == fixture["expected"]["switch_status"]
    assert result.selected is not None
    assert result.selected.name == fixture["expected"]["switch_name"]
