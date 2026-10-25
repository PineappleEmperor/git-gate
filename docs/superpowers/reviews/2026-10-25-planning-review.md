# Planning review: git-gate rework, before Tasks 6-8

> Copied from the build workspace on 2026-10-25 so it outlives it. The user's decisions on section 6
> are recorded in the plan (`docs/superpowers/plans/2026-10-25-gate-rework.md`, "Amendments after the
> planning review") and override anything here they contradict.

Reviewed at `rework` tip ef75981 (Tasks 0-5 built, Task 5 not yet task-reviewed). Inputs: the
design spec, the plan, the SDD ledger, `src/git_gate/*.py`, `install.sh`, `bin/`, the tests.

Claims marked **verified** were reproduced. The scripts are in `.tmp/planning-review/`: `e1/`
(bundle `--prune` and bundle errors), `e3/` (filtered bundles), `e5.py` (handover disclosure and
fetch shortcut, against the real `Mirror` and `handover._show`), `e7/` (rules loading and path
helpers) and `pt/test_probe.py` (gate-level push and fetch probes, run with pytest).
**Reasoned** means read from the code or from documented git behaviour, not run.

The root cause behind the repeated fix rounds is one structural fault plus one missing contract:

1. **The push decision and the user's confirmation are computed by two different functions, at
   two different times, with two different inputs.** `Mirror.touched` (mirror.py:228) decides;
   `handover._protected` (handover.py:348) re-derives the list later from a different base, with
   patterns re-read from a rules.toml that may have changed or may not load (handover.py:290). Each
   fix round patched one of the two. Decision D1 replaces both with one function whose result is
   stored in the handover record and printed as stored.
2. **The gate runs outside the sandbox, but treats "inside a workspace" as "something the agent
   could touch anyway".** It is not. Inside workspaces the sandbox masks secrets (`**/.env`,
   `**/secrets.yaml`, `**/.storage`, `**/.git-credentials`) and blocks writes to config that runs
   unsandboxed (`.mcp.json`, `.claude/settings.json`, `.vscode`, ...). The gate reads and writes
   all of them for the agent. Decision D9.

---

## 1. Threat model per surface

Throughout, the agent is assumed prompt-injected. It controls everything in its sandbox: its
repo, its `.git/config`, every file it can write in a workspace, every tool argument, the content
and names of every commit and branch it pushes, and (through branches it pushes) what non-default
GitHub branches hold. It does not control: the token, `~/.config/git-gate`, the state dir
(mirrors, handover records), `/opt/git-gate`, the default branch on GitHub, and files the
sandbox masks.

### Push (bundle in, checks, handover creation)

- **Agent controls:** the bundle bytes and path, the branch name, every commit, tree and path name
  in it, the bundle's prerequisites, and the order of calls (it can stage content on other
  branches first).
- **Could try:** smuggle a protected-path change past the check (merges hidden by `--cc`,
  rollbacks, renames, case variants, symlinks or gitlinks at or above protected paths, pattern
  quirks). Make the gate parse something that is not a bundle, or read a FIFO or device. Make the
  checks run against a base the agent chose (another branch it pushed). Push to a protected
  branch through a race. Make the gate crash after it has already pushed. Flood the mirror.
- **User relies on:** nothing reaches a protected branch, rewrites a branch, or changes a
  protected path unless they run `gitgatepush`. A refused push leaves GitHub untouched and leaves
  exactly one handover that holds the verified commit.
- **Must fail closed:** any API error or ambiguous answer (403 other than the plan limit);
  malformed git output (the parser fallback at mirror.py:278); a bundle that is not exactly one
  `refs/heads/<branch>`; missing or unconnected prerequisites (**verified**: git refuses
  prerequisites that are present but not reachable from a ref, `e3/`); any decode error.

### Handover confirmation and `gitgatepush`

- **Agent controls:** commit messages, file names, branch name (all printed on the user's
  terminal), the number of commits and files (caps), and the order in which protected and
  ordinary files appear in git's output.
- **Could try:** hide a protected change behind a cap, behind a base that makes it look old, or
  behind a re-read rules.toml. Make a rewrite look like a normal push. Inject terminal escapes
  or bidi text. Race the record between display and push.
- **User relies on:** the screen lists every protected file the push changes, uncapped. Every
  count is exact. A force push says it is one and lists what it removes. The command printed is
  the argv that runs. What is pushed is exactly the SHA shown, guarded by the lease shown.
- **Must fail closed:** a record that does not parse or does not validate; a mirror that lost the
  commit; a record that changed between display and push; a push failure (record stays open).

### Fetch (bundle out, `have`)

- **Agent controls:** the output path, the `have` list (up to 1000 strings), and, through
  branches it pushed, branch names and content in the bundle.
- **Could try:** point the output at a file it may not write (sandbox-masked config), at a FIFO,
  a symlink or a hard link. Pass huge or malformed `have` lists. Probe the mirror for objects
  that are not on GitHub (handover commits) through `have`.
- **User relies on:** the agent's `origin/*` can always be made exactly equal to GitHub's
  branches, so push bundles made with `--not --remotes=origin` have prerequisites GitHub holds.
- **Must fail closed:** the output path rules (D9); bad `have` entries are skipped, never
  trusted.

### Clone

- **Agent controls:** the target path, the repo name.
- **Could try:** a target that is a symlink, a non-empty directory, a sandbox-protected name, or
  outside workspaces.
- **User relies on:** clone writes only a fresh `.git` directory with gate-written config and
  checks nothing out (gate.py:158-221), so no hook or filter ever runs as the user.
- **Must fail closed:** an existing non-empty target; a cross-device rename.

### Prune

- **Agent controls:** the branch name and timing.
- **Could try:** delete a protected branch, a branch with an open PR, or a branch that moved after
  its PR closed.
- **User relies on:** only branches whose last PR is merged or closed, at that PR's head SHA, are
  deleted.
- **Must fail closed:** API errors. A residual race remains between the head check and the
  DELETE, because GitHub's ref delete takes no expected SHA. Accepted: only the agent's own new
  commits are at risk.

### Typed GitHub writes (Task 6, not built)

- **Agent controls:** every parameter: numbers, titles, body-file paths, label, user and branch
  names, run ids, action names.
- **Could try:** approve as the user. Exfiltrate sandbox-masked files as bodies (D9). Rerun an
  old deploy run on the default branch, which rolls production back on push-deploy repos, or
  cancel a deploy run halfway. Path traversal in URL segments (`..`, `%2e`). Smuggle the token
  into a body. Inject instructions into later agents through GitHub content.
- **User relies on:** only the listed writes are possible, only on allowlisted repos with write
  access, never an approval or a merge, and every reply is marked as untrusted content.
- **Must fail closed:** unknown actions; missing arguments; non-int or bool numbers; any GitHub
  error, surfaced with GitHub's own message.

### Read-only `gh` passthrough (Task 6)

- **Agent controls:** the argv.
- **Could try:** reach a write through a read command (`api -X POST`, `--input`, GraphQL),
  widen scope (`-R`, `--search repo:`, URLs), or use download targets to write into protected
  workspace paths (`run download -D`, `release download -O`, both through `copy_tree_into`).
- **User relies on:** GET only, held to one repo, and downloads land only where an exchange file
  may land.
- **Must fail closed:** any flag not on the list (as now).

### The relay and `git-gate call` (built)

- **Agent controls:** tool arguments, which the MCP SDK type-checks against the wrapper
  signatures before they reach the relay.
- **Could try:** an unknown tool name, malformed JSON, or calls that hang the server.
- **User relies on:** each call runs the code `current` points at, from a root-owned release,
  and never hangs.
- **Must fail closed:** an unknown tool (call.py:192 does this); a child with no valid answer
  (server.py:57-61 does this). One gap: a bad-arity `git-gate call` starts a stdio server and
  blocks (ledger, deferred).

### `install.sh` and the `/usr/local/bin/gitgatepush` link (Task 8)

- **Agent controls:** the dev repo the installer reads (sandbox-writable), and possibly a
  directory early on the user's `PATH`, if one is sandbox-writable (none in this session's
  allowlist).
- **Could try:** plant a `gitgatepush` earlier on `PATH`; make the link dangle or point
  elsewhere after a rollback.
- **User relies on:** typing `gitgatepush` runs root-owned code from `/opt/git-gate/current`.
- **Must fail closed:** install refuses to finish without telling the user if `command -v
  gitgatepush` does not resolve to the link.

### Workspace file helpers (`paths.py`)

- **Agent controls:** every path string, every intermediate symlink, hard links (within one
  mount), FIFOs, devices, and the timing of swaps.
- **Could try:** make the gate, which runs outside the sandbox, read a file the sandbox masks
  and upload it to GitHub (**verified**: `read_workspace_file` returns a workspace `.env`, `e7/`).
  Make it overwrite a file the sandbox write-protects (**verified**: fetch replaced a workspace
  `.mcp.json` with bundle bytes, `pt/`).
- **User relies on:** the gate never widens the sandbox: it reads only what the agent could have
  written, and writes only where the agent could have written.
- **Must fail closed:** everything not matching the exchange contract (D9).

### `rules.toml` loading

- **Agent controls:** nothing directly (the file is outside the sandbox). The risk is the user's
  own typos.
- **Could try:** n/a. The failure mode is a typo that silently weakens protection.
  **Verified** (`e7/`): `protected_branches = "main"` becomes `('m','a','i','n')`, which protects
  nothing. An unknown key `protected_branch = [...]` is ignored. `handover_paths = ["skills"]`
  protects only a file named `skills`. `"a/../.githooks/**"` protects nothing.
- **User relies on:** what they wrote is what is enforced, or the gate refuses with the line at
  fault.
- **Must fail closed:** every type or shape error (D10).

---

## 2. Decisions to pin

Each is a rule the code must follow. Where it replaces existing logic, that logic is deleted.

### D1. One protected-change computation, made once, stored, shown as stored

`Mirror.protected_changes(repo, sha, remote, default_tip, patterns, rules) -> Protected` is the
only code that decides what counts as a protected change. `Protected` is a frozen dataclass
`(files: tuple[str, ...], commits: tuple[str, ...])` that is falsy when `files` is empty. The
gate's push refuses iff it is truthy. The handover record stores `files`, `commits` and the
`patterns` used. `gitgatepush` prints the stored `files` and never recomputes them or reads
rules.toml.

Deleted as a result: `Mirror.touched`, `Mirror._commit_touches` (folded in),
`handover._protected` and `handover._handover_paths` with its rules.toml fallback and "Note:"
line.

### D2. The comparison commits, defined once in `Gate.push`

- `remote` = the target branch's SHA in the mirror right after `sync`, or `None`.
- `default_tip` = the default branch's SHA after `sync`, or `None`. The default branch name comes
  from the API.
- `fork` = `merge-base(sha, default_tip)`, or `None` when there is no default tip or no common
  ancestor.
- **No other branch is ever a comparison commit.** Agents can push those branches, so they are
  not a trusted base. `Gate._fork_point`'s fallback to the alphabetically first branch
  (gate.py:316) is removed.

`protected_changes` computes:

- `C` = `rev-list sha --not remote default_tip` (omitting whichever is `None`): the commits new
  to both the branch and the default branch. Commits already on the default branch are the
  user's and are never re-checked; this fixes defect I2.
- `per_commit` = the protected paths in each commit of `C`'s own diff
  (`diff-tree --stdin -z -r --no-renames --name-only --cc --root`, with today's fail-closed
  parser).
- `net` = protected paths in `diff --no-renames --name-only -z (default_tip or empty tree) sha`,
  intersected with protected paths in `diff remote sha` when `remote` exists. A path counts only
  if it differs from both the branch's current state and the default branch's **current** tip.
  (User decision 4, 2026-10-25: comparing with the current tip rather than the fork point also
  refuses a branch whose workflows predate a fix on the default branch.)
  This keeps "stale fork passes" and "clean merge of main passes", lets an existing branch merge
  main after a workflow change on main, and still catches rollback and evil merges and any
  rewrite that puts new content at a protected path. A rewrite that only returns a protected path
  to the default branch's version at the fork is not a protected change. It is still handed over,
  because it is a rewrite, and the stat against the remote shows it.
- `links` = entries of `sha`'s tree with mode `120000` or `160000` that reach a pattern (D4).
- `files` = sorted union of `per_commit`, `net` and `links`. `commits` = the members of `C`
  with a `per_commit` hit, in rev-list order.

The **display base** recorded for the commit list and the stat is `remote`, else `fork`, else
`None` (empty tree). The record also stores the `--not` list used for `C`, so the commit list on
screen is exactly `C`.

### D3. Handover patterns are canonicalised once, at load, in `config.py`

`canonical_pattern(p) -> str`, applied to `DEFAULT_HANDOVER_PATHS` and to every
`handover_paths` entry:

- Refuse, naming the repo and the entry: an empty pattern; NUL or any character for which
  `str.isprintable()` is false; a backslash; a `..` segment; `**` inside a segment with other
  characters (`a/**b`, which Python's `full_match` treats as `*b`, **verified**).
- Casefold. Split on `/`. Drop empty and `.` segments (so a leading `/`, `./` and `//` are
  harmless). Drop trailing `**` segments. A pattern that is then empty means "everything" and is
  stored as `**`.
- `RepoRules.handover_paths` holds canonical patterns only. `mirror._normalise` and
  `mirror._literal` are deleted.

### D4. Matching semantics, end to end

- Paths are repo-relative as git prints them with `-z`, decoded as in D11, and casefolded.
  Patterns are anchored at the repository root (`x/.pre-commit-config.yaml` is not
  `.pre-commit-config.yaml`).
- `protected(path, patterns)`: true iff `path` or any of its ancestors `full_match`es a canonical
  pattern, or a pattern is `**`. This is gitignore's directory rule, so `skills` covers
  `skills/x` (closing the ledger's MUST-FIX), `.github/*` covers `.github/workflows/ci.yml`, and
  `.github/workflows` as a file is covered without the literal-directory special case.
- `reaches(path, patterns)`, for symlinks **and gitlinks** in the pushed tip's tree: true iff
  `protected(path)`, or `path` `full_match`es the first k segments of a pattern for some k >= 1,
  or the pattern's first segment is `**`. A link at, inside or above a protected path is always a
  protected change, changed in this push or not (the existing ruling, extended to gitlinks).
- Both functions are public in `mirror.py`, and `handover.py` no longer imports them.
- Case: matching is casefolded on both sides (conservative on Linux, needed for NTFS and APFS
  checkouts). Unicode normalisation is not done; the user's checkouts are on Linux. Recorded as
  accepted.

### D5. The confirmation screen contract

`gitgatepush <id>` prints, in this order, every field passed through `_clean`:

1. `Handover <id>: <reason>`.
2. `Push <sha12> to <url> as <branch>`.
3. For a lease: `This REPLACES <branch> (now <lease12>): a force push.` Then `Removes N
   commit(s) from <branch>:` followed by `git log --oneline sha..lease`, capped at 50, with an
   exact "and M more" line. Today nothing that a force push removes is shown; that is defect I5.
4. `Files under handover paths (N):` followed by every stored `files` entry, uncapped, links
   marked `(symlink)` or `(submodule)`. When there are none: `Files under handover paths: none.`
   The section is always present, so its absence can never be misread.
5. `New commits (N):` followed by `git log --oneline` over `sha --not <stored exclude list>`,
   capped at 50, with an exact "and M more" line.
6. `git diff --stat --no-renames --no-ext-diff --no-textconv <display base or empty tree> sha`,
   capped at 100 files, with an exact "and M more files" line. Only this section may hide names,
   and only names that are not in section 4.
7. `Run: <shlex.join(argv)>` and the prompt.

Guarantees: every protected file is listed; every count is exact; nothing is read from
rules.toml; the argv that runs is the one printed; the SHA and lease are re-validated after the
prompt (as now).

### D6. Handover record lifecycle

- `Handover` gains `patterns: tuple[str, ...]`, `protected: tuple[str, ...]` and `exclude:
  tuple[str, ...]` (the `--not` list of D2). `base` keeps its meaning as the display base.
  `_parse` validates the new fields: tuples of printable strings, and `exclude` entries must be
  OIDs.
- Order in `Gate.push`: create the record, `keep` the ref, and if `keep` raises, `close` the
  record before re-raising (today gate.py:287-288 can leave a record with no pinned commit).
- A record is closed only after a successful push. An expired record's ref is dropped by the
  purge (done in Task 5).
- `gitgatepush` appends one audit line per run that pushes or fails:
  `{"tool": "gitgatepush", "id", "repo", "branch", "sha", "outcome": "pushed" | "failed"}`. The
  spec requires every handover in `audit.log`, and today only the refusal is logged.

### D7. Fetch bundle contract

- **Every** fetch that finds at least one GitHub branch writes a bundle. The bundle lists exactly
  GitHub's `refs/heads/*` at sync time: no tags, no `refs/gate/*`, no HEAD.
- Prerequisites are only parents of `have` entries that the mirror knows as commits. When the
  incremental bundle would omit any branch, the full bundle is written (as now).
- `export_bundle` returns `False` only when GitHub has no branches. The "every tip is in `have`"
  shortcut (mirror.py:185) is removed: a `have` list carries no branch names, so the gate cannot
  know the agent's refs match. **Verified** (`e5.py`, `pt/`): a branch created on GitHub at an
  existing SHA is never delivered.
- The agent's command is `git fetch --prune <bundle> '+refs/heads/*:refs/remotes/origin/*'`.
  **Verified** (`e1/`): `--prune` from a bundle deletes `origin/*` branches the bundle does not
  list. Because every bundle lists every branch, this keeps `origin/*` exactly equal to GitHub.
  That removes the cause behind Review Focus 1 (stale `origin/*` making push prerequisites GitHub
  lacks) and the reply's "delete it with git branch -dr" step.
- `have` is documented as the output of `git for-each-ref --format='%(objectname)'
  refs/remotes/origin`. The reply says that if git reports missing prerequisites, fetch again
  with `have=[]`.

### D8. Push bundle contract

- The bundle must hold exactly `refs/heads/<branch>` (as now). Its prerequisites must be
  connected to the mirror's refs (git enforces this, **verified**).
- **The checks never depend on what the bundle claims.** `C`, `net` and `links` derive only from
  `(sha, remote, default_tip)`. A bundle that builds on a refused handover commit therefore has
  that commit in `C` and is refused again. This is already true; it is pinned so no future
  change computes `C` from the bundle's prerequisites.

### D9. Workspace exchange contract (the gate must not widen the sandbox)

The gate cannot read the sandbox's configuration, so every agent-named path follows one fixed
contract, enforced by one helper in `paths.py`:

- The real path (`fd_path`, as now) is inside a workspace and outside `forbidden`.
- No component of the real path below the workspace root starts with `.`, except a component
  named exactly `.tmp` (the user's scratch convention). This excludes `.git`, `.claude`,
  `.vscode`, `.idea`, `.storage`, `.env`, `.mcp.json`, `.envrc` and every other dotfile.
- The final name has the purpose's suffix: `*.bundle` for push input and fetch output; `*.md`
  or `*.txt` for body files. This excludes `secrets.yaml`, `.git-credentials`, `.storage/*` and
  scripts.
- Reads take a regular file with `st_nlink == 1`, opened with `O_NOFOLLOW | O_NONBLOCK`, and
  are checked with `fstat` *before* `fdopen`. Writes replace only an absent name or a regular
  file with `st_nlink == 1`, by rename (as now).
- Downloads (`gh run download -D`, `release download -O`) go into a directory the gate creates,
  which must not exist and must pass the same component rule; `--clobber` is dropped.
- rules.toml may add `exchange_deny = [globs]`, matched with D4 against the path below the
  workspace root, for names the user's sandbox masks that the suffix rule would let through.

### D10. `rules.toml` schema is strict

- Top-level keys are exactly `workspaces`, `protected_branches`, `repos`,
  `git_timeout_seconds` and `gh_timeout_seconds`; anything else is refused.
- `workspaces` and `protected_branches` must be lists of non-empty strings. Timeouts must be
  `int` and not `bool`, between 1 and 86400.
- Repo keys must `fullmatch` `REPO_RE`, and two keys that are equal after lower-casing are
  refused.
- `protected_branches` keeps `fnmatchcase` semantics (`*` crosses `/`), now documented.

### D11. Text and identifiers

- `Git.run` decodes with `encoding="utf-8", errors="surrogateescape"`. Matching works on the
  decoded `str`, and display escapes surrogates through `_clean`. A non-UTF-8 path name never
  becomes an internal error in the gate or a traceback in `gitgatepush`.
- Every identifier regex is used with `fullmatch` (`REPO_RE`, `OID`, `ID_RE`, `HANDOVER_REF`);
  `re.match` with `^…$` accepts a trailing newline.

### D12. Typed writes and read-only `gh` (constraints for Task 6)

- Body files follow D9. They must decode as UTF-8, be at most 65,536 characters (GitHub's limit
  for bodies and comments; past it GitHub answers 422) and not contain the token.
- Titles are one printable line of at most 256 characters. Label names are at most 50
  characters, colours match `[0-9a-fA-F]{6}`, and label descriptions are at most 100
  characters.
- Numbers and run ids are `type(n) is int and n > 0`. Branch names go through `check_branch`.
  `create` refuses a `head` equal to the default branch.
- `workflow_run` first GETs the run. It refuses `rerun`, `rerun_failed` and `cancel` when the
  run's `head_branch` is the default branch or matches `protected_branches`: rerunning an old
  deploy run redeploys old code, and cancelling one leaves a deploy half done (pending the
  user's call, section 6).
- GitHub's error `message` (scrubbed, truncated to 500 characters) is part of every refusal.
  Every POST sends a JSON body (`{}` when empty). `edit` with no fields is refused before any
  call. `base` is refused for issues. All names are validated before the first call, so a
  failing call never follows a partial write.
- Every typed-write and `gh` reply starts with the exact untrusted prefix.

### D13. One tool list, one dispatcher, no hangs (Task 7)

- `call.TOOLS` is the only list of tool names. `Gate.dispatch(tool, kwargs)` replaces the three
  copies of `gate.call(tool, getattr(gate, tool), **kwargs)` (server.py:46, server.py:116,
  call.py:195).
- `server.build` registers exactly `call.TOOLS`, and a test asserts the two sets are equal.
- `python -m git_gate call` with the wrong number of arguments prints usage and exits 2, never
  starting the stdio server.

### D14. Trusting what `gitgatepush` runs (Task 8)

- `install.sh` links `${GIT_GATE_BIN_DIR:-/usr/local/bin}/gitgatepush` to
  `$ROOT/current/bin/gitgatepush` after `switch_to` and in `rollback`.
- It then checks that `command -v gitgatepush` in the installing shell resolves to that link,
  and warns, naming the shadowing path, if not.
- `bin/gitgatepush` refuses to run unless `readlink -f "$0"` is under `/opt/git-gate/releases/`
  (or `$GIT_GATE_ROOT/releases/`).
- `switch_to` no longer says "Restart Claude Code sessions": with the relay that is false.

---

## 3. Defects in the built code (Tasks 0-5)

### Critical

**C1. The gate reads sandbox-masked secrets and uploads them to GitHub.**
paths.py:55-68 (`read_workspace_file`) and gate.py:352-358 (`Gate.gh` body uploads).

- Any regular file whose real path is in a workspace is read, including the files this
  session's sandbox masks inside workspaces (`**/.env`, `**/secrets.yaml`, `**/.storage`,
  `**/.git-credentials`, `**/.gitconfig`).
- The gate runs outside the sandbox, so it reads the real file. `gh pr comment 7 --body-file
  /ws/ha-config/secrets.yaml` posts it as the user, and the agent reads it back with
  `gh pr view`.
- **Verified:** the gate reads a workspace `.env` (`e7/`). The masking is visible in this repo's
  root, where `.gitconfig` and `.mcp.json` show as `/dev/null` character devices inside the
  sandbox.
- **Fix:** D9's read rules in one helper used by `read_workspace_file` and `copy_in`, with the
  `.md`/`.txt` suffix for bodies. A test proves `--body-file …/secrets.yaml` and `…/.env` are
  refused before `gh` runs.

### Important

**I1. The handover screen omits protected changes the gate refused for.**
handover.py:290-302, 305-345, 348-368; gate.py:277-286; mirror.py:228-251. This includes the
ledger's open item from 3ca2be9.

- `_protected` lists only per-commit `--cc` diffs, so a merge that rolls a workflow back is
  absent from "Files under handover paths". **Verified** (`e5.py`): the gate refuses, the
  section is missing, and the workflow appears only in the capped stat.
- It also re-reads patterns from rules.toml at display time, falling back to the defaults, so a
  repo's extra `handover_paths` vanish from the screen when rules.toml is broken or has changed.
- The base is computed twice. `touched` uses `merge-base(remote, sha)` (mirror.py:242), while
  the record stores `remote` (gate.py:286). For a rewrite, the decision and the screen compare
  against different commits.
- **Fix:** D1, D2, D5 and D6. Delete `handover._protected`, `_handover_paths` and
  `Mirror.touched`; store `Protected` in the record; print it as stored.

**I2. Merging main into an already pushed branch is refused after any workflow change on
main.** gate.py:277-280, mirror.py:223-226.

- For an existing branch, `new_commits(sha, remote)` includes the default branch's own commits
  brought in by the merge, and the per-commit check flags the user's workflow commit.
- **Verified** (`pt/test_probe.py::test_existing_branch_merging_main_after_workflow_change`):
  "Commit(s) 3a46580b0a73 touch a handover path", naming the user's own main commit.
- This is the HA repos' everyday flow, so every such push becomes a handover.
- **Fix:** D2 (`C` excludes `default_tip`, and `net` is the intersection rule).

**I3. Fetch silently drops new branches.** mirror.py:185-186, gate.py:244-245.

- When every GitHub tip SHA is in `have`, no bundle is written and the reply says "Up to date",
  even if a branch name is new (for example `release/1.2` created at main's SHA). The agent
  never gets `origin/release/1.2`. This breaks the Task 3 ruling that "every branch tip is
  always in the bundle".
- **Verified** (`e5.py`, `pt/`).
- **Fix:** D7. Return `False` only without branches; add `--prune` to the reply and the
  docstrings.

**I4. Fetch output and gh downloads overwrite files the sandbox write-protects.** paths.py:106-133
(`write_out`), paths.py:145-180 (`copy_tree_into`), gate.py:246 and gate.py:385-390.

- `write_out` replaces any regular file in a workspace.
- **Verified** (`pt/test_probe.py::test_bundle_output_over_masked_file`): a workspace
  `.mcp.json` became `# v2 git bundle…`. The same works for `.claude/settings.json`, whose loss
  can drop that project's permission and sandbox settings.
- `gh run download -D <ws> --clobber` writes artifact content the agent can shape (build output
  from a branch it pushed) to arbitrary names, including `.claude/settings.json` hooks and
  `.mcp.json` servers that run unsandboxed.
- **Fix:** D9 (the `.bundle` suffix and the dot-component rule for `write_out`; downloads only
  into a new gate-created directory).

**I5. A force-push handover does not show what it removes.** handover.py:225-232, 319-325.

- With a lease, only `base..sha` is listed, so the commits on the remote branch that the push
  discards are never named. The user sees a short, harmless-looking list for a rewrite.
- Reasoned from the code; there is no test.
- **Fix:** D5 item 3.

**I6. Handover-pattern normalisation fails open.** mirror.py:292-315, config.py:69-71.

- `"skills"` does not cover `skills/x` (this is the ledger's MUST-FIX). `"a/../.githooks/**"`
  and backslash patterns match nothing. `"skills/**x"` silently means `*x`.
- **Verified** (an inline `matches()` probe run during this review, plus the rules load in
  `e7/`).
- **Fix:** D3 and D4.

**I7. rules.toml typos weaken protection silently.** config.py:94-118.

- `protected_branches = "main"` protects nothing by pattern, and unknown keys are ignored.
  `int()` on a bad timeout raises `ValueError`, an internal error rather than a refusal.
- **Verified** (`e7/`).
- **Fix:** D10.

### Minor

**M1. Gitlinks are not treated like symlinks.** mirror.py:253-260, handover.py:364-367.

- A gitlink at `skills/x` moved under the pattern `skills/*/hooks/**` is neither matched nor
  "reached".
- **Fix:** D4 (`reaches` covers modes 120000 and 160000 in the same `ls-tree` pass).

**M2. `Gate._fork_point` can fall back to the alphabetically first branch** (gate.py:316), a
branch an agent can create. In practice it is unreachable, because the default head is present
whenever any branch is.

- **Fix:** D2 (no fallback).

**M3. The handover record is written before its ref is kept** (gate.py:287-288). A failing
`keep` leaves an open record whose commit is not pinned.

- **Fix:** D6 (close the record on failure).

**M4. `read_workspace_file` raises `IsADirectoryError`, an internal error rather than a refusal,
for a directory** (paths.py:58-61, **verified**). Its open logic is duplicated in `copy_in`
(paths.py:74-90).

- **Fix:** one `_open_exchange(rules, path, purpose)` helper that runs `fstat` before `fdopen`
  (D9).

**M5. `copy_in` and `read_workspace_file` accept hard links** (paths.py:58, 74). This is not
exploitable from this sandbox: **verified**, `ln` onto a masked file fails with EXDEV, because
the masks are bind mounts. It is defence in depth.

- **Fix:** `st_nlink == 1` (D9).

**M6. Strict UTF-8 decoding of git output** (gitops.py:71-79). A non-UTF-8 file name in an agent
commit turns a push into an "internal error" (fails closed, but with a misleading message) and
crashes `gitgatepush` with a traceback.

- **Fix:** D11.

**M7. The rate limiter creates one file per unvalidated repo string** (gate.py:77, 103), so a
looping agent can create unbounded files in `state/rate`.

- **Fix:** key the limiter on `repo.lower()` only after `REPO_RE.fullmatch`, and count anything
  else under one shared key.

**M8. `gitgatepush` pushes are not audited** (handover.py:244-259).

- **Fix:** D6.

Severity count for this section: Critical 1, Important 7, Minor 8.

---

## 4. Triage of the ledger's deferred minors

| Ledger item | Verdict | Reason |
|---|---|---|
| T0: OSError, bad JSON and timeout share one message | Task 7 | Name the cause class in the refusal; the relay is touched there anyway |
| T0: relay does not check `current` resolves under `releases/` | Accept | Only root can repoint `current` |
| T0: `errors.log` umask and unbounded growth | Task 7 | Open with 0o600; roll over at 1 MiB |
| T0: `gate.call(...)` repeated three times | Task 7 | D13 (`Gate.dispatch`) |
| T0: `build(gate=, relay=)` ignores `gate` | Task 7 | Refuse both together; trivial |
| T0: bad-arity `git-gate call` starts the stdio server and blocks | Task 7 | D13; it violates the "no hang" anti-goal |
| T0: relay test gaps | Task 7 | Its tests are rewritten there |
| T0: `Relay._gate` created lazily without a lock | Accept | Fallback path for pre-relay releases only; two Gates are harmless |
| T0: relay `errors.log` keeps child stderr unscrubbed | Accept | The relay process never holds the token; the file is in the 0700 state dir |
| T0: `call.main` writes an unscrubbed traceback | Accept | Same file and directory; nothing reaches the agent |
| T1: `check_enabled` uses `.exists()`, so a dangling symlink reads as enabled | **Task 5b** | An off switch must fail closed; use `os.path.lexists` |
| T1: no test for `check_enabled()` falling back to `config_dir()` | **Task 5b** | Lands with the fix above |
| T2: bodyless POSTs may draw 411 | Task 6 | D12 (always send `{}`) |
| T2: label removal `absent=(404,)` hides a missing issue | Task 6 | Reply "not present" rather than claiming success |
| T2: `Refused` drops GitHub's message | Task 6 | D12 |
| T2: `edit()` sends `base` for issues and `PATCH {}` | Task 6 | D12 |
| T2: `edit(state=)` not validated | Task 6 | Actions map to states in `writes.py` |
| T2: `labels()` adds before validating remove names | Task 6 | D12 (validate everything first) |
| T2 carry: every branch reaching GitHub passes `check_branch` | Task 6 | Already a constraint; restated in D12 |
| T3: refused filtered bundle leaves a `.promisor` pack | Accept | **Verified** to fail closed: later bundles building on it are refused as "not connected" (`e3/`) |
| T3: mirror.py:147 wrong message | Accept | Cosmetic |
| T3: `keep()` accepts any object type | Accept | Its only caller passes the SHA `import_bundle` proved is a commit |
| T3: `_clear_incoming` clears every branch | Accept | Runs under the mirror lock; incoming refs are transient |
| T3: glob-free pattern fails open (MUST FIX) | **Task 5b** | D3 and D4 (I6) |
| T3: `set(have)` raises `TypeError` on unhashable entries | Accept | gate.py:226 validates `have` first |
| T3: gitlinks at handover paths | **Task 5b** | D4 (M1); one extra mode in an existing pass |
| T4: expired records leave refs | Accept | Done: `Handovers(state, mirror=)` at gate.py:56 |
| T4: `gitgatepush` ignores `git_timeout_seconds` | Accept | Deliberate, so a broken rules.toml never blocks a handover |
| T4: `ID_RE`/`OID`/`REPO_RE` use `match` with `^…$` | **Task 5b** | D11; one sweep |
| T4: strict decoding can raise after a successful push | **Task 5b** | D11 (M6) |
| T4: a signal-killed git gives a negative returncode | Accept | The exit status stays non-zero |
| T4: mirror lock held across ssh prompts | Accept | As specified; the confirmation itself is outside the lock |
| T4: test gaps (expired record via `main`, double run, real URL) | **Task 5b** | Added as tests below |
| T4: `bin/gitgatepush` comment says install links it | Accept | Task 8 makes it true |
| T4: `REPO_RE`/`OID` `fullmatch` across files | **Task 5b** | Same as D11 |
| T4: file cap follows git's path order | Accept | D5 guarantees every protected file and exact counts; the rest is what the compare view shows |
| T4: `handover.py` imports a private matcher | Accept (superseded) | D1 removes the import |

---

## 5. Plan amendments

### New task (insert after Task 5)

```markdown
### Task 5b: Fixes from the planning review

**Files:**
- Modify: `src/git_gate/config.py`, `src/git_gate/mirror.py`, `src/git_gate/handover.py`,
  `src/git_gate/gate.py`, `src/git_gate/paths.py`, `src/git_gate/gitops.py`, `src/git_gate/server.py`
  (fetch/push docstrings only)
- Test: `tests/test_config.py`, `tests/test_mirror.py`, `tests/test_handover.py`, `tests/test_push.py`,
  `tests/test_fetch.py`, `tests/test_gh.py`, `tests/test_paths.py` (new)

**Interfaces:**
- Produces in `config.py`: `canonical_pattern(p: str) -> str` (raises `Refused`); `DEFAULT_HANDOVER_PATHS`
  and `RepoRules.handover_paths` hold canonical patterns; `load_rules` refuses unknown top-level keys, non-list
  `workspaces`/`protected_branches`, non-int or out-of-range timeouts, duplicate repo keys after lower-casing;
  `check_enabled` uses `os.path.lexists`; every regex check uses `fullmatch`.
- Produces in `mirror.py`: `protected(path, patterns) -> bool`; `reaches(path, patterns) -> bool`;
  `@dataclass(frozen=True) class Protected(files: tuple[str, ...], commits: tuple[str, ...])` with
  `__bool__ = files != ()`; `Mirror.protected_changes(repo, sha, remote: str | None, default_tip: str | None,
  patterns, rules) -> Protected`; `Mirror.fork_point(repo, sha, default_tip, rules) -> str | None`;
  `export_bundle` returns False only when the mirror has no branches.
  Deletes: `touched`, `_commit_touches`, `_symlinks`, `matches`, `link_reaches`, `_normalise`, `_literal`.
- Produces in `handover.py`: `Handover` gains `patterns: tuple[str, ...]`, `protected: tuple[str, ...]`,
  `exclude: tuple[str, ...]`; `Handovers.create(repo, branch, sha, base, lease, reason, *, patterns, protected,
  exclude)`; `_show` prints D5's sections from the record. Deletes: `_protected`, `_handover_paths`.
- Produces in `paths.py`: `open_exchange(rules, path, purpose: str) -> tuple[int, Path]` (purpose `"bundle"`
  or `"body"`), used by `copy_in` and `read_workspace_file`; `write_out` requires a `.bundle` name;
  `copy_tree_into` writes only into a directory the gate created.
- Produces in `gitops.py`: `Git.run` decodes `utf-8` with `surrogateescape`.
- `Gate.push` computes `remote`, `default_tip`, `fork` once (D2), calls `protected_changes` once, records
  `Protected` in the handover, closes the record if `keep` raises. `Gate.fetch` reply and docstring use
  `git fetch --prune <bundle> '+refs/heads/*:refs/remotes/origin/*'`.

- [ ] **Step 1: Write failing tests:**
  - `test_config.py::test_handover_patterns_are_canonical` (parametrised): `"skills"`, `"skills/"`, `"skills/**"`,
    `"/Skills/**"`, `"./skills//**"` → `"skills"`; `".github/workflows/**"` → `".github/workflows"`; `"**"` and `"/"`
    → `"**"`; `"skills/*/SKILL.md"` → `"skills/*/skill.md"`.
  - `test_config.py::test_bad_handover_patterns_refused` (parametrised): `"a/../b"`, `"a\\b"`, `"a/**b"`, `""`,
    `"a\x00b"`, `"a\nb"`, `"a‮b"` → `Refused` naming the repo and the pattern.
  - `test_config.py::test_rules_schema_is_strict` (parametrised): `protected_branches = "main"`, `protected_branch = []`,
    `workspaces = "/x"`, `git_timeout_seconds = true`, `= 0`, `= "5"`, keys `"Me/App"` and `"me/app"` together →
    each `Refused` naming the key.
  - `test_config.py::test_off_switch_dangling_symlink`: `disabled` is a symlink to a missing file → `Refused`
    with the exact off-switch message.
  - `test_config.py::test_check_enabled_defaults_to_config_dir`: `GIT_GATE_CONFIG_DIR` set, `disabled` there,
    `check_enabled()` → `Refused`.
  - `test_config.py::test_identifiers_fullmatch`: `rules.repo("me/app\n")` → `Refused` "not an owner/name";
    `mirror._oid(sha + "\n")` → `Refused`.
  - `test_mirror.py::test_protected_table` (parametrised over (path, pattern, expected)):
    (`skills/x`, `skills`, True), (`skillsx`, `skills`, False), (`.github/workflows`, default, True),
    (`.GitHub/Workflows/ci.yml`, default, True), (`x/.pre-commit-config.yaml`, default, False),
    (`.github/workflows/a.yml`, `.github/*`, True), (`skills/a/b/c`, `skills/*/b`, True), (`anything`, `**`, True).
  - `test_mirror.py::test_reaches_table`: (`.github`, default, True), (`.githooks`, default, True),
    (`skills/x`, `skills/*/hooks/**`, True), (`docs`, default, False), (`a/b`, `**/hooks`, True).
  - `test_mirror.py::test_protected_changes_*`, each asserting `files` and `commits` exactly (port every
    `test_touched_*` and add):
    - `..._clean_merge_of_main_passes` (new branch) → `Protected((), ())`.
    - `..._existing_branch_merges_main_after_user_workflow_change_passes`: remote = pushed topic, main gains
      `.github/workflows/ci.yml`, topic merges main → `Protected((), ())`.
    - `..._rollback_merge_names_the_file` → `files == (".github/workflows/ci.yml",)`, `commits == ()`.
    - `..._evil_merge` → file listed, `commits == (merge,)`.
    - `..._changed_and_changed_back` → file listed, `commits == (one, two)`.
    - `..._rewrite_with_new_protected_content`: remote holds a user-pushed workflow edit, sha rewrites it to
      a third version → file listed.
    - `..._rewrite_back_to_the_fork_version_is_not_protected`: remote holds a user-pushed workflow edit, sha
      rewrites without it → `Protected((), ())` (the push is still a lease handover).
    - `..._fast_forward_keeping_the_remotes_protected_change_passes` → `Protected((), ())`.
    - `..._stale_fork_with_old_workflows_is_protected` (user decision 4): main gains a workflow fix
      after the agent forked; a new branch at the old fork with an unrelated change →
      `files == (".github/workflows/ci.yml",)`, `commits == ()`.
    - `..._stale_fork_without_workflow_drift_passes`: main moves on with no handover-path change →
      `Protected((), ())`.
    - `..._existing_branch_keeping_its_old_workflows_passes`: remote already holds the old workflows,
      sha adds unrelated commits → `Protected((), ())`.
    - `..._gitlink_and_symlink_entries`: `.githooks` added as gitlink via `update-index --cacheinfo 160000,<sha>,.githooks`
      → listed; unchanged symlink `.github -> ci` → listed.
    - `..._no_default_branch_checks_all_history`: `default_tip=None`, root commit touching `.githooks/x` → listed.
    - `..._non_utf8_name`: a commit adding `.githooks/\xff` (via `update-index` with a raw path) → listed,
      no exception.
  - `test_mirror.py::test_export_always_lists_every_branch`: GitHub gains `release-1` at main's sha;
    `have=[main sha]` → returns True, `list-heads` == {main, release-1}; `have` = every tip → True.
  - `test_mirror.py::test_export_false_only_without_branches`.
  - `test_handover.py::test_main_prints_stored_protected_files_first_and_uncapped`: record with 150 stored
    `protected` names and no rules.toml → all 150 appear before `New commits`; no "Note:" line.
  - `test_handover.py::test_main_always_prints_the_protected_section`: record with `protected=()` → output
    contains `Files under handover paths: none.`
  - `test_handover.py::test_main_discloses_a_rollback_merge`: the `e5.py` scenario pushed through `Gate.push`,
    then `main([id])` answered `n` → contains `Files under handover paths (1):` and `.github/workflows/ci.yml`.
  - `test_handover.py::test_main_rewrite_lists_what_it_removes`: remote has 2 commits not in sha → output contains
    `REPLACES`, `Removes 2 commit(s) from feat:` and both subjects.
  - `test_handover.py::test_main_audits_the_push`: after `y`, the last `audit.log` line has `tool == "gitgatepush"`,
    `outcome == "pushed"`, the id and sha; after a failed push, `outcome == "failed"`.
  - `test_handover.py::test_main_second_run_of_a_closed_id`: run `y` twice → second exits 1 with "No open handover".
  - `test_handover.py::test_main_survives_non_utf8_names`: stored name with a surrogate → printed as `?`, exit 0 on `n`.
  - Delete `test_main_uses_default_handover_paths_when_rules_cannot_be_loaded`,
    `test_main_survives_broken_rules_and_uses_default_paths`, `test_main_names_extra_handover_paths_from_the_repo_rules`
    (behaviour removed by D1); the stored-patterns test replaces them.
  - `test_push.py::test_merging_main_into_a_pushed_branch_after_a_workflow_change_passes` → "Pushed".
  - `test_push.py::test_handover_record_matches_the_refusal` (parametrised over the refusal cases):
    `h.protected` equals the files named in the refusal message; `h.patterns == rules.repo(repo).handover_paths`;
    `h.exclude` is `[remote, default_tip]` minus `None`.
  - `test_push.py::test_keep_failure_leaves_no_open_record`: `Mirror.keep` patched to raise `Refused` →
    `Refused`, `Handovers(state).open() == []`.
  - `test_push.py::test_handover_base_is_never_another_branch`: an agent branch `aaa` exists; new branch push
    refused → `h.base` is the fork with main, not `aaa`'s tip.
  - `test_fetch.py::test_fetch_delivers_a_new_branch_at_a_known_sha`: as `pt/test_probe.py`; reply says
    `Wrote 2 branch(es)`; `git fetch --prune` in the clone creates `origin/release-1`.
  - `test_fetch.py::test_fetch_prune_removes_branches_gone_from_github`: delete `gone` on the remote, fetch
    with `have` = all tips, run the reply's command → `origin/gone` absent.
  - `test_fetch.py::test_fetch_output_must_be_an_exchange_name` (parametrised): `.mcp.json`, `x.txt`,
    `.claude/settings.json`, `.git/x.bundle`, a hard-linked `x.bundle` → `Refused`, target bytes unchanged;
    `.tmp/x.bundle` and `out/x.bundle` succeed.
  - `test_paths.py::test_exchange_reads` (parametrised): directory → `Refused` (not `IsADirectoryError`);
    hard link → `Refused`; `.env`, `secrets.yaml`, `.storage/x.md`, `a/.git/b.md`, symlinked parent resolving
    into `.claude/` → `Refused`; `.tmp/body.md`, `notes/body.txt` → bytes returned.
  - `test_gh.py::test_gh_body_file_must_be_an_exchange_file`: `--body-file <ws>/secrets.yaml` → `Refused`,
    fake `gh` never invoked.
  - `test_gh.py::test_gh_download_goes_into_a_new_directory`: `-D <existing dir>` → `Refused`; `-D <new dir>` →
    files written there only; `--clobber` → `Refused`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q` → the new tests FAIL.
- [ ] **Step 3: Implement** D1-D11 as pinned in `.superpowers/sdd/2026-10-25-gate-rework/planning-review.md`.
  Delete the replaced functions; do not keep compatibility shims.
- [ ] **Step 4: Run** `./scripts/ci.sh` → pass; `grep -n "_protected\|_handover_paths\|def touched\|_normalise\|link_reaches" src/git_gate/*.py` → nothing.
- [ ] **Step 5: Commit**, one change per commit:
  `fix: canonicalise handover patterns and validate rules strictly`,
  `fix: decide and disclose protected changes with one function`,
  `fix: let pushed branches merge the default branch`,
  `fix: always bundle every branch and prune on fetch`,
  `fix: exchange only suffixed non-dot files with workspaces`,
  `fix: decode git output losslessly and fullmatch identifiers`,
  `fix: audit gitgatepush and fail the off switch closed`.
```

### Task 6 amendments (append to its Interfaces, then its Step 1)

```markdown
- Body files: `paths.read_workspace_file(rules, path, "body")` under the exchange contract (D9): `*.md`/`*.txt`,
  no dot component except `.tmp`, nlink 1, UTF-8, at most 65 536 characters, no token.
- `triage(..., add_reviewers=(), remove_reviewers=())` (ledger ruling); every name validated before the first call.
- Titles: one printable line, at most 256 characters. Labels: name at most 50 characters, colour `[0-9a-fA-F]{6}`,
  description at most 100. Numbers: `type(n) is int and n > 0`.
- `workflow_run` GETs `repos/<repo>/actions/runs/<id>` first and refuses every action when `head_branch` is the default
  branch or matches `protected_branches` (refusal says to use the web UI).
- Refusals carry GitHub's `message` (scrubbed, at most 500 characters). Every POST sends a JSON body.
- `gh` downloads: the target directory must not exist; the gate creates it; `--clobber` is refused.
```

```markdown
  - `test_body_file_rules` gains: `secrets.yaml`, `.env`, `.claude/x.md`, a hard link, invalid UTF-8,
    65 537 characters → `Refused`; `.tmp/body.md` → the body sent equals the file.
  - `test_numbers_are_strict_ints` (parametrised): `True`, `0`, `-1`, `"7"`, `7.0` → `Refused`.
  - `test_workflow_run_on_default_branch_refused` (parametrised over rerun, rerun_failed, cancel): fake run with
    `head_branch == "main"` → `Refused` mentioning the web UI; no POST recorded.
  - `test_github_message_reaches_the_agent`: fake 422 `{"message": "Validation Failed"}` → `Refused` containing it.
  - `test_triage_validates_before_writing`: one bad remove name → no call recorded at all.
  - `test_edit_without_fields_refused`, `test_issue_edit_base_refused`.
  - `test_replies_are_marked_untrusted` also covers refusals that carry GitHub text.
```

### Task 7 amendments

```markdown
**Interfaces (add):**
- `Gate.dispatch(tool: str, kwargs: dict) -> str` — the only caller of `Gate.call` with `getattr`; used by
  `server.build`, `Relay` fallback and `call.run`.
- `call.TOOLS` is the single tool-name list (ten names); `server.build` registers exactly those.
- `python -m git_gate call` with other than one tool argument prints usage to stderr and exits 2.
- `INSTRUCTIONS` and docstrings carry these exact agent commands:
  `git bundle create <dir>/.tmp/<name>.bundle <branch> --not --remotes=origin`,
  `git fetch --prune <bundle> '+refs/heads/*:refs/remotes/origin/*'`,
  `have`: `git for-each-ref --format='%(objectname)' refs/remotes/origin`,
  and "bundle and body files must be *.bundle / *.md|*.txt outside dot directories (except .tmp)".
- `errors.log` opened with mode 0o600 and rolled over at 1 MiB; relay refusals name the cause (no answer,
  timeout, could not start).

- [ ] Step 1 tests (add):
  - `test_server_tools_equal_call_tools`: `set(registered) == set(call.TOOLS)`.
  - `test_dispatch_is_the_only_path`: `grep`-style test that `getattr(gate, tool)` appears once in `src/`.
  - `test_call_with_bad_arity_exits_without_serving`: `subprocess.run([... "call"], timeout=5)` → returncode 2.
  - `test_instructions_carry_the_agent_steps` asserts the four strings above and `gitgatepush`, "the user merges".
  - `test_relay_timeout_and_oserror_messages_differ`.
  - `test_build_refuses_gate_and_relay_together`.
```

### Task 8 amendments

```markdown
- `install.sh`: after `switch_to` and in `rollback`, link `${GIT_GATE_BIN_DIR:-/usr/local/bin}/gitgatepush`;
  then `command -v gitgatepush` must equal that path, else print a warning naming the shadowing path.
  `switch_to` stops telling the user to restart sessions (the relay applies installs to the next call).
- `bin/gitgatepush` exits 1 with a message unless `readlink -f "$0"` is under `${GIT_GATE_ROOT:-/opt/git-gate}/releases/`.
- Step 1 dry run adds: a fake earlier-on-PATH `gitgatepush` → install prints the warning; a copy of
  `bin/gitgatepush` outside releases → refuses to run.
- `docs/smoke.md` adds, each with its expected reply:
  - fetch after the user creates a branch at main's sha in the web UI → the branch arrives (I3 regression);
  - push a branch that merges main after a workflow change on main → "Pushed" (I2 regression);
  - a handover whose confirmation shows "Files under handover paths" for a rollback merge (I1 regression),
    run with `n`;
  - a bodyless typed write (`workflow_run cancel` on a feature-branch run, `pr ready`) → no 411;
  - push on a private Free-plan repo → the 403 "Upgrade to GitHub Pro" path passes.
- `docs/skills-migration.md` adds: bundles and body files live in `<repo>/.tmp/`; fetch uses `--prune`; how to
  compute `have`.
```

---

## 6. Spec items for the user to decide

1. **The gate as confused deputy (D9).** The spec says only "paths must be inside a workspace".
   The sandbox also masks secrets and write-protects unsandboxed-config files *inside*
   workspaces, and the gate runs outside it. This review pins a fixed contract (suffix
   allowlist, no dot components except `.tmp`, nlink 1, plus an optional `exchange_deny` in
   rules.toml). Your choice: accept that contract (recommended), or mirror your sandbox deny
   globs into rules.toml as the primary rule, which fails open whenever the two drift apart.

2. **The default handover paths miss files that run outside the sandbox.** The sandbox masks
   `.mcp.json`, `.claude/`, `.vscode/`, `.idea/` and dotfiles in the repo root, which shows these
   are treated as dangerous. A pushed branch can still change them, and they run unsandboxed once
   checked out by you or opened in an IDE. Candidates for the defaults: `.claude/**`, `.mcp.json`,
   `.vscode/**`, `.idea/**`, `.envrc`, `.devcontainer/**`. Separately, the defaults protect hook
   *config* but not the scripts it calls (`entry: ./scripts/lint.sh`, `.githooks/pre-commit`
   calling `scripts/ci.sh`). Per-repo `handover_paths` must list those scripts, or the protection
   is cosmetic for hooks. Your call on which to add.

3. **The spec's base rule needs amending.** "The net change against the remote branch (or its
   fork point from the default branch)" read literally refuses the everyday "merge main into my
   pushed branch" after a workflow change on main (I2, verified). D2 replaces it: per-commit
   checks cover only commits new to both the branch and the default branch, and a net change
   counts only if it differs from both the remote branch and the fork point. Please confirm.

4. **Stale forks run stale workflows with secrets.** "Stale fork passes" lets an agent push a
   branch at any old default-branch commit. That branch's push-triggered workflows are the old
   versions, possibly ones you later fixed for security, and they run with the repo's secrets.
   Options: accept (the current spec); or require `.github/workflows/**` in the pushed tip to
   equal the default tip's or the remote's, so stale branches must rebase or become handovers.
   Unverified: whether GitHub's no-Workflows-permission check also blocks such a push (it
   creates no new workflow objects, so probably not).

5. **Workflow reruns and cancels on deploy branches.** The spec lists "rerun or cancel a workflow
   run" without limits. On push-deploy repos, rerunning an old default-branch run redeploys old
   code, and cancelling a deploy leaves it half done. D12 refuses both for runs on the default or
   protected branches. Confirm, or name the run kinds agents need.

6. **"The gate's own git config" for the handover push.** The spec (Handover, third bullet) says
   `gitgatepush` pushes "with hooks off and the gate's own git config". The plan's exact command
   and the built code use *your* global git config, which is needed for your SSH setup (for
   example `core.sshCommand` under WSL), with only hooks forced off. Your config is trusted, so
   this is safe; amend the spec to say "the user's own git config, hooks off".

7. **Push-time code execution in CI is outside what handover paths can protect.** On
   deploy-on-push repos, any pushed branch runs its own build scripts in Actions with whatever
   secrets the workflow exposes (for example a Cloudflare API token). `push_deploys` only
   announces this. The fix lives in the workflows (deploy secrets only in an environment
   restricted to the default branch), not in the gate. Note it in `docs/skills-migration.md` or
   accept.

8. **Tool shapes freeze per conversation.** Because the tool list is fixed per running server,
   `fetch(repo, bundle, have: list[str])`, `push(repo, branch, bundle)` and Task 6's typed tools
   (including `triage.remove_reviewers`) must be final in this release. D7 keeps `have` as a
   list and makes it safe by always bundling every ref. Confirm before Task 7 registers them.
