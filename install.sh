#!/bin/sh
# Install a reviewed git-gate commit into /opt/git-gate/releases/<sha> and point `current` at it.
#
# Run this as yourself from your own terminal, never from an agent session. It calls sudo for
# the steps that write /opt/git-gate.
#
# The working tree of ~/git-gate is writable from the sandbox, so do not run its copy of this
# script. First install: extract it from the commit you reviewed, read it, then run it:
#
#   git -C ~/git-gate cat-file blob <sha>:install.sh > ~/git-gate-install.sh
#
# Later installs: run the root-owned copy in the live release, /opt/git-gate/current/install.sh.
#
# Usage:
#   install.sh install <sha> [--repo DIR] [--python PATH]
#   install.sh rollback <sha>
#   install.sh list
#   install.sh post-switch <sha>   (the links and checks after a switch; install and rollback run it)
set -eu

ROOT=${GIT_GATE_ROOT:-/opt/git-gate}
RELEASES=$ROOT/releases
BIN_DIR=${GIT_GATE_BIN_DIR:-/usr/local/bin}
REPO=$HOME/git-gate
PYTHON=$HOME/.pyenv/versions/3.14.4/bin/python3.14
ROOT_ENV="env -i PATH=/usr/bin:/bin LANG=C.UTF-8 PIP_CONFIG_FILE=/dev/null PIP_NO_CACHE_DIR=1"
# Keep a proxy and its CA, if you use one, for pip under sudo. Nothing else crosses over.
for v in HTTPS_PROXY https_proxy NO_PROXY no_proxy PIP_CERT SSL_CERT_FILE; do
    eval "val=\${$v:-}"
    [ -z "$val" ] || ROOT_ENV="$ROOT_ENV $v=$val"
done

die() { echo "install.sh: $*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run as yourself; the script calls sudo where it needs root."

# The dev repo is sandbox-writable, so it is only read through a throwaway GIT_DIR (shadow/: its
# config, hooks, attributes and replace refs are never read) and only once: the commits are
# fetched from it into private.git, which hashes every object it receives. The review and the
# install both come from private.git, so swapping object files in the dev repo after you have
# read the diff changes nothing.
WORK=
git_in() {
    dir=$1; shift
    env -i PATH=/usr/bin:/bin HOME="$WORK" LANG=C.UTF-8 GIT_DIR="$WORK/$dir" \
        GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
        git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.pager=cat \
        -c protocol.file.allow=always -c transfer.fsckObjects=true --no-pager "$@"
}
shadow_git() { git_in shadow "$@"; }
priv_git() { git_in private.git "$@"; }

prepare() {
    [ -d "$REPO/.git" ] && [ ! -L "$REPO/.git" ] || die "$REPO/.git is not a directory."
    WORK=$(mktemp -d)
    mkdir "$WORK/shadow"
    printf '[core]\n\trepositoryformatversion = 0\n\tbare = true\n[uploadpack]\n\tallowAnySHA1InWant = true\n' \
        > "$WORK/shadow/config"
    echo "ref: refs/heads/main" > "$WORK/shadow/HEAD"
    for n in objects refs packed-refs; do ln -s "$REPO/.git/$n" "$WORK/shadow/$n"; done
    git_in private.git init -q --bare "$WORK/private.git"
}

# Copy one commit and everything it needs into private.git, verifying every object's hash.
take() {
    [ "$(shadow_git cat-file -t "$1" 2>/dev/null)" = commit ] || return 1
    priv_git fetch -q --no-tags "$WORK/shadow" "$1" || die "fetching $1 from $REPO failed its integrity check."
    [ "$(priv_git cat-file -t "$1")" = commit ] || die "$1 did not arrive intact."
}

STAGE=
cleanup() {
    [ -z "$WORK" ] || rm -rf "$WORK"
    [ -z "$STAGE" ] || sudo rm -rf "$STAGE"
}
trap cleanup EXIT INT TERM

check_sha() {
    echo "$1" | grep -Eq '^[0-9a-f]{40}$' || die "give the full 40-character commit sha."
}

current_sha() {
    [ -L "$ROOT/current" ] && basename "$(readlink "$ROOT/current")" || true
}

switch_to() {
    [ -d "$RELEASES/$1" ] || die "no release $1 in $RELEASES."
    sudo ln -sfn "releases/$1" "$ROOT/current.new"
    sudo mv -T "$ROOT/current.new" "$ROOT/current"
    echo "current -> releases/$1."
    post_switch_in "$1"
}

# The steps after a switch come from the release now live, not from this script: an upgrade runs
# the old release's install.sh, which cannot know the steps a newer release adds. That release's
# script is root-owned, like the rest of it. A release from before post-switch gets this script's
# own steps instead.
post_switch_in() {
    script=$RELEASES/$1/install.sh
    if [ -f "$script" ] && grep -q '^    post-switch)' "$script"; then
        GIT_GATE_ROOT=$ROOT GIT_GATE_BIN_DIR=$BIN_DIR sh "$script" post-switch "$1"
    else
        link_launchers
    fi
}

cmd_post_switch() {
    check_sha "$1"
    cur=$(current_sha)
    [ "$cur" = "$1" ] || die "$1 is not the live release (current is ${cur:-unset}); post-switch only follows a switch."
    link_launchers
}

# Point $BIN_DIR/gitgatepush and $BIN_DIR/gitgaterules at the live release's launchers, so they
# work in any shell and always run the root-owned copy. Warn when another one earlier on PATH would
# win. A release without a launcher (gitgaterules is newer) leaves that link as it is.
link_launchers() {
    for name in gitgatepush gitgaterules; do
        link=$BIN_DIR/$name
        if [ ! -e "$ROOT/current/bin/$name" ]; then
            echo "This release has no $name; $link is left as it is."
            continue
        fi
        sudo ln -sfn "$ROOT/current/bin/$name" "$link"
        echo "$link -> $ROOT/current/bin/$name"
        found=$(command -v "$name" || true)
        if [ -z "$found" ]; then
            echo "install.sh: warning: $name is not on your PATH; add $BIN_DIR to it." >&2
        elif [ "$found" != "$link" ]; then
            echo "install.sh: warning: running $name will start $found, not $link. Remove or rename it." >&2
        fi
    done
}

cmd_list() {
    cur=$(current_sha)
    [ -d "$RELEASES" ] || { echo "(no releases)"; return; }
    for d in "$RELEASES"/*/; do
        [ -d "$d" ] || continue
        s=$(basename "$d")
        if [ "$s" = "$cur" ]; then echo "* $s"; else echo "  $s"; fi
    done
}

cmd_install() {
    sha=$1
    check_sha "$sha"
    [ ! -e "$RELEASES/$sha" ] || die "$RELEASES/$sha already exists; use rollback to switch to it."
    prepare
    take "$sha" || die "$sha is not a commit in $REPO."

    if priv_git ls-tree -r "$sha" | awk '$1 == "120000" || $1 == "160000" { found = 1 } END { exit !found }'; then
        die "$sha contains symlinks or submodules; refusing to extract it as root."
    fi

    "$PYTHON" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 14))' || die "$PYTHON is not Python 3.14."

    cur=$(current_sha)
    if [ -n "$cur" ] && take "$cur"; then
        base=$cur
        echo "Diff from current release $cur to $sha:"
    else
        base=$(priv_git hash-object -t tree /dev/null)
        echo "No current release in this repo's history; showing every file in $sha:"
    fi
    priv_git diff --stat "$base" "$sha"
    printf 'Review the full diff? [Y/n] '
    read -r ans
    case $ans in [nN]*) ;; *) priv_git diff "$base" "$sha" | ${PAGER:-less} ;; esac
    printf 'Install %s? [y/N] ' "$sha"
    read -r ans
    case $ans in [yY]*) ;; *) die "not installed." ;; esac

    sudo install -d -o root -g root -m 0755 "$ROOT" "$RELEASES"
    STAGE=$RELEASES/.new-$sha
    sudo rm -rf "$STAGE"
    sudo install -d -o root -g root -m 0755 "$STAGE"
    priv_git archive --format=tar "$sha" | sudo tar -x -C "$STAGE" --no-same-owner --no-same-permissions
    sudo $ROOT_ENV "$PYTHON" -I -m venv "$STAGE/.venv"
    sudo $ROOT_ENV "$STAGE/.venv/bin/python" -I -m pip install --quiet --disable-pip-version-check \
        --require-hashes --only-binary :all: --no-deps -r "$STAGE/requirements.lock"
    echo "$RELEASES/$sha/src" | sudo tee "$STAGE/.venv/lib/python3.14/site-packages/git_gate.pth" > /dev/null
    sudo chown -R root:root "$STAGE"
    sudo chmod -R go-w "$STAGE"
    sudo mv -T "$STAGE" "$RELEASES/$sha"
    STAGE=

    "$RELEASES/$sha/.venv/bin/python" -I -c 'import git_gate.server' || die "release does not import; current unchanged."
    switch_to "$sha"
}

action=${1:-}
[ $# -gt 0 ] && shift
case $action in
    install)
        [ $# -ge 1 ] || die "usage: install.sh install <sha> [--repo DIR] [--python PATH]"
        sha=$1; shift
        while [ $# -gt 0 ]; do
            case $1 in
                --repo) REPO=$2; shift 2 ;;
                --python) PYTHON=$2; shift 2 ;;
                *) die "unknown option $1" ;;
            esac
        done
        cmd_install "$sha" ;;
    rollback)
        [ $# -eq 1 ] || die "usage: install.sh rollback <sha>"
        check_sha "$1"
        switch_to "$1" ;;
    list) cmd_list ;;
    post-switch)
        [ $# -eq 1 ] || die "usage: install.sh post-switch <sha>"
        cmd_post_switch "$1" ;;
    *) die "usage: install.sh install <sha> | rollback <sha> | list | post-switch <sha>" ;;
esac
