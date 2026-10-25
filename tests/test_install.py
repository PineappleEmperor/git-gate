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
