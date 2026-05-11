# Security Policy

## Supported versions

Only the latest minor release line receives security fixes. `swm` is a CLI
that handles cloud-provider API keys and writes SSH config, so most relevant
issues are credential-handling, command-injection, or supply-chain bugs in
the release pipeline.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a vulnerability

**Please do not open public issues for security reports.**

Use GitHub's private vulnerability reporting:
<https://github.com/swm-gpu/swm/security/advisories/new>

Include:

- The version of `swm` (`swm --version`).
- The provider / storage backend involved, if relevant.
- A minimal reproduction and the expected vs. actual behavior.
- Whether credentials are exposed (we will rotate the affected `HOMEBREW_TAP_TOKEN` and re-issue PyPI/Homebrew artifacts if so).

We will acknowledge within 72 hours and ship a fix on a best-effort basis,
prioritizing credential-handling and remote-execution issues. Once a fix is
released, the advisory is published with credit to the reporter unless they
prefer otherwise.

## Out of scope

- Vulnerabilities in upstream cloud providers (RunPod, Vast.ai, Lambda Labs,
  AWS, GCP, Azure, CoreWeave, Vultr, Fluidstack, TensorDock) — report to them
  directly.
- Issues that require already having an attacker's API keys configured in
  `~/.config/swm/config.toml`.
- Self-DoS by running `swm` against accounts with insufficient quotas.
