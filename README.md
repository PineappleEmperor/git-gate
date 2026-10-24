# git-gate

An MCP server that gives sandboxed Claude Code agents `clone`, `fetch`, `push`, `prune` and `gh`
against GitHub while the token stays outside the sandbox. Design: [docs/design.md](docs/design.md).
Decisions and open items: [docs/plan.md](docs/plan.md).

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

## Configure

`~/.config/git-gate/token` (mode 0600) and `~/.config/git-gate/rules.toml`; format in
[docs/plan.md](docs/plan.md#config). Register once:

```bash
claude mcp add --scope user git-gate -- /opt/git-gate/current/bin/git-gate
```
