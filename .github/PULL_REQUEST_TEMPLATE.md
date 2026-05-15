<!--
Thanks for the PR. A few asks to keep review fast:

  - Keep the change focused. One feature, fix, or cleanup per PR.
  - Reference the issue or discussion this addresses, if any.
  - Fill in the testing notes below; we don't have an automated suite yet.

See CONTRIBUTING.md for scope and code style.
-->

## What this changes

<!-- One or two sentences describing the change. -->

## Why

<!-- Linked issue / discussion, user-visible problem, or design rationale. -->

Closes #

## Testing

<!--
Provider drivers and lifecycle changes can't be unit-tested today. Please
include the manual repro you ran.
-->

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
