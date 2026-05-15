# Contributing to swm

Thanks for your interest. swm is a small, opinionated CLI for managing cloud
GPUs, and we welcome contributions that keep it that way: fewer moving parts,
clear scope, and provider behavior that matches what each cloud actually does.

## Scope

We say **yes** to:

- Bug fixes with a clear reproduction (especially provider-specific quirks).
- New provider drivers under `src/swm/providers/` that follow the existing
  interface (search → create → inspect → destroy → cost).
- New framework definitions under `src/swm/frameworks/` (one file per
  framework, declarative `Step` lists).
- Documentation, examples, and clearer error messages.
- Tests once we have a test harness (see "Testing" below).

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
pip install -e ".[dev,all]"
swm --version
```

Python 3.11+ is required. We recommend 3.12 for development since it matches
the release CI runner.

## Running locally

```bash
# install your editable copy into a clean shell
pip install -e .

# point at a scratch config so you don't touch your real one
export SWM_CONFIG_HOME=$(mktemp -d)
swm config init
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
- Provider drivers must raise `swm.errors.ProviderError` (or a subclass), never
  leak raw HTTP exceptions to the user.
- One blank line between logical blocks. No trailing whitespace.

There is no formatter pinned yet. `ruff format` produces output close to what
the codebase currently looks like; we'll commit a config in a follow-up.

## Testing

swm does not have an automated test suite yet. Until it does, contributions
should include in the PR description:

1. The commands you ran (`swm gpus`, `swm pod create ...`, etc.).
2. The provider(s) you tested against.
3. Expected vs. observed output, or a screenshot for visual changes.

This is temporary. A `tests/` directory with pytest + recorded HTTP fixtures
is on the roadmap.

## Branch and PR workflow

1. Fork `swm-gpu/swm`.
2. Branch from `main`. Name it `fix/...`, `feat/...`, `docs/...`, or
   `chore/...`.
3. Keep PRs focused. One feature, one fix, or one cleanup per PR.
4. Commit messages: lowercase imperative summary line. Reference issues with
   `#N` in the body when relevant.
5. Open the PR against `main`. Fill in the PR template. A maintainer will
   review within a few days.
6. CI must be green before merge. Maintainers may push small style fixups
   directly to your branch rather than block on round-trips.

## Adding a new provider

A provider driver is a single file at `src/swm/providers/<name>.py` exporting
a `Provider` subclass. Look at `runpod.py` or `vastai.py` as references:
they're the most complete and battle-tested. The interface is:

- `search(filters)` → list of available instance offers
- `create(offer, ...)` → spin up a pod, return its qualified ID
- `inspect(pod_id)` → live state (running / starting / stopped, cost so far)
- `destroy(pod_id)` → tear it down idempotently
- `cost(pod_id)` → best-effort spend estimate

Register the new provider in `src/swm/providers/__init__.py::_PROVIDERS`. Add
its slug to `src/swm/commands/_helpers.py::_PROVIDER_PREFIXES` so qualified
IDs route correctly.

## Adding a new framework

A framework definition is a single file at `src/swm/frameworks/<name>.py`
exporting a `Framework` instance. See `comfyui.py` and `vllm_server.py` for
the canonical patterns. Most fields are declarative `Step` lists (install,
post_install, pre_start). Register in
`src/swm/frameworks/__init__.py::FRAMEWORKS`.

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
