"""Execution Checkpoint Lattice.

A deterministic, content-addressed checkpoint DAG for long-horizon software work.
It verifies intermediate states, compares divergent branches, plans bounded
rollback to verified ancestors, and admits new checkpoints only when their
parents are already verified.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _digest(obj: object) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Decision(str, Enum):
    ALLOW = "ALLOW"
    REFUSE = "REFUSE"


@dataclass(frozen=True)
class ExecutionCheckpointLatticeRequest:
    subject_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    budget: float = 1.0
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


class LatticeError(ValueError):
    pass


class ExecutionCheckpointLattice:
    """Verify, compare, advance, and recover a content-addressed checkpoint DAG."""

    MIN_BUDGET = 0.0
    VALID_STATUSES = frozenset({"VERIFIED", "PENDING", "FAILED", "QUARANTINED"})

    @staticmethod
    def _finite_nonnegative(value: Any, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LatticeError(f"{label}_invalid")
        value = float(value)
        if not math.isfinite(value):
            raise LatticeError(f"{label}_not_finite")
        if value < 0:
            raise LatticeError(f"{label}_negative")
        return value

    @classmethod
    def _normalize_checkpoint(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise LatticeError("checkpoint_not_object")
        checkpoint_id = str(raw.get("checkpoint_id", "")).strip()
        subgoal = str(raw.get("subgoal", "")).strip()
        if not checkpoint_id:
            raise LatticeError("checkpoint_id_missing")
        if not subgoal:
            raise LatticeError("subgoal_missing")
        parents = raw.get("parent_ids", [])
        if not isinstance(parents, list) or any(not str(p).strip() for p in parents):
            raise LatticeError("parent_ids_invalid")
        parent_ids = tuple(dict.fromkeys(str(p).strip() for p in parents))
        if checkpoint_id in parent_ids:
            raise LatticeError("checkpoint_self_parent")

        state_digest = str(raw.get("state_digest", "")).strip()
        if not SHA256_RE.fullmatch(state_digest):
            raise LatticeError("state_digest_invalid_sha256")

        status = str(raw.get("status", "VERIFIED")).strip().upper()
        if status not in cls.VALID_STATUSES:
            raise LatticeError("checkpoint_status_invalid")
        verification_digest = raw.get("verification_digest")
        if status == "VERIFIED":
            verification_digest = str(verification_digest or "").strip()
            if not SHA256_RE.fullmatch(verification_digest):
                raise LatticeError("verification_digest_invalid_sha256")
        elif verification_digest is not None:
            verification_digest = str(verification_digest).strip()
            if verification_digest and not SHA256_RE.fullmatch(verification_digest):
                raise LatticeError("verification_digest_invalid_sha256")

        artifacts = raw.get("artifacts", {})
        if not isinstance(artifacts, dict):
            raise LatticeError("artifacts_not_object")
        normalized_artifacts: dict[str, str] = {}
        for key, value in sorted(artifacts.items()):
            name = str(key).strip()
            digest = str(value).strip()
            if not name or not SHA256_RE.fullmatch(digest):
                raise LatticeError("artifact_digest_invalid")
            normalized_artifacts[name] = digest

        return {
            "checkpoint_id": checkpoint_id,
            "subgoal": subgoal,
            "parent_ids": list(parent_ids),
            "state_digest": state_digest,
            "verification_digest": verification_digest,
            "status": status,
            "reversible": bool(raw.get("reversible", True)),
            "recovery_cost_units": cls._finite_nonnegative(raw.get("recovery_cost_units", 0.0), "recovery_cost_units"),
            "artifacts": normalized_artifacts,
        }

    @classmethod
    def _load(cls, rows: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(rows, list) or not rows:
            raise LatticeError("checkpoints_missing")
        nodes: dict[str, dict[str, Any]] = {}
        for raw in rows:
            node = cls._normalize_checkpoint(raw)
            checkpoint_id = node["checkpoint_id"]
            if checkpoint_id in nodes:
                raise LatticeError(f"duplicate_checkpoint:{checkpoint_id}")
            nodes[checkpoint_id] = node

        indegree = {node_id: 0 for node_id in nodes}
        children: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        for node_id, node in nodes.items():
            for parent in node["parent_ids"]:
                if parent not in nodes:
                    raise LatticeError(f"unresolved_parent_or_cycle:{node_id}:{parent}")
                indegree[node_id] += 1
                children[parent].append(node_id)

        queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
        visited: list[str] = []
        while queue:
            node_id = queue.popleft()
            visited.append(node_id)
            for child in sorted(children[node_id]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(visited) != len(nodes):
            raise LatticeError("unresolved_parent_or_cycle")
        return nodes

    @staticmethod
    def _snapshot(nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
        ordered = [nodes[key] for key in sorted(nodes)]
        body = {
            "schema": "glaciereq.execution-checkpoint-lattice.v1",
            "checkpoints": ordered,
        }
        return {**body, "lattice_digest": _digest(body)}

    @staticmethod
    def _ancestor_distances(nodes: dict[str, dict[str, Any]], start: str) -> dict[str, int]:
        if start not in nodes:
            raise LatticeError(f"checkpoint_not_found:{start}")
        distances = {start: 0}
        queue = deque([start])
        while queue:
            node_id = queue.popleft()
            distance = distances[node_id]
            for parent in nodes[node_id]["parent_ids"]:
                if parent not in distances or distance + 1 < distances[parent]:
                    distances[parent] = distance + 1
                    queue.append(parent)
        return distances

    @classmethod
    def _compare(cls, nodes: dict[str, dict[str, Any]], left: str, right: str) -> dict[str, Any]:
        left_dist = cls._ancestor_distances(nodes, left)
        right_dist = cls._ancestor_distances(nodes, right)
        common = set(left_dist) & set(right_dist)
        if not common:
            raise LatticeError("branches_have_no_common_checkpoint")
        nearest = min(common, key=lambda node_id: (left_dist[node_id] + right_dist[node_id], max(left_dist[node_id], right_dist[node_id]), node_id))
        body = {
            "left_id": left,
            "right_id": right,
            "common_checkpoint_id": nearest,
            "left_distance": left_dist[nearest],
            "right_distance": right_dist[nearest],
            "state_equal": nodes[left]["state_digest"] == nodes[right]["state_digest"],
        }
        return {**body, "comparison_digest": _digest(body)}

    @classmethod
    def _rollback_path(cls, nodes: dict[str, dict[str, Any]], current: str, target: str) -> list[str]:
        if current not in nodes:
            raise LatticeError(f"checkpoint_not_found:{current}")
        if target not in nodes:
            raise LatticeError(f"checkpoint_not_found:{target}")
        queue: deque[tuple[str, list[str]]] = deque([(current, [current])])
        seen = {current}
        while queue:
            node_id, path = queue.popleft()
            if node_id == target:
                return path
            for parent in sorted(nodes[node_id]["parent_ids"]):
                if parent not in seen:
                    seen.add(parent)
                    queue.append((parent, path + [parent]))
        raise LatticeError("recovery_target_not_ancestor")

    @classmethod
    def _recover(cls, nodes: dict[str, dict[str, Any]], current: str, target: str, budget: float) -> dict[str, Any]:
        path = cls._rollback_path(nodes, current, target)
        traversed = path[:-1]
        for checkpoint_id in traversed:
            if not nodes[checkpoint_id]["reversible"]:
                raise LatticeError(f"recovery_crosses_irreversible_checkpoint:{checkpoint_id}")
        if nodes[target]["status"] != "VERIFIED":
            raise LatticeError("recovery_target_not_verified")
        cost = round(sum(float(nodes[node_id]["recovery_cost_units"]) for node_id in traversed), 12)
        if cost > budget:
            raise LatticeError("recovery_budget_exceeded")
        body = {
            "current_id": current,
            "target_id": target,
            "rollback_path": path,
            "steps": len(path) - 1,
            "recovery_cost_units": cost,
        }
        return {**body, "plan_digest": _digest(body)}

    @classmethod
    def _advance(cls, nodes: dict[str, dict[str, Any]], raw: Any) -> dict[str, Any]:
        candidate = cls._normalize_checkpoint(raw)
        checkpoint_id = candidate["checkpoint_id"]
        if checkpoint_id in nodes:
            raise LatticeError(f"duplicate_checkpoint:{checkpoint_id}")
        if not candidate["parent_ids"]:
            raise LatticeError("advance_requires_parent")
        for parent in candidate["parent_ids"]:
            if parent not in nodes:
                raise LatticeError(f"parent_missing:{parent}")
            if nodes[parent]["status"] != "VERIFIED":
                raise LatticeError(f"parent_not_verified:{parent}")
        updated = dict(nodes)
        updated[checkpoint_id] = candidate
        checkpoint_digest = _digest(candidate)
        return {
            "checkpoint_id": checkpoint_id,
            "checkpoint_digest": checkpoint_digest,
            "snapshot": cls._snapshot(updated),
        }

    def evaluate(self, req: ExecutionCheckpointLatticeRequest) -> ExecutionCheckpointLatticeReceipt:
        reasons: list[str] = []
        if not str(req.subject_id or "").strip():
            reasons.append("subject_id_missing")
        try:
            budget = self._finite_nonnegative(req.budget, "budget")
        except LatticeError as exc:
            budget = 0.0
            reasons.append(str(exc))
        if budget <= self.MIN_BUDGET:
            reasons.append("budget_non_positive")
        payload = req.payload if isinstance(req.payload, dict) else {}
        if not isinstance(req.payload, dict):
            reasons.append("payload_not_object")

        result: dict[str, Any] | None = None
        snapshot: dict[str, Any] | None = None
        try:
            nodes = self._load(payload.get("checkpoints"))
            snapshot = self._snapshot(nodes)
            operation = payload.get("operation")
            if not isinstance(operation, dict):
                raise LatticeError("operation_missing")
            kind = str(operation.get("kind", "")).strip().lower()
            if kind == "verify":
                result = snapshot
            elif kind == "compare":
                result = self._compare(nodes, str(operation.get("left_id", "")), str(operation.get("right_id", "")))
            elif kind == "recover":
                result = self._recover(nodes, str(operation.get("current_id", "")), str(operation.get("target_id", "")), budget)
            elif kind == "advance":
                result = self._advance(nodes, operation.get("checkpoint"))
            else:
                raise LatticeError("operation_kind_invalid")
        except LatticeError as exc:
            reasons.append(str(exc))

        decision = Decision.REFUSE if reasons else Decision.ALLOW
        metrics: dict[str, Any] = {
            "checkpoint_count": len(snapshot["checkpoints"]) if snapshot else 0,
            "lattice_digest": snapshot.get("lattice_digest") if snapshot else None,
            "result": result,
        }
        body = {
            "subject_id": req.subject_id,
            "decision": decision.value,
            "reasons": reasons,
            "metrics": metrics,
        }
        return ExecutionCheckpointLatticeReceipt(
            decision=decision,
            reasons=tuple(reasons or ["checkpoint_lattice_operation_verified"]),
            digest=_digest(body),
            metrics=metrics,
        )


Mechanism = ExecutionCheckpointLattice
