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
        lambda path, hinge, hub, _runtime=None: fake_device(path, hinge, hub),
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


def test_parses_kernel_mount_matrix_and_applies_it():
    matrix = core.parse_mount_matrix("0, 1, 0\n-1, 0, 0\n0, 0, 1\n")

    assert matrix == ((0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert core.apply_mount_matrix(matrix, (2.0, 3.0, 4.0)) == (3.0, -2.0, 4.0)


def test_rejects_non_unitary_mount_matrix():
    try:
        core.parse_mount_matrix("1, 0, 0; 1, 0, 0; 0, 0, 1")
    except ValueError as exc:
        assert "orthogonal" in str(exc)
    else:
        raise AssertionError("non-unitary matrix was accepted")


def test_required_mount_matrix_rejects_sensor_without_one(monkeypatch):
    paths = {
        "/sensor/in_accel_x_raw",
        "/sensor/in_accel_y_raw",
        "/sensor/in_accel_z_raw",
        "/sensor/in_accel_scale",
    }
    monkeypatch.setattr(core.os.path, "isfile", paths.__contains__)
    monkeypatch.setattr(core.os, "access", lambda path, _mode: path in paths)
    monkeypatch.setattr(core, "MOUNT_MATRIX_MODE", "require")

    assert core._make_accel_device("/sensor", "", "") is None


def test_make_device_reads_accel_specific_mount_matrix_first(monkeypatch):
    paths = {
        "/sensor/in_accel_x_raw",
        "/sensor/in_accel_y_raw",
        "/sensor/in_accel_z_raw",
        "/sensor/in_accel_scale",
        "/sensor/in_accel_mount_matrix",
        "/sensor/mount_matrix",
    }
    monkeypatch.setattr(core.os.path, "isfile", paths.__contains__)
    monkeypatch.setattr(core.os, "access", lambda path, _mode: path in paths)
    monkeypatch.setattr(core, "_read_text", lambda _path: "0,1,0;-1,0,0;0,0,1")
    monkeypatch.setattr(core, "MOUNT_MATRIX_MODE", "auto")

    device = core._make_accel_device("/sensor", "", "")

    assert device is not None
    assert device.mount_matrix_path == "/sensor/in_accel_mount_matrix"
    assert device.mount_matrix is not None
