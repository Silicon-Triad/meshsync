# AI Use Log

Record of every AI-assisted contribution, for the report's AI-use appendix (PROJECT_CONTEXT.md §16).

| Date | Member | Tool | Used for | Kept / changed |
| ---- | ------ | ---- | -------- | -------------- |
| 2026-11-02 | Muhammad Ahmed Qazi | Claude Code | Repository scaffold from the scaffold brief: pyproject, CI, PR template, PROTOCOL.md, constants.py, framing.py + unit tests, module stubs, CLI skeleton, gen_certs.sh | Kept as generated; to be reviewed line by line by another member before merge (PR #27) |
| 2026-11-02 | Muhammad Ahmed Qazi | Claude Code | GitHub setup guide (docs/setup/github.md), CODEOWNERS, CONTRIBUTING.md from the team settings brief | Kept as generated; to be reviewed by another member before merge (PR #28) |
| 2026-11-02 | Muhammad Ahmed Qazi | Claude Code | AGENTS.md rulebook, CLAUDE.md, folder READMEs, FROZEN INTERFACE headers, scripts/check.sh, CONTRIBUTING.md, PR template (#29) | Draft reviewed and adjusted by Ahmed (branch names, PR size rule); to be reviewed by another member before merge |
| 2026-11-07 | Muhammad Ahmed Qazi | Claude Code (Opus 5.5) | TLS contexts (#5): net/tls.py, tests/unit/test_tls.py, PROTOCOL.md §6, README | |
| 2026-11-07 | Muhammad Ahmed Qazi | Claude Code | messages.py scaffold + tests (#31) | Reviewed messages.py and tests line by line; decided review round 1 (decision label (c), tombstone shape (f), kept branch name); fixes from Mujtaba's review (RecursionError, path control chars, allow_nan, hex case (d)) applied via Claude |
