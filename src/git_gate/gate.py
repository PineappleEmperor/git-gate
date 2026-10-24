"""The gate's five operations. Each call re-reads rules and token."""

import fnmatch
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from git_gate import Refused, ghpolicy
from git_gate.config import Rules, config_dir, load_rules, load_token, state_dir
from git_gate.github import PROTECTING_RULES, GitHub
from git_gate.gitops import EMPTY, Git
from git_gate.paths import copy_tree_into, open_dir, read_workspace_file, require_absolute

MAX_OUTPUT = 200_000
HANDOVER = "git -c core.hooksPath=/dev/null push git@github.com:{repo}.git {branch}"


class Gate:
    def __init__(
        self,
        config: Path | None = None,
        state: Path | None = None,
        *,
        base_url: str = "https://github.com",
        git: str = "/usr/bin/git",
        gh: str = "/usr/bin/gh",
        github: Callable[[str], GitHub] = GitHub,
    ):
        self.config = config or config_dir()
        self.state = state or state_dir()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.git = Git(self.state, base_url, git)
        self.gh_bin = gh
        self.github = github
        (self.state / "gh").mkdir(exist_ok=True, mode=0o700)

    # --- plumbing -----------------------------------------------------------------------

    def _load(self) -> tuple[Rules, str]:
        return load_rules(self.config), load_token(self.config)

    def call(self, tool: str, fn: Callable[..., str], **kwargs) -> str:
        """Run one tool, scrub the token from whatever comes back, and audit it."""
        token = None
        outcome = "ok"
        try:
            rules, token = self._load()
            result = fn(rules, token, **kwargs)
            return _scrub(result, token)
        except Refused as e:
            outcome = "refused"
            raise Refused(_scrub(str(e), token)) from None
        except Exception:
            outcome = "error"
            raise
        finally:
            self._audit(tool, kwargs, outcome, token)

    def _audit(self, tool: str, kwargs: dict, outcome: str, token: str | None) -> None:
        line = json.dumps(
            {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "args": kwargs, "outcome": outcome}
        )
        with open(self.state / "audit.log", "a") as f:
            f.write(_scrub(line, token) + "\n")

    def _refuse_protected(self, rules: Rules, api: GitHub, repo: str, branch: str, default: str) -> None:
        if branch == default:
            raise Refused(f"{branch} is the default branch of {repo}.")
        for pattern in rules.protected_branches:
            if fnmatch.fnmatchcase(branch, pattern):
                raise Refused(f"{branch} matches protected pattern {pattern!r} in rules.toml.")
        info = api.branch(repo, branch)
        if info and info["protected"]:
            raise Refused(f"{branch} is protected on GitHub.")
        rules_on = api.rule_types(repo, branch) & PROTECTING_RULES
        if rules_on:
            raise Refused(f"{branch} is covered by ruleset rules: {', '.join(sorted(rules_on))}.")

    # --- tools --------------------------------------------------------------------------

    def clone(self, rules: Rules, token: str, repo: str, path: str) -> str:
        rules.access(repo)
        target = require_absolute(path, "Clone path")
        name = target.name
        if name in ("", ".", "..") or target != target.parent / name:
            raise Refused(f"{path} does not name a new directory.")
        url = self.git.url(repo)
        t = rules.git_timeout
        with open_dir(rules, target.parent, "Clone parent") as (pfd, preal):
            try:
                os.rmdir(name, dir_fd=pfd)
            except FileNotFoundError:
                pass
            except OSError:
                raise Refused(f"{preal / name} already exists and is not an empty directory.") from None
            staging = Path(tempfile.mkdtemp(dir=self.state / "staging"))
            try:
                work = staging / "repo"
                self.git.run(["init", "--quiet", "--initial-branch=main", str(work)], timeout=t)
                gd = work / ".git"
                self.git.run(["config", "remote.origin.url", url], timeout=t, git_dir=gd)
                self.git.run(
                    ["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"], timeout=t, git_dir=gd
                )
                sym = self.git.run(["ls-remote", "--symref", url, "HEAD"], timeout=t, token=token, git_dir=gd)
                default = None
                for line in sym.stdout.splitlines():
                    if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                        default = line.removeprefix("ref: refs/heads/").removesuffix("\tHEAD")
                self.git.run(
                    ["fetch", "--no-tags", url, "+refs/heads/*:refs/remotes/origin/*"],
                    timeout=t,
                    token=token,
                    git_dir=gd,
                )
                if default:
                    self.git.run(
                        ["update-ref", f"refs/heads/{default}", f"refs/remotes/origin/{default}"], timeout=t, git_dir=gd
                    )
                    self.git.run(["symbolic-ref", "HEAD", f"refs/heads/{default}"], timeout=t, git_dir=gd)
                    self.git.run(
                        ["symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{default}"],
                        timeout=t,
                        git_dir=gd,
                    )
                    self.git.run(["config", f"branch.{default}.remote", "origin"], timeout=t, git_dir=gd)
                    self.git.run(["config", f"branch.{default}.merge", f"refs/heads/{default}"], timeout=t, git_dir=gd)
                branches = self.git.run(
                    ["for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes/origin/"], timeout=t, git_dir=gd
                ).stdout.split()
                try:
                    os.rename(work, name, dst_dir_fd=pfd)
                except OSError as e:
                    raise Refused(f"Could not move the clone into {preal}: {e.strerror}.") from None
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        dest = preal / name
        if not default:
            return f"Cloned {repo} into {dest}. The remote has no default branch yet (empty repository)."
        return (
            f"Cloned {repo} into {dest}: {len([b for b in branches if b != 'HEAD'])} branches, "
            f"default {default}. Nothing is checked out yet; inside the sandbox run:\n\n"
            f"git -C {dest} reset --hard"
        )

    def fetch(self, rules: Rules, token: str, repo: str, path: str) -> str:
        rules.access(repo)
        info = self.git.validate(rules, repo, path)
        url = self.git.url(repo)
        t = rules.git_timeout
        with self.git.shadow(rules, info) as sd:
            heads = self.git.remote_heads(url, token, rules, sd)
            proc = self.git.run(
                ["fetch", "--no-tags", "--no-write-fetch-head", url, "+refs/heads/*:refs/remotes/origin/*"],
                timeout=t, token=token, git_dir=sd,
            )  # fmt: skip
            self.git.write_back(sd, "refs/remotes/origin/", rules)
            tracking = self.git.run(
                ["for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes/origin/"], timeout=t, git_dir=sd
            ).stdout.split()
        stale = sorted(b for b in tracking if b != "HEAD" and b not in heads)
        out = proc.stderr.strip() or f"origin/* already up to date ({len(heads)} branches)."
        if stale:
            out += (
                "\n\nThese remote-tracking branches no longer exist on GitHub (the gate does not prune). "
                "Delete them in the sandbox with:\n\ngit -C "
                + str(info.real)
                + " branch -dr "
                + " ".join(f"origin/{b}" for b in stale)
            )
        return out

    def push(self, rules: Rules, token: str, repo: str, path: str, branch: str) -> str:
        rules.require_write(repo)
        self.git.check_branch(branch, rules)
        api = self.github(token)
        default = api.default_branch(repo)
        self._refuse_protected(rules, api, repo, branch, default)
        info = self.git.validate(rules, repo, path)
        url = self.git.url(repo)
        t = rules.git_timeout
        with self.git.shadow(rules, info) as sd:
            local = self.git.run(
                ["rev-parse", "--verify", "--quiet", "--end-of-options", f"refs/heads/{branch}^{{commit}}"],
                timeout=t, git_dir=sd, check=False,
            ).stdout.strip()  # fmt: skip
            if not local:
                raise Refused(f"No local branch {branch} in {info.real}.")
            heads = self.git.remote_heads(url, token, rules, sd)
            remote = heads.get(branch)
            base = remote or heads.get(default)
            wanted = [f"refs/heads/{b}" for b in {branch, default} if b in heads]
            if wanted:
                self.git.run(
                    ["fetch", "--no-tags", "--no-write-fetch-head", url, *wanted], timeout=t, token=token, git_dir=sd
                )
            if remote == local:
                return f"{branch} is already at {local[:12]} on {repo}."
            if remote:
                ff = self.git.run(["merge-base", "--is-ancestor", remote, local], timeout=t, git_dir=sd, check=False)
                if ff.returncode != 0:
                    raise Refused(
                        f"{branch} on {repo} has commits yours does not ({remote[:12]}); fetch, then rebase or "
                        "merge origin/" + branch + " and push again. The gate never force-pushes."
                    )
            new = self.git.run(
                ["rev-list", local, *(["--not", base] if base else [])], timeout=t, git_dir=sd
            ).stdout.split()
            touched = self.git.workflow_touches(new, sd, rules)
            if not touched and base:
                # Per-commit diffs miss a merge that takes an older workflow from one parent, so also
                # compare the whole tree with the remote branch, or with where it forked from default.
                fork = (
                    remote
                    or self.git.run(["merge-base", base, local], timeout=t, git_dir=sd, check=False).stdout.strip()
                    or base
                )
                if self.git.workflows_differ(fork, local, sd, rules):
                    touched = [local]
            if touched:
                raise Refused(
                    f"Commits {', '.join(c[:12] for c in touched)} touch .github/workflows/, which the gate never "
                    "pushes. Hand this to the user to push over SSH from their own terminal:\n\n"
                    + HANDOVER.format(repo=repo, branch=branch)
                )
            proc = self.git.run(
                ["push", "--porcelain", "--no-verify", "--no-follow-tags", url, f"{local}:refs/heads/{branch}"],
                timeout=t, token=token, git_dir=sd, check=False,
            )  # fmt: skip
        if proc.returncode != 0:
            raise Refused(f"git push was rejected:\n{(proc.stdout + proc.stderr).strip()}")
        return f"Pushed {len(new)} commit(s) to {repo} {branch} ({(remote or 'new')[:12]} -> {local[:12]})."

    def prune(self, rules: Rules, token: str, repo: str, branch: str) -> str:
        rules.require_write(repo)
        self.git.check_branch(branch, rules)
        api = self.github(token)
        default = api.default_branch(repo)
        self._refuse_protected(rules, api, repo, branch, default)
        info = api.branch(repo, branch)
        if info is None:
            raise Refused(f"{repo} has no branch {branch}.")
        pulls = api.pulls_from(repo, branch)
        if not pulls:
            raise Refused(
                f"No pull request was ever opened from {branch}; the gate only prunes merged or closed PR branches."
            )
        open_ = [p["number"] for p in pulls if p["state"] == "open"]
        if open_:
            raise Refused(f"{branch} still has open pull request(s): {', '.join(f'#{n}' for n in open_)}.")
        last = max(pulls, key=lambda p: p["closed_at"])
        if last["head_sha"] != info["sha"]:
            raise Refused(
                f"{branch} has moved since PR #{last['number']} closed "
                f"({last['head_sha'][:12]} -> {info['sha'][:12]}); "
                "those commits were never reviewed, so the gate will not delete them."
            )
        api.delete_branch(repo, branch)
        return f"Deleted {branch} from {repo} (PR #{last['number']} {last['state']})."

    def gh(self, rules: Rules, token: str, repo: str, args: list[str]) -> str:
        p = ghpolicy.plan(repo, rules.access(repo), args)
        work = Path(tempfile.mkdtemp(dir=self.state / "tmp"))
        try:
            for i, (index, src) in enumerate(sorted(p.uploads.items())):
                data = read_workspace_file(rules, src, "Body file")
                if token.encode() in data:
                    raise Refused(f"{src} contains the gate's token; refusing to upload it.")
                copy = work / f"upload-{i}"
                copy.write_bytes(data)
                p.argv[index] = str(copy)
            if p.download:
                index, _, is_dir = p.download
                dl = work / "download"
                dl.mkdir()
                p.argv[index] = str(dl if is_dir else dl / Path(p.download[1]).name)
            try:
                proc = subprocess.run(
                    [self.gh_bin, *p.argv],
                    env=self._gh_env(token) | p.env,
                    cwd=self.state / EMPTY,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=rules.gh_timeout,
                )
            except subprocess.TimeoutExpired:
                raise Refused(f"gh timed out after {rules.gh_timeout}s.") from None
            out = proc.stdout + (("\n" + proc.stderr) if proc.stderr.strip() else "")
            if proc.returncode != 0:
                raise Refused(f"gh exited {proc.returncode}:\n{_tail(out)}")
            if p.download:
                out += self._deliver(rules, p, dl)
            return _tail(out) or "(no output)"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _deliver(self, rules: Rules, p: ghpolicy.GhPlan, dl: Path) -> str:
        _, target, is_dir = p.download
        dest = Path(target) if is_dir else Path(target).parent
        with open_dir(rules, dest, "Download target") as (fd, real):
            written = copy_tree_into(dl, fd, p.clobber)
        return f"\nSaved to {real}: {', '.join(written) or 'nothing'}"

    def _gh_env(self, token: str) -> dict[str, str]:
        return self.git.env() | {
            "GH_TOKEN": token,
            "GH_HOST": "github.com",
            "GH_CONFIG_DIR": str(self.state / "gh"),
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
            "GH_SPINNER_DISABLED": "1",
            "GH_PAGER": "cat",
            "PAGER": "cat",
            "GH_EDITOR": "false",
            "EDITOR": "false",
            "VISUAL": "false",
            "GH_BROWSER": "false",
            "BROWSER": "false",
            "NO_COLOR": "1",
        }


def _scrub(text: str, token: str | None) -> str:
    return text.replace(token, "[token]") if token else text


def _tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else "[... truncated ...]\n" + text[-MAX_OUTPUT:]
