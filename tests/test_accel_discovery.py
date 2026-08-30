from tablet_auto_rotate import core


def fake_device(path, hinge, hub):
    return core.AccelDevice(
        iio_path=path,
        hinge_path=hinge,
        hid_hub=hub,
        name_path=f"{path}/name",
        raw_paths=("x", "y", "z"),
        scale_paths=("sx", "sy", "sz"),
    )


def configure_topology(monkeypatch, names, hubs):
    paths = list(names)
    monkeypatch.setattr(core, "_iio_device_dirs", lambda: paths)
    monkeypatch.setattr(core, "_read_text", lambda path: names[path.rsplit("/", 1)[0]])
    monkeypatch.setattr(core, "_sysfs_device_path", lambda path: path)
    monkeypatch.setattr(core, "find_hid_hub_ancestor", lambda path: hubs.get(path))
    monkeypatch.setattr(
        core,
        "_make_accel_device",
        lambda path, hinge, hub: fake_device(path, hinge, hub),
    )


def test_prefers_unique_accelerometer_sharing_hinge_hub(monkeypatch):
    names = {"/hinge": "hinge", "/display": "accel_3d", "/base": "accel_3d"}
    configure_topology(
        monkeypatch,
        names,
        {"/hinge": "hub-a", "/display": "hub-a", "/base": "hub-b"},
    )
    selected = core.discover_accel()
    assert selected is not None
    assert selected.iio_path == "/display"
    assert selected.hinge_path == "/hinge"


def test_accepts_only_accelerometer_without_hinge(monkeypatch):
    configure_topology(monkeypatch, {"/sensor": "accel_3d"}, {"/sensor": None})
    selected = core.discover_accel()
    assert selected is not None
    assert selected.iio_path == "/sensor"
    assert selected.hinge_path == ""


def test_refuses_ambiguous_accelerometers(monkeypatch):
    names = {"/one": "accel_3d", "/two": "accel_3d"}
    configure_topology(monkeypatch, names, {"/one": None, "/two": None})
    assert core.discover_accel() is None
