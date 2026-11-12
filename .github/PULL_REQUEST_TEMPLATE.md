<!--
Thanks for the PR. A few asks to keep review fast:

  - Keep the change focused. One feature, fix, or cleanup per PR.
  - Reference the issue or discussion this addresses, if any.
  - Fill in the testing notes below; there's no CI on pull requests yet.

See CONTRIBUTING.md for scope and code style.
-->

## What this changes

<!-- One or two sentences describing the change. -->

## Why

<!-- Linked issue / discussion, user-visible problem, or design rationale. -->

Closes #

## Testing

<!--
Run `HOME=$(mktemp -d) pytest -q` and paste the summary line. Provider
drivers and lifecycle changes also need the real run you did.
-->

- `pytest` result:
- Commands run:
  ```
  $ swm ...
  ```
- Provider(s) tested against:
- OS / Python version:
- Expected vs. observed:

## Checklist

- [ ] PR scope is one logical change.
- [ ] Public functions have type hints.
- [ ] User-facing output uses `rich` (no bare `print`).
- [ ] New provider / framework registered in the corresponding `__init__.py`.
- [ ] Docs (`README.md`, `docs/`, or `swmgpu.com`) updated if behavior changed.
- [ ] No new dependencies added without discussion.
