# User service

The packaged systemd user unit is optional and intended primarily for
non-Omarchy sessions. Omarchy users can continue using the small UWSM-aware Lua
autostart rule in `examples/hypr/autostart.lua`.

Preview installation first:

```console
tablet-auto-rotate --install-service --service-dry-run
```

Then install the unit into the XDG user configuration directory:

```console
tablet-auto-rotate --install-service
systemctl --user daemon-reload
systemctl --user enable --now tablet-auto-rotate.service
```

The installer manages only
`$XDG_CONFIG_HOME/systemd/user/tablet-auto-rotate.service` (falling back to
`~/.config`). It refuses symlinked paths and differing existing units. An
explicit `--replace-service` makes a non-overwriting `.bak` before replacement.
It never invokes `systemctl` itself.

Disable the service before uninstalling it:

```console
systemctl --user disable --now tablet-auto-rotate.service
tablet-auto-rotate --uninstall-service --service-dry-run
tablet-auto-rotate --uninstall-service
systemctl --user daemon-reload
```

Uninstall removes the file only when it exactly matches the generated unit. A
locally modified unit is left untouched.
