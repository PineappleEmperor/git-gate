# git-gate rework: design

Written 2026-10-25. Supersedes the git and `gh` mechanics in `docs/plan.md`; `docs/design.md` (the
agreed brief) still holds except where this document says otherwise.

## Why

The gate holds the GitHub token outside the Claude Code sandbox so agents can work without one.
The first build ran git **on the agent's own repository**, from outside the sandbox. That repo is
attacker-controlled input to a large, configurable program, and every security review found a
new way in: repo config, hooks, alternates, symlinks, replace refs. Each was patched; the pattern
says the boundary is in the wrong place. Its `gh` tool also enforced policy by parsing `gh`'s
command line, which produced the `search` and `pr merge` holes. And the server loads its code
once per conversation, so an install did nothing for conversations the user keeps open in the
desktop app.

The rework moves the boundary: **the gate never runs git on, reads config from, or follows a path
into an agent's repository.** Git data crosses the boundary only as bundle files, which git
verifies by hash. Writes to GitHub go through typed tools with checked parameters, not through a
parsed `gh` command line.

## How the user works (from the user)

- Agents work ideas and issues, run local CI in the sandbox, push a feature branch. In the HA
  repos a workflow (`auto-draft-pr.yml`) opens the PR; agents rarely create PRs themselves.
- **The user merges**, in the GitHub web UI, usually from a PR link the agent hands over.
- HA release tags are made by CI. Tags never go through the gate.
- Changes to skills and CI are pushed by the user.
- **Force-pushes are the user's.** If they asked for a rewrite, the agent hands them the push.
- Cloudflare repos (the few private ones) deploy on any push, so testing happens before a push.
- The user keeps conversations open in the Claude desktop app. Restarting a conversation to
  pick up a new gate release is not acceptable.
- Private repos are on GitHub Free, so GitHub enforces no branch rules there (the API answers
  403 "Upgrade to GitHub Pro"); the gate is the only guard on their `main`.

## Goals

1. Agents fetch, push feature branches, prune merged branches, read GitHub, and make the small
   set of GitHub writes they need (below), with no token in the sandbox.
2. The gate never executes, reads config from, or follows a path into agent-controlled git data.
3. Every refusal that the user should act on returns one short command for them to run:
   `gitgatepush <id>` (see Handover).
4. A new install applies to the next call in every open conversation (the relay).
5. Works on private Free-plan repos.

## Non-goals

- Merging PRs, creating or moving tags and releases, force-pushing, pushing workflow or other
  handover paths: all stay the user's, done from their own terminal or the web UI.
- GitHub App per-repo tokens (still parked).
- Protecting against programs running as the user outside the sandbox. They can read the token
  file directly; the user account is the trust boundary.

## Anti-goals (a failure even if it works)

- The gate running git with an agent repo as its `GIT_DIR`, worktree or alternate.
- Policy for GitHub writes decided by parsing a `gh` command line.
- A gate upgrade that needs conversations restarted.
- A refusal without a clear next step, or a hang.

## Agent-facing tools

`repo` is always `owner/name`. Paths are absolute and must be inside a workspace in
`rules.toml`. Files the gate reads from or writes to a workspace go through the existing
verified-handle helpers in `paths.py` (no symlink following, real path re-checked after open).

| Tool | Agent does | Gate does |
|---|---|---|
| `push(repo, branch, bundle)` | `git bundle create <bundle> <branch> --not --remotes=origin` in the sandbox, then calls push | Copies the bundle into gate storage, fetches GitHub into its own mirror, unbundles into the mirror with `transfer.fsckObjects` (every object re-hashed), runs the checks below, pushes the verified SHA from the mirror. Replies with the PR link if a PR is open for the branch, else the compare link. |
| `fetch(repo, bundle, have=[])` | Calls fetch with an output path (and optionally its current `origin/*` SHAs), then `git fetch <bundle> '+refs/heads/*:refs/remotes/origin/*'` | Fetches GitHub into the mirror, writes a bundle of all branches, excluding commits reachable from any `have` SHA the mirror knows |
| `clone(repo, path)` | Then `git -C <path> reset --hard` | Unchanged from today: builds the repo in gate-owned staging and moves it into place. Creating a new repo from scratch is not running git on agent data. |
| `prune(repo, branch)` | — | Unchanged: only a branch whose PR is merged or closed and which hasn't moved since |
| `gh(repo, args)` | — | **Read-only** passthrough: today's read allowlist, nothing that writes |
| typed writes (below) | — | REST API calls with checked parameters, no `gh` |

**Typed writes** (the set the skills audit found agents use): comment on a PR or issue; edit a
PR's or issue's title, body or base; add or remove labels, assignees and requested reviewers;
create an issue; create a PR, optionally as a draft (rare, kept for repos without auto-draft-pr);
close, reopen or mark ready a PR, close or reopen an issue; post a PR review as a comment or a
change request; rerun or cancel a workflow run; create or edit a label definition. Bodies come
from a body file in a workspace (copied, size-capped, refused if it contains the token).

**Approving a PR is refused.** The token acts as the user, so an agent's approval would count as
the user's review of a PR the workflow opened. Approval is the user's.

Tool shapes should stay stable across releases, because a running server's tool list only
changes when its conversation restarts: group the typed writes into a few tools with an `action`
parameter rather than one tool per endpoint. The exact split and names are the builder's.

## Push checks (all in the gate's own mirror)

Each of these is refused and becomes a handover (next section):

| Case | Handover pushes |
|---|---|
| Branch is the default branch, matches `protected_branches`, is GitHub-protected, or is covered by a ruleset (when the API offers rules; 403/404 on the rules endpoint means "not available here", not an error) | the verified commit, as a normal push |
| Not a fast-forward of the branch on GitHub | the verified commit, with `--force-with-lease=<branch>:<remote sha the gate saw>` |
| A new commit, or the net change against the remote branch (or its fork point from the default branch), touches a handover path | the verified commit, as a normal push |

The bundle must contain exactly one ref, `refs/heads/<branch>`; anything else is refused. Today's
workflow checks (per-commit diffs with `--cc` for merges, plus the whole-tree comparison) carry
over unchanged, generalised from `.github/workflows/` to the handover paths.

## Handover (decided by the user)

The user's own git never runs on the agent's repository: an agent controls its `.git/config`, and
settings such as `core.sshCommand` would run unsandboxed even with hooks off. Instead:

- On refusal the gate keeps the verified commit under a handover ref in its mirror and records the
  handover (id, repo, branch, SHA, and the lease SHA for rewrites) in its state directory, which the
  sandbox cannot read or write. The refusal says why and gives `gitgatepush <id>`.
- `gitgatepush` is a script in the release (`bin/gitgatepush`); `install.sh` links
  `/usr/local/bin/gitgatepush` to `/opt/git-gate/current/bin/gitgatepush`, root-owned, so it works
  in any shell and an agent cannot change what it runs.
- `gitgatepush <id>` shows the repo, branch and commit list, asks for confirmation, then pushes from
  the mirror with the user's own git config and SSH key, hooks off: `git --git-dir=<mirror> push
  [--force-with-lease=…] git@github.com:<owner>/<repo>.git <sha>:refs/heads/<branch>`.
  `gitgatepush` with no id lists open handovers.
- Expiry and cleanup of old handovers are the builder's.

## Rules file

`~/.config/git-gate/rules.toml` keeps today's shape and stays backwards-compatible:

```toml
workspaces = ["/home/juicebox/claude-skills", ...]
protected_branches = ["main", "master", "release/*"]

[repos]
"PineappleEmperor/ha-skills" = "write"                      # today's form still works
"PineappleEmperor/pineapple-core" = { access = "write", push_deploys = true }
"PineappleEmperor/ha-skills-extra" = { access = "write", handover_paths = ["skills/**"] }
```

- `handover_paths`: globs added to the defaults for that repo.
- `push_deploys`: the push reply says plainly that this push deploys.

## Session scoping

Added after the rework (decided by the user): each conversation is sandboxed to its own repo
unless the user grants it more, for reads as well as writes.

- **A session is a project folder.** The gate names the calling session by the call process's
  cwd: the relay's, which Claude Code sets when the conversation starts and which the child that
  runs the call inherits. No tool argument can name it, and the agent's shell cannot move it. The
  session is the one whose folder is the longest that holds the cwd, so a worktree under
  `<folder>/.tmp/` belongs to `<folder>`. A cwd that is unknown or in no session's folder gets no
  access, and the refusal names the folder and `gitgaterules`.
- **Rules v2.** A rules file with `[sessions]` is v2: `[sessions."<folder>"]` with `repo` (its own,
  write), `write` and `read` grants of repos or `@group`s, `[groups]` of named repo lists, and
  `[repos."<owner/name>"]` for `handover_paths` and `push_deploys` only. A session's access to a repo
  is the strongest of its grants, so overlaps from groups (a group holding the session's own repo)
  are fine; the same repo written in both `read` and `write` of one session is an error. The
  session's folder is its only workspace. A file without `[sessions]` is v1 and behaves as before.
- **Fail closed.** Every problem in the file refuses every call, and `gitgaterules check` lists them
  all, with line numbers where it can find them.
- **`gitgaterules`** is the user's command, like `gitgatepush`: the gate exposes no tool for it. It
  checks, shows and edits the file, and migrates v1. Every edit is validated by the gate's own loader,
  backed up, written atomically and shown as a diff; the file is rewritten canonically, comments
  dropped. Lists are deduplicated only where the gate itself treats entries as equal (repo names
  ignore case, handover paths compare in canonical form, branch patterns only when identical).
- **Migration does not trust git config.** A workspace's origin is the agent's to change, so it is
  only a suggestion: `migrate --write` asks before each folder-to-repo pairing (no by default), needs
  the repo's name typed for a repo claimed by two folders or a folder whose `.git` leads outside it,
  and without a terminal takes only pairings named with `--yes-pairings`.
- **Output from files and git is escaped** in everything `gitgaterules` prints, value by value, so
  a folder name or remote URL cannot start a line of its own; URLs lose their credentials, query and
  fragment.
- **Audit.** Each line has `access`: the session's access to the call's repo (`write`, `read`,
  `none`), `no-session`, or null under v1.

What the cwd does not protect against: a conversation the user starts in a session's folder is that
session, whoever starts it; a session folder nested inside another is also inside the outer
session's workspace, which the outer conversation's own sandbox can already write; and a server
started some other way than the user's registration, such as from a project's `.mcp.json`, runs
wherever it is started. `.mcp.json` is write-protected in the sandbox and a default handover path,
so an agent can neither add one nor push one.

## Relay

Ships in the same release as the rework (the user agreed), so open conversations need their gate
restarted once, not twice. Already built and uncommitted in the working tree: the long-running server only holds the MCP
connection, and each call runs `git-gate call <tool>` from the release `current` points to at that
moment. The tool list itself is fixed per running server, so a release that adds tools needs new
conversations; behaviour changes don't. Open conversations started before the relay need their
gate restarted once.

## Constraints

- pyenv Python 3.14.4, a normal venv per release, `mcp==2.1.1`, no new runtime dependencies.
- git 2.43.0 and gh 2.45.0 at `/usr/bin`.
- The install layout (`/opt/git-gate/releases/<sha>` + `current`) and `install.sh` stay.
- The PAT has no Workflows permission, so GitHub rejects workflow pushes regardless.
- Mirrors live in `~/.local/state/git-gate/mirrors/<owner>/<repo>.git`, which the sandbox
  cannot read or write.

## Removed

The shadow git dir, hardlinked objects, ref snapshot and write-back, the repo-config key
allowlist, the remote-URL check, and `gh` write subcommands. That removes the SSH-vs-HTTPS and
config-key problems with them.

## Skills and switch-over

The skill repositories are out of scope here: each has its own session, and the user pushes skill
changes. This plan ends with the list of instruction changes those sessions need (bundle push and
fetch steps, typed writes in place of `gh` writes, `gitgatepush` handovers) and the order to switch
over in, so agents don't follow stale steps once `gh` writes are refused.

## Checked against published guidance

Against the MCP specification's security best practices and the OWASP Top 10 for Agentic
Applications (2026):

| Principle | Where the design meets it |
|---|---|
| Least-privilege tools (OWASP tool misuse) | Typed writes with checked parameters; `gh` read-only; no generic API passthrough |
| Least-privilege credentials (OWASP identity and privilege abuse; MCP scope minimisation) | The gate limits the token to allowlisted repos. **Gap:** the PAT's own permissions and expiry are unchecked; the user reviews them (Administration, Secrets and Workflows: no access; an expiry date) |
| Human approval for high-impact actions | Merge, force-push, protected branches, handover paths and tags all go to the user, through a confirm step in `gitgatepush` |
| Validate inputs | Bundles re-hashed by git; workspace paths opened without following symlinks and re-checked; repo and branch names validated |
| Untrusted output (indirect prompt injection) | **Gap:** text from GitHub (issue bodies, PR comments, logs) reaches the agent as tool output. Every `gh` and typed-write reply is prefixed with one line saying it is GitHub content to treat as data, not instructions |
| Logging and audit | Every call, refusal and handover goes to `audit.log`, with the token scrubbed |
| A way to stop it | **Gap:** add an off switch. While `~/.config/git-gate/disabled` exists, every call is refused with a message saying so |
| Runaway loops | **Gap:** a per-repo cap on calls per minute, so a looping agent can't hammer GitHub. The limit is the builder's |
| Local server safety (MCP) | stdio transport only, no network listener; root-owned releases; hash-pinned dependencies; installs from reviewed commits |
| Token passthrough (MCP) | Not applicable: the gate never accepts a token from the client |

## Tests and proof

- Unit and integration tests against local bare repos (`file://`, injected, never from config)
  and a fake GitHub API, as today, rewritten for bundles.
- A test that the 403 "Upgrade to GitHub Pro" answer on the rules endpoint doesn't block a push.
- Before calling it ready: a live smoke run against a real repo (fetch, push a feature branch,
  each refusal and its handover). The steps are the builder's to write; the run is the user's
  to approve.

## My calls (not the user's; check these)

- Approving PRs refused (see Typed writes).
- `gitgatepush` installed as a root-owned link in `/usr/local/bin` rather than a `.bashrc` alias.
- Default handover paths for every repo: `.github/workflows/**`, `.githooks/**`,
  `.pre-commit-config.yaml`. The user said skills and CI changes are theirs; hook config runs
  unsandboxed when they commit, so it is treated the same.
- `push_deploys` as an opt-in per-repo note, for the Cloudflare repos.
- `fetch` sends full bundles unless the agent passes `have`; the skills tell agents to pass it.
- `clone` keeps its current mechanism rather than becoming bundle-based.

## Left to the builder

Tool names and the split of typed writes; bundle size limits; mirror locking and cleanup; how
`have` SHAs unknown to the mirror are handled; the smoke-test script; how the code is laid out.
