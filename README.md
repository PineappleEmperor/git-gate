# git-gate

An MCP server that gives sandboxed Claude Code agents GitHub access while the token stays outside
the sandbox. Agents get `clone`, `fetch`, `push` and `prune`, a read-only `gh`, and typed writes
(`pr`, `issue`, `triage`, `workflow_run`, `label`). Anything the gate will not do for an agent
becomes a handover the user runs with `gitgatepush <id>`.

- Design: [docs/superpowers/specs/2026-10-25-gate-rework-design.md](docs/superpowers/specs/2026-10-25-gate-rework-design.md)
  (current). The earlier [docs/design.md](docs/design.md) and [docs/plan.md](docs/plan.md) are
  superseded for git and `gh` mechanics; plan.md still holds the token, config and install layout.
- What the skill repositories must change: [docs/skills-migration.md](docs/skills-migration.md).
- The live check on a real repo: [docs/smoke.md](docs/smoke.md).

## What agents do

Agents never run `git push`, `git fetch` against GitHub, `gh` writes or SSH. Files cross the sandbox
boundary as bundles and body files:

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

Each install and rollback also links `/usr/local/bin/gitgatepush` to
`/opt/git-gate/current/bin/gitgatepush` (override the directory with `GIT_GATE_BIN_DIR`), then
checks that `gitgatepush` in your shell resolves to that link and warns, naming the other path,
if it does not. The launcher refuses to run from anywhere but `/opt/git-gate/releases/`.

A new install applies to the next call in every open conversation: the registered server is a
relay that hands each call to the current release. Conversations opened before the first
relay release need one restart, to replace their old server.

## Configure

`~/.config/git-gate/token` (mode 0600) and `~/.config/git-gate/rules.toml`; format in
[docs/plan.md](docs/plan.md#config) and, for per-repo `handover_paths` and `push_deploys`, the
[rework design](docs/superpowers/specs/2026-10-25-gate-rework-design.md#rules-file). While
`~/.config/git-gate/disabled` exists, every call is refused. Register once:

```bash
claude mcp add --scope user git-gate -- /opt/git-gate/current/bin/git-gate
```
