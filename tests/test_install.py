"""install.sh's switch steps, run against a scratch root with a stand-in sudo."""

import os
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"
NEW = "a" * 40
OLD = "b" * 40


@pytest.fixture
def root(tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    (fake / "sudo").chmod(0o755)
    (tmp_path / "bin").mkdir()
    return tmp_path


def release(root: Path, sha: str, launchers=("gitgatepush", "gitgaterules"), script: str | None = None) -> Path:
    path = root / "opt/releases" / sha
    (path / "bin").mkdir(parents=True)
    for name in launchers:
        (path / "bin" / name).write_text("#!/bin/sh\n")
        (path / "bin" / name).chmod(0o755)
    if script is not None:
        (path / "install.sh").write_text(script)
    return path


def install(root: Path, *args: str, on_path: bool = True) -> subprocess.CompletedProcess:
    path = [str(root / "fakebin"), *([str(root / "bin")] if on_path else []), "/usr/bin", "/bin"]
    env = {
        "PATH": os.pathsep.join(path), "HOME": str(root), "GIT_GATE_ROOT": str(root / "opt"),
        "GIT_GATE_BIN_DIR": str(root / "bin"),
    }  # fmt: skip
    return subprocess.run(["sh", str(INSTALL), *args], env=env, capture_output=True, text=True, check=False)


def test_both_launchers_are_linked_and_checked(root):
    release(root, NEW)
    proc = install(root, "rollback", NEW)
    assert proc.returncode == 0, proc.stderr
    for name in ("gitgatepush", "gitgaterules"):
        link = root / "bin" / name
        assert os.readlink(link) == str(root / "opt/current/bin" / name)
        assert link.resolve() == root / "opt/releases" / NEW / "bin" / name
    assert "warning" not in proc.stderr


def test_a_launcher_off_path_is_warned_about(root):
    release(root, NEW)
    proc = install(root, "rollback", NEW, on_path=False)
    assert proc.returncode == 0
    assert "gitgatepush is not on your PATH" in proc.stderr
    assert "gitgaterules is not on your PATH" in proc.stderr


def test_a_launcher_earlier_on_path_is_warned_about(root):
    release(root, NEW)
    shadow = root / "fakebin/gitgaterules"
    shadow.write_text("#!/bin/sh\n")
    shadow.chmod(0o755)
    proc = install(root, "rollback", NEW)
    assert f"running gitgaterules will start {shadow}" in proc.stderr


def test_a_release_without_gitgaterules_leaves_its_link_alone(root):
    release(root, OLD, launchers=("gitgatepush",))
    proc = install(root, "rollback", OLD)
    assert proc.returncode == 0, proc.stderr
    assert (root / "bin/gitgatepush").is_symlink()
    assert not os.path.lexists(root / "bin/gitgaterules")
    assert "gitgaterules" in proc.stdout and "has no" in proc.stdout


# --- the steps after the switch are the new release's own -----------------------------------------
# An upgrade runs the old release's install.sh; the links and checks a newer release adds must still run.

STUB = """#!/bin/sh
case $1 in
    post-switch) echo "post-switch by the new release: $2 root=$GIT_GATE_ROOT bin=$GIT_GATE_BIN_DIR" ;;
    *) exit 9 ;;
esac
"""


def test_the_switch_hands_its_last_steps_to_the_new_release(root):
    release(root, NEW, script=STUB)
    proc = install(root, "rollback", NEW)
    assert proc.returncode == 0, proc.stderr
    assert f"post-switch by the new release: {NEW} root={root / 'opt'} bin={root / 'bin'}" in proc.stdout
    assert not os.path.lexists(root / "bin/gitgatepush")  # the new release's script did the linking, not this one


def test_a_release_whose_script_predates_post_switch_gets_this_scripts_steps(root):
    release(root, OLD, script="#!/bin/sh\necho old script ran\nexit 9\n")
    proc = install(root, "rollback", OLD)
    assert proc.returncode == 0, proc.stderr
    assert "old script ran" not in proc.stdout
    assert (root / "bin/gitgatepush").is_symlink() and (root / "bin/gitgaterules").is_symlink()


def test_this_script_as_the_new_release_links_everything(root):
    release(root, NEW, script=INSTALL.read_text())
    proc = install(root, "rollback", NEW)
    assert proc.returncode == 0, proc.stderr
    assert (root / "bin/gitgatepush").is_symlink() and (root / "bin/gitgaterules").is_symlink()


def test_post_switch_runs_only_for_the_live_release(root):
    release(root, NEW)
    release(root, OLD)
    (root / "opt/current").symlink_to(f"releases/{NEW}")
    proc = install(root, "post-switch", OLD)
    assert proc.returncode == 1 and "is not the live release" in proc.stderr
    assert not os.path.lexists(root / "bin/gitgaterules")
    proc = install(root, "post-switch", NEW)
    assert proc.returncode == 0, proc.stderr
    assert (root / "bin/gitgaterules").is_symlink()
    assert install(root, "post-switch", "nope").returncode == 1
