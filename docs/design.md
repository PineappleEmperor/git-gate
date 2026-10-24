# git-gate: design brief

Agreed 2026-10-24 in a claude-skills session that diagnosed the sandbox proxy.

## Goal

No GitHub token, and no placeholder for one, inside the Claude Code sandbox. Agents still clone,
fetch, push, prune branches and use `gh`, through an MCP server that runs outside the sandbox and
holds the token.

## Why the alternatives were rejected

| Approach | Reason rejected |
|---|---|
| `sandbox.credentials` mask + `tlsTerminate` (current managed setup) | The proxy substitutes placeholders in request bodies, so an endpoint that echoes its input returns the real token (giantswarm/beekeeper#389). Placeholder still lives in the sandbox. |
| Mask for git over HTTPS | Basic auth is base64; the proxy never substitutes inside it (anthropics/claude-code#95752, open). |
| Masked token file under `~` | Not mounted under a read-denied path. Confirmed here: `~/.config/claude-git-token` is not readable in the sandbox. |
| `credential.helper store` | Token in plaintext `~/.git-credentials`; whatever runs git can read it. Also read-denied by managed settings. |
| `excludedCommands` for git | git runs unsandboxed and executes repo-controlled config and hooks with the token. |
| agent-vault / airlock-cred-proxy / mattolson agent-sandbox | Preview, archived 2026-10-20, or replaces the sandbox with Docker. |
| GitHub MCP server for `gh` work | No `gh run watch`; wrapping the real `gh` covers everything with one allowlist. |

## Layout

| Copy | Path | Writable by |
|---|---|---|
| Development | `~/git-gate` (own repo) | sandbox (via `allowWrite`) and the user |
| Live | `/opt/git-gate/releases/<sha>/`, `/opt/git-gate/current` symlink | root only |
| Token and rules | `~/.config/git-gate/` | user only; sandbox read-denied |
| MCP registration | user scope (`claude mcp add --scope user`), command under `/opt/git-gate/current` | user only |

The user installs a release by reviewing the diff, copying into `releases/`, and repointing
`current`. Rollback is repointing `current`. Nothing in the development copy ever runs outside the
sandbox.

## Tools

| Tool | Does | Rules |
|---|---|---|
| `clone(repo, path)` | Clones into an allowed workspace path. | Repo allowlist; path allowlist. |
| `fetch(repo)` | `git fetch` from an explicit `https://github.com/<owner>/<repo>.git` in the agent's repo. Agent merges or rebases locally. | Repo allowlist. |
| `push(repo, branch)` | Pushes the branch to the explicit URL. | Repo allowlist; never a protected branch; fast-forward only; refuses commits touching `.github/workflows/` with a handover message; no tags; no force. |
| `prune(repo, branch)` | Deletes a remote branch. | PR merged or closed; branch not protected. |
| `gh(repo, args)` | Runs the real `gh` with `-R <repo>` forced and the gate's own `GH_CONFIG_DIR`. | Subcommand allowlist (includes `run watch`); `gh api` GET-only or excluded. |

Force-push, tags, and workflow-file commits stay manual: the user pushes over SSH.

## Running git in place safely

- Hooks never run: `-c core.hooksPath=/dev/null`, `--no-verify`. Pre-push checks run in the sandbox before the agent calls `push`.
- Global and system config replaced: `GIT_CONFIG_GLOBAL` points at a gate-owned file, `GIT_CONFIG_NOSYSTEM=1`.
- `.git/config` checked against a key allowlist on every call; remote URL must match the repo allowlist.
- `.git` must be a directory, not a `gitdir:` file. Repo path must be on the allowlist.
- Token passed to git through the environment at call time, never argv or a config file.
- Sandbox cannot change `.git/config` or `.git/hooks` (settings below), so the check cannot race a swap.

## Settings changes (user applies; managed settings need sudo)

| Change | Where |
|---|---|
| `sandbox.filesystem.denyWrite`: `**/.git/config`, `**/.git/hooks` | managed |
| `permissions.deny`: `Edit(**/.git/config)`, `Edit(**/.git/hooks/**)`, `Write(**/.git/config)`, `Write(**/.git/hooks/**)` | managed |
| Remove `GH_TOKEN` mask, `~/.config/claude-git-token` mask, `network.tlsTerminate` | managed, after the gate works |
| `allowRead` for `/home/juicebox/git-gate`, only if a session started there cannot read its own folder | managed |

Side effects: `git push -u`, `git branch --set-upstream-to`, `git remote add`, `git config` fail in
the sandbox. `prek install` into `.git/hooks` becomes a one-off user step per clone.

## Hooks run by the user's own terminal

In claude-skills, `.githooks/` (its `core.hooksPath`) and `.pre-commit-config.yaml` are committed and
agent-editable; they run unsandboxed when the user commits or pushes. Put both under governance, and
change the push handover form to:

```
git -c core.hooksPath=/dev/null push git@github.com:<owner>/<repo>.git <branch>
```

## Open items

- Inventory every `gh` subcommand used in the skills and CI docs to seed the allowlist.
- Token: fine-grained PAT first; GitHub App per-repo tokens later (parked project).
- Check whether the governance gate's own source is editable through the governance gate.
- Local CI: install `ruff`, `pytest`, `pyyaml` into `.venv` (Python 3.14.4 now works in the sandbox) and re-run `ci.yml`'s steps. Snap `ruff` and system Python 3.12 cannot run them.
- `node_modules/` at the repo root has no `package.json`; orphaned install.

## Facts at handover

| Item | Value |
|---|---|
| Sandbox git | 2.43.0 (`/usr/bin/git`) |
| `gh` | `/usr/bin/gh` |
| Python for the gate | pyenv 3.14.4 |
| MCP SDK in use here | `mcp` 2.1.1 (depends on `httpx2`, Pydantic-maintained) |
| Managed allowed domains | api.anthropic.com, api.github.com, registry.npmjs.org, github.com, pypi.org, files.pythonhosted.org |
