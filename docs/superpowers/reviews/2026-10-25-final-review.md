# Final whole-branch review: git-gate rework, 33b0a48..45d3172

Reviewed 2026-10-25 at `rework` tip 45d3172 (35 commits). Against: the design spec, the plan with its
amendments and the user's eight decisions, the planning review (D1-D14), and the build ledger.
`./scripts/ci.sh` was re-run at the tip: ruff clean, **690 passed**.

**Verified** means reproduced. The probes are in `.tmp/final-review/`: `ghjq/` (token leak through
`gh --jq`, run end to end through `Gate.dispatch` against a local fake API), `fsck/` (bundle fetch and
`transfer.fsckObjects` on git 2.43), `probe/test_probe_final.py` (stale-fork refusal and confirmation
screen). Run the last with
`PYTHONPATH=tests:src .venv/bin/pytest -q -s -p conftest .tmp/final-review/probe/test_probe_final.py`.
**Reasoned** means read from code or documented behaviour, not run. Two read-only `gh` calls were made
through the installed gate (`workflow list` and `workflow view` on `PineappleEmperor/ha-skills`) to
check the smoke plan against the real repo.

---

## Strengths

- **The boundary moved, as the spec asked.** The gate runs git only in the mirror, the clone staging
  directory, and on bundle files it has already copied into its own storage (`mirror._stored_file`).
  No code path puts a workspace path into `GIT_DIR`, a worktree or an alternate. The shadow and the
  config allowlist are gone, and with them the class of findings that caused this rework.
- **One decision, stored and shown as stored (D1).** `Mirror.protected_changes` is the only judge.
  The handover record keeps `patterns`, `protected` and `exclude`, and `gitgatepush` prints them
  without re-reading `rules.toml`. The two-function drift behind the repeated fix rounds cannot
  come back.
- **The comparison commits are fixed (D2, D8).** The checks depend only on `(sha, remote,
  default_tip)`, never on another branch or on what the bundle claims. A bundle that builds on a
  refused handover commit is refused again. An approval for one branch does not carry over to
  another.
- **The confirmation screen holds up against a hostile author.** Every field goes through `_clean`
  (`isprintable`, which covers bidi and zero-width characters). The protected list is uncapped and
  the counts are exact. A force push says so and lists what it removes. The `Run:` line is
  `shlex.join` of the argv that runs. The record and the commit are re-checked under the lock after
  the prompt. `log --oneline` subjects and `--stat` names cannot inject lines.
- **The exchange contract (D9) is enforced in one helper**: `fd_path` after open, dot components
  refused, suffix allowlist, `nlink == 1`, `O_NONBLOCK`, a type check before any read, and writes by
  rename only. Downloads go into a new directory the gate creates.
- **Typed writes are tight.** Arguments are checked before the first write. Ints are strict. Titles
  and names are single printable lines. Approve and merge are refused by name. `workflow_run`
  applies the same protection rule as push and also refuses tag, release, dispatch, schedule and
  fork runs, including the tag-named branch trick. GitHub's error text comes back to the agent
  marked untrusted.
- **The relay is small and fails closed.** It has one tool list (`call.TOOLS`), one dispatcher, a
  bad arity exits 2, errors are scrubbed and UTF-8 safe before they reach the transport, and
  `errors.log` is 0600 and bounded.
- **Strict rules.** Unknown keys, wrong types, and duplicate repos after lower-casing are refused
  (D10). The off switch fails closed on a dangling symlink.
- **Tests:** 690 tests, many of them ported regression cases from earlier review rounds, plus a test
  that the agent's repository is never touched.

---

## Issues

### Critical

**C1. The agent can read the token through `gh --jq`.** `gate.py:493-510` (`_gh_env` puts `GH_TOKEN`
in gh's environment); `ghpolicy.py:63-66` and `ghpolicy.py:118-155` (`-q/--jq` values pass through
unchecked, for `api` and for every read subcommand); `gate.py:532` (`_scrub` removes only the
literal token).

- gh 2.45.0 compiles jq filters with `gojq.WithEnvironLoader(os.Environ)`: the symbol
  `jq.EvaluateFormatted.WithEnvironLoader` is in `/usr/bin/gh`. So `$ENV` and `env` in a filter
  read gh's environment.
- **Verified end to end** (`ghjq/`): `Gate.dispatch("gh", {"repo": "me/app", "args": ["api",
  "repos/{owner}/{repo}", "--jq", "$ENV.GH_TOKEN|explode|implode|ascii_downcase"]})` returned
  `ghp_secrettoken123`. `explode` alone returns the exact token as code points. The plain
  `$ENV.GH_TOKEN` comes back as `[token]`, which shows the scrub is the only defence.
- `pr list --json number -q 'env.GH_TOKEN|explode'` passes `ghpolicy.plan` the same way.
- Read access to any allowlisted repo is enough. The gate exists to keep this token out of the
  sandbox. The hole was already on main (`--jq` was allowed there too), but this release must not
  ship with it.
- `gh api --verbose` was also checked: gh redacts the Authorization header there.
- **Smallest fix (both parts):**
  1. Take the token out of gh's environment. Write it per call to `hosts.yml` in a fresh, gate-owned
     `GH_CONFIG_DIR` under `work` (mode 0600), so `$ENV` holds nothing worth reading.
  2. Refuse `-q`/`--jq` values that contain the identifier `env` or `$ENV`.

  Add a test that runs the real `/usr/bin/gh` against a fake API, as the probe does.

### Important

**I1. Merging the default branch into a pushed branch after a workflow change on it probably dies at
GitHub, and the agent gets no next step.** `gate.py:331-333`.

- D2 (planning-review I2) lets this push through the gate, and the tests prove it.
- But the spec says the PAT has no Workflows permission. GitHub rejects a ref update whose
  `.github/workflows/*` content changes ("refusing to allow a Personal Access Token to create or
  update workflow … without `workflows` permission"), and merging upstream workflow changes into a
  branch is the well-known case that triggers it. **Reasoned**: not verifiable offline.
- If GitHub rejects it, the gate replies `git push was rejected: …` with no handover. That is the
  "refusal without a clear next step" anti-goal, in the HA repos' everyday flow, right after the
  user changes CI on main.
- **Smallest fix:** when the push output contains `create or update workflow`, keep a handover for
  the verified commit (a normal push) with the reason "GitHub refuses workflow changes from the
  gate's token". The user's SSH push is not bound by the PAT.
- Smoke step 6 must settle which way it goes.

**I2. A stale-fork refusal tells the agent something false and shows the user nothing they can
judge.** `gate.py:513-519` (`_refusal_for`), `gate.py:316-317`, `handover.py:363-385`.
**Verified** (`probe/`).

- The case is user decision 4: a new branch forked before main changed `.github/workflows/ci.yml`,
  where the branch itself never touches it.
- The agent is told `It changes 1 file(s) under handover paths: .github/workflows/ci.yml.` That is
  false. Following INSTRUCTIONS ("Do not retry or work around it"), the agent hands the user
  `gitgatepush` instead of rebasing.
- The screen lists `.github/workflows/ci.yml` under the protected files, but no listed commit
  touches it and the stat (fork..sha) does not show it either. A user who answers `y` pushes the
  stale workflow, which is exactly what decision 4 meant to stop, and the screen never showed them
  that.
- **Smallest fix:** in `Gate.push`, split off the protected files that are not in
  `diff(fork, sha)`; these are stale, not changed. For them, refuse without a handover (or keep one
  that marks them) and tell the agent: "fetch, then rebase or merge origin/<default>, and push
  again". Wherever such a file still reaches the screen, mark it `(older than <default>)`.

**I3. The smoke plan never runs a handover, and one of its steps cannot pass on `ha-skills`.**
`docs/smoke.md` steps 3.4 and 7-8.

- Every handover is left unrun ("leave H1 to H4 unrun"). So the live run never exercises the push
  path that actually goes out: the user's SSH key and git config from the mirror,
  `--force-with-lease`, hooks off, the record closing, the `gitgatepush` audit line.
- Step 3.4 creates a PR, but `ha-skills` has a `Draft PR` workflow that runs
  `auto-draft-pr.yml` on every push to a branch other than main (**verified** with `gh workflow
  view`). That workflow will already have opened the draft, so `pr create` gets 422 "A pull request
  already exists".
- **Fix:**
  - In 3.4, wait for the draft and take `P` from `gh pr list --head smoke/gate`. Optionally keep
    `pr create` as a check that GitHub's 422 message comes back with the untrusted prefix.
  - Add a step where the user runs `gitgatepush H3` (the non-fast-forward handover on the throwaway
    `smoke/gate`) and answers `y`, before step 10.

**I4. Task 7's commit bundles six changes.** 45d3172 (`feat: expose the reworked tools`; the ledger
calls it e3774b6).

- It holds `Gate.dispatch`, `InternalError`, `log_error` (0600 and rollover), the relay's cause
  messages, the bad-arity exit, and the new tools with their instructions. The user's floor rule is
  one change per commit.
- Ruling: **split it before install.** The branch is unpushed, and the installed sha should be one
  that survives into main.
- Suggested split:
  - `refactor: dispatch every call through Gate.dispatch`
  - `fix: scrub internal errors before they reach the agent`
  - `fix: bound errors.log and name the relay failure`
  - `fix: exit on a bad call arity`
  - `feat: expose the reworked tools`

### Minor

**M1. Bundles are not fsck-checked on git 2.43.** `mirror.py:1-7` and the spec's "with
`transfer.fsckObjects` (every object re-hashed)".

- **Verified** (`fsck/`): a tree with a `.git` entry fetched from a bundle with
  `transfer.fsckObjects=true` is accepted. The same object fetched from a repository is refused
  (`hasDotgit`). git applies fsck to bundle fetches only from 2.46 on.
- Object IDs are still re-hashed (index-pack names objects by hash), and the gate never checks out.
  GitHub fscks on receive, so malformed trees die at the push either way.
- **Fix:** correct the docstring and the spec, or run `git fsck --strict` over the new objects.

**M2. Exchange paths reach every workspace in `rules.toml`, not just the calling session's sandbox.**
`paths.py:34-39`, `paths.py:95-118`, `paths.py:216-266`.

- A session sandboxed to `~/git-gate` cannot read `~/claude-skills`, yet it can hand any `*.md` or
  `*.txt` there (other sessions' `.tmp/` notes included) to `issue create` and read it back.
- It can also create new files and directories there through `gh run download` and
  `release download`. For example, a new directory holding `conftest.py` and `test_x.py` is picked
  up by a later `pytest` run.
- **Fix:** document it. Also require download targets (ideally all exchange files) to sit under a
  `.tmp/` component, which tools skip by default.

**M3. `open_exchange` opens any absolute path before checking that it is in a workspace.**
`paths.py:101-109`, with the message at `paths.py:39`.

- The refusal text gives away whether a file exists anywhere the user can read, and its resolved
  real path: for example `Bundle /home/juicebox/.ssh/id_ed25519 is outside the workspaces` versus
  `No such file or directory`. Opening is also a side effect on device files. This is deferred
  item 146, promoted to a finding because it is a read across the boundary, though of existence
  only.
- **Fix:** open the parent with `open_dir` (which checks the workspace), then `openat` the name.

**M4. `triage` can refuse after a partial write.** `writes.py:148-153`, `github.py:219-224`.

- A remove-label name of `.` or `..` passes `_line`, and only `_segment` refuses it, after the adds
  are already POSTed. That contradicts the module's "never a partial write" promise.
- **Fix:** call `GitHub._segment` during validation.

**M5. Open pre-relay conversations keep the old gate.** `docs/skills-migration.md` step 2.

- Until they restart, they run release 67440d79 in-process: `gh` writes, the shadow push, and C1's
  hole. The doc says to restart "to see the new tools", not that the old gate stays live until then.
- **Fix:** say so, and have the smoke run check `/mcp` lists ten git-gate tools in each open
  conversation.

**M6. A rollback to a pre-relay release leaves `/usr/local/bin/gitgatepush` dangling**, so open
handovers cannot be run. Relay-capable servers keep running their own in-process code for that time.
`install.sh:86-105`, `server.py:42-46`. **Fix:** document both in the README's install section.

**M7. The mirror grows without bound.** With `gc.auto=0`, objects from refused and incoming bundles
stay forever. A looping agent can add up to 512 MiB per push. **Fix:** a periodic `git gc --prune`
under the mirror lock (the spec left cleanup to the builder).

**M8. The strict `rules.toml` schema can switch off a file the old release accepted.** Every call is
then refused (with a clear message). The README, the migration doc and the smoke "Before you start"
do not warn about it. **Fix:** one line in each, plus a first call that must succeed.

Severity count: **Critical 1, Important 4, Minor 8.**

---

## Deferred-minor triage (all 56 ledger lines)

| Line | Item | Verdict | Reason |
|---|---|---|---|
| 37 | T0: OSError/bad JSON/timeout share one message | ACCEPT (fixed) | `Relay.__call__` now names timeout, could not start, gave no answer |
| 38 | T0: relay doesn't check `current` under `releases/` | ACCEPT | Only root can repoint `current` |
| 39 | T0: errors.log umask, unbounded | ACCEPT (fixed) | `call.log_error`: 0600, rolls over at 1 MiB |
| 40 | T0: `gate.call(getattr…)` repeated ×3 | ACCEPT (fixed) | `Gate.dispatch` is the only path |
| 41 | T0: `build(gate=, relay=)` ignores gate | ACCEPT (fixed) | Raises `ValueError` |
| 42 | T0: bad-arity `call` starts the server and blocks | ACCEPT (fixed) | `__main__` exits 2 with usage |
| 43 | T0: relay test gaps | ACCEPT | Task 7 rewrote them; the remaining crash path is item 183 |
| 44 | T0: `Relay._gate` lazy, no lock | ACCEPT | Fallback only; two Gates are harmless |
| 45 | T1: `check_enabled` `.exists()` | ACCEPT (fixed) | `os.path.lexists` |
| 46 | T1: no test of the `config_dir()` fallback | ACCEPT (fixed) | `test_check_enabled_defaults_to_config_dir` |
| 53 | T0: relay errors.log keeps child stderr unscrubbed | ACCEPT | The relay never holds the token; file is 0600 in a 0700 dir |
| 54 | T0: `call.main` traceback unscrubbed | ACCEPT | Same file; the agent sees only a generic line |
| 62 | T2: bodyless POSTs may draw 411 | ACCEPT (fixed) | `_request` always sends `{}`; smoke step 4 confirms live |
| 63 | T2: label removal `absent=(404,)` hides a missing issue | ACCEPT | Reply overstates, nothing is changed wrongly; the agent can `issue view` |
| 64 | T2: `Refused` drops GitHub's message | ACCEPT (fixed) | `FromGitHub` carries it, cleaned and marked |
| 65 | T2: `edit()` sends base for issues, `PATCH {}` | ACCEPT (fixed) | `writes.issue` has no base; `_action` one-of refuses empty edits |
| 66 | T2: `edit(state=)` unvalidated | ACCEPT (fixed) | `STATES` map in writes.py |
| 70 | T2: `labels()` adds before validating removes | ACCEPT (mostly fixed) | Names validated first; the dot-only residue is M4 |
| 82 | T3: filtered bundle leaves a `.promisor` pack | ACCEPT | Verified fail-closed in the planning review |
| 83 | T3: mirror.py:147 wrong message | ACCEPT | Unreachable branch, cosmetic |
| 84 | T3: `keep()` accepts any object type | ACCEPT | Only caller passes a SHA proved a commit |
| 85 | T3: `_clear_incoming` clears every branch | ACCEPT | Under the mirror lock; callers use SHAs |
| 88 | T4: expired records leave refs | ACCEPT (fixed) | `Handovers(state, mirror=)` drops them |
| 89 | T4: gitgatepush ignores `git_timeout_seconds` | ACCEPT | Deliberate: a broken rules.toml never blocks a handover |
| 92 | T4: `match` with `^…$` | ACCEPT (fixed) | `fullmatch` everywhere |
| 93 | T4: strict decoding after a successful push | ACCEPT (fixed) | `surrogateescape` in `_run` and `Git.run` |
| 94 | T4: negative returncode | ACCEPT | Exit status stays non-zero |
| 95 | T4: mirror lock across ssh prompts, comment it | ACCEPT | User-present; see 143 |
| 96 | T4: test gaps (expired via main, double run, real URL) | ACCEPT (fixed) | Tests present (test_handover.py:133, :368, :785) |
| 97 | T4: launcher comment premature | ACCEPT (fixed) | Task 8 links it |
| 107 | T4: `REPO_RE`/`OID` cross-file fullmatch | ACCEPT (fixed) | As 92 |
| 108 | T4: file cap follows git's order | ACCEPT | D5: protected files uncapped and first; only the stat caps |
| 111 | T3: glob-free pattern fails open (**MUST FIX before finishing**) | **CLOSED** | `canonical_pattern("skills") == "skills"`; `protected()` matches any leading part, so `skills/x` is covered (`test_protected_table`, test_mirror.py:52, :655-669); `.github/workflows` as a file is covered too |
| 112 | T3: `set(have)` TypeError | ACCEPT (fixed) | `Gate.fetch` requires strings; `dict.fromkeys` over filtered strings |
| 113 | T3: gitlinks at handover paths | ACCEPT | D4 flags every gitlink that reaches a pattern; submodule content is another repo the gate does not push |
| 117 | T4: handover imports a private matcher | ACCEPT (fixed) | D1 removed the import |
| 143 | 5b: gitgatepush push has no timeout while holding the lock | ACCEPT | User-present (Ctrl-C frees it); follow-up: bounded `Git.lock` wait so agent calls refuse ("mirror busy") rather than hang |
| 144 | 5b: clone target ignores the dot-component rule | ACCEPT | The gate writes only a fresh `.git` with its own config; checkout happens in the sandbox under its rules |
| 145 | 5b: `Git.remote_heads` dead code | ACCEPT | Delete when convenient; unused |
| 146 | 5b: `open_exchange` opens before the workspace check | ACCEPT for install | Existence and real-path oracle only; filed as M3 |
| 147 | 5b: audit.log default umask | ACCEPT | State dir is 0700 |
| 148 | 5b: rate limit untested across Gate instances | ACCEPT | File-and-flock based by construction |
| 149 | 5b: root path named like a SHA parsed as a header | ACCEPT | Needs root-wide patterns plus a change-and-revert; net diff catches the rest |
| 153 | T8: PATH check compares spelling | ACCEPT | A spurious warning only |
| 158 | T8: launcher case pattern allows `releases/<sha>/x/bin/…` | ACCEPT | Root-only paths |
| 159 | T8: install aborts if `GIT_GATE_BIN_DIR` missing | ACCEPT | `/usr/local/bin` exists on this host |
| 160 | T8: no test of launcher refusal or PATH warning | ACCEPT | Dry-run done in Task 8; smoke checks the warning |
| 164 | T6: triage caps 100 names, 39-char logins | ACCEPT | Reasonable limits |
| 169 | T6: triage partial GitHub failure unexplained | ACCEPT | GitHub's message is returned; the agent can view |
| 170 | T6: `draft=None` refused | ACCEPT | The MCP schema defaults to False |
| 171 | T6: web URL hard-coded to github.com | ACCEPT | `base_url` differs only in tests |
| 181 | T7: dispatch kwargs `tool`/`fn`/`self` | ACCEPT | Unreachable through the schema or relay |
| 182 | T7: one combined commit (**final review to rule**) | **MUST FIX before install** | User's floor rule; unpushed, so splitting is cheap (I4) |
| 183 | T7: no relay-crash test; `log_error` without O_NOFOLLOW | ACCEPT | 0700 dir |
| 186 | T6: socket TimeoutError escapes `_request` | ACCEPT | Becomes a scrubbed `InternalError` |
| 187 | T6: `_branch` keeps dots | ACCEPT | Refnames cannot hold `.`/`..` segments |

---

## Docs check (against the merged code)

**README.md.** Accurate on tools, the exchange rules, the relay and the install link. Missing:

- the strict-schema warning (M8);
- the rollback caveats (M6).

**docs/skills-migration.md.** The commands and the call table match `writes.py` and `server.py`.
Gaps:

- `workflow_run` is described without its real limits. It acts only on `push` and `pull_request`
  runs, from this repo's own branches, on a branch that still exists, is unprotected and has no tag
  of the same name; everything else is refused.
- "Handovers" says "any commit touching a handover path". It omits stale forks (I2) and symlinks or
  gitlinks that reach a handover path, which are refused whether or not they changed.
- Step 2 should say the old gate stays live until restart (M5).
- No mention that a merge of main after a workflow change may be rejected by GitHub (I1).

**docs/smoke.md.**

- Step 3.4 conflicts with `ha-skills`' Draft PR workflow, and no handover is ever run (both I3).
- Step 6's expectation depends on I1.
- Step 7.1 leaves the agent's local `main` ahead of `origin/main` and never resets it.
- The expected strings were otherwise checked against the code: the fetch reply, `Pushed …`, the
  compare link, each `Not pushed: …` reason, the `_list` format and the screen sections all match.

**Server INSTRUCTIONS and docstrings** (`server.py:90-122`).

- Accurate on bundle, fetch, `have` and the exchange rules.
- The `workflow_run` line ("never on the default or a protected branch") understates the rule.
- "Do not retry or work around it" conflicts with the non-fast-forward advice to rebase, and with
  the stale-fork case (I2).
- The `gh` docstring says only "absolute paths inside a workspace" for downloads; the target must
  also be new and outside dot directories.
- The `triage` docstring says label names "must already exist", but GitHub creates missing labels
  on add.

**Spec.** The `transfer.fsckObjects` claim is wrong for bundles on git 2.43 (M1).

---

## Declined to judge

- **Unicode normalisation of handover paths.** D4 accepted it as a known gap; checkouts are on Linux.
- **The user's own git config and `PATH` during `gitgatepush`**, which includes resolving `git` from
  `PATH` and settings such as `push.recurseSubmodules`. The user account is the trust boundary
  (spec non-goals).
- **Hook-called scripts outside the default handover paths** (`scripts/ci.sh` and the like). The
  user deferred them to per-repo `handover_paths` (decision 2).
- **Push-triggered CI on agent branches seeing repository secrets.** Decision 7 puts this in the
  workflows. Note: `ha-skills`' Draft PR workflow hands `RELEASE_TOKEN` to a pinned reusable
  workflow on every branch push.
- **Prune's race between the head check and the DELETE.** Accepted in the planning review.
- **`pr edit base` and label renames.** These are typed writes the spec grants; the user merges and
  sees the base.
- **`gh run watch` holding a call for up to `gh_timeout` (1800 s).** It is bounded and configured;
  that is not a hang.
- **`gh-pages` or other deploying branches not in `protected_branches`.** That is per-repo
  configuration, which `push_deploys` announces.
- **pydantic coercing `"7"` or `True` to ints before `writes._number` sees them.** The result is a
  valid issue number either way, so nothing is gained.
- **Malformed trees confusing `protected_changes`.** GitHub's receive fsck refuses such objects on
  either push path. Only the docs claim is wrong (M1).
- **Old and new releases sharing the state dir.** Their gitconfig settings are identical, and their
  lock keys and directories do not collide.
- **Commit-message attribution.** Checked: no `Co-Authored-By`, `Claude` or "Generated" lines in
  33b0a48..45d3172.

---

## Readiness

**Not ready to install.** C1 lets any agent with read access to an allowlisted repo read the PAT.
Fix C1 and I1-I4 first (I1 can be the defensive handover mapping even before the live answer). Then
install the final sha, restart every open conversation once, and run the corrected `docs/smoke.md`.

**The smoke run must confirm live:**

1. **The token stays hidden.** `gh(repo, ["api", "repos/{owner}/{repo}", "--jq",
   "$ENV.GH_TOKEN|explode"])` and the `env.GH_TOKEN` form are refused, or return nothing
   token-shaped.
2. **Every open conversation runs the new gate.** After the restart, `/mcp` shows ten git-gate
   tools, and an old-style `gh pr comment` is refused with the typed-tool hint.
3. **Bodyless typed writes work.** `pr ready` and `workflow_run cancel` do not draw a 411.
4. **The Free-plan rules 403 passes.** On a private Free-plan repo, the "Upgrade to GitHub Pro"
   answer does not block the push.
5. **Step 6 settles I1.** Does GitHub accept the PAT's push of a merge that brings main's workflow
   change into a pushed branch? If not, the reply must be a handover, not a dead end.
6. **A real `gitgatepush` push succeeds.** One handover (H3, the non-fast-forward on `smoke/gate`)
   is run with `y` and pushes with the user's SSH key and git config. `--force-with-lease` takes
   effect, the record closes, the audit line reads `"tool": "gitgatepush", "outcome": "pushed"`, and
   a second run of the same id says `No open handover`.
7. **The rollback merge screen** (step 8) shows `Files under handover paths (1):`. After I2's fix, a
   stale-fork push gets the rebase advice rather than an unexplained handover.
8. **New and deleted branches sync.** A web-UI branch at main's sha arrives on fetch (step 2), and
   `--prune` removes branches deleted on GitHub (step 10.5).
9. **The off switch works.** `touch ~/.config/git-gate/disabled` makes the next call return the
   exact off-switch message; removing the file restores service.
10. **The live `rules.toml` loads under the strict schema**, and `install.sh` printed no PATH
    warning for `gitgatepush`.
