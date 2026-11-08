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
| `repo_setup(repo, description, topics, ruleset_file, bundle?, secrets?)` | Validates and stores a setup request; answers with `gitgatesetup <id>`. | Write access to the repo; never calls GitHub. |

Force-push, tags, and workflow-file commits stay manual: the user pushes over SSH.

## Repo setup

Creating a repo and its settings needs administration rights the gate's token does not hold, so
`repo_setup` records a request and the user applies it with `gitgatesetup <id>`.

- No `gh` on this path: every call goes through `git_gate.github.GitHub` (`urllib`).
- Credential: a GitHub App user-to-server token from the OAuth device flow, requested at each run.
  It is never written to disk, logged or printed, and expires after 8 hours. It is revoked at the end
  of every run that obtained one, however the run ends. `device_login` hands the tokens to the run
  the moment GitHub sends them, before it returns, so an interrupt in between still leaves them to
  revoke; on the main thread, SIGINT, SIGHUP and SIGTERM are blocked from the token request until the
  tokens are handed over (at most the 30 s request timeout), so an interrupt while GitHub's answer is
  read lands after the hand-over. Revocation is two `POST https://api.github.com/credentials/revoke` requests with no
  `Authorization` header: `{"credentials": [<access token>]}`, then `{"credentials": [<refresh
  token>]}` once (the refresh token GitHub returns is never exchanged, only revoked). Each must
  answer 202; otherwise the run prints `GitHub refused the revocation (<status>).` or `GitHub refused
  the refresh token's revocation (<status>).` and the warning. When the first POST cannot reach
  GitHub, the refresh token's POST is still sent, and nothing is confirmed. SIGHUP and SIGTERM end a
  run as Ctrl-C does, so they revoke too, unless the signal was already ignored (`nohup`), which
  stays so. Both, and SIGINT, are blocked with `pthread_sigmask` while the handlers go in and come
  out. The restore's block is the first statement of the run's last `finally`, retried until it
  holds, so a hangup that lands just before it (whose handler `pthread_sigmask` runs) cannot skip
  it: the handlers always come back and the mask always returns to what it was. The run's exit
  code is fixed before its audit line is written, and an interrupt after that is dropped, so none
  escapes, or changes the exit code, after the run has audited. Once both POSTs answered 202, the run calls
  `GET /user` with the access token, up to 6 times 5 seconds apart, and says
  `Signed out: GitHub revoked this run's token.` only once that answers 401.
  Otherwise it prints a warning with the page to revoke it by hand. Neither token reaches stdout,
  `audit.log` or `errors.log`. Output that cannot be written (a hung-up terminal, a closed stdout)
  is dropped, so the run still revokes and audits.
- App: one private app per user, registered once by `gitgatesetup --register` through the manifest
  flow and installed on All repositories. Permissions are `administration: write` and
  `metadata: read`; no webhook. Only `client_id`, `slug` and `owner` are kept, in
  `~/.config/git-gate/setup-app.json`; the client secret, private key and webhook secret are dropped
  unread.
- The gate's own token is unchanged. `repo_setup` stores the request under `<state>/setups/<id>.json`
  (14-day expiry, as handovers) after checking the description (one line, at most 350 characters),
  topics (1-20) and ruleset file (a plain branch ruleset for the default branch, at most 64 KiB). A
  request with a first push takes its handover's creation time, so it expires no later than the
  handover. `gitgatesetup` runs the same checks again on the stored record (repo name, description,
  topics, ruleset, secrets), and requires a `created` time that is finite and no more than 60 s in
  the future (so a tampered time cannot stop expiry); it refuses one that fails:
  `Setup <id> is unreadable: <reason>`. The id-less listing runs the same checks and shows such a
  record as `<id>: (unreadable)`, without its contents. A record that does not parse has no created time, so the
  purge removes it, with its lock file, once the file's mtime is older than the 14-day expiry.
- One run per setup: a run holds an exclusive, non-blocking `flock` on `<state>/setups/<id>.lock`
  (0600) from before sign-in until it has audited. A second run is refused before sign-in with
  `Setup <id> is already running.` Closing a setup removes its lock file.
- Order, in one run: when the setup holds a first push, `gitgatesetup` applies, behind one `Apply?`:
  create the repo, description and Issues, topics, dependency graph, push `main`, ruleset. The ruleset
  goes on after the push lands, so its required checks never refuse it. With `main` already on GitHub
  and no handover, the ruleset applies in that run. Without a first push (or once its handover is
  gone) and with no `main` on GitHub, the ruleset is deferred, the setup stays open, and the user
  re-runs after `gitgatepush`. When the handover was pushed but GitHub does not show `main` yet, the
  ruleset line says `deferred: main was pushed; GitHub does not show it yet. Re-run in a moment:
  gitgatesetup <id>`. A handover record is gone both once pushed and once purged on expiry; a gone
  handover of a setup within 5 minutes of its own expiry (the setup expires with its handover) is
  read as expired, and the ruleset line says `deferred: main does not exist yet, and handover <h>
  with its first push has expired; ask the agent for a new repo_setup`.
- A live ruleset of the requested name is compared only when its shape is the one `gitgatesetup`
  reads (rules a list of objects with a text `type`, `parameters` an object, required checks a list
  of objects with a text `context`, `conditions.ref_name` an object of text-pattern lists,
  `bypass_actors` a list of objects). Any other shape counts as differing: its plan line shows
  `rules: unreadable → <wanted>` and `GitHub's copy has an unexpected shape; it is replaced whole.`,
  and Apply replaces it.
- The first push is a push handover: `repo_setup(..., bundle=<path>)` keeps the bundle's commit as a
  refused `push` keeps one, in the gate's mirror under `refs/gate/handover/<id>`, with the files under
  handover paths recorded; the setup records the handover id. The kept screen is shown before `Apply?`.
  `gitgatepush <id>` still works on it, and whichever runs first pushes it. `gitgatesetup` pushes it
  only while its record names the setup's repo, branch `main`, no base and no lease, and only the
  record it showed: one that changes before the push is refused. A record that is unreadable, or is
  not that push, is named on the `main` line (`the handover cannot be used: <reason>`) and left for
  `gitgatepush`; the ruleset is then deferred.
  The branch is always `main`, and the bundle must hold the complete history (no prerequisites), because the gate does not sync from a
  repo that may not exist yet.
- `gitgatesetup` never overwrites a branch: if `main` exists on GitHub the push item is `already
  set`, whatever its commit. The kept handover is left for `gitgatepush` and the ruleset applies.
- Actions secrets: `repo_setup(..., secrets=[names])` records 1-10 names (letters, digits and
  underscores, not starting with `GITHUB_`; shown upper-cased). `gitgatesetup` prints each with its
  settings link (`https://github.com/<repo>/settings/secrets/actions/new`) at the end of every run.
  The app has no `secrets` permission and cannot read them, so a reminder never keeps a setup open.
- No suspension: suspending an installation needs the app's JWT (private key), which is never kept;
  revocation is the control.
- A run: login, owner check, plan (`current → wanted`), `Apply? [y/N]`, apply (repository,
  description, Issues, topics, dependency graph, push `main`, ruleset), verify by reading back. The
  setup closes only when all of it verified. Exit 0 closed, 1 open, interrupted or refused, 2 usage.
  Each run writes one `audit.log` line (id, repo, outcome, revoked) and never the token. Outcomes:
  `closed`, `open`, `declined`, `interrupted` (stopped after applying had started: the run prints
  `Re-run: gitgatesetup <id>` and the setup stays open) and `refused`. A refusal while reading the
  changes back is `open`: the run prints `Reading the changes back failed: <reason>` and
  `Setup <id> stays open. Re-run: gitgatesetup <id>`.
- Personal account only: the repo owner must equal the signed-in user's `login`.
- Out of scope: reading or writing secret values, and local clone settings such as `core.hooksPath`.
- GitHub attributes changes to the user; the app's name appears only in their security log.

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
