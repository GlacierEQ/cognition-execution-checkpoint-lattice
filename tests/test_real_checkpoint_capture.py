from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from checkpoint_capture import capture_verified_checkpoint
from execution_checkpoint_lattice import CheckpointStatus
from execution_receipt import execute_command
from worktree_snapshot import capture_worktree, verify_worktree


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "crystal@example.invalid")
    git(repo, "config", "user.name", "Crystallization Test")
    (repo / "engine.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo


def test_worktree_snapshot_changes_when_real_file_changes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    first = capture_worktree(repo)
    (repo / "engine.py").write_text("VALUE = 2\n", encoding="utf-8")
    second = capture_worktree(repo)
    assert first.snapshot_digest != second.snapshot_digest
    assert verify_worktree(repo, second.snapshot_digest)
    assert not verify_worktree(repo, first.snapshot_digest)


def test_capture_verified_checkpoint_binds_real_state_and_command_receipt(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    checkpoint, proof = capture_verified_checkpoint(
        checkpoint_id="root",
        subgoal="environment verified",
        parent_ids=[],
        repo=repo,
        verification_commands=[
            {"name": "unit", "argv": [sys.executable, "-c", "print('verified')"]}
        ],
        reversible=True,
        recovery_cost_units=0.5,
    )
    assert checkpoint.status is CheckpointStatus.VERIFIED
    assert len(checkpoint.state_digest) == 64
    assert len(checkpoint.verification_digest or "") == 64
    assert checkpoint.artifacts["worktree_snapshot"] == checkpoint.state_digest
    assert proof["verification_receipts"][0]["status"] == "PASS"
    assert "verified" in proof["verification_receipts"][0]["stdout_tail"]


def test_capture_refuses_failed_verification_instead_of_minting_verified_checkpoint(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    with pytest.raises(ValueError, match="checkpoint_verification_failed:unit"):
        capture_verified_checkpoint(
            checkpoint_id="root",
            subgoal="environment verified",
            parent_ids=[],
            repo=repo,
            verification_commands=[
                {"name": "unit", "argv": [sys.executable, "-c", "raise SystemExit(7)"]}
            ],
        )


def test_command_receipt_binds_failure_output_and_exit_code(tmp_path: Path) -> None:
    receipt = execute_command(
        "integration",
        [sys.executable, "-c", "import sys; print('broken'); sys.exit(9)"],
        cwd=tmp_path,
    )
    assert receipt.status == "FAIL"
    assert receipt.returncode == 9
    assert "broken" in receipt.stdout_tail
    assert len(receipt.receipt_digest) == 64


def test_snapshot_includes_untracked_files_because_they_affect_recoverability(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    first = capture_worktree(repo)
    (repo / "generated.txt").write_text("material state\n", encoding="utf-8")
    second = capture_worktree(repo)
    assert first.snapshot_digest != second.snapshot_digest
    assert any(row["path"] == "generated.txt" for row in second.files)
