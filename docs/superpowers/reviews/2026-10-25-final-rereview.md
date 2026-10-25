# Re-review of the final-review fix wave: 8340a73..bc6b83d (plus the I4 split 1e6d1a4..8340a73)

Re-reviewed on 2026-10-25 at `rework` tip bc6b83d. This was a read-only pass: the tree, index, HEAD and
branches were not touched. The probes are in `.tmp/final-rereview/` and run with
`PYTHONPATH=tests:src .venv/bin/pytest -q -s -p conftest .tmp/final-rereview/<file>`:

- `test_c1_probe.py`: jq-regex variants, plus what the real gh 2.45.0's jq can reach once the regex
  is switched off.
- `test_c1_cache.py`: `gh api --cache`, `--verbose`, `-i` and `-t` against the real gh, with the
  token searched for in the output and in the state dir.
- `test_i2_probe.py`: three extra D2 combinations.

Focused suites re-run at the tip: push/mirror `-k "stale or github_refusing or other_github"` gave
12 passed; gh/e2e `-k "e2e or jq or attached or hosts_file or environment"` gave 44 passed.

---

## Finding Verdicts

### C1: the token was readable through `gh --jq`. **ADDRESSED**

- **The token is out of gh's environment.** `_gh_env` (`src/git_gate/gate.py:508`) no longer
  sets `GH_TOKEN`. `_gh_config` (`gate.py:528`) writes the token into `work/gh/hosts.yml`:
  the directory is 0700, the file 0600, opened `O_EXCL|O_NOFOLLOW`, and `config.yml` holds
  `version: "1"`, so gh migrates nothing to a keyring. `work` is a fresh `mkdtemp` under
  `<state>/tmp`, and `shutil.rmtree(work)` runs in the `finally` at `gate.py:494`. The gh
  environment is built from scratch (`Git.env()` with `token=None`, so no `GIT_GATE_TOKEN`), and
  HOME is `<state>/home`.
- **The config dir is never reachable from a workspace.** `load_rules` refuses workspaces that
  overlap the state dir (`config.py:162-171`). Downloads go to `work/download` and are copied out by
  `copy_tree_into`/`place_new_file` from there only, skipping symlinks. The download flag value is
  rewritten by the gate, and an attached `-D…` is now refused (see below). No read subcommand on the
  allowlist (`ghpolicy.py:8-17`) reads a local file, and `auth`/`config` are not on it. `gh api -F
  k=@file`, `--input` and `--hostname` stay refused.
- **The jq check works.** `_check_jq` (`ghpolicy.py:110`, called at `:147` and `:207`) refuses
  `(?i)\benv\b` in every `-q`/`--jq` value (`ghpolicy.py:79`). A jq identifier cannot be split or
  escaped, so every way of naming `env`/`$ENV` has word boundaries. Probes confirm refusals for
  `$ ENV.GH_TOKEN`, `builtin::env`, `"\( env )"`, `def f: env; f` and `[$__loc__, $ENV]`.
- **Nothing else in gh's jq leaks.** gh 2.45.0 is compiled with `gojq.WithEnvironLoader` only and
  has no module loader. With the regex off, `import`/`include` give `cannot load module`,
  `input_filename` and `$__loc__` are undefined, and `inputs` is not allowed. `$ENV` holds only the
  config dir path, never the token, and `<state>/tmp` is empty after every call. `--verbose` redacts
  `Authorization`. `-i`, `-t {{.}}` and `--cache 1h` return nothing token-shaped and leave no state
  file holding the token.
- **The end-to-end test exists as ruled.** `tests/test_gh_e2e.py` runs the real `/usr/bin/gh`
  through `Gate.dispatch` against a fake API, in two layers: refused before gh runs, and with the
  check disabled, still no token in any form.
- **Follow-up (attached short-flag values): ADDRESSED.** `SHORT_CLUSTER = ^-[A-Za-z].` with DOTALL
  (`ghpolicy.py:73`) is checked on every token in `_common_checks`, for `api` and for every read
  subcommand, before any value check. Any argument longer than `-x` that starts `-<letter>` is
  refused. That covers `-qX`, `-q=X`, `-q X Y`, tabs, newlines, `-R…`, `-S…`, `-D…` and `-XPOST`.
  Separate-argument values are read the way pflag reads them (the next argument, even one that is
  `--` or dash-led). Every `-q`/`--jq` token is checked wherever it sits, so the gate and gh cannot
  disagree about which argument is the filter.

### I1: GitHub's workflow-scope rejection got no next step. **ADDRESSED**

- `gate.py:327`: a handover is made only when `mirror.push` exits non-zero and its output contains
  `create or update workflow` (`gate.py:45`). That substring is in every PAT, OAuth-app and
  GitHub-App form of GitHub's message. Every other rejection stays `git push was rejected`
  (`test_other_github_rejections_stay_plain_refusals`).
- **It pins the verified commit.** `_hand_over` (`gate.py:346`), bound at `gate.py:319`, records
  the imported `sha`. It runs under the mirror lock, `keep()` pins the commit at
  `refs/gate/handover/<id>`, and the record is closed if pinning fails. The push is reached only with
  no reasons, so `lease` is None (a normal push), `protected` is `()` and `base` is `remote or fork`.
  The test checks the record, the mirror ref, the unchanged remote, the `handover` audit line, and a
  `gitgatepush … y` that pushes and closes.
- The substring match can be triggered by agent-controlled text. That is a new Minor, N2 below.

### I2: the stale-fork refusal was false and unjudgeable. **ADDRESSED**

- `mirror.py:289-291`: a file is stale when it is in `net`, no new commit touches it, it is not a
  link, and it is unchanged in `diff(fork, sha)`. Operator precedence is correct
  (`(stale - changed) if fork else set()`). `Gate.push` (`gate.py:305`) raises a plain `Refused`
  before any handover, with rebase/merge advice and audit outcome `refused`. Files the branch itself
  changes still hand over (`test_stale_fork_changing_the_same_workflow_is_handed_over`,
  `..._with_its_own_workflow_change_rebases_first`).
- **Each D2 case, checked:**
  - **New branch.** A stale fork is refused (`test_stale_fork_with_old_workflows_is_refused_…`).
    The probe for a workflow main *added* after the fork is stale too. After a rebase the push
    passes.
  - **Existing branch.** A fast-forward that keeps the old workflows still passes, because `net` is
    intersected with `changed(remote, sha)` (existing tests). In the probe where a rewrite drops the
    user's branch change after main moved, the push is stale. After a rebase stale is empty, and the
    push is left to the non-fast-forward lease handover.
  - **Merged main.** A clean merge of the current main passes. In the probe for a branch that merged
    an *older* main before main moved again, the push is stale, and merging again clears it.
- **No refusal loop.** Once the branch contains `default_tip`, the fork equals `default_tip`, so
  stale is empty by construction.
- **Links never count as stale** (`test_..._unchanged_symlink_on_a_stale_fork_is_not_stale`).
- **The rollback merge (smoke step 8) is unchanged.** The fork is main's tip, so the file is in
  `diff(fork, sha)`: a handover, not stale.

### I3: the smoke plan never ran a handover. **ADDRESSED**

- **The PR comes from the Draft PR workflow.** Step 3.4 (`docs/smoke.md:55`) waits for the draft
  and takes `P` from `pr list --head smoke/gate`. Step 3.5 keeps `pr create` as a check that the
  422 comes back marked untrusted.
- **A real handover runs.** Step 10 (`docs/smoke.md:187`) runs H3, the non-fast-forward handover
  from 7.3, with `y`. Nothing moves `smoke/gate` on GitHub between 7.3 and 10, so the lease still
  holds. Checked against the code:
  - The `REPLACES … a force push.` screen, the `--force-with-lease=refs/heads/<b>:<sha>` Run line,
    and `core.hooksPath=/dev/null` (`handover.py:203-206, 354-360`).
  - The push goes to the SSH URL `git@github.com:<repo>.git` (`gitops.py:35-39`).
  - The command ends `Pushed. Handover <id> is closed.`, and the audit line carries `tool`, `id`,
    `branch` and `outcome` (`handover.py:284-292`).
  - A second run prints `No open handover <id>.` and exits 1.
- **Step 6 accepts either I1 outcome.** It names the exact `GitHub refuses workflow changes…`
  reply and `Files under handover paths: none.` (`handover.py:372`).
- **Step 6.3's stale text matches `_stale_refusal` verbatim**, and the step ends with a rebase and a
  passing push.
- **Step 6a's three probes return the stated refusals.** `_common_checks` runs before the jq check,
  so the one-argument form gets the short-flag message.

### I4: Task 7's commit bundled six changes. **ADDRESSED**

- 1e6d1a4, 5e24a0e, 063f04b, 19bbb8a and 8340a73 are each one coherent change, and each message is
  subject-only.
  - 1e6d1a4 adds `Gate.dispatch` and routes `call.run`, the Relay fallback and `build` through it.
  - 5e24a0e adds `InternalError` and the UTF-8-safe ToolError.
  - 063f04b adds `log_error` (0600, rollover) and the relay's cause messages; this is the review's
    own suggested grouping.
  - 19bbb8a makes a bad arity exit 2.
  - 8340a73 adds the tools, INSTRUCTIONS and docstrings.
- The seven fix commits are also subject-only, one change each, with no trailers or attribution.

---

## New Breakage in the Fix Diff

No Critical or Important problems. Four Minor ones, all hardening:

- **N1 (Minor). Token copies can outlive a hard kill.** `gate.py:467-494`. `hosts.yml` lives in a
  `mkdtemp` under `<state>/tmp` and is removed in a `finally`. A SIGKILL, or a SIGTERM to the
  in-process (non-relay) server mid-call, leaves `<state>/tmp/<x>/gh/hosts.yml` behind, and nothing
  sweeps `<state>/tmp`. It stays 0600 in a 0700 dir outside every workspace, the same trust domain as
  `config/token`, so nothing becomes reachable. **Fix:** sweep `<state>/tmp` entries older than
  `gh_timeout` at `Gate.__init__`.
- **N2 (Minor). Agent text can trigger the workflow-scope handover.** `gate.py:327`. The match is a
  bare substring over all push output, and GitHub echoes agent-controlled file paths, which may hold
  spaces, in some rejections: push protection's `path:` lines, and ruleset file-path or size
  violations. A file named `create or update workflow` in such a push gets a handover whose reason
  says it is a workflow-scope refusal. The user still sees the commits and stat, and must answer `y`.
  If the user bypasses the ruleset, though, they are approving under a false reason.
  - **Fix:** match the porcelain line for the pushed ref
    (`\[remote rejected\] \(refusing to allow an? .+ to create or update workflow`), since ref names
    cannot hold spaces.
  - **Also:** require `diff(remote or fork, sha)` to touch `.github/workflows/`.
- **N3 (Minor). A change-and-revert of a file main changed reaches the screen unmarked.**
  `mirror.py:289`, `handover.py:363-372`. If the branch edits a protected file and puts it back while
  main changed it after the fork, the push hands over (as ruled: the branch's own commits touch it).
  The screen lists the file but does not mark it `(older than <default>)`, as the review suggested
  "wherever such a file still reaches the screen". The listed commits do touch the file, so the
  screen is not silent, but answering `y` puts main's older version on the branch. **Fix:** in the
  screen, mark protected files absent from `diff(fork, sha)`.
- **N4 (Minor). Two consequences of the stricter refusals.**
  - **Old-base branches.** A new hotfix or maintenance branch cut from an old commit or tag, before
    main's workflow change, is now refused with "rebase or merge origin/main" and no handover. The
    old behaviour was a user-approvable handover. The advice is wrong for that branch type, so the
    user must push it outside the gate. This is bound by the ruling, but the INSTRUCTIONS and
    skills-migration doc should name it.
  - **Dash-led values.** `SHORT_CLUSTER` now also refuses dash-led *values* such as
    `--search "-label:bug"`, with a misleading "short flags" message. The `--search=-label:bug`
    form works, and the fix report says so.

---

## Out-of-Scope Observations

- **Docs still describe the token in gh's environment.** `docs/plan.md:89-90` says gh runs with
  "`GH_TOKEN` in its environment only", and `docs/design.md:44` describes the config dir. Both now
  contradict the code.
- **`gh api --cache` writes response bodies outside the state dir.** The gh environment has no
  `TMPDIR`, and gh's cache lives under gh's temp dir (`gh-cli-cache` is in the binary), so cached
  private-repo bodies would land in the system temp dir, not `<state>`. This is reasoned, not
  observed: the sandbox blocked the write. It holds no token, since the auth header is only hashed
  into the cache key. It pre-dates this wave. **Fix:** set `TMPDIR=<work>` in `_gh_env`, or refuse
  `--cache`.
- **Doc gaps from the final review are still open, as the fix report says.**
  - Server INSTRUCTIONS and `docs/skills-migration.md` do not yet mention the stale-fork refusal, the
    workflow-scope handover, or the jq `env` ban.
  - `workflow_run`'s real limits are not described.
  - M1-M8 are untouched.

---

## Verdict

**All findings addressed.** C1, its short-flag follow-up, I1, I2, I3 and I4 are each closed as the
controller ruled. The fix diff adds only Minor hardening items (N1-N4).

**Install readiness:** ready to install bc6b83d and run the corrected `docs/smoke.md`. N1-N4 and the
doc gaps can follow, though N2's tighter match is worth taking before the smoke run's step 6.
