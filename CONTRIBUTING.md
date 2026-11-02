# Contributing

**All rules are in [`AGENTS.md`](AGENTS.md)**, for humans and AI agents alike. Cite them by number
(e.g. "R-7.4") in reviews. `docs/PROJECT_CONTEXT.md` is the source of truth and wins any conflict.

Quick start:

1. Pick or open one issue. Branch: `<type>/<issue>-<slug>`, e.g. `feat/4-hmac-handshake`.
2. Set up: `python -m venv .venv && . .venv/bin/activate && pip install -e .[dev]`.
3. Before pushing: `scripts/check.sh` (runs what CI runs).
4. Open a PR, fill in the template including the done checklist, get a different member's
   approval, squash merge.

Repository settings for admins: [`docs/setup/github.md`](docs/setup/github.md).
