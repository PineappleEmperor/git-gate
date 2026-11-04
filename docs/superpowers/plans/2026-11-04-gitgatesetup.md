# gitgatesetup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent on the gate path request a new repository's GitHub settings (creation, description, topics, Issues, dependency graph, branch ruleset), and let the user apply them programmatically with `gitgatesetup <id>`, authorised by a one-off browser login, with no `gh`, no stored admin token and no settings pages.

**Architecture:** A new `repo_setup` MCP tool validates the request and stores it as a *setup handover* under the gate's state directory, the same shape as a refused push. `gitgatesetup <id>`, run by the user outside the sandbox, gets a short-lived user token through GitHub's device flow for a private GitHub App the user registers once. It shows the difference from the live settings, asks, applies the changes over the REST API, re-reads them, and closes the handover only when everything has taken. The token lives in that process's memory only. The gate's own token and the MCP server never gain the Administration permission.

**Tech Stack:** Python 3.14, stdlib `urllib`, `http.server` and `webbrowser`, `mcp==2.1.1` (existing), pytest. No new dependency.

**Spec:** the design agreed in conversation on 2026-11-04. Its decisions are recorded under *Design decisions* below, which executors treat as the spec.

## Design decisions

1. **No `gh` anywhere** on the gate path. Every GitHub call goes through `git_gate.github.GitHub` (`urllib`).
2. **Credential:** a GitHub App user-to-server token from the OAuth **device flow**, requested at each `gitgatesetup <id>` run. It is never written to disk, logged or printed, and it expires after GitHub's 8 hours. There is no revocation call, because that needs the app's client secret, which is never kept.
3. **App:** one private GitHub App per user. It is registered once by `gitgatesetup --register` through the app-manifest flow and installed on "All repositories". Its repository permissions are `administration: write` and `metadata: read`, and nothing else. It has no webhook. Only `client_id`, `slug` and `owner` are kept, in `~/.config/git-gate/setup-app.json`. The manifest conversion's client secret, private key and webhook secret are dropped unread.
4. **The gate's own token is unchanged.** `repo_setup` never calls GitHub. It validates and stores the request.
5. **Order:** a repo is created before its first push. The ruleset is applied only once the default branch exists on GitHub, because its required checks would otherwise refuse the push that creates `main`. A setup whose ruleset is deferred stays open, and the user re-runs `gitgatesetup <id>` after the main push lands.
6. **Actions secrets are out of scope.** A secret such as `RELEASE_TOKEN` is a PAT the user creates on GitHub by hand anyway, so they paste it into the repo's secret page there. Routing it through a terminal adds a step and a permission (`secrets: write`) for nothing. The skill hands the user the page's link: `https://github.com/<repo>/settings/secrets/actions/new`.
7. **The user's personal account only:** the repo owner must equal the device-login user's `login`. Organisation repos are refused with a message.
8. **Local clone settings (`core.hooksPath`) are out of scope.** `gitgatesetup` never touches an agent's working tree, the same rule `gitgatepush` keeps.
9. **The plain (`gh`) path in ha-skills keeps `scripts/bootstrap_repo.sh`.** Only the gate path moves to `repo_setup` → `gitgatesetup`.

## Global Constraints

- Python floor `py314`; ruff line length 120; tests run with `pytest` from the repo root (`pythonpath = ["src", "tests"]`).
- No new runtime dependency.
- Setup handovers expire after `EXPIRES = 14 * 24 * 3600` seconds, the same as push handovers.
- Description: one printable line, at most 350 characters.
- Topics: 1–20, unique, each matching `^[a-z0-9][a-z0-9-]{0,49}$`.
- Ruleset file: absolute path inside a workspace, named `*.json`, no dot directory in the path except `.tmp`, at most 64 KiB.
- App name in the manifest: `git-gate setup <6 lowercase hex>`, at most 34 characters.
- Device flow endpoints: `POST https://github.com/login/device/code` and `POST https://github.com/login/oauth/access_token` with `grant_type=urn:ietf:params:oauth:grant-type:device_code` and `Accept: application/json`.
- Manifest flow: form `POST https://github.com/settings/apps/new?state=<state>`, then `POST https://api.github.com/app-manifests/{code}/conversions`.
- The device-flow token must never appear in stdout, stderr, `audit.log` or `errors.log`. Every refusal text goes through the same scrub the gate uses for its own token.
- Launchers live in `bin/`, resolve their installed release exactly as `bin/gitgatepush` does, and are linked by `install.sh`.

## Review Focus

1. **The default branch doesn't exist yet** (fresh repo, nothing pushed). The ruleset is reported as deferred, everything else applies, and the handover stays open with a "re-run after `gitgatepush`" line. It never POSTs a ruleset that blocks the first push. *Test: Task 5, `test_ruleset_deferred_until_default_branch_exists`.*
2. **Re-running a finished or half-finished setup.** Every step is idempotent: an equal description or topic list is shown as "already set" and not re-sent, and a ruleset named the same is replaced with PUT, never duplicated. *Test: Task 5, `test_second_run_changes_nothing_already_applied`.*
3. **The user declines or abandons the device login** (`access_denied`, `expired_token`, Ctrl-C). It exits 1 with a plain message and leaves the handover open, with nothing applied and no traceback. *Test: Task 3, `test_denied_and_expired_end_cleanly`.*
4. **The app isn't installed on the repo, or lacks a permission** (403 or 404 from an admin endpoint). The message names the install URL `https://github.com/apps/<slug>/installations/new`. It isn't GitHub's raw text alone. *Test: Task 5, `test_forbidden_names_the_install_page`.*
5. **The agent hands in a ruleset that weakens protection** (non-empty `bypass_actors`, `enforcement` other than `active`, no required checks). `repo_setup` refuses it before anything is stored. *Test: Task 1, `test_ruleset_refusals`.*

---

### Task 1: Setup request validation and the setup handover store

**Files:**
- Create: `src/git_gate/setup.py`
- Modify: `src/git_gate/handover.py` (extract id allocation from `Handovers.create`)
- Modify: `src/git_gate/paths.py:68` (`EXCHANGE` gains `"ruleset": ("Ruleset file", (".json",))`)
- Test: `tests/test_setup.py`

**Interfaces:**
- Produces:
  - `handover.allocate_id(dir: Path, highest: int) -> int`: the flock-guarded counter logic now inside `Handovers.create`, moved verbatim. `Handovers.create` calls it.
  - `setup.SetupRequest` (frozen dataclass): `id: int, repo: str, description: str, topics: tuple[str, ...], ruleset: dict, created: float`
  - `setup.check_description(value: object) -> str`, `setup.check_topics(value: object) -> tuple[str, ...]`, `setup.check_ruleset(data: bytes, path: str) -> dict`. Each raises `Refused` with the reason.
  - `setup.Setups(state: Path, clock=time.time)` with `create(repo, description, topics, ruleset) -> SetupRequest`, `get(id) -> SetupRequest`, `open() -> list[SetupRequest]` and `close(id) -> None`. Records go in `<state>/setups/<id>.json` (dir mode 0o700, files 0o600), with expiry purge as in `Handovers`.

- [ ] **Step 1: Write the failing tests**

`test_ruleset_refusals` is parametrised over a valid base copied from ha-skills' `templates/ruleset.json` and mutated. Each mutation is refused with a message containing the quoted fragment:
  - `bypass_actors: [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]` → `"bypass_actors"`
  - `enforcement: "evaluate"` → `"enforcement"`
  - `target: "tag"` → `"target"`
  - `conditions.ref_name.include: ["refs/heads/dev"]` → `"~DEFAULT_BRANCH"`
  - no `required_status_checks` rule, or one with an empty `required_status_checks` list → `"required status check"`
  - an unknown top-level key `"source"` → `"source"`
  - not JSON, or a JSON list → `"JSON object"`
  - 65 537 bytes → `"64 KiB"`

Other tests:
- `test_ruleset_accepts_template`: the unmutated template is returned as an equal dict.
- `test_topics`: `["home-assistant","hacs"]` → tuple. Refused: `[]`, 21 items, `"Has-Upper"`, `"-lead"`, 51 characters, and a duplicate.
- `test_description`: 350 characters accepted. 351 characters, `"a\nb"` and `""` are refused.
- `test_store_roundtrip_and_expiry`: `create` then `get` gives an equal record. Once the clock is past `EXPIRES`, `get` raises `Refused("No open setup <id>.")`. `close` removes the record. Ids rise across `create` calls and never reuse a closed id.
- In `tests/test_handover.py`, the existing tests must stay green after the extraction.

- [ ] **Step 2: Run** `pytest tests/test_setup.py -v`. Expected: FAIL (`ModuleNotFoundError: git_gate.setup`).
- [ ] **Step 3: Implement** `setup.py` and the `allocate_id` extraction. Ruleset keys allowed at top level: `name`, `target`, `enforcement`, `conditions`, `rules`, `bypass_actors`. `name` must be a non-empty `_line` of at most 100 characters.
- [ ] **Step 4: Run** `pytest tests/test_setup.py tests/test_handover.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: validate and store repo setup requests`

---

### Task 2: The `repo_setup` MCP tool

**Files:**
- Modify: `src/git_gate/call.py:21` (`TOOLS` gains `"repo_setup"`)
- Modify: `src/git_gate/gate.py` (method `repo_setup`, beside `label`)
- Modify: `src/git_gate/server.py` (register the tool before `return server`)
- Modify: `src/git_gate/server.py` `INSTRUCTIONS` (one line: repo settings go through `repo_setup`, and the user runs `gitgatesetup <id>`)
- Test: `tests/test_setup.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: Task 1's `check_*`, `Setups`, and `paths.read_workspace_file(rules, path, "ruleset")`.
- Produces: `Gate.repo_setup(self, rules, token, repo: str, description: str, topics: list[str], ruleset_file: str) -> str`. It requires write access (`rules.require_write(repo)`), makes no GitHub call, and returns exactly:
  `Setup {id} recorded for {repo}. Ask the user to run, in their own terminal:\n\ngitgatesetup {id}\n\nIt signs them in to GitHub in the browser, shows what will change, and applies it. Do not retry.`

- [ ] **Step 1: Write the failing tests**
  - `test_repo_setup_records_and_answers`: through `Gate.dispatch("repo_setup", {...})` with the template ruleset under a workspace, the answer contains `gitgatesetup 1`, `Setups(state).get(1).topics == ("home-assistant", "hacs")`, and the fake GitHub saw no calls.
  - `test_repo_setup_needs_write`: on a read-only session it's refused with the same text `require_write` gives elsewhere.
  - `test_repo_setup_ruleset_outside_workspace`: `/etc/passwd.json` is refused.
  - In `test_server.py`, the existing tool-list test includes `repo_setup`.
- [ ] **Step 2: Run** `pytest tests/test_setup.py tests/test_server.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** The tool docstring states each argument's rules from *Global Constraints*, and that the repository does not need to exist yet.
- [ ] **Step 4: Run** `pytest -q`. Expected: PASS (whole suite: tool lists are checked in several places).
- [ ] **Step 5: Commit** `feat: add the repo_setup tool`

---

### Task 3: Device-flow login

**Files:**
- Create: `src/git_gate/devicelogin.py`
- Test: `tests/test_devicelogin.py`

**Interfaces:**
- Produces: `device_login(client_id: str, *, say: Callable[[str], None], open_url: Callable[[str], object] = webbrowser.open, post: Callable[[str, dict], dict] | None = None, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> str`. It returns the access token or raises `Refused`. `post(url, form) -> dict` defaults to a `urllib` form POST with `Accept: application/json`. Tests inject a fake.

- [ ] **Step 1: Write the failing tests** (fake `post` and a fake clock that advances on `sleep`):
  - `test_happy_path`: the first response has `user_code "ABCD-1234"`, `verification_uri "https://github.com/login/device"`, `interval 5` and `expires_in 900`. Polls answer `authorization_pending`, then `{"access_token": "ghu_x", "token_type": "bearer"}`. It returns `"ghu_x"`. `say` received the user code and the URI. `open_url` was called with the URI. It slept 5 seconds between polls.
  - `test_slow_down_adds_five_seconds`: after `slow_down`, the next sleep is the previous interval plus 5.
  - `test_denied_and_expired_end_cleanly`: `access_denied` gives `Refused` containing `"declined"`. `expired_token`, or the clock passing `expires_in`, gives `Refused` containing `"expired"`. Neither message contains a token.
  - `test_open_url_failure_is_not_fatal`: `open_url` raising `webbrowser.Error` still lets the login complete.
  - `test_token_never_said`: no `say` call contains `"ghu_x"`.
- [ ] **Step 2: Run** `pytest tests/test_devicelogin.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: sign in through GitHub's device flow`

---

### Task 4: One-time app registration (`gitgatesetup --register`)

**Files:**
- Create: `src/git_gate/appreg.py`
- Test: `tests/test_appreg.py`

**Interfaces:**
- Produces:
  - `AppConfig` (frozen dataclass): `client_id: str, slug: str, owner: str`
  - `load_app(cfg: Path | None = None) -> AppConfig`: reads `config_dir()/"setup-app.json"`. If it's missing, it raises `Refused("No setup app registered; run: gitgatesetup --register")`.
  - `manifest(name: str, redirect_url: str) -> dict`: returns `{"name", "url": "https://github.com/PineappleEmperor/git-gate", "redirect_url", "public": false, "default_permissions": {"administration": "write", "metadata": "read"}, "default_events": [], "request_oauth_on_install": false}` with no `hook_attributes`.
  - `register(*, say, open_url=webbrowser.open, convert: Callable[[str], dict] | None = None, serve: Callable[[str, str], str] | None = None, cfg: Path | None = None) -> AppConfig`. `serve(form_html, state)` runs a one-shot `http.server` on `127.0.0.1:0`. It serves an auto-submitting form that POSTs the manifest to GitHub, waits for `/done?code=…&state=…`, checks `state` with `secrets.compare_digest`, and returns `code`. `convert(code)` POSTs to `app-manifests/{code}/conversions`. `register` writes `setup-app.json` with mode 0o600, keeping only `client_id`, `slug` and `owner.login`. It then says and opens `https://github.com/apps/<slug>/installations/new`, and says the one-time manual step the live smoke confirms (see Task 7).
- [ ] **Step 1: Write the failing tests**
  - `test_manifest_permissions_exact`: `default_permissions` equals the two keys above exactly, and the result has no `hook_attributes` key.
  - `test_register_keeps_only_public_fields`: fake `serve` returns `"c0de"`. Fake `convert` returns a body with `client_id`, `slug`, `owner.login`, `client_secret`, `pem` and `webhook_secret`. The written JSON has exactly `{client_id, slug, owner}`, its mode is `0o600`, and none of the three secret values appears in the file or in any `say` output.
  - `test_state_mismatch_refused`: the `serve` helper given a callback with the wrong `state` raises `Refused` containing `"state"`.
  - `test_load_app_missing`: the refusal names `gitgatesetup --register`.
- [ ] **Step 2: Run** `pytest tests/test_appreg.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: register the setup app from a manifest`

---

### Task 5: The admin calls and the `gitgatesetup` command

**Files:**
- Modify: `src/git_gate/github.py` (methods below, on `GitHub`)
- Create: `src/git_gate/setupcli.py`
- Modify: `src/git_gate/__main__.py` (`setup` → `setupcli.main(sys.argv[2:])`)
- Create: `bin/gitgatesetup` (copy of `bin/gitgatepush`, launching `-m git_gate setup`)
- Modify: `install.sh:118` (the link loop gains `gitgatesetup`)
- Test: `tests/test_github.py`, `tests/test_setupcli.py`, `tests/test_install.py`

**Interfaces:**
- Consumes: `Setups`, `SetupRequest` (Task 1); `device_login` (Task 3); `load_app`, `AppConfig` (Task 4).
- Produces, on `GitHub`:
  - `viewer() -> str` (login)
  - `repo(repo) -> dict | None`
  - `create_user_repo(name, description) -> None` (`has_issues: true`)
  - `edit_repo(repo, *, description=None, has_issues=None) -> None`
  - `topics(repo) -> list[str]`, `set_topics(repo, names) -> None`
  - `dependency_graph_on(repo) -> bool`, `enable_dependency_graph(repo) -> None`
  - `rulesets(repo) -> list[dict]`, `create_ruleset(repo, body) -> int`, `update_ruleset(repo, id, body) -> None`
  - `branch_rule_rulesets(repo, branch) -> list[dict]` (the `required_status_checks` entries of `rules/branches/{branch}`)
- Produces: `setupcli.main(argv, *, stdin=sys.stdin, stdout=sys.stdout, login=device_login, github=GitHub) -> int`. With no argument it lists open setups. `--register` calls `register`. `<id>` runs the flow below.

**Flow for `gitgatesetup <id>`**, in this order:
1. Load the record and the app config.
2. Run the device login and build `GitHub(token)`.
3. Check the repo owner equals `viewer()`.
4. Read the live state and print one line per item, `<item>: <current> → <wanted>`, or `already set`, or `deferred: <reason>`. Ask `Apply? [y/N]`.
5. Apply in this order: create the repo, then description and Issues, topics, dependency graph, and the ruleset (PUT over the same-name Repository-sourced branch ruleset, else POST; skipped while the default branch has no commits).
6. Re-read and verify every item.
7. Report other rulesets that also require checks on the default branch, with their settings URL. Don't delete them.
8. Close the record only if every item is verified, and say `Done. Setup <id> is closed.`. Otherwise list what remains and say `Re-run: gitgatesetup <id>`.
9. Write one `audit.log` line per run (`tool: "gitgatesetup"`, `id`, `repo`, and `outcome` set to `closed`, `open` or `declined`), with no token.

- [ ] **Step 1: Write the failing tests**
  - In `test_github.py`, one test per new method against the existing fake HTTP layer: method, path, and JSON body.
  - In `test_setupcli.py`, a fake `GitHub` records calls and the login is faked to return `"ghu_test"`:
    - `test_fresh_repo_full_run`: the repo is missing, the user answers `y`. Calls happen in the order above, the ruleset is deferred, the record stays open, and stdout contains `Re-run: gitgatesetup 1`.
    - `test_ruleset_deferred_until_default_branch_exists`: the default branch is missing, so there's no ruleset POST. When the second run sees the branch, the ruleset is POSTed and the record closes.
    - `test_second_run_changes_nothing_already_applied`: with everything equal, there are no write calls and `already set` is printed for each item.
    - `test_same_name_ruleset_replaced`: an existing `Protect main` (Repository, branch) gets PUT, not POST.
    - `test_decline_applies_nothing`: answering `n` gives no write calls, exit 1, and the record stays open.
    - `test_owner_mismatch_refused`: when the owner is `SomeOrg` and the viewer is `me`, it's refused before any write.
    - `test_forbidden_names_the_install_page`: when the fake raises `GitHubError(..., 403, ...)` on a write, stdout contains `https://github.com/apps/<slug>/installations/new` and the record stays open.
    - `test_token_never_printed`: `"ghu_test"` doesn't appear in stdout or `audit.log`.
  - In `test_install.py`, extend the existing link test so `gitgatesetup` is linked like `gitgatepush`.
- [ ] **Step 2: Run** `pytest tests/test_github.py tests/test_setupcli.py tests/test_install.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` and `ruff check . && ruff format --check .`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: apply repo setups with gitgatesetup`

---

### Task 6: Document it

**Files:**
- Modify: `docs/design.md`: add `repo_setup` to the Tools table, and add a *Repo setup* section covering *Design decisions* 2–8. The `gh(repo, args)` row is already stale; don't touch it in this change.
- Modify: `README.md`: user-facing steps `gitgatesetup --register` (once), then `gitgatesetup <id>`.
- Modify: `docs/smoke.md`: add the live checks from Task 7.

- [ ] **Step 1: Write the three edits.**
- [ ] **Step 2: Commit** `docs: describe repo setup and gitgatesetup`

---

### Task 7: Live smoke (the user, on real GitHub)

These assumptions can't be tested offline. The user runs this checklist after installing the release, and anything that fails comes back as a fix before Task 8:

- [ ] `gitgatesetup --register` creates the app and saves `setup-app.json`. **Check whether the device flow is already enabled on the new app.** If the manifest can't enable it, `register` must tell the user to tick *Enable Device Flow* at `https://github.com/settings/apps/<slug>` (a once-ever checkbox). Update `register`'s message to whichever is true.
- [ ] Installing on "All repositories" covers a repo created afterwards.
- [ ] A device-flow user token can `POST /user/repos`. If GitHub refuses it, change design decision 5 so `repo_setup` requires an existing repo, and say so in the refusal.
- [ ] Rulesets, topics and the dependency graph PATCH all succeed with this token on a scratch repo, and the token stops working after 8 hours.
- [ ] Delete the scratch repo afterwards. That's a user action in their own terminal, not something `gitgatesetup` does.

---

### Task 8: ha-skills — the gate path uses `repo_setup` (separate PR in `PineappleEmperor/ha-skills`)

**Files** (all under `plugins/ha/skills/ha-integration/`):
- `reference/github-access.md:161`: replace the row for `gh repo create`, settings, rulesets, secrets and `bootstrap_repo.sh` on the gate path with: `git-gate:repo_setup(repo, description, topics, ruleset_file)`, then hand the user `gitgatesetup <id>`. Add a *Step 8: Hand a repo setup to the user*, shaped like Step 7. On the gate path, `gh secret set` becomes: the user pastes the value at `https://github.com/<repo>/settings/secrets/actions/new`, and the session gives them that link.
- `reference/scaffold.md:55`: in the "no GitHub repository yet" row, the gate path calls `repo_setup` (which creates it), and the user runs `gitgatesetup <id>` before Step 9.
- `reference/scaffold.md` Step 10, gate path: the user re-runs the same `gitgatesetup <id>` after the main push lands, which applies the ruleset. Pass topics `["home-assistant", "hacs", "home-assistant-custom-component"]`, and `ruleset_file` = `<abs>/ruleset.json`. Hand the user the `RELEASE_TOKEN` secret link from `reference/github-setup.md`. Keep the `core.hooksPath` line as the local step it is today. Line 239's mention of `bootstrap_repo.sh` becomes plain-path only.
- `reference/github-setup.md` Steps 2 and 74–92: the gate-path equivalent of the ruleset command and `bootstrap_repo.sh` is `gitgatesetup`. Step 2 (store `RELEASE_TOKEN`) gains the direct link. Creating the PAT itself stays as written, because release-flow's README says a GitHub App can't stand in for it.
- `reference/github-actions.md:62`: the `scripts/bootstrap_repo.sh` row is marked "plain path only".
- `templates/scripts/bootstrap_repo.sh` is unchanged.

- [ ] **Step 1: Make the edits.**
- [ ] **Step 2: Run** `pytest -q` in ha-skills (`tests/test_skill_meta_audit.py` checks references resolve). Expected: PASS.
- [ ] **Step 3: Commit** `feat: set up new repos through gitgatesetup on the gate path`
