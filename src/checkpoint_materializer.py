"""Materialize a stored verified checkpoint and prove exact state identity."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from checkpoint_store import CheckpointStore
from worktree_snapshot import capture_worktree
from workspace_archive import materialize_workspace


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def materialize_checkpoint(
    store: CheckpointStore,
    checkpoint_id: str,
    destination: str | Path,
) -> dict[str, Any]:
    checkpoint = store.get_checkpoint(checkpoint_id)
    archive = store.get_archive(checkpoint_id)
    result = materialize_workspace(archive, destination)
    snapshot = capture_worktree(destination)
    if snapshot.snapshot_digest != checkpoint.state_digest:
        raise ValueError("materialized_checkpoint_state_mismatch")
    core = {
        "schema": "glaciereq.checkpoint-materialization.v1",
        "checkpoint_id": checkpoint_id,
        "checkpoint_state_digest": checkpoint.state_digest,
        "archive_digest": archive.archive_digest,
        "materialized_snapshot_digest": snapshot.snapshot_digest,
        "head_sha": snapshot.head_sha,
        "destination": str(Path(destination).resolve()),
        "verified": True,
        "workspace_result": result,
    }
    return {**core, "materialization_digest": _digest(core)}
