# gitgatesetup in one run: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new repo is set up by one `gitgatesetup <id>` run with one `Apply?`: create, settings, first push of `main`, ruleset. Every run ends by revoking its own token, and names the Actions secrets the user still has to add.

**Architecture:** `repo_setup` optionally takes the first `main` as a bundle and keeps it as an ordinary push handover (as a refused `push` does), recording its id in the setup. `gitgatesetup` shows that handover's screen in its plan and, on `y`, pushes it with the same code `gitgatepush` runs (the user's SSH key), then applies the ruleset in the same run. The device-flow tokens are revoked through GitHub's unauthenticated `POST /credentials/revoke`, then checked dead.

**Tech Stack:** Python 3.14 stdlib (`urllib`, `subprocess`), git, pytest.

**Spec:** *Design decisions* of `docs/superpowers/plans/2026-11-04-gitgatesetup.md`, as amended below. Smoke §12 results on `60195da` (2026-11-04) motivated it: every Expect held, but a new repo took three commands.

## Design decisions (amend the original plan's)

- **A1 (replaces 5). Order, in one run.** When the setup holds a first push, `gitgatesetup` applies, behind one `Apply?`: create the repo, description and Issues, topics, dependency graph, push `main`, ruleset. The ruleset is applied after the push lands, so its required checks never refuse it. Without a first push (or once its handover is gone), the ruleset is deferred as before and the user re-runs.
- **A2. The first push is a push handover.** `repo_setup(..., bundle=<path>)` keeps the bundle's commit exactly as a refused `push` keeps one: in the gate's mirror under `refs/gate/handover/<id>`, with the files under handover paths recorded. The setup records the handover id. `gitgatepush <id>` still works on it; whichever runs first pushes it. The branch is always `main`. The bundle must hold the complete history (no prerequisites), because the gate does not sync from a repo that may not exist yet.
- **A3. `gitgatesetup` never overwrites a branch.** If `main` exists on GitHub, the push item is `already set` (whatever its commit); the kept handover is left for `gitgatepush` and the ruleset applies.
- **A4 (amends 2). The token is revoked at the end of every run** that obtained one, however it ends: `POST https://api.github.com/credentials/revoke` with no `Authorization` header and `{"credentials": [access, refresh?]}`. The refresh token GitHub returns with the device flow is never used, only revoked. Then `GET /user` with the access token must answer 401 within the check window; only then does the run say the token is revoked.
- **A5 (amends 6). Secret reminders.** `repo_setup(..., secrets=[names])` records Actions secret names. `gitgatesetup` prints each with its settings link at the end of every run. It cannot read secrets (no `secrets` permission), so a reminder never keeps a setup open.
- **A6. No suspension.** Suspending an installation needs the app's JWT (private key), which is never kept; revocation (A4) is the control.

## Global Constraints

- Everything in the original plan's Global Constraints still holds.
- Secret names: 1–10 unique, each matching `^[A-Za-z_][A-Za-z0-9_]{0,99}$`, not starting with `GITHUB_` (any case). Shown upper-cased, as GitHub stores them.
- Secrets link: `https://github.com/<repo>/settings/secrets/actions/new`.
- Revoke endpoint: `POST https://api.github.com/credentials/revoke`, `Content-Type: application/json`, `Accept: application/vnd.github+json`, no `Authorization`. Success is 202.
- Revocation check: `GET /user` with the access token, up to 6 tries, 5 s apart; 401 means revoked.
- Revoked line: `Signed out: GitHub revoked this run's token.` Otherwise: `Warning: GitHub has not confirmed this run's token is revoked. Revoke it at https://github.com/settings/apps/authorizations` (with the cause first when there is one, e.g. `GitHub refused the revocation (422).`).
- Neither token appears in stdout, audit.log or errors.log (the existing `secrets` scrub covers both).

## Review Focus

1. **The new repo's default branch is not `main`** (the user's GitHub default-branch setting): after the push, the ruleset line must name the real default branch and defer, not apply to the wrong branch or crash. Test in Task 3.
2. **The push fails** (no SSH key, host key prompt, network): the setup stays open with `Re-run`, the ruleset is not applied, the handover stays open, and the token is still revoked. Test in Task 3.
3. **`main` already exists on GitHub** at another commit: nothing is pushed or overwritten; the ruleset applies (A3). Test in Task 3.
4. **The handover is gone** (pushed by `gitgatepush`, or expired) while `main` is absent: the line falls back to the old deferred text naming `gitgatepush`. Test in Task 3.
5. **Ctrl-C while revoking or checking**: the run prints the warning line and keeps its exit status; the audit line is still written. Test in Task 4.

---

### Task 1: Setup records carry a first push and secret names; `repo_setup` keeps the bundle

**Files:**
- Modify: `src/git_gate/setup.py` (`SetupRequest`, `check_secrets`, `Setups.create`, `_parse`)
- Modify: `src/git_gate/gate.py:512` (`Gate.repo_setup`)
- Modify: `src/git_gate/server.py:121,394` (instructions line, tool signature and docstring)
- Test: `tests/test_setup.py`

**Interfaces:**
- Produces:
  - `SetupRequest` gains `handover: int | None = None` and `secrets: tuple[str, ...] = ()` (last, with defaults, so records written by `60195da` still parse).
  - `setup.check_secrets(value: object) -> tuple[str, ...]`: `None` → `()`; else the Global Constraints rule, returned upper-cased; raises `Refused`.
  - `Setups.create(repo, description, topics, ruleset, *, handover: int | None = None, secrets: tuple[str, ...] = ()) -> SetupRequest`.
  - `Gate.repo_setup(self, rules, token, repo, description, topics, ruleset_file, bundle: str | None = None, secrets: list[str] | None = None) -> str`. Still no GitHub call. With a bundle: stage it as `push` does (`copy_in`), then under `mirror.lock(repo)`, without `sync`: `import_bundle(repo, staged, "main", rules)`; refuse unless `git bundle verify` reports a complete history; `protected_changes(repo, sha, None, None, repo_rules.handover_paths, rules)`; `Handovers.create(repo, "main", sha, None, None, "first push of main, part of a repo setup", patterns=..., protected=found.files, exclude=())`; `mirror.keep(repo, h.ref, sha, rules)`. The answer gains `It includes the first push of main (handover <h>).` and, with secrets, `It reminds the user to add the Actions secrets: <names>.`
  - The MCP tool `repo_setup` gains `bundle: str | None = None` and `secrets: list[str] | None = None`; its docstring documents both (bundle: `git bundle create <dir>/.tmp/main.bundle main`, complete history, branch main).

- [ ] **Step 1: Write the failing tests** in `tests/test_setup.py`:
  - `test_check_secrets`: `check_secrets(None) == ()`, `check_secrets(["release_token"]) == ("RELEASE_TOKEN",)`; refused with a message containing the fragment for `[]` ("1 to 10"), 11 names ("1 to 10"), `["GITHUB_X"]` ("GITHUB_"), `["9X"]` ("letters, digits and underscores"), `["A", "a"]` ("repeat").
  - `test_old_records_still_parse`: a record JSON without `handover`/`secrets` reads back with `handover is None` and `secrets == ()`.
  - `test_repo_setup_keeps_the_first_push`: a repo with one commit touching `.github/workflows/ci.yml`, bundled as `main` under the workspace; the answer contains `handover 1`; `Setups(state).get(1).handover == 1`; `Handovers(state).get(1)` has branch `main`, `base is None`, `protected == (".github/workflows/ci.yml",)`; the mirror holds the commit at `refs/gate/handover/1`; the fake GitHub saw no calls.
  - `test_repo_setup_bundle_needs_complete_history`: a bundle made with `main --not <first commit>` is refused with "complete history"; no setup and no handover recorded.
  - `test_repo_setup_bundle_must_hold_main`: a bundle of branch `feat` is refused (the `import_bundle` message).
  - `test_repo_setup_records_secrets`: `secrets=["RELEASE_TOKEN"]` → `get(1).secrets == ("RELEASE_TOKEN",)`, answer names it.

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_setup.py -v`
Expected: the six new tests FAIL (unknown argument / missing attribute).

- [ ] **Step 3: Implement** the Interfaces above. Complete history: `git bundle verify` stdout or stderr contains `complete history`; otherwise refuse `The first-push bundle must hold main's complete history; make it with: git bundle create <dir>/.tmp/main.bundle main`. Record the setup only after the handover is kept.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_setup.py tests/test_server.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/git_gate/setup.py src/git_gate/gate.py src/git_gate/server.py tests/test_setup.py
git commit -m "feat: repo_setup keeps the first push of main and secret names"
```

### Task 2: One push routine shared by `gitgatepush` and `gitgatesetup`

**Files:**
- Modify: `src/git_gate/handover.py:257` (split `_run`)
- Test: `tests/test_handover.py`

**Interfaces:**
- Produces:
  - `handover.screen(h: Handover, mirror: Mirror, rules: Rules) -> str`: `_check`, then under the mirror lock `_require_commit` and `_show`, plus the `Run: <command>` line. What `_run` prints today before the prompt.
  - `handover.push_kept(handovers: Handovers, id: int, mirror: Mirror, rules: Rules, say) -> None`: the part of `_run` after `y` (re-read, `_check`, push, close, audit, drop). Raises `Refused` with today's `git push failed (exit N). Handover N is still open.` text on failure (after printing git's output). Returns after `Pushed. Handover N is closed.`
  - `_run` becomes: get, `say(screen(...))`, prompt, `push_kept(...)`. Its output and exit codes are unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_handover.py`:
  - `test_push_kept_pushes_and_closes`: with a staged handover (existing `stage` helper) and a bare remote, `push_kept(hs, h.id, m, rules, say)` puts the commit on the remote branch, closes the record and writes a `pushed` audit line.
  - `test_push_kept_failure_raises_and_keeps_open`: remote missing → `Refused` containing `is still open`; the record is still open.
  - `test_screen_matches_the_gitgatepush_screen`: `screen(...)` equals the text `gitgatepush <id>` prints before `Push it?`.

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_handover.py -v`
Expected: the three new tests FAIL with `AttributeError`.

- [ ] **Step 3: Implement** the split. No behaviour change in `gitgatepush`.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_handover.py -v`
Expected: PASS, the existing tests included.

- [ ] **Step 5: Commit**

```bash
git add src/git_gate/handover.py tests/test_handover.py
git commit -m "refactor: share the kept-push screen and push between gitgatepush and gitgatesetup"
```

### Task 3: `gitgatesetup` pushes `main` and applies the ruleset in the same run; secret reminders

**Files:**
- Modify: `src/git_gate/setupcli.py` (`main`, `_setup`, `_survey`, `_ruleset`, new `_first_push`, `_secrets`)
- Test: `tests/test_setupcli.py`

**Interfaces:**
- Consumes: Task 1's `SetupRequest.handover`/`.secrets`; Task 2's `screen` and `push_kept`.
- Produces:
  - `setupcli.main(argv, *, stdin, stdout, login=device_login, github=GitHub, kept: KeptPushes | None = None) -> int`.
  - `class KeptPushes(Protocol)`: `open(id: int) -> bool`, `screen(id: int) -> str`, `push(id: int, say) -> None`. The default wraps `Handovers(state)`, `Mirror(Git(state), state)`, `RULES`, Task 2's functions (`open` is `get` not raising).
  - Survey item `main`, between `dependency graph` and `ruleset`:
    - branch exists → `SET` (A3).
    - absent and `kept.open(s.handover)` → line `absent → push <sha12> (handover <h>; shown below)`, apply `kept.push(h, say)`. Before `Apply?`, print `kept.screen(h)` under a blank line.
    - otherwise → no item; the ruleset keeps today's deferred text.
  - Ruleset with a pending `main` item: line `absent → "<name>" after main is pushed; rules: <rules>`, apply = `_apply_ruleset(gh, s) -> None`, which reads the repo again and writes nothing when its default branch has no commits (e.g. the default is `master` and only `main` was pushed), else PUTs over a same-name ruleset or POSTs (the lookup `_ruleset` does today, moved into a shared helper). Either way the re-survey after applying decides what is left, so a skipped ruleset shows under `Not done yet:` with today's deferred text naming the real default branch.
  - Repository line for an absent repo, and every repo-absent survey, treats `main` as absent.
  - `_secrets(s, say)`: when `s.secrets`, after the last line of every run that reached the survey: `Add these Actions secrets yourself on GitHub (gitgatesetup cannot check them):` then `  <NAME>: https://github.com/<repo>/settings/secrets/actions/new` per name.

- [ ] **Step 1: Write the failing tests** in `tests/test_setupcli.py`, with a `FakeKept` (records `push` calls; `push` makes `FakeGitHub.has_branch = True`) and `FakeGitHub` extended with a settable `default_branch` (default `"main"`):
  - `test_fresh_repo_with_first_push_closes_in_one_run`: repo absent, `handover=7`. Output order: `repository`, …, `main: absent → push`, `ruleset: absent → "Protect main" after main is pushed`, the kept screen, `Apply?`; calls in order `create_user_repo`, `set_topics`, `enable_dependency_graph`, push 7, `create_ruleset`; ends `Done. Setup 1 is closed.`, exit 0.
  - `test_existing_main_is_never_pushed` (Review Focus 3): `has_branch=True`, `handover=7` → `main` not listed as a change, no push call, ruleset created.
  - `test_failed_push_keeps_the_setup_open` (Review Focus 2): `FakeKept.push` raises `Refused("git push failed (exit 128). Handover 7 is still open.")` → that text, `Setup 1 stays open. Re-run: gitgatesetup 1`, no ruleset call, exit 1.
  - `test_default_branch_not_main_defers_the_ruleset` (Review Focus 1): after the push the fake reports `default_branch="master"` → no ruleset call, `Not done yet:` names `master`, exit 1.
  - `test_gone_handover_falls_back_to_deferred` (Review Focus 4): `FakeKept.open` false, branch absent → today's `deferred: … push it (gitgatepush)` line, no `main` item.
  - `test_secrets_are_reminded_at_the_end`: `secrets=("RELEASE_TOKEN",)` → the last lines are the reminder with `https://github.com/me/thing/settings/secrets/actions/new` (use the module's `REPO`), on a closing run and on a declined run.
  - `test_no_first_push_behaves_as_before`: the existing `test_fresh_repo_full_run` still passes unchanged.

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_setupcli.py -v`
Expected: the six new tests FAIL.

- [ ] **Step 3: Implement** the Interfaces above.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_setupcli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/git_gate/setupcli.py tests/test_setupcli.py
git commit -m "feat: gitgatesetup pushes the first main and applies the ruleset in the same run"
```

### Task 4: Revoke the run's tokens

**Files:**
- Modify: `src/git_gate/devicelogin.py` (`device_login` return type, new `Login`, `revoke`)
- Modify: `src/git_gate/setupcli.py` (`_run`, `_setup`, `_audit`)
- Test: `tests/test_devicelogin.py`, `tests/test_setupcli.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Login: token: str; refresh: str | None` (`repr=False` on both fields).
  - `device_login(...) -> Login`; `refresh` is the answer's `refresh_token` when it is a non-empty string.
  - `setupcli.main` gains `revoke=devicelogin.revoke` (injected in tests).
  - `devicelogin.revoke(login: Login, *, alive: Callable[[str], bool], post=_post_json, sleep=time.sleep, say) -> bool`: POST per Global Constraints; on 202, up to 6 `alive(token)` checks 5 s apart (sleep before each); `True` and the revoked line when one answers dead; otherwise the warning line (cause first) and `False`. A `KeyboardInterrupt` inside counts as not confirmed.
  - In `setupcli`, `alive = lambda t: <github(t).viewer() succeeds; GitHubError 401 → False>`. `_run` calls `revoke` in its `finally`, before `_audit`, whenever a login completed; `_audit` gains `"revoked": true|false|null` (null when there was no login).

- [ ] **Step 1: Write the failing tests:**
  - `tests/test_devicelogin.py::test_login_keeps_the_refresh_token`: the token answer has `refresh_token: "ghr_x"` → `Login("ghu_x", "ghr_x")`; `repr(login)` contains neither.
  - `test_revoke_posts_both_without_auth`: fake `post` sees URL `https://api.github.com/credentials/revoke`, body `{"credentials": ["ghu_x", "ghr_x"]}`, no `Authorization` header; `alive` false on the first check → `True`, the revoked line.
  - `test_revoke_still_alive_warns`: `alive` always true → 6 checks, the warning line, `False`.
  - `test_revoke_refused_warns_with_cause`: post answers 422 → `GitHub refused the revocation (422).` then the warning, no `alive` call.
  - `test_revoke_interrupted_warns` (Review Focus 5): `sleep` raises `KeyboardInterrupt` → warning, `False`, no exception.
  - `tests/test_setupcli.py::test_every_run_revokes`: closing, declined, refused-after-login (owner mismatch) and failed-write runs each call the fake `revoke` once with the fake login; the audit line has `"revoked": true`.
  - `test_no_login_no_revoke`: unknown setup id → `revoke` not called; audit `"revoked": null`.
  - Existing `test_token_never_printed` extended: the refresh token is never printed either.

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_devicelogin.py tests/test_setupcli.py -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement** the Interfaces above. The test `Login` fake in `tests/test_setupcli.py` returns a `devicelogin.Login`.

- [ ] **Step 4: Run the whole suite and the linters**

Run: `pytest -q && ruff check . && ruff format --check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/git_gate/devicelogin.py src/git_gate/setupcli.py tests/test_devicelogin.py tests/test_setupcli.py
git commit -m "feat: gitgatesetup revokes its token at the end of every run"
```

### Task 5: Docs

**Files:**
- Modify: `docs/superpowers/plans/2026-11-04-gitgatesetup.md` (decisions 2, 5 and 6 each gain `Amended: see 2026-11-04-gitgatesetup-one-run.md A<n>.`)
- Modify: `docs/design.md` (*Repo setup* section: A1–A6)
- Modify: `docs/smoke.md` §12

- [ ] **Step 1: Re-cut smoke §12** for one run, on a new scratch repo `PineappleEmperor/smoke-setup-2`, with the output wording the code prints (the 60195da run differed in two lines: the repository line's `(or not visible to the setup app)` and the ruleset's `the default branch`). Steps: register (skip if registered); agent `repo_setup` with the ruleset, a complete-history `main` bundle containing `.github/workflows/ci.yml`, and `secrets=["RELEASE_TOKEN"]`; user runs `gitgatesetup <id>` once. Expect: one sign-in, the kept screen listing the workflow file, one `Apply?`, `Done. Setup <id> is closed.`, exit 0, the secrets reminder, `Signed out: GitHub revoked this run's token.`, and a GitHub notification that a credential was revoked. Keep the re-run, `already set`, `grep ghu_` and step 8 checks; add: `gitgatepush` with no id lists no handover for the pushed `main`; the repo's default branch is `main`.
- [ ] **Step 2: Commit**

```bash
git add docs
git commit -m "docs: gitgatesetup in one run, token revocation and secret reminders"
```
