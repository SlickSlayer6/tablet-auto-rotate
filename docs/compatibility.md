# Hardware compatibility

Tablet hardware varies even between computers sold under the same model name.
This project records evidence rather than promising that an entire product line
works from a single report.

## Evidence levels

| Level | Requirements | What it means |
| --- | --- | --- |
| Reported | A community report identifies the exact model, relevant software versions, configuration, and observed results. | One person reports the stated behavior; it is not reproduced by CI or maintainers. |
| Fixture-tested | A sanitized hardware fixture and expected result exercise discovery and command behavior in automated tests. | Future code changes are checked against the submitted machine description, but CI does not test physical sensors or touch alignment. |
| Physically verified | A contributor completes the physical test checklist on the named hardware and records the result. | The reported hardware exercised tablet mode, rotation, and applicable input alignment; maintainers may not own it. |
| Maintainer-verified | A maintainer personally completes the physical test checklist on the named hardware and records the result. | A maintainer reproduced the result on physical hardware. This is not a warranty for every revision or software version. |

Levels are cumulative only when their requirements are met. In particular,
fixture-tested does not imply physically verified, and a successful community
report is not automatically maintainer-verified.

## Recording a device

Each entry in a future compatibility table should include:

- manufacturer, exact model, and hardware revision when available;
- kernel, Hyprland, project, firmware/BIOS, and desktop integration versions;
- evidence level and a link to the report or fixture;
- results for tablet-mode detection, all four orientations, touchscreen,
  stylus, suspend/resume, and external-display behavior;
- explicit `not tested`, `not applicable`, and failure states rather than blanks;
- the date of the latest physical test.

Support claims apply only to the recorded combination. Firmware and kernel
updates can change sensor behavior, and manufacturers may reuse a model name for
different internal hardware.

## Physical test checklist

Test with unsaved work closed and a recovery path available. Confirm:

1. Laptop mode does not rotate unexpectedly.
2. Entering and leaving tablet mode is detected.
3. Upright, left, right, and inverted orientations settle correctly.
4. Touchscreen corners align with the displayed corners in every orientation.
5. Stylus alignment works, if present.
6. Suspend/resume restores correct behavior.
7. Disconnecting and reconnecting relevant devices fails safely.
8. External displays are not rotated, enabled, disabled, or repositioned.

Submit results with the repository's **Hardware report** issue form. Diagnostic
attachments must be reviewed and sanitized before publication.
