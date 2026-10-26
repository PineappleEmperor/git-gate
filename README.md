# git-gate

An MCP server that gives sandboxed Claude Code agents GitHub access while the token stays outside
the sandbox. Agents get `clone`, `fetch`, `push` and `prune`, typed reads (`pr` and `issue` views
and lists, `checks`, `run`, `runs`, `release`, `file`, `api_get`) and typed writes (`pr`, `issue`,
`triage`, `workflow_run`, `label`). There is no `gh`. Anything the gate will not do for an agent
becomes a handover the user runs with `gitgatepush <id>`.

- Design: [docs/superpowers/specs/2026-10-25-gate-rework-design.md](docs/superpowers/specs/2026-10-25-gate-rework-design.md)
  (current). The earlier [docs/design.md](docs/design.md) and [docs/plan.md](docs/plan.md) are
  superseded for git and `gh` mechanics; plan.md still holds the token, config and install layout.
- What the skill repositories must change: [docs/skills-migration.md](docs/skills-migration.md).
- The live check on a real repo: [docs/smoke.md](docs/smoke.md).

## What agents do

Agents never run `git push`, `git fetch` against GitHub, `gh` (reads included) or SSH. Files cross
the sandbox boundary as bundles and body files:

- Bundles end `.bundle`; body files end `.md` or `.txt`. No folder in the path may be dot-named
  except `.tmp`, so `<repo>/.tmp/` is the place for both.
- Push: `git bundle create /abs/path/.tmp/x.bundle <branch> --not --remotes=origin`, then the
  gate's `push(repo, branch, bundle)`.
- Fetch: the gate's `fetch(repo, bundle, have)`, then
  `git fetch --prune <bundle> '+refs/heads/*:refs/remotes/origin/*'`. `have` is the output of
  `git for-each-ref --format='%(objectname)' refs/remotes/origin`.
- GitHub writes go through the typed tools. Approving and merging PRs, force-pushes, tags and
  releases, and changes under handover paths are the user's.

When the gate refuses a push the user should make, the refusal says why and names
`gitgatepush <id>`. The user runs it in their own terminal: it shows the repo, branch, commits and
files under handover paths, asks, and pushes with the user's own git config and SSH key, hooks off.
`gitgatepush` with no id lists open handovers. Handovers expire after 14 days.

## Tools

Every tool takes `repo` as owner/name, needs the session's access to it (read for reads, write for
writes), counts once against the per-repo rate limit and writes one audit line. Every reply that
carries GitHub's text begins with a line marking it as GitHub content, and that text is cleaned of
escapes and control characters.

- `clone`, `fetch`, `push`, `prune`: git, through bundles (above).
- `pr`: read with `view`, `list` (state open, closed, merged or all; head, base, limit, search) and
  `diff`;
  write with `create`, `edit`, `comment`, `review_comment`, `request_changes`, `close`, `reopen`,
  `ready`.
- `issue`: read with `view` (with the last 20 comments) and `list` (state, labels, limit, search);
  write with `create`, `edit`, `comment`, `close`, `reopen`.
- `search` on `pr` and `issue` `list` runs GitHub's issue search, one page, with `repo:<repo>` and
  `is:pr` or `is:issue` put first by the gate. Text of at most 256 printable characters; refused if it
  holds a `repo:`, `org:`, `user:` or `owner:` qualifier or `is:public`/`is:private` in any form
  (quoted, negated, spaced), since GitHub ORs repeated `repo:` qualifiers, or `OR`, `AND`, `NOT`,
  parentheses, backslashes or double quotes. Label names in a search get the same checks and may hold
  no `x:` word at all. GitHub allows 30 searches a minute; a rate-limited search is refused, not
  retried.
- `checks(repo, ref, wait=False, timeout_minutes=20)`: whether CI passed on a pull request (`12` or
  `"#12"`), branch or commit SHA. One line per check run and commit status (name, state,
  conclusion, duration, Actions run id, URL), then a verdict: `All N passed`, `N failing: <names>`,
  `N still running` or `No checks reported`. Neutral and skipped count as not failing.
- `run(repo, run_id, wait=False, timeout_minutes=20)`: one Actions run, its jobs, the failed steps
  of failed jobs and, for a failed run, the last 80 lines of each failed job's log (at most 10 jobs;
  each log download capped at 2 MiB, read from its end), then a verdict.
- `runs(repo, branch, workflow, status, limit)`: the newest Actions runs, one line each.
- `release(repo, action, tag, limit)`: `list`, or `view` of a tag or the latest release. Assets are
  listed, not downloaded.
- `file(repo, path, ref)`: one text file (up to 1 MiB) or a directory listing, at a ref.
- `api_get(repo, path, query)`: a GET of one endpoint under `repos/{owner}/{name}/`, JSON
  pretty-printed. No other host, no `..`, no GraphQL, no jq.
- `triage`, `label`, `workflow_run`: labels, assignees and reviewers; label definitions; rerun or
  cancel a run off the default and protected branches.

With `wait=true`, `checks` and `run` poll GitHub inside the one call: every 15 s, then 30 s, then
every 60 s, until nothing is queued or in progress. A rate limit is waited out rather than failing:
for GitHub's `Retry-After`, or until its quota resets, when that lies ahead, otherwise 60 s doubling
to 5 minutes; no wait is shorter than 15 s. Before each poll after the first the gate checks again
that it is switched on, that the session still has access, and that the token is unchanged, and
refuses if not. After `timeout_minutes` (at most 20, well inside the 30 minutes after which Claude
Code drops a silent stdio tool call) the call answers with the state so far and says it is still
running. Merging stays the user's.

## Develop

```bash
~/.pyenv/versions/3.14.4/bin/python -m venv .venv
.venv/bin/pip install --require-hashes --only-binary :all: --no-deps -r requirements.lock
.venv/bin/pip install pytest ruff
./scripts/ci.sh
```

## Install a release

From your own terminal, never an agent session. First install:

```bash
git -C ~/git-gate cat-file blob <sha>:install.sh > ~/git-gate-install.sh
sh ~/git-gate-install.sh install <sha>
```

Later: `/opt/git-gate/current/install.sh install <sha>`, `rollback <sha>`, `list`.

After switching `current`, `install` and `rollback` hand the remaining steps (the links below and
their checks) to the script of the release now live, as `install.sh post-switch <sha>`, so a step a
release adds runs on the install that brings it. Releases before session rules lack that hand-off:
upgrading from one of them with its own `install.sh` runs its older steps, so after that one
upgrade, run the new release's steps yourself:

```bash
/opt/git-gate/current/install.sh post-switch <sha>
```

Or upgrade with the extracted script, as for a first install, which needs no extra step.

Each install and rollback also links `/usr/local/bin/gitgatepush` and `/usr/local/bin/gitgaterules`
to the same names in `/opt/git-gate/current/bin/` (override the directory with `GIT_GATE_BIN_DIR`),
then checks that each name in your shell resolves to its link and warns, naming the other path, if
it does not. The launchers refuse to run from anywhere but `/opt/git-gate/releases/`.

A new install applies to the next call in every open conversation: the registered server is a
relay that hands each call to the current release. Conversations opened before the first
relay release need one restart, to replace their old server. The tool list is the one exception:
a conversation keeps the tools of the release it started on, so **new tools appear only in
conversations started after the install**. A conversation started before `gh` was retired still
lists it; each call refuses and names the tool that replaces the command.

## Configure

`~/.config/git-gate/token` (mode 0600) and `~/.config/git-gate/rules.toml`, both re-read on every
call. While `~/.config/git-gate/disabled` exists, every call is refused. Register once:

```bash
claude mcp add --scope user git-gate -- /opt/git-gate/current/bin/git-gate
```

### Sessions

Each Claude Code conversation is sandboxed to one session: its project folder, the folder Claude
Code was started in. A session reaches its own repo (read and write), the repos you grant it, and
nothing else, for reads as well as writes. Its folder is its only workspace: bundles, body files,
clone targets and downloads must be inside it. A worktree or subfolder belongs to the session of the
longest session folder that holds it, so `<folder>/.tmp/wt-x` is `<folder>`'s. A conversation whose
folder is no session's, or is unknown, reaches nothing; the refusal names the folder and the
`gitgaterules` command that would grant it.

```toml
protected_branches = ["main", "master", "release/*"]

[groups]                                     # named repo lists, used as "@name"
ha-reads = ["home-assistant/core", "PineappleEmperor/release-flow"]

[sessions."/home/juicebox/ha-lego"]
repo = "PineappleEmperor/ha-lego"            # its own repo: read and write
read = ["@ha-reads"]                         # read-only grants
write = []                                   # write grants

[sessions."/home/juicebox/claude-skills"]
repo = "PineappleEmperor/ha-skills"
write = ["PineappleEmperor/release-flow", "PineappleEmperor/ha-integration-ci"]
read = ["@ha-reads"]

[repos."PineappleEmperor/pineapple-core"]    # per-repo options; access is set per session
handover_paths = []                          # paths added to the default handover paths
push_deploys = true
```

- A session's access to a repo is the strongest of its grants: its own `repo` and `write` grants
  write, `read` grants read. Groups may hold other groups, and a group may hold a session's own repo:
  one `pineapple` group of every site, read by each site's session, gives each write to its own repo
  and read to the rest. Naming the same repo in both `read` and `write` of one session is an error.
- `repo` may be left out for a session that only gets grants.
- Session folders must be absolute and stay clear of `~/.config/git-gate`,
  `~/.local/state/git-gate` and `/opt/git-gate`. A folder that does not exist is only a warning
  (`gitgaterules check` names `gitgaterules remove-session <folder>`): its session simply matches
  nothing.
- Every problem refuses every call: an unknown key, a repo not written owner/name, an unknown or
  recursive group, a relative or gate-owned folder, a folder listed twice.
- `gitgatepush` is yours and is not scoped.
- A file without `[sessions]` is v1 (`workspaces` plus `[repos]` with `access`, format in
  [docs/plan.md](docs/plan.md#config)) and works as before: every conversation reaches every repo in
  `[repos]` and every workspace. `gitgaterules check` warns that it is not scoped.

### gitgaterules

Run it in your own terminal; the gate has no tool for it.

```bash
gitgaterules check [FILE]          # every problem, with line numbers; or "OK: N sessions, M repos"
gitgaterules show                  # each session's repos, read or write, groups expanded
gitgaterules grant <folder> read|write <repo|@group>
gitgaterules revoke <folder> <repo|@group>
gitgaterules add-session <folder> [--repo <owner/name>]
gitgaterules remove-session <folder>
gitgaterules group <name> add|remove <repo|@group>
gitgaterules migrate [--write]
```

`check` also warns about a session folder that is not a git checkout. It does not ask GitHub
whether the token covers a repo.

Every edit builds the new file, checks it with the gate's own loader, and writes only a file that
loads: it keeps the old one as `rules.toml.bak-<YYYYmmddTHHMMSS>` (0600), replaces `rules.toml`
atomically (0600), and prints the diff. A symlinked `rules.toml` is written through. The file is
written in one fixed, sorted order with a header saying `gitgaterules` manages it, and **comments are
not kept**: keep notes elsewhere, or edit by hand and run `gitgaterules check`. Backups pile up;
delete old ones yourself.

### Moving from v1

1. Install this release, then run `gitgaterules check`: it warns the file is v1.
2. Run `gitgaterules migrate` and read the result and its judgements. Each workspace becomes a
   session. Its git origin, read from that checkout's own config only, is just a suggestion for its
   own repo, since agents working there can change it: a v1 `write` repo is proposed, a `read` repo
   stays read-only, and a repo not in v1 is left out. The v1 `read` repos become group `reads`, read
   by every session. A v1 `write` repo no workspace is paired with gets no session: grant it where it
   belongs. `handover_paths` and `push_deploys` are kept.
3. Run `gitgaterules migrate --write`. It asks about each proposed pairing, no by default. A repo two
   folders claim, or a folder whose `.git` leads outside it (a worktree, a submodule, a symlink),
   needs the repo's name typed. Without a terminal, name the pairings you confirm:
   `gitgaterules migrate --write --yes-pairings <folder>=<owner/name> ...`. An unconfirmed pairing
   leaves the session without a repo of its own.
4. Add the write grants migrate reported as left out (`gitgaterules grant <folder> write <repo>`),
   then `gitgaterules show` to read the result.
5. Files a session hands the gate must now be inside its own folder. A conversation started outside
   every session folder, such as in your home folder, reaches nothing.

Releases from before session rules refuse every call while `rules.toml` is v2. To roll back to one,
restore the v1 backup first.

## Audit log

Every tool call, and every `gitgatepush` push, appends one JSON line to
`~/.local/state/git-gate/audit.log` (mode 0600). The token never appears in it. A tool call never
fails over its audit line: if the line cannot be written, `errors.log` beside it says so. A tool
call's line:

- `time`: local time the call ended.
- `tool`: the tool.
- `args`: its arguments. Each string is cut at 1,000 characters, each list at 20 entries plus a
  count of the rest. Past 8 KiB in all, lists shrink further, down to the count alone; other
  arguments are kept.
- `outcome`: `ok`, `refused`, `handover` or `error`.
- `reason`: for anything but `ok`, the first line of what the agent was told (at most 300
  characters); for `error`, the exception. Otherwise null.
- `ms`: how long the call took, in milliseconds.
- `session`: the conversation, as its relay's `<pid>-<start time>`.
- `cwd`: the conversation's project folder, or null.
- `access`: with session rules, the calling session's access to the call's repo: `write`, `read`,
  `none`, or `no-session` when the project folder is no session's. Null with v1 rules, and for a
  call refused before the rules were read.
- `polls` and `waited_s`, only on `checks` and `run` lines that reached GitHub: how many times the call asked GitHub
  for the state, and the seconds it slept between those polls.

A `gitgatepush` line has `time`, `tool` (`gitgatepush`), `id`, `repo`, `branch`, `sha` and `outcome`
(`pushed` or `failed`). Lines from older releases lack the newer fields.

At 8 MiB, `audit.log` is renamed to `audit.log.<YYYYmmddTHHMMSS>`, with `.<n>` added if that name is
taken. The gate never deletes these files: remove old ones yourself.

To summarise the log, run this from your own terminal:

```bash
python3 /opt/git-gate/current/scripts/audit_report.py [--since YYYY-MM-DD] [path/to/audit.log]
```

It reads the rotated files oldest first, then `audit.log`. It prints calls per project folder and
per repo and tool, each with outcome counts. It also prints the most common refusal reasons and
handover outcomes per repo.
