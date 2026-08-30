# Contributing

Thanks for helping tablet-auto-rotate work on more convertible computers. You do
not need to own hardware already represented in the repository: sanitized probe
data, bug reports, documentation, and tests are all useful contributions.

## Before opening a change

- Search existing issues and pull requests for the same model or problem.
- Keep hardware-specific facts in data or fixtures rather than general control
  logic.
- Preserve the project's safety rule: when device selection is ambiguous, do not
  change a display or input-device configuration.
- Never include usernames, serial numbers, hostnames, or unrelated device data
  in a report or fixture.

For a newly tested computer, use the **Hardware report** issue form. A maintainer
can help turn its sanitized diagnostics into a fixture or profile. Reports of
failures are just as valuable as successful reports.

Generate the preferred attachment with:

```console
tablet-auto-rotate --probe --json > probe.json
```

The command applies built-in redaction, but you must still review the complete
file before publishing it. The schema and privacy boundary are documented in
[`docs/diagnostics.md`](docs/diagnostics.md).

## Development workflow

1. Fork the repository and create a focused branch.
2. Make the smallest coherent change that solves the problem.
3. Add or update automated tests when behavior changes.
4. Create an isolated development environment and run the local release checks:

   ```console
   python3 -m venv .venv
   .venv/bin/python -m pip install -e '.[dev]'
   .venv/bin/python -m pytest -q
   .venv/bin/python -m compileall -q src tests
   .venv/bin/tablet-auto-rotate --self-test
   ```

5. Explain the user-visible behavior, safety implications, and testing in the
   pull request.

Hardware profiles should include a sanitized fixture and its expected discovery
result whenever the fixture format supports the relevant machine. State exactly
which features were physically tested; do not infer untested support.

## Developer Certificate of Origin

This project uses the [Developer Certificate of Origin 1.1][dco] instead of a
contributor license agreement. Add a `Signed-off-by` trailer to every commit to
certify that you have the right to submit the contribution under the project's
license:

```text
Signed-off-by: Your Name <your.email@example.com>
```

Git can add this automatically:

```console
git commit --signoff
```

Use a name and email address by which you can be identified. If a pull request
contains an unsigned commit, amend or rebase it and add the sign-off; do not add
someone else's sign-off without their permission.

## Security and privacy

Avoid publishing sensitive diagnostics. Report security vulnerabilities through
GitHub's private vulnerability-reporting form as described in
[`SECURITY.md`](SECURITY.md), not through a public issue.

[dco]: https://developercertificate.org/
