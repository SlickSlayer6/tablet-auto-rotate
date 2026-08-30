import json

import pytest

from tablet_auto_rotate import core
from tablet_auto_rotate.config import HardwareConfig
from tablet_auto_rotate.discovery import SwitchCandidate, select_tablet_switch


def _successful_probe(monkeypatch):
    candidate = SwitchCandidate.from_codes(
        "/home/alice/dev/event7", "Tablet\nSwitch", {core.SW_TABLET_MODE}
    )
    selection = select_tablet_switch([candidate])
    device = core.SwitchDevice(
        path=candidate.path,
        event_name="event7",
        name_path="/home/alice/sys/event7/name",
        name=candidate.name,
    )
    sensor = core.AccelDevice(
        iio_path="/sys/bus/iio/devices/iio:device0",
        hinge_path="/sys/bus/iio/devices/iio:device1",
        hid_hub="001F:8087:0AC2.00AF",
        name_path="/sys/bus/iio/devices/iio:device0/name",
        raw_paths=("/sys/raw_x", "/sys/raw_y", "/sys/raw_z"),
        scale_paths=("/sys/scale", "/sys/scale", "/sys/scale"),
    )
    monkeypatch.setattr(core, "discover_switch_selection", lambda: (selection, {candidate.path: device}))
    monkeypatch.setattr(core, "open_switch_device", lambda _device: (99, True))
    monkeypatch.setattr(core.os, "close", lambda _fd: None)
    monkeypatch.setattr(core, "_read_text", lambda _path: "Tablet\nSwitch")
    monkeypatch.setattr(core, "discover_accel", lambda: sensor)
    monkeypatch.setattr(
        core,
        "read_accel",
        lambda _sensor: core.AccelReading((1, 2, 3), (0.1, 0.1, 0.1), (0.1, 0.2, 0.3)),
    )


def test_probe_json_is_versioned_structured_and_sanitized(monkeypatch, capsys):
    _successful_probe(monkeypatch)
    config = HardwareConfig(preferred_switch_path="/home/alice/dev/event7")

    result = core.run_probe(
        config=config,
        config_path="/home/alice/.config/tablet-auto-rotate/config.toml",
        json_output=True,
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 0
    assert report["schema_version"] == 1
    assert report["report_type"] == "probe"
    assert report["ok"] is True
    assert report["switch"]["selected"]["state"] == "tablet"
    assert report["switch"]["selection"]["assessed_candidate_count"] == 1
    assert report["sensor"]["selected"]["hid_hub"].endswith(".<instance>")
    assert report["sensor"]["reading"]["raw"] == [1, 2, 3]
    assert "alice" not in captured.out
    assert "Tablet\\nSwitch" not in captured.out
    assert captured.out == json.dumps(report, indent=2, sort_keys=True) + "\n"


def test_probe_json_failure_remains_valid(monkeypatch, capsys):
    monkeypatch.setattr(core, "discover_switch_selection", lambda: (_ for _ in ()).throw(OSError("bad\npath")))
    monkeypatch.setattr(core, "discover_accel", lambda: None)

    result = core.run_probe(json_output=True)

    report = json.loads(capsys.readouterr().out)
    assert result == 1
    assert report["ok"] is False
    assert report["switch"]["selection"]["status"] == "error"
    assert {error["component"] for error in report["errors"]} == {
        "sensor_discovery",
        "switch_discovery",
    }


def test_probe_omits_unrelated_input_names_unless_verbose(monkeypatch):
    selected = SwitchCandidate.from_codes("/dev/input/event1", "Tablet", {core.SW_TABLET_MODE})
    unrelated = SwitchCandidate.from_codes("/dev/input/event2", "Private accessory", {0})
    selection = select_tablet_switch([selected, unrelated])
    device = core.SwitchDevice(selected.path, "event1", "/sys/name", selected.name)
    monkeypatch.setattr(core, "discover_switch_selection", lambda: (selection, {selected.path: device}))
    monkeypatch.setattr(core, "open_switch_device", lambda _device: (99, False))
    monkeypatch.setattr(core.os, "close", lambda _fd: None)
    monkeypatch.setattr(core, "_read_text", lambda _path: selected.name)
    monkeypatch.setattr(core, "discover_accel", lambda: None)

    normal = core.collect_probe_report(HardwareConfig(), None)
    verbose = core.collect_probe_report(HardwareConfig(), None, verbose=True)

    assert normal["switch"]["selection"]["assessed_candidate_count"] == 2
    assert [item["name"] for item in normal["switch"]["selection"]["candidates"]] == ["Tablet"]
    assert [item["name"] for item in verbose["switch"]["selection"]["candidates"]] == [
        "Tablet",
        "Private accessory",
    ]


def test_doctor_json_has_stable_check_ids(monkeypatch, capsys):
    monkeypatch.setattr(core.shutil, "which", lambda _name: None)
    monkeypatch.setattr(core, "discover_switch_selection", lambda: (_ for _ in ()).throw(OSError("missing")))
    monkeypatch.setattr(core, "discover_accel", lambda: None)

    result = core.run_doctor(HardwareConfig(), None, json_output=True)

    report = json.loads(capsys.readouterr().out)
    assert result == 1
    assert report["report_type"] == "doctor"
    assert report["ok"] is False
    assert all(set(check) == {"detail", "id", "name", "status"} for check in report["checks"])
    assert report["checks"][0]["id"] == "input_abi"
    assert "/home/<user>/" in next(
        check["detail"] for check in report["checks"] if check["id"] == "configuration"
    )


def test_json_requires_probe_or_doctor():
    with pytest.raises(SystemExit) as error:
        core.parse_args(["--json"])
    assert error.value.code == 2


def test_json_cli_modes_are_accepted():
    assert core.parse_args(["--probe", "--json"]).json_output is True
    assert core.parse_args(["--doctor", "--json"]).json_output is True
