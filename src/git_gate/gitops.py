"""Running git for the gate with the gate's own configuration and environment only.

git runs in gate-owned places only: the mirrors (see `mirror.py`), the clone staging dir, or on a
bundle file already copied into gate storage. It never gets a path into an agent's repository.
"""

import fcntl
import hashlib
import os
import secrets
import subprocess
from contextlib import contextmanager
from pathlib import Path

from git_gate import Refused
from git_gate.config import Rules

EMPTY = "empty"
MAX_STDERR = 4000


class Git:
    def __init__(self, state: Path, base_url: str, git: str = "/usr/bin/git"):
        self.state = state
        self.base_url = base_url.rstrip("/")
        self.git = git
        for d in ("locks", "staging", "template", "home", EMPTY, "tmp"):
            (state / d).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.gitconfig = state / "gitconfig"
        self._write_gitconfig()

    def url(self, repo: str) -> str:
        return f"{self.base_url}/{repo}.git"

    def user_push_url(self, repo: str) -> str:
        """Where a handover pushes from the user's terminal: SSH for GitHub, the base URL otherwise."""
        if self.base_url == "https://github.com":
            return f"git@github.com:{repo}.git"
        return self.url(repo)

    def env(self, token: str | None = None, git_dir: Path | None = None) -> dict[str, str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.state / "home"),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.gitconfig),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        if token:
            env["GIT_GATE_TOKEN"] = token
        if git_dir:
            env["GIT_DIR"] = str(git_dir)
        return env

    def run(
        self,
        args: list[str],
        *,
        timeout: int,
        token: str | None = None,
        git_dir: Path | None = None,
        check: bool = True,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                [self.git, *args],
                env=self.env(token, git_dir),
                cwd=self.state / EMPTY,
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise Refused(f"git {args[0]} timed out after {timeout}s.") from None
        if check and proc.returncode != 0:
            raise Refused(f"git {args[0]} failed:\n{proc.stderr.strip()[-MAX_STDERR:]}")
        return proc

    def _write_gitconfig(self) -> None:
        scheme = self.base_url.split("://", 1)[0]
        settings = [
            ("core.hooksPath", "/dev/null"),
            ("core.fsmonitor", "false"),
            ("core.pager", "cat"),
            ("core.editor", "false"),
            ("core.askPass", ""),
            ("protocol.allow", "never"),
            (f"protocol.{scheme}.allow", "always"),
            ("transfer.fsckObjects", "true"),
            ("fetch.unpackLimit", "1"),
            ("transfer.unpackLimit", "1"),
            ("gc.auto", "0"),
            ("maintenance.auto", "false"),
            ("fetch.recurseSubmodules", "false"),
            ("fetch.writeCommitGraph", "false"),
            ("push.followTags", "false"),
            ("push.recurseSubmodules", "no"),
            ("push.autoSetupRemote", "false"),
            ("submodule.recurse", "false"),
            ("init.templateDir", str(self.state / "template")),
            ("advice.detachedHead", "false"),
            ("credential.helper", ""),
            (
                f"credential.{self.base_url}.helper",
                '!f() { test "$1" = get && printf \'username=x-access-token\\npassword=%s\\n\' "$GIT_GATE_TOKEN"; }; f',
            ),
        ]
        # A name of its own: with the relay, each call is a process that writes this file at start-up.
        tmp = self.state / f"gitconfig.{os.getpid()}.{secrets.token_hex(6)}.new"
        tmp.touch(mode=0o600)
        try:
            for key, value in settings:
                subprocess.run(
                    [self.git, "config", "--file", str(tmp), key, value],
                    env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.state / "home")},
                    cwd=self.state / EMPTY,
                    check=True,
                    capture_output=True,
                )
            tmp.replace(self.gitconfig)
        finally:
            tmp.unlink(missing_ok=True)

    @contextmanager
    def lock(self, key: str):
        path = self.state / "locks" / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.lock"
        with open(path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def check_branch(self, branch: str, rules: Rules) -> None:
        if not branch or branch.startswith("-") or branch == "HEAD":
            raise Refused(f"{branch!r} is not a branch name.")
        proc = self.run(["check-ref-format", f"refs/heads/{branch}"], timeout=rules.git_timeout, check=False)
        if proc.returncode != 0:
            raise Refused(f"{branch!r} is not a valid branch name.")

    def remote_heads(self, url: str, token: str, rules: Rules, git_dir: Path | None = None) -> dict[str, str]:
        out = self.run(["ls-remote", "--heads", url], timeout=rules.git_timeout, token=token, git_dir=git_dir)
        heads = {}
        for line in out.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                heads[ref.removeprefix("refs/heads/")] = sha
        return heads
