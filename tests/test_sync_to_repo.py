"""Tests for tools/sync_to_repo.py against a throwaway local git repo — no
network, no real remote, no push (push/PR stay opt-in by design)."""

import subprocess
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import sync_to_repo as sync  # noqa: E402


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-q", "-m", "initial"], repo)
    return repo


def _make_generated(tmp_path) -> Path:
    gen = tmp_path / "generated"
    gen.mkdir()
    (gen / "output_1.sqlx").write_text("SELECT 1\n")
    return gen


def test_is_git_repo_true_and_false(tmp_path):
    repo = _make_repo(tmp_path)
    assert sync.is_git_repo(repo)
    assert not sync.is_git_repo(tmp_path / "not_a_repo")


def test_sync_files_copies_without_deleting_existing(tmp_path):
    gen = _make_generated(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "hand_written.sqlx").write_text("-- keep me\n")

    copied = sync.sync_files(gen, dest)

    assert copied == ["output_1.sqlx"]
    assert (dest / "hand_written.sqlx").exists()
    assert (dest / "output_1.sqlx").read_text() == "SELECT 1\n"


def test_full_sync_creates_branch_and_local_commit_without_pushing(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    gen = _make_generated(tmp_path)

    sys.argv = [
        "sync_to_repo.py", str(gen), "--repo", str(repo),
        "--dest", "definitions", "--branch", "tfl-sync/test",
    ]
    sync.main()

    branches = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert "tfl-sync/test" in branches

    log = subprocess.run(["git", "log", "--oneline", "tfl-sync/test"], cwd=repo, capture_output=True, text=True).stdout
    assert "Sync generated SQL" in log

    assert (repo / "definitions" / "output_1.sqlx").exists()
    assert "Not pushed" in capsys.readouterr().out


def test_refuses_to_sync_into_dirty_repo(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "uncommitted.txt").write_text("oops\n")
    gen = _make_generated(tmp_path)

    sys.argv = [
        "sync_to_repo.py", str(gen), "--repo", str(repo), "--dest", "definitions",
    ]
    try:
        sync.main()
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert "uncommitted changes" in str(exc)
