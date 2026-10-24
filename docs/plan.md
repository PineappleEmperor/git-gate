# git-gate: build plan

Follows `docs/design.md` (agreed 2026-10-24). This file resolves its open items and records
where the build adds to or tightens the design. Written 2026-10-24.

## Open items

| Item | Resolution |
|---|---|
| `gh` subcommand inventory | Seeded from the copies of the skill repos readable from the sandbox (`~/.claude/plugins/marketplaces/{cloudflare-skills,home-assistant-skills}`). `claude-skills` itself is not at `~/claude-skills` and not readable here, so its own docs are not yet counted. Result and allowlist below. |
| Token | Fine-grained PAT, selected repositories only. Permissions: Contents RW, Pull requests RW, Issues RW, Actions RW (`run rerun`/`cancel`), Metadata R. **Workflows: no access**, so GitHub itself rejects a push touching `.github/workflows/` even if the gate's own check failed. GitHub App tokens stay parked. |
| Governance gate editing its own source | Yes, in principle. `scripts/governance_gate.py` and `scripts/governance_server.py` are inside the governed root under tier `scripts/`, and `get_file` answers for them with the normal "read the tier docs first" refusal rather than an exclusion. Stopped before taking an EditKey, so a self-patch was not attempted. Recommend the governance gate get the same split as this one: live copy under `/opt`, dev copy governed. |
| Local CI in `claude-skills` | Not reachable from this session. For this repo: `.venv` on pyenv 3.14.4 with `pytest` + `ruff`, run by `scripts/ci.sh`. The same recipe applies to `claude-skills` (`python -m venv .venv && .venv/bin/pip install ruff pytest pyyaml`), run from a session started there. |
| Orphan `node_modules/` in `claude-skills` | Not reachable from here. Deleting it is a user step in that repo. |

### `gh` usage found

| Subcommand | Uses | Gate |
|---|---|---|
| `pr create`, `pr view`, `pr edit`, `pr comment`, `pr list`, `pr review`, `pr diff`, `pr checks`, `pr close`, `pr ready` | many | allowed |
| `pr merge` | 14 | **refused**: a merge lands code on the base branch without a push, so it would sidestep push's protected-branch rule. Merging stays the user's. |
| `issue view`, `issue list`, `issue create`, `issue comment`, `issue edit`, `issue close` | many | allowed |
| `api` (GET, e.g. `repos/home-assistant/core/contents/...`) | 22 | allowed: GET forced, endpoint must be inside the forced repo |
| `api -X PATCH .../pulls/{n} -F body=@file` | 1 (pineapple-pages `commits.md`, workaround for `pr edit`) | **refused**; use `pr edit --body-file`. The skill doc should drop the workaround. |
| `release download`, `release view` | 8 | allowed; download goes through a gate temp dir |
| `release create`, `release edit`, `release upload` | 25 (mostly third-party plugins) | **refused**: they create or move tags, which stay manual |
| `run list`, `run view`, `run watch`, `workflow view` | 8 | allowed |
| `search issues` | 9 | **refused**: GitHub ORs repeated `repo:` qualifiers, so forcing `--repo` does not contain it. Use `issue list --search` / `pr list --search`, which refuse `repo:`, `org:`, `user:`, `owner:` and `is:public/private`. |
| `label create` | 2 | allowed |
| `repo clone`, `pr checkout` | 6 | refused; use the `clone` tool, or `fetch` + local checkout |
| `auth *`, `extension install` | 18 | refused |
| `issue develop` | 1 | refused (creates a remote branch outside `push`'s rules) |

## Additions to the design

1. **`fetch` and `push` take a `path`.** The design's signatures name only the repo, but the server
   cannot know which checkout the agent means: a user-scope MCP server serves every session, and
   agents work in clones under `.tmp/`. Signatures: `clone(repo, path)`, `fetch(repo, path)`,
   `push(repo, path, branch)`, `prune(repo, branch)`, `gh(repo, args)`.
2. **Git never reads the agent's `.git/config` or hooks.** The design's config check relies on
   `denyWrite: **/.git/config` to rule out a swap between check and use. A glob deny can only
   protect files that exist when the sandbox starts; a repo cloned or `git init`ed mid-session has
   an unprotected config. So the gate runs git with `GIT_DIR` set to a gate-owned *shadow* dir
   (`~/.local/state/git-gate/shadow/<hash>/`) holding its own `config`, `HEAD`, `objects/` and
   refs. The agent's object files are hardlinked in (copied across filesystems), skipping
   `objects/info/`, and its refs are a `packed-refs` snapshot the gate reads itself, skipping
   symlinks. No `hooks/`, `info/`, `commondir` or alternates means nothing the agent writes is read
   as configuration or followed as a path. After a fetch the gate copies new packs and `origin/*`
   refs back through directory handles. The key allowlist check stays, as tamper detection and to
   bind the path to the repo through `remote.origin.url`. Consequences: `fetch` never prunes, so it
   lists stale remote-tracking branches for the agent to delete with `git branch -dr`. Shallow
   repos are refused.
3. **`clone` builds the repo in a gate-owned staging dir and moves it into place.** It writes
   `.git/config` itself, fetches every branch, sets `HEAD` and the default branch, and does **not**
   check out. The agent runs `git -C <path> reset --hard` inside the sandbox, so untrusted tree
   content is only ever written by sandboxed git.
4. **`push` pushes the SHA it checked, not the branch name.** Checks run against a resolved SHA and
   the push is `git push <url> <sha>:refs/heads/<branch>`, so moving the branch mid-call changes
   nothing. Protected means any of: the repo's default branch, a glob in `protected_branches`,
   GitHub's `protected` flag, or any active ruleset rule on the branch. Workflow check: every new
   commit (reachable from the SHA, not from the remote branch or, for a new branch, the default
   branch) is diffed against its parent with rename detection off. Merges use `--cc`, so a
   clean merge of `main` passes and an evil merge editing a workflow does not. `--cc` hides a
   merge that keeps one parent's *older* workflow, so the whole `.github/workflows` tree is also
   compared with the remote branch, or with the fork point from the default branch.
   An `objects/info/alternates` file in the agent's repo is never read (see 2), so it cannot make
   git read, and push, objects from another repository on disk.
5. **`prune` refuses if the branch moved after its PR closed.** It needs at least one PR from the
   branch, no open PR, and the branch tip equal to the latest closed PR's head SHA, so unreviewed
   commits pushed after a close are not deleted.
6. **`gh` file arguments are copied, never passed through.** `--body-file`/`-F` (pr and issue
   commands) must name an absolute path inside a workspace. The gate opens it, re-checks the opened
   file's real path, copies at most 1 MiB to a gate temp file, and passes that path instead.
   Content containing the token is refused. Downloads (`-D`/`-O`) go to a gate temp dir and are
   then copied into the target through a verified directory handle. A symlink swap can therefore
   neither upload `~/.config/git-gate/token` nor overwrite it.
7. **`gh` argv rules beyond the subcommand allowlist:** `-R`/`--repo`/`--hostname`/`--web`/
   `--editor`/`--recover` refused; clustered short flags (`-Rfoo`) refused; any argument that is
   a bare GitHub URL refused (it would override `-R`). For `api` every flag must be known, the
   method is forced to `GET`, `--input` and `-F x=@file` are refused, only `Accept:` headers pass,
   and the endpoint must be `repos/{owner}/{repo}...` or the literal forced repo, with no `..`.
   `gh` runs with `GH_CONFIG_DIR` and `cwd` in gate-owned empty dirs, prompts, pager, editor and
   browser disabled, and `GH_TOKEN` in its environment only.
8. **Access levels per repo.** `rules.toml` marks each repo `read` (clone, fetch, read-only `gh`
   commands, `api` GET) or `write` (everything). That keeps HA-core lookups working without putting
   `home-assistant/core` on the write list.
9. **Audit log.** Each call appends one JSON line (time, tool, repo, outcome) to
   `~/.local/state/git-gate/audit.log`. Arguments are logged; the token never is. Every output is
   scrubbed of the token as well.

## Config

`~/.config/git-gate/token`: the PAT on one line, mode `0600`. Refused if group- or world-readable.

`~/.config/git-gate/rules.toml`:

```toml
workspaces = ["/home/juicebox/git-gate", "/home/juicebox/claude-skills"]
protected_branches = ["main", "master", "release/*"]
git_timeout_seconds = 300
gh_timeout_seconds = 1800

[repos]
"PineappleEmperor/git-gate" = "write"
"home-assistant/core" = "read"
```

Workspaces may not overlap `~/.config/git-gate`, `~/.local/state/git-gate` or `/opt/git-gate`.
Both files are re-read on every call, so edits apply without a restart.

## Install layout

`install.sh install <sha>` is run by the user from their own terminal, not by an agent. The dev
copy of the script is sandbox-writable, so it is never the one run: the first install runs a copy
extracted from the reviewed commit (`git cat-file blob <sha>:install.sh`), later ones run the
root-owned `/opt/git-gate/current/install.sh`.

1. Fetches the commit (and the current release's) through a shadow git dir, so the dev repo's
   config, hooks and attributes never run, into a private temporary repo that hashes every
   object it receives. The diff against `current` and the archive both come from that copy,
   so swapping object files in the dev repo after the review changes nothing.
2. Asks for confirmation, then `git archive`s the commit to a temp dir.
3. With `sudo`: extracts into `/opt/git-gate/releases/<sha>/` (refuses if it exists, or if the
   tree holds symlinks or submodules), builds `.venv` from `requirements.lock` with
   `--require-hashes --only-binary :all:` in a cleared environment (only proxy and CA variables
   pass through), adds a `.pth`
   pointing at the release's `src/`, makes everything `root:root`, `go-w`, then swaps `current`
   atomically (`ln -sfn` to a temp name + `mv -T`).

`install.sh rollback <sha>` repoints `current`; `install.sh list` shows releases. The launcher
`bin/git-gate` resolves its own real path first, so a running server keeps its release when
`current` moves. The base interpreter stays in `~/.pyenv` (sandbox cannot write it).

## Residual risks

- Hardlinked object files are the agent's own inodes, so the agent can still rewrite an object's
  bytes in place. That corrupts only its own repository's data; it cannot point git elsewhere.
- `fetch.fsckObjects` is on; a repo with historical malformed objects will refuse to fetch.
- LFS content arrives as pointer files: the gate defines no filter drivers.
- Each release's `.venv` is root-owned, but a venv runs its base interpreter, and pyenv 3.14.4
  (with its shared `libpython`) is user-owned in `~/.pyenv`. The sandbox cannot write there.
  Anything running as the user outside the sandbox could, but it could also just read the token,
  so a root-owned interpreter would add little. Accepted; `install.sh` keeps pyenv 3.14.4.
- Ubuntu's git 2.43.0 has had clone CVEs. The gate never checks out, which removes the known
  checkout paths.

## Build order

1. `src/git_gate/`: `config.py`, `paths.py`, `gitops.py`, `github.py`, `ghpolicy.py`,
   `gate.py`, `server.py`.
2. `tests/` against local bare repos (`file://` base URL injected into `Gate`, never read from
   config or environment) and a fake GitHub API and fake `gh`.
3. `install.sh`, `bin/git-gate`, `requirements.lock`, `scripts/ci.sh`.
