from __future__ import annotations

import hashlib

from execution_checkpoint_lattice import Decision, ExecutionCheckpointLattice, ExecutionCheckpointLatticeRequest


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def checkpoint(
    checkpoint_id: str,
    subgoal: str,
    parents: list[str],
    *,
    status: str = "VERIFIED",
    reversible: bool = True,
    cost: float = 1.0,
) -> dict:
    return {
        "checkpoint_id": checkpoint_id,
        "subgoal": subgoal,
        "parent_ids": parents,
        "state_digest": sha("state:" + checkpoint_id),
        "verification_digest": sha("verify:" + checkpoint_id) if status == "VERIFIED" else None,
        "status": status,
        "reversible": reversible,
        "recovery_cost_units": cost,
        "artifacts": {"workspace": sha("artifact:" + checkpoint_id)},
    }


def lattice_rows() -> list[dict]:
    return [
        checkpoint("root", "environment ready", []),
        checkpoint("impl-a", "implementation branch A", ["root"]),
        checkpoint("impl-b", "implementation branch B", ["root"]),
        checkpoint("test-a", "tests green on branch A", ["impl-a"], cost=1.5),
    ]


def evaluate(operation: dict, *, rows: list[dict] | None = None, budget: float = 10.0):
    return ExecutionCheckpointLattice().evaluate(
        ExecutionCheckpointLatticeRequest(
            subject_id="task-42",
            payload={"checkpoints": rows if rows is not None else lattice_rows(), "operation": operation},
            budget=budget,
        )
    )


def test_verifies_content_addressed_checkpoint_dag() -> None:
    receipt = evaluate({"kind": "verify"})
    assert receipt.decision is Decision.ALLOW
    assert receipt.metrics["checkpoint_count"] == 4
    assert len(receipt.metrics["lattice_digest"]) == 64
    assert receipt.metrics["result"]["schema"] == "glaciereq.execution-checkpoint-lattice.v1"


def test_compares_divergent_branches_at_nearest_common_checkpoint() -> None:
    receipt = evaluate({"kind": "compare", "left_id": "test-a", "right_id": "impl-b"})
    assert receipt.decision is Decision.ALLOW
    result = receipt.metrics["result"]
    assert result["common_checkpoint_id"] == "root"
    assert result["left_distance"] == 2
    assert result["right_distance"] == 1
    assert result["state_equal"] is False
    assert len(result["comparison_digest"]) == 64


def test_builds_bounded_recovery_plan_to_verified_ancestor() -> None:
    receipt = evaluate({"kind": "recover", "current_id": "test-a", "target_id": "root"}, budget=3.0)
    assert receipt.decision is Decision.ALLOW
    result = receipt.metrics["result"]
    assert result["rollback_path"] == ["test-a", "impl-a", "root"]
    assert result["steps"] == 2
    assert result["recovery_cost_units"] == 2.5
    assert len(result["plan_digest"]) == 64


def test_refuses_recovery_to_sibling_branch() -> None:
    receipt = evaluate({"kind": "recover", "current_id": "test-a", "target_id": "impl-b"}, budget=10.0)
    assert receipt.decision is Decision.REFUSE
    assert "recovery_target_not_ancestor" in receipt.reasons


def test_refuses_recovery_when_budget_is_insufficient() -> None:
    receipt = evaluate({"kind": "recover", "current_id": "test-a", "target_id": "root"}, budget=2.0)
    assert receipt.decision is Decision.REFUSE
    assert "recovery_budget_exceeded" in receipt.reasons


def test_refuses_recovery_across_irreversible_checkpoint() -> None:
    rows = lattice_rows()
    rows[3] = checkpoint("test-a", "irreversible migration", ["impl-a"], reversible=False)
    receipt = evaluate({"kind": "recover", "current_id": "test-a", "target_id": "root"}, rows=rows, budget=10.0)
    assert receipt.decision is Decision.REFUSE
    assert any(reason.startswith("recovery_crosses_irreversible_checkpoint") for reason in receipt.reasons)


def test_advance_adds_verified_checkpoint_and_changes_snapshot_digest() -> None:
    base = evaluate({"kind": "verify"})
    candidate = checkpoint("ship", "package candidate", ["test-a"])
    advanced = evaluate({"kind": "advance", "checkpoint": candidate})
    assert advanced.decision is Decision.ALLOW
    result = advanced.metrics["result"]
    assert result["checkpoint_id"] == "ship"
    assert len(result["checkpoint_digest"]) == 64
    assert result["snapshot"]["lattice_digest"] != base.metrics["lattice_digest"]


def test_refuses_checkpoint_with_unverified_parent() -> None:
    rows = [checkpoint("root", "environment", [], status="PENDING")]
    candidate = checkpoint("impl", "implementation", ["root"])
    receipt = evaluate({"kind": "advance", "checkpoint": candidate}, rows=rows)
    assert receipt.decision is Decision.REFUSE
    assert "parent_not_verified:root" in receipt.reasons


def test_refuses_missing_parent_or_cycle_in_serialized_state() -> None:
    rows = [checkpoint("child", "orphan", ["missing"])]
    receipt = evaluate({"kind": "verify"}, rows=rows)
    assert receipt.decision is Decision.REFUSE
    assert any(reason.startswith("unresolved_parent_or_cycle") for reason in receipt.reasons)


def test_refuses_malformed_content_digest() -> None:
    rows = lattice_rows()
    rows[0]["state_digest"] = "not-a-digest"
    receipt = evaluate({"kind": "verify"}, rows=rows)
    assert receipt.decision is Decision.REFUSE
    assert "state_digest_invalid_sha256" in receipt.reasons
