"""The gate's tools. Each call re-reads rules and token."""

import fcntl
import functools
import hashlib
import inspect
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
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import NoReturn

from git_gate import Refused, ci, ghpolicy, reads, writes
from git_gate.call import TOOLS, audit, log_error
from git_gate.config import REPO_RE, Rules, check_enabled, config_dir, load_rules, load_token, state_dir
from git_gate.github import FromGitHub, GitHub
from git_gate.gitops import EMPTY, MAX_STDERR, Git
from git_gate.handover import Handovers
from git_gate.mirror import MAX_HAVE, Mirror, Protected, comparison
from git_gate.paths import (
    check_new_target,
    copy_in,
    copy_tree_into,
    open_dir,
    place_new_file,
    require_absolute,
    write_out,
)

MAX_OUTPUT = 200_000
MAX_BUNDLE = 512 << 20
RATE_LIMIT = 60
RATE_WINDOW = 60
FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
# What GitHub says when a token without the Workflows permission pushes a change to .github/workflows.
WORKFLOW_SCOPE = "create or update workflow"
MAX_NAMED = 20  # protected files named in a refusal; the handover record and screen hold them all
PROC = Path("/proc")
MAX_REASON = 300  # characters of a refusal's first line kept in audit.log
MAX_ARG = 1000  # characters of one string argument kept in audit.log
MAX_ARGS = 8 << 10  # characters of all the arguments, as JSON, kept in audit.log
# (entries kept, characters kept of each string in them) for list arguments, tried in turn until all fit
LIST_STEPS = ((20, MAX_ARG), (20, 100), (5, 100), (0, 0))


# Fields a tool adds to its own audit line (the CI tools' polls and waited_s). Per call and per
# thread: the fallback server runs calls in worker threads of one process.
_AUDIT_EXTRA: ContextVar[dict] = ContextVar("audit_extra")


class InternalError(Exception):
    """A tool failed in a way nobody planned for; the message is scrubbed and safe to show an agent."""


class HandedOver(Refused):
    """A refused push kept for the user: the message names `gitgatepush <id>`."""


def _untrusted(tool: Callable[..., str]) -> Callable[..., str]:
    """Mark a tool's reply as GitHub content; refusals carrying GitHub's text are marked in `Gate.call`."""

    @functools.wraps(tool)
    def marked(*args, **kwargs) -> str:
        return writes.untrusted(tool(*args, **kwargs))

    return marked


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
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
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
        self.sleep = sleep
        self.monotonic = monotonic
        (self.state / "rate").mkdir(exist_ok=True, mode=0o700)

    # --- plumbing -----------------------------------------------------------------------

    def _load(self) -> tuple[Rules, str]:
        return load_rules(self.config), load_token(self.config)

    def dispatch(self, tool: str, kwargs: dict) -> str:
        """Run the named tool with `kwargs` through `call`: the one place a tool name becomes a method."""
        if tool not in TOOLS:
            raise Refused(f"git-gate has no tool {tool!r}.")
        if not isinstance(kwargs, dict):
            raise Refused(f"{tool}: the arguments must be a JSON object.")
        return self.call(tool, getattr(self, tool), **kwargs)

    def call(self, tool: str, fn: Callable[..., str], **kwargs) -> str:
        """Run one tool, scrub the token from whatever comes back, and audit it.

        The off switch is checked first, then the per-repo rate limit, before anything is read.
        Arguments the tool does not take are refused, never an internal error. With v2 rules the
        tool gets only the calling session's rules: the session is the conversation's project
        folder, this process's cwd, which no argument can name. A refusal that carries GitHub's
        text is marked as untrusted, whichever tool raised it.
        """
        started = time.monotonic()
        cwd = _cwd()
        token = None
        access = None  # the session's access to the repo, for audit.log
        outcome = "ok"
        message = None  # what the agent is told when the outcome is not ok, scrubbed
        extra: dict = {}
        reset = _AUDIT_EXTRA.set(extra)
        try:
            check_enabled(self.config)
            self._rate_limit(kwargs.get("repo"))
            try:
                inspect.signature(fn).bind(None, None, **kwargs)
            except TypeError as e:
                raise Refused(f"{tool}: {e}.") from None
            rules, token = self._load()
            access = "no-session" if rules.version == 2 else None
            rules = rules.scoped(cwd)
            access = rules.level(kwargs.get("repo"))
            result = fn(rules, token, **kwargs)
            return utf8_safe(_scrub(result, token))
        except HandedOver as e:
            outcome, message = "handover", utf8_safe(_scrub(str(e), token))
            raise Refused(message) from None
        except FromGitHub as e:
            outcome, message = "refused", utf8_safe(_scrub(str(e), token))
            raise Refused(writes.untrusted(message)) from None
        except Refused as e:
            outcome, message = "refused", utf8_safe(_scrub(str(e), token))
            raise Refused(message) from None
        except Exception as e:
            outcome = "error"
            # Nothing unplanned reaches the agent raw: the text is scrubbed and holds no lone surrogate.
            message = utf8_safe(_scrub(f"{type(e).__name__}: {e}", token))
            raise InternalError(message) from e
        finally:
            _AUDIT_EXTRA.reset(reset)
            ms = int((time.monotonic() - started) * 1000)
            self._audit(tool, kwargs, outcome, token, message, ms, cwd, access, extra)

    def _audit(
        self, tool: str, kwargs: dict, outcome: str, token: str | None, message: str | None, ms: int,
        cwd: str | None, access: str | None, extra: dict | None = None,
    ) -> None:  # fmt: skip
        """One line per call. `reason` is the first line of what the agent was told, without the
        untrusted-content marker a refusal carrying GitHub's text starts with. `access` is the
        calling session's access to the repo under v2 rules ("write", "read", "none", or
        "no-session" when the folder is no session's), and null under v1 or before rules are read.
        `extra` holds the fields the tool added, after the common ones."""
        reason = message.strip().partition("\n")[0][:MAX_REASON] if message is not None else None
        try:
            line = json.dumps({
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "args": _logged_args(kwargs, token),
                "outcome": outcome, "reason": reason, "ms": ms, "session": _session(), "cwd": cwd,
                "access": access, **(extra or {}),
            })  # fmt: skip
            audit(self.state, _scrub(line, token))
        except Exception as e:
            # The call has already happened: failing it now would misreport what was done.
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            log_error(_scrub(f"{stamp} {tool} audit.log could not be written: {type(e).__name__}: {e}\n", token))

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

    def _require_unprotected(self, rules: Rules, api: GitHub, repo: str, branch: str, default: str) -> None:
        reason = writes.protection(rules, api, repo, branch, default)
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
            protected = writes.protection(rules, api, repo, branch, default)
            with self.mirror.lock(repo):
                heads = self.mirror.sync(repo, token, rules)
                sha = self.mirror.import_bundle(repo, staged, branch, rules)
                remote = heads.get(branch)
                if remote == sha:
                    return f"{branch} is already at {sha[:12]} on {repo}."
                # The comparison commits, fixed once (D2): the branch on GitHub and the default
                # branch's tip after sync. No other branch is ever one: agents can push those.
                default_tip = heads.get(default)
                patterns = repo_rules.handover_paths
                found = self.mirror.protected_changes(repo, sha, remote, default_tip, patterns, rules)
                if found.stale:
                    raise Refused(_stale_refusal(branch, default, found.stale))
                reasons = [protected] if protected else []
                advice = []
                lease = None
                if remote and not self.mirror.is_ancestor(repo, remote, sha, rules):
                    lease = remote
                    reasons.append(f"{branch} on {repo} is at {remote[:12]}, so this is not a fast-forward.")
                    advice.append(
                        f"If the rewrite was not meant, fetch, rebase or merge origin/{branch} and push again instead."
                    )
                fork = self.mirror.fork_point(repo, sha, default_tip, rules)
                if found:
                    reasons.append(_refusal_for(found))
                hand_over = functools.partial(
                    self._hand_over, rules, repo, branch, sha, remote or fork, lease,
                    patterns=patterns, protected=found.files, exclude=comparison(remote, default_tip),
                )  # fmt: skip
                if reasons:
                    hand_over(reasons, advice)
                new = self.mirror.new_commits(repo, sha, remote or default_tip, rules)
                proc = self.mirror.push(repo, sha, branch, token, rules)
                if proc.returncode != 0 and WORKFLOW_SCOPE in proc.stdout + proc.stderr:
                    # The PAT has no Workflows permission; the user's own push is not bound by it (I1).
                    hand_over(["GitHub refuses workflow changes from the gate's token."], [])
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

    def _hand_over(
        self, rules: Rules, repo: str, branch: str, sha: str, base: str | None, lease: str | None,
        reasons: list[str], advice: list[str], **record,
    ) -> NoReturn:  # fmt: skip
        """Keep sha for the user's `gitgatepush` and tell the agent so; the caller holds the mirror lock."""
        h = self.handovers.create(repo, branch, sha, base, lease, " ".join(reasons), **record)
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

    # --- typed writes (the checks live in writes.py) -------------------------------------

    def _writes(self, rules: Rules, token: str) -> writes.Context:
        return writes.Context(rules, token, self.github(token), self.git)

    @_untrusted
    def pr(
        self,
        rules: Rules,
        token: str,
        repo: str,
        action: str,
        number: int | None = None,
        title: str | None = None,
        body_file: str | None = None,
        base: str | None = None,
        head: str | None = None,
        draft: bool = False,
        state: str | None = None,
        limit: int | None = None,
    ) -> str:
        fields = dict(number=number, title=title, body_file=body_file, base=base, head=head, draft=draft, state=state,
                      limit=limit)  # fmt: skip
        if isinstance(action, str) and action in reads.PR_READS:
            return reads.pr(self._ci(rules, token, repo), repo, action, **fields)
        return writes.pr(self._writes(rules, token), repo, action, **fields, reads=tuple(reads.PR_READS))

    @_untrusted
    def issue(
        self,
        rules: Rules,
        token: str,
        repo: str,
        action: str,
        number: int | None = None,
        title: str | None = None,
        body_file: str | None = None,
    ) -> str:
        return writes.issue(self._writes(rules, token), repo, action, number, title, body_file)

    @_untrusted
    def triage(
        self,
        rules: Rules,
        token: str,
        repo: str,
        number: int,
        add_labels: Sequence[str] = (),
        remove_labels: Sequence[str] = (),
        add_assignees: Sequence[str] = (),
        remove_assignees: Sequence[str] = (),
        add_reviewers: Sequence[str] = (),
        remove_reviewers: Sequence[str] = (),
    ) -> str:
        return writes.triage(
            self._writes(rules, token), repo, number,
            add_labels, remove_labels, add_assignees, remove_assignees, add_reviewers, remove_reviewers,
        )  # fmt: skip

    @_untrusted
    def workflow_run(self, rules: Rules, token: str, repo: str, action: str, run_id: int) -> str:
        return writes.workflow_run(self._writes(rules, token), repo, action, run_id)

    @_untrusted
    def label(
        self,
        rules: Rules,
        token: str,
        repo: str,
        action: str,
        name: str,
        color: str | None = None,
        description: str | None = None,
        new_name: str | None = None,
    ) -> str:
        return writes.label(self._writes(rules, token), repo, action, name, color, description, new_name)

    # --- CI status (the reading lives in ci.py) -----------------------------------------

    def _ci(self, rules: Rules, token: str, repo: str) -> ci.Context:
        return ci.Context(
            rules, self.github(token), self.git, MAX_OUTPUT, self.sleep, self.monotonic, self.clock, _note_polls,
            functools.partial(self._recheck, token, repo),
        )  # fmt: skip

    def _recheck(self, token: str, repo: str) -> None:
        """The checks `call` made before the tool ran, made again: a wait outlasts them. The off
        switch, the session's read access to `repo` under the rules as they are now, and the token,
        which must still be the one this call holds (its replies are scrubbed of that one)."""
        check_enabled(self.config)
        rules, now = self._load()
        rules.scoped(_cwd()).access(repo)
        if now != token:
            raise Refused("The gate's token changed while waiting; call again to wait with the new one.")

    @_untrusted
    def checks(
        self, rules: Rules, token: str, repo: str, ref: int | str, wait: bool = False, timeout_minutes: int = 20
    ) -> str:
        return ci.checks(self._ci(rules, token, repo), repo, ref, wait, timeout_minutes)

    @_untrusted
    def run(
        self, rules: Rules, token: str, repo: str, run_id: int, wait: bool = False, timeout_minutes: int = 20
    ) -> str:
        return ci.run(self._ci(rules, token, repo), repo, run_id, wait, timeout_minutes)

    # --- read-only gh -------------------------------------------------------------------

    @_untrusted
    def gh(self, rules: Rules, token: str, repo: str, args: list[str]) -> str:
        rules.access(repo)
        p = ghpolicy.plan(repo, args)
        work = Path(tempfile.mkdtemp(dir=self.state / "tmp"))
        try:
            if p.download:
                index, target, is_dir = p.download
                check_new_target(rules, target, "Download target")
                dl = work / "download"
                dl.mkdir()
                p.argv[index] = str(dl if is_dir else dl / Path(p.download[1]).name)
            try:
                proc = subprocess.run(
                    [self.gh_bin, *p.argv],
                    env=self._gh_env(_gh_config(work, token)) | p.env,
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
                raise FromGitHub(f"gh exited {proc.returncode}:\n{_tail(out)}")
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

    def _gh_env(self, config: Path) -> dict[str, str]:
        """gh's whole environment. It never holds the token: `--jq` filters can read it (`$ENV`, `env`)."""
        return self.git.env() | {
            "GH_HOST": "github.com",
            "GH_CONFIG_DIR": str(config),
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


def _note_polls(polls: int, waited: float) -> None:
    """A CI tool's GitHub polls and seconds slept between them, for its audit line."""
    extra = _AUDIT_EXTRA.get(None)
    if extra is not None:
        extra.update(polls=polls, waited_s=round(waited))


def _gh_config(work: Path, token: str) -> Path:
    """A fresh gh config directory for one call, its token in hosts.yml (0600) rather than the environment.

    Written in gh's multi-account layout with `version: "1"`, so gh migrates nothing and asks
    GitHub for no user name. The directory goes when `work` does, at the end of the call.
    """
    config = work / "gh"
    config.mkdir(mode=0o700)
    quoted = json.dumps(token)  # a JSON string is a valid YAML scalar
    files = {
        "config.yml": 'version: "1"\n',
        "hosts.yml": (
            "github.com:\n"
            f"    users:\n        git-gate:\n            oauth_token: {quoted}\n"
            f"    git_protocol: https\n    user: git-gate\n    oauth_token: {quoted}\n"
        ),
    }
    for name, text in files.items():
        fd = os.open(config / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
    return config


def _stale_refusal(branch: str, default: str, stale: tuple[str, ...]) -> str:
    """No handover: the branch is older than the default branch at these files, and a rebase fixes that."""
    shown = stale[:MAX_NAMED]
    more = f" and {len(stale) - MAX_NAMED} more" if len(stale) > MAX_NAMED else ""
    return (
        f"Not pushed: {branch} does not change {len(stale)} file(s) under handover paths that {default} has "
        f"changed since {branch} forked from it: {', '.join(shown)}{more}. Pushing it would put their older "
        f"versions on GitHub. No handover was kept: fetch, then rebase or merge origin/{default}, and push again."
    )


def _refusal_for(found: Protected) -> str:
    shown = found.files[:MAX_NAMED]
    more = f" and {len(found.files) - MAX_NAMED} more" if len(found.files) > MAX_NAMED else ""
    reason = f"It changes {len(found.files)} file(s) under handover paths: {', '.join(shown)}{more}."
    if found.commits:
        reason += f" Commit(s) {', '.join(c[:12] for c in found.commits)} change them."
    return reason


def _logged_args(kwargs: dict, token: str | None) -> dict:
    """The arguments as audit.log keeps them, at most MAX_ARGS characters as JSON.

    They are the agent's, so this is what bounds how fast it can fill the log, and padding one
    argument must not hide the others: every argument that is not a list is kept, cut at MAX_ARG.
    Lists give way step by step (LIST_STEPS), keeping their heads, where gh's command is, and a
    count of the rest. Only arguments no tool declares can make even that too long.
    """
    for items, chars in LIST_STEPS:
        args = {
            _cut(k, MAX_ARG, token): _cut(v, MAX_ARG, token) if isinstance(v, str) else _head(v, items, chars, token)
            for k, v in kwargs.items()
        }
        if len(json.dumps(args)) <= MAX_ARGS:
            return args
    kept, size = {}, 0
    for k, v in args.items():
        size += len(json.dumps({k: v}))
        if size > MAX_ARGS:
            break
        kept[k] = v
    return kept | {"…": f"[+{len(args) - len(kept)} more arguments, {len(json.dumps(kwargs))} chars in all]"}


def _head(value: object, items: int, chars: int, token: str | None) -> object:
    """A list or object argument as its first `items` entries, strings cut at `chars`, and a count of the rest."""
    if isinstance(value, str):
        return _cut(value, chars, token)
    if isinstance(value, list | tuple):
        rest = [f"…[+{len(value) - items} more]"] if len(value) > items else []
        return [_head(v, items, chars, token) for v in value[:items]] + rest
    if isinstance(value, dict):
        rest = {"…": f"[+{len(value) - items} more]"} if len(value) > items else {}
        return {_cut(k, chars, token): _head(v, items, chars, token) for k, v in list(value.items())[:items]} | rest
    return value


def _cut(text: str, limit: int, token: str | None) -> str:
    text = _scrub(text, token)  # before the cut, which could leave half a token unmatched
    return text if len(text) <= limit else f"{text[:limit]}…[+{len(text) - limit} chars]"


def _session(proc: Path = PROC) -> str:
    """The calling conversation: its relay's pid and start time (/proc stat field 22), so a reused pid
    is a new session. The relay is this process's parent; without /proc, its pid alone."""
    ppid = os.getppid()
    try:
        # The process name, in parentheses, may hold spaces and ")": the fields start after the last ")".
        return f"{ppid}-{int((proc / str(ppid) / 'stat').read_text().rpartition(')')[2].split()[19])}"
    except OSError, ValueError, IndexError:
        return str(ppid)


def _cwd() -> str | None:
    """The conversation's project folder: the relay runs there and its child inherits it; None if gone."""
    try:
        return os.getcwd()
    except OSError:
        return None


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
