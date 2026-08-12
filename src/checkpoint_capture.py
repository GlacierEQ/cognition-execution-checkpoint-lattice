"""Capture a verified checkpoint from an actual worktree and command plan."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from execution_checkpoint_lattice import Checkpoint, CheckpointStatus
from execution_receipt import execute_verification_plan, verification_digest
from worktree_snapshot import capture_worktree


def capture_verified_checkpoint(
    *,
    checkpoint_id: str,
    subgoal: str,
    parent_ids: Iterable[str],
    repo: str | Path,
    verification_commands: Iterable[Mapping[str, Any]],
    reversible: bool = True,
    recovery_cost_units: float = 1.0,
) -> tuple[Checkpoint, dict[str, Any]]:
    root = Path(repo).resolve()
    snapshot = capture_worktree(root)
    receipts = execute_verification_plan(verification_commands, cwd=root)
    failed = [receipt for receipt in receipts if receipt.status != "PASS"]
    if failed:
        names = ",".join(receipt.name for receipt in failed)
        raise ValueError(f"checkpoint_verification_failed:{names}")
    verify_digest = verification_digest(receipts)
    checkpoint = Checkpoint(
        checkpoint_id=checkpoint_id,
        subgoal=subgoal,
        parent_ids=tuple(parent_ids),
        state_digest=snapshot.snapshot_digest,
        verification_digest=verify_digest,
        status=CheckpointStatus.VERIFIED,
        reversible=reversible,
        recovery_cost_units=recovery_cost_units,
        artifacts={
            "worktree_snapshot": snapshot.snapshot_digest,
            "verification_plan": verify_digest,
        },
    )
    proof = {
        "schema": "glaciereq.execution-checkpoint-capture.v1",
        "checkpoint": checkpoint.as_dict(),
        "worktree_snapshot": snapshot.as_dict(),
        "verification_receipts": [receipt.as_dict() for receipt in receipts],
    }
    return checkpoint, proof
