"""Execute a bounded recovery plan and prove the recovered workspace."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from checkpoint_materializer import materialize_checkpoint
from checkpoint_store import CheckpointStore
from execution_checkpoint_lattice import ExecutionCheckpointLattice
from execution_receipt import execute_verification_plan, verification_digest
from worktree_snapshot import capture_worktree


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def execute_recovery(
    store: CheckpointStore,
    *,
    current_id: str,
    target_id: str,
    destination: str | Path,
    budget: float,
) -> dict[str, Any]:
    lattice = ExecutionCheckpointLattice(store.list_checkpoints())
    plan = lattice.recovery_plan(current_id, target_id, budget=budget)
    target = store.get_checkpoint(target_id)
    original_proof = store.get_proof(target_id)
    original_receipts = original_proof.get("verification_receipts")
    if not isinstance(original_receipts, list) or not original_receipts:
        raise ValueError("target_verification_receipts_missing")
    verification_plan: list[dict[str, Any]] = []
    for row in original_receipts:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("argv"), list):
            raise ValueError("target_verification_receipt_invalid")
        verification_plan.append({"name": row["name"], "argv": row["argv"]})

    materialization = materialize_checkpoint(store, target_id, destination)
    rerun = execute_verification_plan(verification_plan, cwd=destination)
    failed = [receipt for receipt in rerun if receipt.status != "PASS"]
    if failed:
        raise ValueError("recovered_verification_failed:" + ",".join(receipt.name for receipt in failed))
    recovered_snapshot = capture_worktree(destination)
    if recovered_snapshot.snapshot_digest != target.state_digest:
        raise ValueError("recovered_snapshot_mismatch")

    core = {
        "schema": "glaciereq.execution-checkpoint-recovery.v1",
        "current_id": current_id,
        "target_id": target_id,
        "plan": plan,
        "materialization": materialization,
        "target_state_digest": target.state_digest,
        "recovered_state_digest": recovered_snapshot.snapshot_digest,
        "target_verification_digest": target.verification_digest,
        "rerun_verification_digest": verification_digest(rerun),
        "rerun_verification_receipts": [receipt.as_dict() for receipt in rerun],
        "verified": True,
    }
    return {**core, "recovery_digest": _digest(core)}
