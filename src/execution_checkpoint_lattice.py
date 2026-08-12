"""Execution Checkpoint Lattice — verified long-horizon execution state graph.

The lattice persists content-addressed checkpoints keyed to task subgoals.  A
checkpoint can branch from one or more verified parents, compare divergent
execution branches, and produce bounded recovery plans without replaying an
entire trajectory.

The implementation is independent and does not imply Cognition affiliation or
proprietary system access.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _digest(obj: object) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finite(value: float, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{label}_not_numeric")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label}_not_finite")
    return value


def _sha(value: str, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label}_invalid_sha256")
    return value


def _id(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}_missing")
    value = value.strip()
    if len(value) > 160:
        raise ValueError(f"{label}_too_long")
    return value


class Decision(str, Enum):
    ALLOW = "ALLOW"
    REFUSE = "REFUSE"


class CheckpointStatus(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    PENDING = "PENDING"


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    subgoal: str
    parent_ids: tuple[str, ...]
    state_digest: str
    verification_digest: str | None
    status: CheckpointStatus
    reversible: bool
    recovery_cost_units: float = 1.0
    artifacts: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Checkpoint":
        if not isinstance(raw, Mapping):
            raise ValueError("checkpoint_must_be_object")
        checkpoint_id = _id(raw.get("checkpoint_id"), "checkpoint_id")
        subgoal = _id(raw.get("subgoal"), "subgoal")
        parents_raw = raw.get("parent_ids") or []
        if not isinstance(parents_raw, list) or not all(isinstance(x, str) and x.strip() for x in parents_raw):
            raise ValueError("parent_ids_invalid")
        parent_ids = tuple(x.strip() for x in parents_raw)
        if len(parent_ids) != len(set(parent_ids)):
            raise ValueError("duplicate_parent_id")
        if checkpoint_id in parent_ids:
            raise ValueError("checkpoint_self_parent")
        state_digest = _sha(raw.get("state_digest"), "state_digest")
        verification_raw = raw.get("verification_digest")
        verification_digest = None if verification_raw is None else _sha(verification_raw, "verification_digest")
        try:
            status = CheckpointStatus(raw.get("status"))
        except Exception as exc:
            raise ValueError("checkpoint_status_invalid") from exc
        reversible = raw.get("reversible")
        if not isinstance(reversible, bool):
            raise ValueError("reversible_invalid")
        cost = _finite(raw.get("recovery_cost_units", 1.0), "recovery_cost_units")
        if cost < 0:
            raise ValueError("recovery_cost_negative")
        artifacts_raw = raw.get("artifacts") or {}
        if not isinstance(artifacts_raw, Mapping):
            raise ValueError("artifacts_invalid")
        artifacts: dict[str, str] = {}
        for name, digest in artifacts_raw.items():
            artifacts[_id(name, "artifact_name")] = _sha(digest, "artifact_digest")
        if status is CheckpointStatus.VERIFIED and verification_digest is None:
            raise ValueError("verified_checkpoint_missing_verification_digest")
        return cls(
            checkpoint_id=checkpoint_id,
            subgoal=subgoal,
            parent_ids=parent_ids,
            state_digest=state_digest,
            verification_digest=verification_digest,
            status=status,
            reversible=reversible,
            recovery_cost_units=cost,
            artifacts=artifacts,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "subgoal": self.subgoal,
            "parent_ids": list(self.parent_ids),
            "state_digest": self.state_digest,
            "verification_digest": self.verification_digest,
            "status": self.status.value,
            "reversible": self.reversible,
            "recovery_cost_units": self.recovery_cost_units,
            "artifacts": dict(sorted(self.artifacts.items())),
        }


@dataclass(frozen=True)
class ExecutionCheckpointLatticeRequest:
    subject_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    budget: float = 0.0
    grant_id: str | None = None
    not_after: float | None = None


@dataclass(frozen=True)
class ExecutionCheckpointLatticeReceipt:
    decision: Decision
    reasons: tuple[str, ...]
    digest: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reasons": list(self.reasons),
            "digest": self.digest,
            "metrics": self.metrics,
        }


class ExecutionCheckpointLattice:
    """Content-addressed DAG of verified intermediate execution states."""

    def __init__(self, checkpoints: Iterable[Checkpoint] = ()) -> None:
        self._checkpoints: dict[str, Checkpoint] = {}
        pending = list(checkpoints)
        while pending:
            progressed = False
            remaining: list[Checkpoint] = []
            for checkpoint in pending:
                if all(parent in self._checkpoints for parent in checkpoint.parent_ids):
                    self.add_checkpoint(checkpoint)
                    progressed = True
                else:
                    remaining.append(checkpoint)
            if not progressed:
                missing = sorted({p for c in remaining for p in c.parent_ids if p not in self._checkpoints})
                raise ValueError("unresolved_parent_or_cycle:" + ",".join(missing))
            pending = remaining

    @property
    def checkpoints(self) -> tuple[Checkpoint, ...]:
        return tuple(self._checkpoints[key] for key in sorted(self._checkpoints))

    def add_checkpoint(self, checkpoint: Checkpoint) -> str:
        if checkpoint.checkpoint_id in self._checkpoints:
            raise ValueError("duplicate_checkpoint_id")
        for parent_id in checkpoint.parent_ids:
            parent = self._checkpoints.get(parent_id)
            if parent is None:
                raise ValueError(f"parent_missing:{parent_id}")
            if parent.status is not CheckpointStatus.VERIFIED:
                raise ValueError(f"parent_not_verified:{parent_id}")
        canonical = checkpoint.as_dict()
        checkpoint_digest = _digest(canonical)
        self._checkpoints[checkpoint.checkpoint_id] = checkpoint
        return checkpoint_digest

    def _ancestors_with_distance(self, checkpoint_id: str) -> dict[str, int]:
        if checkpoint_id not in self._checkpoints:
            raise ValueError("checkpoint_unknown")
        distances = {checkpoint_id: 0}
        queue: deque[str] = deque([checkpoint_id])
        while queue:
            current = queue.popleft()
            distance = distances[current]
            for parent in self._checkpoints[current].parent_ids:
                if parent not in distances or distance + 1 < distances[parent]:
                    distances[parent] = distance + 1
                    queue.append(parent)
        return distances

    def compare(self, left_id: str, right_id: str) -> dict[str, Any]:
        left = self._ancestors_with_distance(left_id)
        right = self._ancestors_with_distance(right_id)
        common = set(left).intersection(right)
        if not common:
            raise ValueError("branches_have_no_common_checkpoint")
        common_id = min(common, key=lambda cid: (left[cid] + right[cid], max(left[cid], right[cid]), cid))
        left_unique = sorted(set(left) - set(right))
        right_unique = sorted(set(right) - set(left))
        return {
            "left_id": left_id,
            "right_id": right_id,
            "common_checkpoint_id": common_id,
            "left_distance": left[common_id],
            "right_distance": right[common_id],
            "left_unique_checkpoints": left_unique,
            "right_unique_checkpoints": right_unique,
            "state_equal": self._checkpoints[left_id].state_digest == self._checkpoints[right_id].state_digest,
            "comparison_digest": _digest(
                {
                    "left": self._checkpoints[left_id].as_dict(),
                    "right": self._checkpoints[right_id].as_dict(),
                    "common": common_id,
                }
            ),
        }

    def _path_to_ancestor(self, current_id: str, target_id: str) -> list[str] | None:
        if current_id not in self._checkpoints or target_id not in self._checkpoints:
            raise ValueError("checkpoint_unknown")
        queue: deque[tuple[str, list[str]]] = deque([(current_id, [current_id])])
        visited = {current_id}
        while queue:
            node, path = queue.popleft()
            if node == target_id:
                return path
            for parent in sorted(self._checkpoints[node].parent_ids):
                if parent not in visited:
                    visited.add(parent)
                    queue.append((parent, path + [parent]))
        return None

    def recovery_plan(self, current_id: str, target_id: str, *, budget: float) -> dict[str, Any]:
        budget = _finite(budget, "budget")
        if budget < 0:
            raise ValueError("budget_negative")
        path = self._path_to_ancestor(current_id, target_id)
        if path is None:
            raise ValueError("recovery_target_not_ancestor")
        target = self._checkpoints[target_id]
        if target.status is not CheckpointStatus.VERIFIED:
            raise ValueError("recovery_target_not_verified")
        if not target.reversible:
            raise ValueError("recovery_target_not_reversible")
        traversed = [self._checkpoints[node] for node in path[:-1]]
        irreversible = [cp.checkpoint_id for cp in traversed if not cp.reversible]
        if irreversible:
            raise ValueError("recovery_crosses_irreversible_checkpoint:" + ",".join(irreversible))
        cost = sum(cp.recovery_cost_units for cp in traversed)
        if cost > budget:
            raise ValueError("recovery_budget_exceeded")
        return {
            "current_id": current_id,
            "target_id": target_id,
            "rollback_path": path,
            "steps": max(0, len(path) - 1),
            "recovery_cost_units": round(cost, 12),
            "target_state_digest": target.state_digest,
            "target_verification_digest": target.verification_digest,
            "plan_digest": _digest({"path": path, "cost": cost, "target": target.as_dict()}),
        }

    def snapshot(self) -> dict[str, Any]:
        rows = [checkpoint.as_dict() for checkpoint in self.checkpoints]
        return {
            "schema": "glaciereq.execution-checkpoint-lattice.v1",
            "checkpoints": rows,
            "lattice_digest": _digest(rows),
        }

    @classmethod
    def from_records(cls, rows: Any) -> "ExecutionCheckpointLattice":
        if not isinstance(rows, list):
            raise ValueError("checkpoints_must_be_list")
        checkpoints = [Checkpoint.from_dict(row) for row in rows]
        ids = [checkpoint.checkpoint_id for checkpoint in checkpoints]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_checkpoint_id")
        return cls(checkpoints)

    def evaluate(self, req: ExecutionCheckpointLatticeRequest) -> ExecutionCheckpointLatticeReceipt:
        reasons: list[str] = []
        if not isinstance(req.subject_id, str) or not req.subject_id.strip():
            reasons.append("subject_id_missing")
        if not isinstance(req.payload, dict):
            reasons.append("payload_invalid")
            payload: dict[str, Any] = {}
        else:
            payload = req.payload

        try:
            lattice = self.from_records(payload.get("checkpoints", []))
            operation = payload.get("operation")
            if not isinstance(operation, dict):
                raise ValueError("operation_missing")
            kind = operation.get("kind")
            result: dict[str, Any]
            if kind == "verify":
                result = lattice.snapshot()
            elif kind == "compare":
                result = lattice.compare(_id(operation.get("left_id"), "left_id"), _id(operation.get("right_id"), "right_id"))
            elif kind == "recover":
                result = lattice.recovery_plan(
                    _id(operation.get("current_id"), "current_id"),
                    _id(operation.get("target_id"), "target_id"),
                    budget=req.budget,
                )
            elif kind == "advance":
                candidate = Checkpoint.from_dict(operation.get("checkpoint"))
                checkpoint_digest = lattice.add_checkpoint(candidate)
                result = {
                    "checkpoint_id": candidate.checkpoint_id,
                    "checkpoint_digest": checkpoint_digest,
                    "snapshot": lattice.snapshot(),
                }
            else:
                raise ValueError("operation_kind_invalid")
        except ValueError as exc:
            reasons.append(str(exc))
            body = {"subject_id": req.subject_id, "decision": Decision.REFUSE.value, "reasons": reasons}
            return ExecutionCheckpointLatticeReceipt(
                decision=Decision.REFUSE,
                reasons=tuple(dict.fromkeys(reasons)),
                digest=_digest(body),
                metrics={"operation_ok": False, "checkpoint_count": 0},
            )

        if reasons:
            decision = Decision.REFUSE
        else:
            decision = Decision.ALLOW
        body = {
            "subject_id": req.subject_id,
            "operation": operation,
            "lattice_digest": lattice.snapshot()["lattice_digest"],
            "result": result,
            "decision": decision.value,
            "grant_id": req.grant_id,
        }
        return ExecutionCheckpointLatticeReceipt(
            decision=decision,
            reasons=tuple(reasons or ["checkpoint_operation_verified"]),
            digest=_digest(body),
            metrics={
                "operation_ok": decision is Decision.ALLOW,
                "operation_kind": operation.get("kind"),
                "checkpoint_count": len(lattice.checkpoints),
                "lattice_digest": lattice.snapshot()["lattice_digest"],
                "result": result,
            },
        )


Mechanism = ExecutionCheckpointLattice
