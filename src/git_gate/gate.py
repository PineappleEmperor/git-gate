"""The gate's five operations. Each call re-reads rules and token."""

import fcntl
import fnmatch
import hashlib
import json
import math
import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from git_gate import Refused, ghpolicy
from git_gate.config import REPO_RE, Rules, check_enabled, config_dir, load_rules, load_token, state_dir
from git_gate.github import PROTECTING_RULES, GitHub
from git_gate.gitops import EMPTY, MAX_STDERR, Git
from git_gate.handover import Handovers
from git_gate.mirror import MAX_HAVE, Mirror, Protected, comparison
from git_gate.paths import (
    check_new_target,
    copy_in,
    copy_tree_into,
    open_dir,
    place_new_file,
    read_workspace_file,
    require_absolute,
    write_out,
)

MAX_OUTPUT = 200_000
MAX_BUNDLE = 512 << 20
RATE_LIMIT = 60
RATE_WINDOW = 60
FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
MAX_NAMED = 20  # protected files named in a refusal; the handover record and screen hold them all


class HandedOver(Refused):
    """A refused push kept for the user: the message names `gitgatepush <id>`."""


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
        clock: Callable[[], float] = time.time,
    ):
        self.config = config or config_dir()
        self.state = state or state_dir()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.git = Git(self.state, base_url, git)
        self.mirror = Mirror(self.git, self.state)
        self.handovers = Handovers(self.state, mirror=self.mirror)
        self.gh_bin = gh
        self.github = github
        self.clock = clock
        for d in ("gh", "rate"):
            (self.state / d).mkdir(exist_ok=True, mode=0o700)

    # --- plumbing -----------------------------------------------------------------------

    def _load(self) -> tuple[Rules, str]:
        return load_rules(self.config), load_token(self.config)

    def call(self, tool: str, fn: Callable[..., str], **kwargs) -> str:
        """Run one tool, scrub the token from whatever comes back, and audit it.

        The off switch is checked first, then the per-repo rate limit, before anything is read.
        """
        token = None
        outcome = "ok"
        try:
            check_enabled(self.config)
            self._rate_limit(kwargs.get("repo"))
            rules, token = self._load()
            result = fn(rules, token, **kwargs)
            return utf8_safe(_scrub(result, token))
        except HandedOver as e:
            outcome = "handover"
            raise Refused(utf8_safe(_scrub(str(e), token))) from None
        except Refused as e:
            outcome = "refused"
            raise Refused(utf8_safe(_scrub(str(e), token))) from None
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

    def _rate_limit(self, repo: object) -> None:
        """At most RATE_LIMIT calls per RATE_WINDOW seconds per repo, counted in a file: each call may
        be its own process (the relay), so the count cannot live in memory."""
        # Only a well-formed owner/name gets a counter of its own; anything else shares one, so a
        # looping agent cannot create a file per made-up repo string.
        name = repo.lower() if isinstance(repo, str) and REPO_RE.fullmatch(repo) else "\0not-a-repo"
        key = hashlib.sha256(name.encode()).hexdigest()[:24]
        fd = os.open(self.state / "rate" / key, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            now = self.clock()
            stamps = []
            for word in f.read().split():
                try:
                    t = float(word)
                except ValueError:
                    continue
                if now - RATE_WINDOW < t <= now:
                    stamps.append(t)
            if len(stamps) >= RATE_LIMIT:
                wait = max(1, math.ceil(min(stamps) + RATE_WINDOW - now))
                raise Refused(
                    f"{repo} has had {RATE_LIMIT} calls in the last {RATE_WINDOW} s, the gate's limit; "
                    f"wait {wait} s and try again."
                )
            stamps.append(now)
            f.seek(0)
            f.truncate()
            f.write("".join(f"{t!r}\n" for t in stamps))

    def _protection(self, rules: Rules, api: GitHub, repo: str, branch: str, default: str) -> str | None:
        """Why `branch` is the user's to push, or None. API failures raise: they are not answers."""
        if branch == default:
            return f"{branch} is the default branch of {repo}."
        for pattern in rules.protected_branches:
            if fnmatch.fnmatchcase(branch, pattern):
                return f"{branch} matches protected pattern {pattern!r} in rules.toml."
        info = api.branch(repo, branch)
        if info and info["protected"]:
            return f"{branch} is protected on GitHub."
        rules_on = api.rule_types(repo, branch) & PROTECTING_RULES
        if rules_on:
            return f"{branch} is covered by ruleset rules: {', '.join(sorted(rules_on))}."
        return None

    def _require_unprotected(self, rules: Rules, api: GitHub, repo: str, branch: str, default: str) -> None:
        reason = self._protection(rules, api, repo, branch, default)
        if reason:
            raise Refused(reason)

    @contextmanager
    def _staged(self, kind: str):
        """A fresh, not yet existing path in gate storage, removed afterwards."""
        path = self.state / "staging" / f"{kind}-{secrets.token_hex(12)}.bundle"
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)

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

    def fetch(self, rules: Rules, token: str, repo: str, bundle: str, have: list[str] = ()) -> str:
        """Write every GitHub branch of `repo` to a bundle at `bundle`, leaving out what `have` covers.

        Every bundle lists all of GitHub's branches, so the agent's `git fetch --prune` keeps
        origin/* exactly equal to GitHub, which push bundles made with --not --remotes=origin rely on.
        """
        rules.access(repo)
        if not isinstance(have, list | tuple) or not all(isinstance(h, str) for h in have):
            raise Refused("have must be a list of commit SHA strings.")
        if len(have) > MAX_HAVE:
            raise Refused(f"have lists {len(have)} SHAs; the gate takes at most {MAX_HAVE}.")
        require_absolute(bundle, "Bundle output path")
        with self._staged("fetch") as tmp:
            with self.mirror.lock(repo):
                heads = self.mirror.sync(repo, token, rules)
                wrote = self.mirror.export_bundle(repo, tmp, list(have), rules)
            if not wrote:
                return f"{repo} has no branches on GitHub yet; nothing to fetch, and no bundle was written."
            write_out(rules, bundle, tmp, "bundle")
        listing = "\n".join(f"{name} {sha}" for name, sha in sorted(heads.items()))
        return (
            f"Wrote {len(heads)} branch(es) of {repo} to {bundle}. Inside the sandbox run:\n\n"
            f"git fetch --prune {shlex.quote(bundle)} '{FETCH_REFSPEC}'\n\n"
            "That makes origin/* exactly GitHub's branches. If git reports missing prerequisite commits, "
            "call fetch again with have=[].\n\n"
            f"GitHub's branches:\n{listing}"
        )

    def push(self, rules: Rules, token: str, repo: str, branch: str, bundle: str) -> str:
        """Push the one commit in `bundle` to `branch`, or keep it as a handover for the user."""
        rules.require_write(repo)
        repo_rules = rules.repo(repo)
        self.git.check_branch(branch, rules)
        with self._staged("push") as staged:
            copy_in(rules, bundle, staged, MAX_BUNDLE, "bundle")
            api = self.github(token)
            default = api.default_branch(repo)
            protected = self._protection(rules, api, repo, branch, default)
            with self.mirror.lock(repo):
                heads = self.mirror.sync(repo, token, rules)
                sha = self.mirror.import_bundle(repo, staged, branch, rules)
                remote = heads.get(branch)
                if remote == sha:
                    return f"{branch} is already at {sha[:12]} on {repo}."
                reasons = [protected] if protected else []
                advice = []
                lease = None
                if remote and not self.mirror.is_ancestor(repo, remote, sha, rules):
                    lease = remote
                    reasons.append(f"{branch} on {repo} is at {remote[:12]}, so this is not a fast-forward.")
                    advice.append(
                        f"If the rewrite was not meant, fetch, rebase or merge origin/{branch} and push again instead."
                    )
                # The comparison commits, fixed once (D2): the branch on GitHub and the default
                # branch's tip after sync. No other branch is ever one: agents can push those.
                default_tip = heads.get(default)
                fork = self.mirror.fork_point(repo, sha, default_tip, rules)
                patterns = repo_rules.handover_paths
                found = self.mirror.protected_changes(repo, sha, remote, default_tip, patterns, rules)
                if found:
                    reasons.append(_refusal_for(found))
                if reasons:
                    h = self.handovers.create(
                        repo, branch, sha, remote or fork, lease, " ".join(reasons),
                        patterns=patterns, protected=found.files, exclude=comparison(remote, default_tip),
                    )  # fmt: skip
                    try:
                        self.mirror.keep(repo, h.ref, sha, rules)
                    except BaseException:
                        self.handovers.close(h.id)  # a record must never name a commit the mirror may lose
                        raise
                    raise HandedOver(
                        f"Not pushed: {' '.join(reasons)} That push is the user's. The gate kept {sha[:12]} as "
                        f"handover {h.id}; hand the user this command, which shows the commits and asks before "
                        f"pushing from their own terminal:\n\ngitgatepush {h.id}" + "".join(f"\n\n{a}" for a in advice)
                    )
                new = self.mirror.new_commits(repo, sha, remote or default_tip, rules)
                proc = self.mirror.push(repo, sha, branch, token, rules)
        if proc.returncode != 0:
            raise Refused(f"git push was rejected:\n{(proc.stdout + proc.stderr).strip()[-MAX_STDERR:]}")
        lines = [f"Pushed {len(new)} commit(s) to {repo} {branch} ({(remote or 'new')[:12]} -> {sha[:12]})."]
        try:
            pull = api.open_pull(repo, branch)
        except Refused:
            pull = None
        if pull:
            lines.append(f"Pull request: {pull}")
        else:
            q = urllib.parse.quote
            lines.append(f"Open a pull request: https://github.com/{repo}/compare/{q(default)}...{q(branch)}")
        if repo_rules.push_deploys:
            lines.append("This push deploys.")
        return "\n".join(lines)

    def prune(self, rules: Rules, token: str, repo: str, branch: str) -> str:
        rules.require_write(repo)
        self.git.check_branch(branch, rules)
        api = self.github(token)
        default = api.default_branch(repo)
        self._require_unprotected(rules, api, repo, branch, default)
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
                data = read_workspace_file(rules, src, "body")
                if token.encode() in data:
                    raise Refused(f"{src} contains the gate's token; refusing to upload it.")
                copy = work / f"upload-{i}"
                copy.write_bytes(data)
                p.argv[index] = str(copy)
            if p.download:
                index, target, is_dir = p.download
                check_new_target(rules, target, "Download target")
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
        if is_dir:
            real, written, skipped = copy_tree_into(rules, dl, target, "Download target")
        else:
            got = dl / Path(target).name
            if not got.is_file() or got.is_symlink():
                return "\nNothing was downloaded."
            real, written, skipped = place_new_file(rules, got, target, "Download target"), [Path(target).name], []
        note = f"; skipped symlinks and dot-named entries: {', '.join(skipped)}" if skipped else ""
        return f"\nSaved to {real}: {', '.join(written) or 'nothing'}{note}"

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


def _refusal_for(found: Protected) -> str:
    shown = found.files[:MAX_NAMED]
    more = f" and {len(found.files) - MAX_NAMED} more" if len(found.files) > MAX_NAMED else ""
    reason = f"It changes {len(found.files)} file(s) under handover paths: {', '.join(shown)}{more}."
    if found.commits:
        reason += f" Commit(s) {', '.join(c[:12] for c in found.commits)} change them."
    return reason


def utf8_safe(text: str) -> str:
    """Text that encodes as strict UTF-8: each lone surrogate becomes "?".

    git output is decoded with surrogateescape, so a file name that is not UTF-8 arrives as lone
    surrogates. A tool reply goes out as JSON, whose encoder refuses them, and a refused reply
    takes down the whole stdio server rather than one call.
    """
    return text.encode("utf-8", "replace").decode("utf-8")


def _scrub(text: str, token: str | None) -> str:
    return text.replace(token, "[token]") if token else text


def _tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else "[... truncated ...]\n" + text[-MAX_OUTPUT:]
