from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from checkpoint_materializer import materialize_checkpoint
from checkpoint_store import CheckpointStore, capture_checkpoint_to_store
from recovery_executor import execute_recovery
from worktree_snapshot import capture_worktree


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "crystal@example.invalid")
    git(repo, "config", "user.name", "Crystallization Test")
    (repo / "engine.py").write_text("VALUE = 'root'\n", encoding="utf-8")
    (repo / "verify.py").write_text(
        "from pathlib import Path\n"
        "text = Path('engine.py').read_text()\n"
        "assert 'VALUE' in text\n"
        "print('verified')\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "root")
    return repo


def verify_plan() -> list[dict]:
    return [{"name": "runtime", "argv": [sys.executable, "verify.py"]}]


def test_store_persists_and_reopens_verified_checkpoint_with_integrity(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store_path = tmp_path / "store"
    store = CheckpointStore(store_path)
    checkpoint, proof = capture_checkpoint_to_store(
        store,
        checkpoint_id="root",
        subgoal="root state verified",
        parent_ids=[],
        repo=repo,
        verification_commands=verify_plan(),
        recovery_cost_units=0.5,
    )
    assert checkpoint.artifacts["workspace_archive"]
    assert proof["verification_receipts"][0]["status"] == "PASS"

    reopened = CheckpointStore(store_path)
    loaded = reopened.get_checkpoint("root")
    assert loaded.as_dict() == checkpoint.as_dict()
    report = reopened.integrity_report()
    assert report["status"] == "PASS"
    assert report["checkpoint_count"] == 1
    assert len(report["integrity_digest"]) == 64


def test_materialization_is_self_contained_after_original_repository_is_removed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = CheckpointStore(tmp_path / "store")
    checkpoint, _ = capture_checkpoint_to_store(
        store,
        checkpoint_id="root",
        subgoal="root",
        parent_ids=[],
        repo=repo,
        verification_commands=verify_plan(),
    )
    expected = checkpoint.state_digest
    shutil.rmtree(repo)

    destination = tmp_path / "materialized"
    receipt = materialize_checkpoint(store, "root", destination)
    assert receipt["verified"] is True
    assert receipt["materialized_snapshot_digest"] == expected
    assert capture_worktree(destination).snapshot_digest == expected
    assert len(receipt["materialization_digest"]) == 64


def test_recovery_executes_plan_materializes_target_and_reruns_verification(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = CheckpointStore(tmp_path / "store")
    root, _ = capture_checkpoint_to_store(
        store,
        checkpoint_id="root",
        subgoal="root",
        parent_ids=[],
        repo=repo,
        verification_commands=verify_plan(),
        recovery_cost_units=0.5,
    )

    (repo / "engine.py").write_text("VALUE = 'child'\n", encoding="utf-8")
    (repo / "child.txt").write_text("later state\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "child")
    child, _ = capture_checkpoint_to_store(
        store,
        checkpoint_id="child",
        subgoal="child",
        parent_ids=["root"],
        repo=repo,
        verification_commands=verify_plan(),
        recovery_cost_units=1.0,
    )
    assert child.state_digest != root.state_digest

    destination = tmp_path / "recovered"
    receipt = execute_recovery(
        store,
        current_id="child",
        target_id="root",
        destination=destination,
        budget=1.0,
    )
    assert receipt["verified"] is True
    assert receipt["plan"]["rollback_path"] == ["child", "root"]
    assert receipt["recovered_state_digest"] == root.state_digest
    assert capture_worktree(destination).snapshot_digest == root.state_digest
    assert "VALUE = 'root'" in (destination / "engine.py").read_text(encoding="utf-8")
    assert not (destination / "child.txt").exists()
    assert receipt["rerun_verification_receipts"][0]["status"] == "PASS"
    assert len(receipt["recovery_digest"]) == 64


def test_recovery_refuses_insufficient_budget_before_materialization(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = CheckpointStore(tmp_path / "store")
    capture_checkpoint_to_store(
        store,
        checkpoint_id="root",
        subgoal="root",
        parent_ids=[],
        repo=repo,
        verification_commands=verify_plan(),
    )
    (repo / "engine.py").write_text("VALUE = 'child'\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "child")
    capture_checkpoint_to_store(
        store,
        checkpoint_id="child",
        subgoal="child",
        parent_ids=["root"],
        repo=repo,
        verification_commands=verify_plan(),
        recovery_cost_units=2.0,
    )
    destination = tmp_path / "should-not-exist"
    with pytest.raises(ValueError, match="recovery_budget_exceeded"):
        execute_recovery(store, current_id="child", target_id="root", destination=destination, budget=1.0)
    assert not destination.exists()


def test_store_refuses_child_when_parent_is_not_persisted(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = CheckpointStore(tmp_path / "store")
    with pytest.raises(ValueError, match="parent_missing:missing"):
        capture_checkpoint_to_store(
            store,
            checkpoint_id="child",
            subgoal="child",
            parent_ids=["missing"],
            repo=repo,
            verification_commands=verify_plan(),
        )


def test_checkpoint_id_is_immutable_but_exact_replay_is_idempotent(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = CheckpointStore(tmp_path / "store")
    checkpoint, proof = capture_checkpoint_to_store(
        store,
        checkpoint_id="root",
        subgoal="root",
        parent_ids=[],
        repo=repo,
        verification_commands=verify_plan(),
    )
    archive = store.get_archive("root")
    first = store.save(checkpoint, proof, archive)
    second = store.save(checkpoint, proof, archive)
    assert first == second

    (repo / "engine.py").write_text("VALUE = 'different'\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "different")
    with pytest.raises(ValueError, match="checkpoint_id_conflict"):
        capture_checkpoint_to_store(
            store,
            checkpoint_id="root",
            subgoal="root",
            parent_ids=[],
            repo=repo,
            verification_commands=verify_plan(),
        )
