# Contributing to swm

Thanks for your interest. swm is a small, opinionated CLI for managing cloud
GPUs, and we welcome contributions that keep it that way: fewer moving parts,
clear scope, and provider behavior that matches what each cloud actually does.

## Scope

We say **yes** to:

- Bug fixes with a clear reproduction (especially provider-specific quirks).
- New provider drivers under `src/swm/providers/` that implement the
  `CloudProvider` interface (see "Adding a new provider" below).
- New framework definitions under `src/swm/frameworks/` (one file per
  framework, declarative `Step` lists).
- Documentation, examples, and clearer error messages.
- Tests for new or changed behavior (see "Testing" below).

We say **no** (or "not yet") to:

- A web UI or dashboard. swm is a CLI; the website is documentation only.
- Vendor lock-in features that only work on one provider.
- Optional dependencies that aren't gated behind `pip install swm-gpu[<extra>]`.
- Bundling third-party tools by default. Use the framework / extension
  mechanism instead.

If you're not sure, open a [Discussion][discussions] before writing code.

## Development setup

```bash
git clone https://github.com/swm-gpu/swm.git
cd swm
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]" pytest
swm --version
```

Python 3.11+ is required. We recommend 3.12 for development since it matches
the release CI runner.

## Running locally

```bash
# install your editable copy into a clean shell
pip install -e .

# config and the cost database live under ~/.config/swm, so a throwaway
# HOME keeps your real ones untouched
export HOME=$(mktemp -d)
swm config path
swm gpus -g a100 --max-price 2
```

Provider drivers need real credentials to test end-to-end. For most PRs a
single provider is enough (RunPod and Vast.ai have the cheapest test pods).

## Code style

- Python: standard library + a small number of pinned deps (see
  `pyproject.toml`). Prefer adding nothing to dependencies.
- Type hints on all public functions. `from __future__ import annotations` at
  the top of every module.
- `rich` for any user-facing output. No bare `print`.
- Provider drivers raise `RuntimeError` with a message the user can act on,
  never a raw HTTP exception. CLI commands raise `click.ClickException`.
- One blank line between logical blocks. No trailing whitespace.

There is no formatter pinned yet. `ruff format` produces output close to what
the codebase currently looks like; we'll commit a config in a follow-up.

## Testing

The test suite lives in `tests/` and runs with pytest. Use a throwaway `HOME`
so the tests never read your real config or credentials:

```bash
HOME=$(mktemp -d) pytest -q
```

Tests never call a real provider. Provider tests stub the HTTP payloads; see
`tests/test_gpu_search.py` for the pattern. Add or update tests for the
behavior you change.

Provider drivers and lifecycle changes also need a real run, because stubbed
payloads can't prove a pod comes up. Include in the PR description:

1. The commands you ran (`swm gpus`, `swm pod create ...`, etc.).
2. The provider(s) you tested against.
3. Expected vs. observed output, or a screenshot for visual changes.

## Branch and PR workflow

1. Fork `swm-gpu/swm`.
2. Branch from `main`. Name it `fix/...`, `feat/...`, `docs/...`, or
   `chore/...`.
3. Keep PRs focused. One feature, one fix, or one cleanup per PR.
4. Commit messages: lowercase imperative summary line. Reference issues with
   `#N` in the body when relevant.
5. Open the PR against `main`. Fill in the PR template. A maintainer will
   review within a few days.
6. There is no CI on pull requests yet. Run the test suite locally and include
   the result in the PR. Maintainers may push small fixups directly to your
   branch rather than block on round-trips.

## Adding a new provider

A provider driver is a single file at `src/swm/providers/<name>.py` with a
subclass of `CloudProvider` from `src/swm/providers/base.py`. Look at
`runpod.py` or `vastai.py` as references: they're the most complete and
battle-tested. The required members are:

- `name`, `slug`: display name and the short id used in `slug:id` pod ids
- `is_configured()`: whether the user has credentials set
- `list_gpus(gpu_count=None)`: offers as `GpuInfo`, one row per real offer,
  with the hourly price for the whole configuration
- `list_instances()`: the user's instances as `Instance`
- `create_instance(config)`: start an instance from a `CreateConfig`
- `start_instance(id)`, `stop_instance(id)`: resume or pause; raise
  `RuntimeError` with a clear message if the provider can't
- `terminate_instance(id)`: destroy it, safely repeatable

Search filters are applied locally by default. To push them into the
provider's API, override `_search_gpus` and list the fields it enforces in
`native_search_fields`. A filter that is in neither `native_search_fields`
nor `local_search_fields` is refused with `UnsupportedSearchFilters`, so drop
any field the provider can't honor (for example region, if offers carry no
region) instead of letting it silently return nothing.

Register the class in `ALL_PROVIDERS` in `src/swm/providers/__init__.py`, and
add its slug to `_PROVIDER_PREFIXES` in `src/swm/commands/_helpers.py` so
qualified ids route correctly. Add tests with recorded API payloads, and
include a real end-to-end run in the PR (see "Testing").

## Adding a new framework

A framework definition is a single file at `src/swm/frameworks/<name>.py`
exporting a `Framework` instance named `FRAMEWORK`. See `comfyui.py` and
`vllm_server.py` for the canonical patterns. Most fields are declarative
`Step` lists (install, post_install, pre_start). Register the module name in
`_FRAMEWORK_MODULES` in `src/swm/frameworks/__init__.py`.

## Code of conduct

This project follows the [Contributor Covenant v2.1][coc]. Be kind, be
specific, and assume the other person is doing their best.

## Security

Don't open public issues for security reports. See [SECURITY.md][security].

## Questions

- Bug or feature: [open an issue][issues] using the templates.
- General question or design discussion: [GitHub Discussions][discussions].
- Security: [private vulnerability report][security].

[discussions]: https://github.com/swm-gpu/swm/discussions
[issues]: https://github.com/swm-gpu/swm/issues/new/choose
[coc]: ./CODE_OF_CONDUCT.md
[security]: ./SECURITY.md
