# Contributing

These rules are the team agreement (`docs/PROJECT_CONTEXT.md` §9). They are binding whether or not
GitHub enforces them (see `docs/setup/github.md`).

## Issues and branches

- One issue per branch, one branch per PR. Link the issue in the PR (`Closes #<n>`).
- Branch names: `feat/<area>-<short>`, `fix/<area>-<short>`, `docs/<short>`, `exp/<short>`
  (measurements). Example: `feat/net-hmac-handshake`.
- Never push directly to `main`; never force-push to `main`.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`, e.g.
`feat(net): add HMAC handshake`, `fix(sync): reject paths containing ..`, `docs: update PROTOCOL.md`.
The **PR title** must follow this format too: it becomes the commit message on `main`.

## Pull requests and reviews

- Small, one concern. Fill in the PR template (what changed, how tested, protocol impact, AI use).
- CI (`test (3.11)`, `test (3.12)`: ruff + pytest) must pass.
- At least **1 approval from a member other than the author**. CODEOWNERS requests the area owner
  automatically; when the author *is* the owner, ask another member.
- **Changes to `docs/PROTOCOL.md` or the §8.3 module contracts need approval from all three
  members.** GitHub cannot enforce this; the reviewer checks it before merging.
- Protocol changes update `docs/PROTOCOL.md` in the same PR.
- All review conversations must be resolved before merging.
- Any push after an approval dismisses it: expect to re-approve after fixes.
- AI-assisted work is logged in `docs/AI_USE_LOG.md`, and the author must be able to explain it.

## Merging

- **Squash merge only.** The PR title and description become the commit on `main`.
- The branch must be up to date with `main` (use "Update branch" or rebase).
- The head branch is deleted automatically after merging.

### Stacked branches

If branch `child` was built on `parent` and `parent` has been squash-merged, do **not** merge
`main` into `child` (it conflicts with the squashed copy). Rebase only `child`'s own commits:

```bash
git fetch origin
git rebase --onto origin/main <old parent tip> child
git push --force-with-lease
```

`<old parent tip>` is the last commit of `parent` before it was merged (e.g. from `git log child`).
Force-pushing is fine on your own feature branch, never on `main`.
