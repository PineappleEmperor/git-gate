"""The gate's own bare mirror of each GitHub repo, where all of its git work happens.

Agent git data arrives only as a bundle file already copied into gate storage. It is unbundled
here with the gate's global config (transfer.fsckObjects on, so every object is re-hashed; hooks
off; replace refs ignored), and nothing an agent wrote is ever read as config or followed as a
path. Methods do not lock: callers hold `lock(repo)` around each sequence of calls.
"""

import os
import re
import shutil
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from git_gate import Refused
from git_gate.config import REPO_RE, Rules
from git_gate.gitops import MAX_STDERR, Git

MAX_HAVE = 1000
INIT_TIMEOUT = 60
INCOMING = "refs/gate/incoming/"
HANDOVER_REF = re.compile(r"^refs/gate/handover/[0-9]+$")
OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
MISSING_PREREQS = ("lacks these prerequisite commits", "Repository lacks")
BUNDLE_HOWTO = "git bundle create <file> {branch} --not --remotes=origin"


class Mirror:
    def __init__(self, git: Git, state: Path):
        self.git = git
        self.state = state
        self.root = state / "mirrors"
        self.template = state / "template"

    # --- the mirror itself --------------------------------------------------------------

    def path(self, repo: str) -> Path:
        """state/mirrors/<owner>/<name>.git, created bare from the gate's empty template on first use."""
        if not REPO_RE.match(repo):
            raise Refused(f"'{repo}' is not an owner/name repository.")
        owner, name = repo.lower().split("/")
        path = self.root / owner / f"{name}.git"
        if not (path / "HEAD").is_file():
            self._create(path)
        return path

    def _create(self, path: Path) -> None:
        self.root.mkdir(exist_ok=True, mode=0o700)
        path.parent.mkdir(exist_ok=True, mode=0o700)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.new")
        shutil.rmtree(tmp, ignore_errors=True)
        self.git.run(["init", "--bare", "--quiet", f"--template={self.template}", str(tmp)], timeout=INIT_TIMEOUT)
        try:
            tmp.rename(path)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)  # another process made it first
            if not (path / "HEAD").is_file():
                raise Refused(f"Could not create the gate's mirror at {path}.") from None

    @contextmanager
    def lock(self, repo: str):
        with self.git.lock(f"mirror:{repo.lower()}"):
            yield

    def _git(
        self, repo: str, rules: Rules, args: list[str], *, token: str | None = None, check: bool = True,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess:  # fmt: skip
        return self.git.run(
            args, timeout=rules.git_timeout, token=token, git_dir=self.path(repo), check=check, stdin=stdin
        )

    def _heads(self, repo: str, rules: Rules) -> dict[str, str]:
        out = self._git(repo, rules, ["for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/"]).stdout
        return {ref.removeprefix("refs/heads/"): sha for sha, ref in (line.split(" ", 1) for line in out.splitlines())}

    # --- GitHub -------------------------------------------------------------------------

    def sync(self, repo: str, token: str, rules: Rules) -> dict[str, str]:
        """Make the mirror's branches exactly GitHub's; return branch -> sha."""
        refspec = "+refs/heads/*:refs/heads/*"
        args = ["fetch", "--prune", "--no-tags", "--no-write-fetch-head", self.git.url(repo), refspec]
        self._git(repo, rules, args, token=token)
        return self._heads(repo, rules)

    def push(self, repo: str, sha: str, branch: str, token: str, rules: Rules) -> subprocess.CompletedProcess:
        """Push one verified commit to a branch on GitHub. Never forced; the caller reads the result."""
        self.git.check_branch(branch, rules)
        return self._git(
            repo, rules,
            ["push", "--porcelain", "--no-verify", "--no-follow-tags", self.git.url(repo),
             f"{_oid(sha)}:refs/heads/{branch}"],
            token=token, check=False,
        )  # fmt: skip

    # --- bundles ------------------------------------------------------------------------

    def import_bundle(self, repo: str, bundle: Path, branch: str, rules: Rules) -> str:
        """Unbundle refs/heads/<branch> into refs/gate/incoming/<branch>; return its commit.

        Call after `sync`, so prerequisites the bundle needs from GitHub are already here. The
        bundle must be a regular file in gate storage: git would read a directory as a repository.
        """
        bundle = self._stored_file(bundle)
        self.git.check_branch(branch, rules)
        ref = f"refs/heads/{branch}"
        listed = self._git(repo, rules, ["bundle", "list-heads", str(bundle)], check=False)
        if listed.returncode != 0:
            raise Refused(f"{bundle.name} is not a git bundle:\n{_tail(listed.stderr)}")
        heads = [line.split(" ", 1) for line in listed.stdout.splitlines() if line]
        if len(heads) != 1 or len(heads[0]) != 2 or heads[0][1] != ref:
            held = ", ".join(h[-1] for h in heads) or "none"
            raise Refused(
                f"The bundle must hold exactly one ref, {ref}; it holds {held}. Make it with:\n\n"
                + BUNDLE_HOWTO.format(branch=branch)
            )
        sha = _oid(heads[0][0])

        verify = self._git(repo, rules, ["bundle", "verify", str(bundle)], check=False)
        if verify.returncode != 0:
            if any(s in verify.stderr for s in MISSING_PREREQS):
                missing = re.findall(r"\b[0-9a-f]{40,64}\b", verify.stderr)
                raise Refused(
                    f"The bundle builds on commits GitHub does not have ({', '.join(c[:12] for c in missing[:5])}), "
                    "usually because an origin/* branch in your clone is stale. To fix it, run the gate's fetch, "
                    "delete each origin/* branch GitHub no longer lists (git branch -dr origin/<name>), then "
                    "recreate the bundle with:\n\n" + BUNDLE_HOWTO.format(branch=branch)
                )
            raise Refused(f"The bundle does not verify:\n{_tail(verify.stderr)}")

        self._clear_incoming(repo, rules)
        fetched = self._git(
            repo, rules,
            ["-c", "protocol.file.allow=always", "fetch", "--no-tags", "--no-write-fetch-head", str(bundle),
             f"{ref}:{INCOMING}{branch}"],
            check=False,
        )  # fmt: skip
        if fetched.returncode != 0:
            self._clear_incoming(repo, rules)
            raise Refused(f"git rejected the bundle's contents:\n{_tail(fetched.stderr)}")
        got = self._git(repo, rules, ["rev-parse", "--verify", "--quiet", f"{INCOMING}{branch}"], check=False)
        kind = self._git(repo, rules, ["cat-file", "-t", sha], check=False).stdout.strip()
        if got.stdout.strip() != sha or kind != "commit":
            self._clear_incoming(repo, rules)
            raise Refused(f"{ref} in the bundle must point at a commit; it points at a {kind or 'missing'} object.")
        return sha

    def _stored_file(self, bundle: Path) -> Path:
        """The bundle's real path, if it is a regular file (not a symlink) inside the state dir."""
        if not bundle.is_absolute():
            raise Refused("The bundle path must be absolute.")
        try:
            st = os.lstat(bundle)
        except OSError as e:
            raise Refused(f"Cannot read the bundle at {bundle}: {e.strerror}.") from None
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"The bundle at {bundle} must be a regular file.")
        real = bundle.resolve()
        if not real.is_relative_to(self.state.resolve()):
            raise Refused(f"The bundle at {bundle} is not in the gate's storage.")
        return real

    def _clear_incoming(self, repo: str, rules: Rules) -> None:
        refs = self._git(repo, rules, ["for-each-ref", "--format=%(refname)", INCOMING]).stdout.split()
        if refs:
            self._git(repo, rules, ["update-ref", "--stdin"], stdin="".join(f"delete {r}\n" for r in refs))

    def export_bundle(self, repo: str, out: Path, have: list[str], rules: Rules) -> bool:
        """Write every branch to `out`, leaving out the history below each known `have` commit.

        The haves' parents are excluded, not the haves, so a branch whose tip the agent already has
        still carries its ref. If that would still drop a branch (a tip below a have's parents), the
        bundle is written in full instead. Malformed or unknown `have` entries are skipped. False, and
        no file, only when the mirror has no branches or every branch tip is itself in `have`.
        """
        if len(have) > MAX_HAVE:
            raise Refused(f"have lists {len(have)} SHAs; the gate takes at most {MAX_HAVE}.")
        if not out.is_absolute():
            raise Refused("The output path must be absolute.")
        heads = self._heads(repo, rules)
        if not heads or set(heads.values()) <= set(have):
            return False
        parents = [f"{h}^@" for h in self._known_commits(repo, have, rules)]
        if parents:
            made = self._git(repo, rules, ["bundle", "create", "--quiet", str(out), "--branches", "--not", *parents],
                             check=False)  # fmt: skip
            if made.returncode == 0 and self._bundle_refs(repo, out, rules) == {f"refs/heads/{b}" for b in heads}:
                return True
        self._git(repo, rules, ["bundle", "create", "--quiet", str(out), "--branches"])
        return True

    def _bundle_refs(self, repo: str, bundle: Path, rules: Rules) -> set[str]:
        out = self._git(repo, rules, ["bundle", "list-heads", str(bundle)]).stdout
        return {line.split(" ", 1)[-1] for line in out.splitlines() if line}

    def _known_commits(self, repo: str, have: list[str], rules: Rules) -> list[str]:
        wanted = list(dict.fromkeys(h for h in have if isinstance(h, str) and OID.match(h)))
        if not wanted:
            return []
        out = self._git(
            repo, rules, ["cat-file", "--batch-check=%(objectname) %(objecttype)"], stdin="\n".join(wanted) + "\n"
        ).stdout
        return [parts[0] for parts in (line.split(" ") for line in out.splitlines()) if parts[-1] == "commit"]

    # --- history ------------------------------------------------------------------------

    def is_ancestor(self, repo: str, a: str, b: str, rules: Rules) -> bool:
        proc = self._git(repo, rules, ["merge-base", "--is-ancestor", _oid(a), _oid(b)], check=False)
        if proc.returncode not in (0, 1):
            raise Refused(f"git merge-base failed:\n{_tail(proc.stderr)}")
        return proc.returncode == 0

    def merge_base(self, repo: str, a: str, b: str, rules: Rules) -> str | None:
        proc = self._git(repo, rules, ["merge-base", _oid(a), _oid(b)], check=False)
        if proc.returncode not in (0, 1):
            raise Refused(f"git merge-base failed:\n{_tail(proc.stderr)}")
        return proc.stdout.strip() or None

    def new_commits(self, repo: str, sha: str, base: str | None, rules: Rules) -> list[str]:
        """Commits reachable from sha but not from base (all of them when base is None)."""
        args = ["rev-list", _oid(sha), *(["--not", _oid(base)] if base else [])]
        return self._git(repo, rules, args).stdout.split()

    def touched(
        self, repo: str, sha: str, remote: str | None, default_tip: str | None, patterns, rules: Rules
    ) -> list[str]:
        """Commits of this push whose own changes touch a path matching `patterns`.

        Only commits new to both the branch (`remote`) and the default branch (`default_tip`) are
        diffed: what the default branch already holds is the user's. Merges are diffed
        dense-combined (`--cc`), which hides a merge that keeps one parent's older file, so when no
        commit is named the tree is also compared: a protected path that differs from both the
        default branch's current tip and the branch on GitHub names sha itself. sha is also named
        when its tree holds a symlink at, inside or above a handover path.
        """
        exclude = comparison(remote, default_tip)
        shas = self._git(repo, rules, ["rev-list", _oid(sha), *_not(exclude)]).stdout.split()
        hits = self._commit_touches(repo, shas, patterns, rules)
        if not hits:
            net = self._changed(repo, default_tip or self._empty_tree(repo, rules), sha, patterns, rules)
            if remote:
                net &= self._changed(repo, remote, sha, patterns, rules)
            if net:
                hits = [sha]
        if sha not in hits and any(reaches(p, patterns) for p in self._symlinks(repo, sha, rules)):
            hits.append(sha)
        return hits

    def _changed(self, repo: str, a: str, b: str, patterns, rules: Rules) -> set[str]:
        """The protected paths that differ between two commits (or trees)."""
        out = self._git(
            repo, rules,
            ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", _oid(a), _oid(b)],
        ).stdout  # fmt: skip
        return {p for p in out.split("\0") if p and protected(p, patterns)}

    def _empty_tree(self, repo: str, rules: Rules) -> str:
        return self._git(repo, rules, ["hash-object", "-t", "tree", "--stdin"], stdin="").stdout.strip()

    def _symlinks(self, repo: str, sha: str, rules: Rules) -> list[str]:
        out = self._git(repo, rules, ["ls-tree", "-r", "-z", "--full-tree", _oid(sha)]).stdout
        links = []
        for entry in out.split("\0"):
            meta, _, path = entry.partition("\t")
            if meta.startswith("120000 "):
                links.append(path)
        return links

    def _commit_touches(self, repo: str, shas: list[str], patterns, rules: Rules) -> list[str]:
        if not shas:
            return []
        out = self._git(
            repo, rules,
            ["diff-tree", "--stdin", "-z", "-r", "--no-renames", "--name-only", "--cc", "--root"],
            stdin="\n".join(_oid(s) for s in shas) + "\n",
        ).stdout  # fmt: skip
        tokens = [t for t in out.split("\0") if t]
        wanted = set(shas)
        current, hits = None, []
        for token in tokens:
            if token in wanted:
                current = token
            elif current and current not in hits and protected(token, patterns):
                hits.append(current)
        if not hits and any(t not in wanted and protected(t, patterns) for t in tokens):
            hits.append(shas[0])  # output git framed in a way this parser did not expect: fail closed
        return hits

    # --- handover refs ------------------------------------------------------------------

    def keep(self, repo: str, ref: str, sha: str, rules: Rules) -> None:
        """Pin a refused push's commit under refs/gate/handover/<id> until the user runs it."""
        self._git(repo, rules, ["update-ref", _handover_ref(ref), _oid(sha)])

    def drop(self, repo: str, ref: str, rules: Rules) -> None:
        self._git(repo, rules, ["update-ref", "-d", _handover_ref(ref)])


def protected(path: str, patterns) -> bool:
    """Whether `path` (as git prints it) is under a canonical handover pattern, ignoring case.

    A pattern covers what it names and everything below it, as a gitignore directory rule does:
    `path` or any of its leading parts must `full_match` it. Patterns are anchored at the root.
    """
    parts = path.casefold().split("/")
    prefixes = [PurePosixPath("/".join(parts[:k])) for k in range(1, len(parts) + 1)]
    return any(pattern == "**" or any(p.full_match(pattern) for p in prefixes) for pattern in patterns)


def reaches(path: str, patterns) -> bool:
    """Whether a symlink or gitlink at `path` can lead into a handover path.

    True when `path` is protected itself, or matches a leading part of a pattern: for
    `skills/*/skill.md` that is `skills` or `skills/*`. A pattern starting with `**` is reached by
    every link.
    """
    if protected(path, patterns):
        return True
    p = PurePosixPath(path.casefold())
    for pattern in patterns:
        parts = pattern.split("/")
        if parts[0] == "**" or any(p.full_match("/".join(parts[:k])) for k in range(1, len(parts) + 1)):
            return True
    return False


def comparison(remote: str | None, default_tip: str | None) -> tuple[str, ...]:
    """The commits a push is compared with: the branch on GitHub and the default branch's tip.

    No other branch ever is: agents can push those, so they are no trusted base.
    """
    return tuple(_oid(c) for c in (remote, default_tip) if c)


def _not(exclude: tuple[str, ...]) -> list[str]:
    return ["--not", *exclude] if exclude else []


def _oid(value: str) -> str:
    if not isinstance(value, str) or not OID.match(value) or not value.strip("0"):
        raise Refused(f"{value!r} is not a commit SHA.")
    return value


def _handover_ref(ref: str) -> str:
    if not HANDOVER_REF.match(ref):
        raise Refused(f"{ref!r} is not a refs/gate/handover/<id> ref.")
    return ref


def _tail(text: str) -> str:
    return text.strip()[-MAX_STDERR:]
