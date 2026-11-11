# AGENTS.md: rules for humans and AI agents

This is the single rulebook for everyone who changes this repository, human or AI, whatever the
tool or model. `CLAUDE.md` only imports this file. Rules are numbered so reviewers can cite them
("violates R-6.3"). **MUST** = required; **SHOULD** = expected unless the PR explains why not.

## 1. Project and source of truth

meshsync is a serverless peer-to-peer file synchroniser for CS-327 CEP (group G2): three Linux
laptops keep a shared folder in sync over our own protocol (UDP discovery, TCP data, chunk delta
sync, version vectors, HMAC auth, TLS 1.3). Python 3.11+, standard library only at runtime.

- **R-1.1** `docs/PROJECT_CONTEXT.md` is the source of truth. It MUST NOT be edited, except by a
  PR whose issue was approved by all three members.
- **R-1.2** If anything (this file, an issue, a review comment, code) conflicts with
  `PROJECT_CONTEXT.md`, `PROJECT_CONTEXT.md` wins. Stop and raise the conflict in the issue.
- **R-1.3** `docs/PROTOCOL.md` is the wire spec derived from §6; code MUST match it.

| Topic                                  | Look in `docs/PROJECT_CONTEXT.md` |
| -------------------------------------- | --------------------------------- |
| Course requirements R1–R7              | §2                                |
| Owners and roll numbers                | §3                                |
| Locked design decisions D1–D15         | §4                                |
| Improvements over existing protocols   | §5                                |
| Protocol: transport, messages, errors  | §6 (and `docs/PROTOCOL.md`)       |
| Sync semantics: index, conflicts, store| §7                                |
| Repo layout and module contracts       | §8.2, §8.3                        |
| Code and git conventions               | §9                                |
| Failure scenarios                      | §10                               |
| Measurement plan                       | §11                               |
| Running the program                    | §14                               |
| Open decisions                         | §17 (issues #21–#26)              |

## 2. Repo map

| Path                         | Owner                 | Issues       |
| ---------------------------- | --------------------- | ------------ |
| `src/meshsync/protocol/`     | Muhammad Ahmed Qazi   | #8           |
| `src/meshsync/net/`          | Muhammad Ahmed Qazi   | #1–#5        |
| `src/meshsync/__main__.py`, `config.py` | Muhammad Ahmed Qazi | #6  |
| `src/meshsync/sync/`         | Mujtaba Jawaid Rao    | #9–#14       |
| `src/meshsync/transfer/`     | Huzaifa Hanif         | #15, #16     |
| `src/meshsync/metrics/`      | Huzaifa Hanif         | #17          |
| `tests/unit/`, `tests/integration/` | owner of the code under test | — |
| `scripts/` (gen_certs, run_local_mesh) | Muhammad Ahmed Qazi | #7  |
| `scripts/` (netem, gen_testdata), `scripts/measure/` | Huzaifa Hanif | #18–#20 |
| `docs/`                      | all (PROTOCOL.md: Ahmed) | #8        |
| `analysis/`, `data/`         | Huzaifa Hanif         | #20          |
| `report/`                    | all                   | —            |

- **R-2.1** Every folder above has a short `README.md` naming its owner and related issues.
  A new folder MUST get one in the same PR.
- **R-2.2** Code MUST go where §8.2 puts it. New top-level folders need an issue first.

## 3. Commands (run from the repo root)

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e .[dev]   # setup
scripts/check.sh                 # everything CI runs, locally (added by #29)
ruff check .                     # lint
ruff format --check .            # format check (ruff format . to fix)
pytest -q                        # all tests
pytest -q tests/unit/test_framing.py   # one file
meshsync --help                  # CLI smoke
bash -n scripts/*.sh             # shell syntax check
scripts/gen_certs.sh certs ahmed mujtaba huzaifa   # TLS certs (never commit)
scripts/gen_testdata.py testdata --sets A,D      # measurement data, #25 (never commit)
```

Not yet available: `scripts/run_local_mesh.sh` (added by #7),
`scripts/measure/` runners (added by #20).
`tests/integration/` is empty (no issue yet).

CI (`.github/workflows/ci.yml`, checks `test (3.11)` and `test (3.12)`) runs on every PR and push
to `main`: `pip install -e .[dev]`, `ruff check .`, `ruff format --check .`, `pytest -q`.

- **R-3.1** Run `scripts/check.sh` before pushing; it MUST pass.
- **R-3.2** Docs MUST NOT show a command that does not exist yet without "(added by #N)".

## 4. Coding standards

Python:
- **R-4.1** Python 3.11+, type hints on every function, `dataclasses` for records.
- **R-4.2** Runtime uses the standard library only (D15). Dev tools: `pytest`, `ruff` only.
- **R-4.3** `ruff` lint + format, line length 100. Names: `snake_case` functions/modules,
  `PascalCase` classes, `UPPER_CASE` constants.
- **R-4.4** Protocol/limit constants live only in `protocol/constants.py`; import them.
- **R-4.5** `logging.getLogger(__name__)` per module; no `print` (except CLI output).
- **R-4.6** Errors: raise specific exceptions (`ProtocolError(ErrorCode...)`, `ValueError`,
  `ConnectionError`); never a bare `except:`; never swallow an exception without logging it.
- **R-4.7** Threads: one reader thread per connection, sends under a lock, no blocking call while
  holding a lock. Every socket has a timeout; use `framing.read_exact`.
- **R-4.8** Tests: `pytest`, files `tests/unit/test_<module>.py`; no real network in unit tests
  (`socket.socketpair()`, `tmp_path`). Every bug fix adds a test that fails without the fix.

Bash:
- **R-4.9** `#!/usr/bin/env bash`, `set -euo pipefail`, a usage line, quote all variables.

## 5. Citing requirements

IDs: `R1`–`R7` (§2), `D1`–`D15` (§4), sections as `§6.3`, decisions as `PROTOCOL.md (a)`.

- **R-5.1** Code implementing a requirement SHOULD cite it in a comment: `# §6.5, D4`.
- **R-5.2** Commit and PR title: `<type>(<area>): <summary> [<IDs>]`, body ends with
  `Refs #<issue>`. Example: `feat(net): add HMAC handshake [D4, §6.5]`. Types: feat, fix, docs,
  test, refactor, chore, exp.
- **R-5.3** The PR lists its requirement IDs in the template.

## 6. Frozen interfaces

| Interface                                               | File                              | Owner   |
| ------------------------------------------------------- | --------------------------------- | ------- |
| Wire protocol (header, types, errors, handshake)        | `docs/PROTOCOL.md`                | Ahmed   |
| Constants, `MsgType`, `ErrorCode`                       | `src/meshsync/protocol/constants.py` | Ahmed |
| `send_msg` / `recv_msg`, `ProtocolError`                | `src/meshsync/protocol/framing.py`| Ahmed   |
| `Connection.request` / `on`                             | `src/meshsync/net/connection.py`  | Ahmed   |
| `PeerManager.peers`, `peer_up` / `peer_down`            | `src/meshsync/net/manager.py`     | Ahmed   |
| `Index.get/put/entries`, index entry (§7.1)             | `src/meshsync/sync/index.py`      | Mujtaba |
| `ChunkStore.has/get/put`                                | `src/meshsync/sync/store.py`      | Mujtaba |
| `Engine.on_remote_entry -> Action`                      | `src/meshsync/sync/engine.py`     | Mujtaba |
| `Scheduler.fetch`                                       | `src/meshsync/transfer/scheduler.py` | Huzaifa |
| `metrics.record`                                        | `src/meshsync/metrics/logger.py`  | Huzaifa |

- **R-6.1** Each file above starts with a `FROZEN INTERFACE` comment pointing here.
- **R-6.2** Changing a frozen interface (signature, behaviour, wire format; **adding counts**)
  needs an issue and approval from all three members **before** the code PR.
- **R-6.3** Implementing a stub to its stated contract is not a change.

## 7. Branches, PRs and reviews

- **R-7.1** One issue → one branch → one PR. Branch: `<type>/<issue>-<slug>`,
  e.g. `feat/4-hmac-handshake` (§9.2 type prefix + issue number).
- **R-7.2** PRs SHOULD stay under ~400 changed lines (tests and docs excluded). A bigger PR is
  fine when its description explains the work well enough to review it; otherwise split it.
- **R-7.3** CI MUST be green before asking for review. Fill in the whole PR template.
- **R-7.4** Reviewer is a different member than the author (CODEOWNERS requests the area owner;
  if the author is the owner, ask another member). Frozen interfaces and `PROTOCOL.md`: all three.
- **R-7.5** Squash merge only; the PR title becomes the commit. Branch up to date with `main`.
- **R-7.6** A push after approval needs a fresh approval. Resolve all conversations first.
- **R-7.7** Stacked branches: after the parent is squash-merged, `git rebase --onto origin/main
  <old parent tip> <branch>`; never merge `main` into it.
- **R-7.8** An AI agent MUST NOT approve, merge or close PRs or issues.

Who enforces what (org is on the free plan, see `docs/setup/github.md`):

| Rule                                         | Enforced by GitHub | Kept by team |
| -------------------------------------------- | ------------------ | ------------ |
| Squash only, delete branch on merge          | yes (repo setting) |              |
| CI runs on every PR                          | yes (runs; not required) |        |
| PR + 1 approval, CI green, no force push to `main` | no (ruleset not enforced) | yes |
| Reviewer ≠ author                            | yes (GitHub never lets authors approve) | |
| All-member approval for frozen interfaces    | no                 | yes          |
| One issue per PR, branch names, commit format, PR size | no       | yes          |
| AI never approves/merges                     | no                 | yes          |

## 8. Owners

| Name                | Roll No. | GitHub               | Areas                                   | Folders |
| ------------------- | -------- | -------------------- | --------------------------------------- | ------- |
| Muhammad Ahmed Qazi | CS-24045 | `Muhammad-Ahmed-Qazi`| discovery, connections, framing, auth, TLS, CLI | `protocol/`, `net/`, `__main__.py`, `config.py` |
| Mujtaba Jawaid Rao  | CS-24047 | `MujtabaJRao`        | scanner, index, chunking, engine, vclocks | `sync/` |
| Huzaifa Hanif       | CS-24039 | `Huzaifa-Hanif-01`   | transfer, metrics, measurement          | `transfer/`, `metrics/`, `scripts/measure/`, `analysis/` |

## 9. Done checklist (copied into the PR template)

- [ ] Linked to exactly one issue (`Closes #N`); stays within it.
- [ ] `scripts/check.sh` passes locally; CI green.
- [ ] Tests added/updated for the change.
- [ ] Requirement IDs cited in PR title, commits and code comments.
- [ ] No frozen interface changed, or all-member approval linked.
- [ ] `docs/PROTOCOL.md` updated if the protocol changed.
- [ ] No secrets, certs, keys or personal test data committed.
- [ ] AI use logged in `docs/AI_USE_LOG.md` (tool and model), or none.
- [ ] Author can explain every line.

## 10. Rules for AI agents

Follow these steps in order.

1. **R-10.1** Work only on the one issue you were assigned. No issue? Stop and ask.
2. **R-10.2** Read this file, the issue and the sections of `PROJECT_CONTEXT.md` it cites.
3. **R-10.3** Post a short plan (files, approach, tests) and wait for approval before code.
4. **R-10.4** Stay inside the issue. Other problems: list them as "Suggested issues" in the PR.
   No mass refactors, no reformatting unrelated files.
5. **R-10.5** No new dependencies unless the issue asks for them.
6. **R-10.6** Never edit `docs/PROJECT_CONTEXT.md`. Never change a frozen interface (§6).
7. **R-10.7** Never invent domain values (ports, timers, sizes, limits, message/error codes,
   crypto parameters, measured numbers, cited figures). Copy them from `PROJECT_CONTEXT.md` /
   `PROTOCOL.md` / `constants.py` with a citation, or stop and ask.
8. **R-10.8** Run `scripts/check.sh` before saying "done". Report every failure and anything you
   did not run. Never weaken, skip or delete a test to make it pass.
9. **R-10.9** In the PR, name the tool and model used, and add the `docs/AI_USE_LOG.md` entry.
10. **R-10.10** Never approve, merge or close a PR or issue.
