# GitHub repository setup

Repository: `Silicon-Triad/meshsync` (private, organisation on the **free** plan).
Implements `docs/PROJECT_CONTEXT.md` §8.1 (protect `main`: PR + 1 approval + CI, no force push;
squash merge only; delete branch on merge). Team rules for contributors: `CONTRIBUTING.md`.

## For a repository admin

> **Ruleset defined but not enforced (free org plan). The rules below are binding as team rules.**
>
> Classic branch protection on a private repo returns HTTP 403 ("Upgrade to GitHub Pro or make
> this repository public"). Rulesets can be saved but are not enforced. To check: open
> Settings → Rules → Rulesets → `main`. A banner saying the ruleset **won't be enforced** means
> it is not active; `CONTRIBUTING.md` is then the only safeguard. If the org is upgraded and the
> banner disappears, remove this note.

### 1. Pull request settings

Settings → General → scroll to **Pull Requests**. Confirm:

- [ ] Allow merge commits: **off**
- [ ] Allow squash merging: **on**, default message **Pull request title and description**
- [ ] Allow rebase merging: **off**
- [ ] Always suggest updating pull request branches: **on**
- [ ] Automatically delete head branches: **on**

### 2. Collaborators

Settings → Collaborators and teams → Add people (or change role):

- [ ] `MujtabaJRao`: **Write**
- [ ] `Huzaifa-Hanif-01`: **Write**

(Not Admin, so only the admin can change these settings.)

### 3. Ruleset for `main`

Settings → Rules → Rulesets → **New ruleset** → **New branch ruleset**:

- [ ] Ruleset name: `main`
- [ ] Enforcement status: **Active**
- [ ] Bypass list: **empty** (or only the admin, set to *For pull requests only*, as an emergency exit)
- [ ] Target branches → Add target → **Include default branch**
- [ ] Restrict deletions: **on**
- [ ] Require linear history: **on**
- [ ] Require a pull request before merging: **on**, then expand it:
  - Required approvals: **1**
  - Dismiss stale pull request approvals when new commits are pushed: **on**
  - Require review from Code Owners: **off** (see lesson 1)
  - Require approval of the most recent reviewable push: **on**
  - Require conversation resolution before merging: **on**
- [ ] Require status checks to pass: **on**, then:
  - Require branches to be up to date before merging: **on**
  - Add checks: **`test (3.11)`** and **`test (3.12)`** (job `test`, matrix `python-version` in
    `.github/workflows/ci.yml`; rename here if the workflow changes)
- [ ] Block force pushes: **on**
- [ ] Click **Create**, then check for the "won't be enforced" banner (note above).

## Lessons from the previous project

1. **Leave "Require review from Code Owners" off.** Each area has one owner and GitHub never lets
   authors approve their own PR, so with it on an owner's PR in their own area can never merge.
   `.github/CODEOWNERS` is kept because it still requests the right reviewer automatically.
2. **CODEOWNERS cannot require everyone's approval.** When a line lists several people, any one
   approval satisfies it. "All members approve `docs/PROTOCOL.md` and §8.3 contract changes" is
   a written team rule in `CONTRIBUTING.md` (a CI check counting approvals would be the only way
   to enforce it). Don't claim CODEOWNERS enforces it.
3. **Squash merging + stacked branches conflict.** After a parent PR is squash-merged, rebase the
   child with `git rebase --onto origin/main <old parent tip> <branch>` instead of merging `main`
   into it (`CONTRIBUTING.md` → Stacked branches).
4. **Stale approvals are dismissed.** Any push after a review needs a fresh approval, so reviewers
   should expect to re-approve.
