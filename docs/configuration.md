# Configuration

`tablet-auto-rotate` reads `$XDG_CONFIG_HOME/tablet-auto-rotate/config.toml`,
or `~/.config/tablet-auto-rotate/config.toml` when `XDG_CONFIG_HOME` is unset.
Use `--config PATH` to test a different file. When no file exists, the bundled
Acer TravelMate values are used for backward compatibility.

Start from the physically verified profile in
`profiles/acer-travelmate-b311r-33.toml` and change only values confirmed by
`tablet-auto-rotate --probe` or other read-only system tools.

```toml
[hardware]
output = "eDP-1"
touch_device = "elan9004:00-04f3:4110"
switch_name = "Intel HID switches"
preferred_switch_path = "/dev/input/by-path/platform-INTC1070:00-event"
desktop_integration = "omarchy" # or "none"

[sensor]
axis_order = ["x", "y", "z"]
axis_signs = [1, 1, 1]
orientation_transforms = [1, 2, 3, 0]
mount_matrix = "ignore" # "auto" or "require" to apply an IIO mount matrix
```

`axis_order` maps physical IIO axes to logical screen X, Y, and Z.
`axis_signs` then flips the corresponding logical axes. The four orientation
transforms correspond, in order, to gravity along logical `+X`, `+Y`, `-X`,
and `-Y`.

`mount_matrix` controls standard Linux IIO mount-matrix handling:

- `"ignore"` preserves the configured axis behavior and is the default for
  backward compatibility;
- `"auto"` applies a valid matrix when the selected sensor exposes one and
  otherwise uses the configured axes unchanged;
- `"require"` applies a valid matrix and refuses sensors where it is missing or
  invalid.

The mount matrix is applied first, converting chip axes to the computer's main
hardware frame. `axis_order` and `axis_signs` are then applied to that result.
Existing calibrated profiles should remain on `"ignore"` unless they are
retested with mount-matrix handling enabled. Use `--probe --json` to inspect the
selected sensor's matrix before changing this setting.

Only transforms 0 through 3 are currently supported. Invalid, incomplete, or
ambiguous configuration is rejected before the daemon opens a device or
changes compositor state.

Set both `switch_name` and `preferred_switch_path` to `"auto"` to select by
the advertised `SW_TABLET_MODE` capability. Automatic selection succeeds only
when exactly one capable switch exists; equally ranked candidates are reported
as ambiguous and no display change is made. Prefer explicit values once a
machine has been calibrated and verified.

The `none` desktop integration applies Hyprland output and input transforms
without using the Omarchy layer-surface refresh workaround. This makes the
sensor core usable outside Omarchy, although other Hyprland configuration
providers still require future backend work.
