"""Rules and token, read from ~/.config/git-gate on every call."""

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from git_gate import Refused

REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
ACCESS_LEVELS = ("read", "write")


def config_dir() -> Path:
    return Path(os.environ.get("GIT_GATE_CONFIG_DIR", Path.home() / ".config/git-gate"))


def state_dir() -> Path:
    return Path(os.environ.get("GIT_GATE_STATE_DIR", Path.home() / ".local/state/git-gate"))


@dataclass(frozen=True)
class Rules:
    workspaces: tuple[Path, ...]
    repos: dict[str, str]  # lower-case "owner/name" -> "read" | "write"
    protected_branches: tuple[str, ...] = ("main", "master")
    git_timeout: int = 300
    gh_timeout: int = 1800
    forbidden: tuple[Path, ...] = field(default=())

    def access(self, repo: str) -> str:
        if not REPO_RE.match(repo) or repo.endswith(".git") or ".." in repo:
            raise Refused(f"'{repo}' is not an owner/name repository.")
        level = self.repos.get(repo.lower())
        if level is None:
            raise Refused(f"{repo} is not on the git-gate repo allowlist (rules.toml [repos]).")
        return level

    def require_write(self, repo: str) -> None:
        if self.access(repo) != "write":
            raise Refused(f"{repo} is read-only in rules.toml; this call needs write access.")


def load_rules(cfg: Path | None = None, extra_forbidden: tuple[Path, ...] = ()) -> Rules:
    cfg = cfg or config_dir()
    path = cfg / "rules.toml"
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise Refused(f"No rules file at {path}; the user has to create it.") from None
    except tomllib.TOMLDecodeError as e:
        raise Refused(f"{path} is not valid TOML: {e}") from None

    repos = {}
    for name, level in data.get("repos", {}).items():
        if not REPO_RE.match(name) or level not in ACCESS_LEVELS:
            raise Refused(f"{path}: bad repo entry {name!r} = {level!r}.")
        repos[name.lower()] = level

    forbidden = tuple(p.resolve() for p in (cfg, state_dir(), Path("/opt/git-gate"), *extra_forbidden))
    workspaces = []
    for w in data.get("workspaces", []):
        wp = Path(w)
        if not wp.is_absolute():
            raise Refused(f"{path}: workspace {w!r} must be absolute.")
        wp = wp.resolve()
        for f in forbidden:
            if wp.is_relative_to(f) or f.is_relative_to(wp):
                raise Refused(f"{path}: workspace {wp} overlaps gate-owned {f}.")
        workspaces.append(wp)

    return Rules(
        workspaces=tuple(workspaces),
        repos=repos,
        protected_branches=tuple(data.get("protected_branches", ("main", "master"))),
        git_timeout=int(data.get("git_timeout_seconds", 300)),
        gh_timeout=int(data.get("gh_timeout_seconds", 1800)),
        forbidden=forbidden,
    )


def load_token(cfg: Path | None = None) -> str:
    path = (cfg or config_dir()) / "token"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise Refused(f"No token at {path}; the user has to create it.") from None
    except OSError as e:
        raise Refused(f"Cannot open {path}: {e.strerror}.") from None
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise Refused(f"{path} must be a regular file owned by you with mode 0600.")
        token = f.read().strip()
    if not token or any(c.isspace() for c in token):
        raise Refused(f"{path} must hold the token on a single line.")
    return token
