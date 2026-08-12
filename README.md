# Execution Checkpoint Lattice

Independent GlacierEQ portfolio exhibit aligned to **Cognition** operating themes.

> **Not affiliated.** This repository is not affiliated with, endorsed by, employed by, or deployed at Cognition. No proprietary access, production deployment, customer impact, or company partnership is claimed.

## Problem

Long-horizon software tasks fail expensively when environment setup, hidden dependencies, ambiguous requirements, and partial failures compound. Restarting the entire trajectory wastes work and can erase the last known-good state.

## Working mechanism

**Execution Checkpoint Lattice** is a content-addressed DAG of verified intermediate execution states keyed to task subgoals.

Each checkpoint binds:

- checkpoint and subgoal identity;
- one or more parent checkpoints;
- exact state and verification SHA-256 digests;
- verification status;
- reversibility;
- bounded recovery cost;
- optional content-addressed artifacts.

The lattice supports four real operations:

- **verify** a serialized checkpoint DAG and its ancestry;
- **advance** by adding a new checkpoint only when all parents exist and are verified;
- **compare** divergent branches and locate their nearest common checkpoint;
- **recover** to a verified reversible ancestor using a bounded rollback path and explicit recovery budget.

It fails closed on unresolved ancestry, duplicate checkpoints, malformed digests, unverified parents, sibling-branch recovery, irreversible rollback paths, and insufficient recovery budgets.

## Run it

```bash
python -m pytest -q
python scripts/operate.py

python -m pip install build
python -m build
python -m pip install dist/*.whl

execution-checkpoint-lattice examples/recovery_plan.json \
  --budget 3.0 \
  --output recovery-receipt.json
```

The CLI exits `0` only when the requested lattice operation is valid. A refused recovery or malformed lattice exits non-zero, making the mechanism usable in an agent loop or CI promotion path.

## Proof surface

| Surface | Path |
|---|---|
| Checkpoint DAG + recovery engine | `src/execution_checkpoint_lattice.py` |
| Installed CLI | `src/checkpoint_lattice_cli.py` |
| Behavioral / recovery tests | `tests/test_execution_checkpoint_lattice.py` |
| Adversarial tests | `tests/test_adversarial.py` |
| Reproducible recovery fixture | `examples/recovery_plan.json` |
| Cold-start operation | `scripts/operate.py` |
| Issue contract | `ISSUE_CONTRACT.md` |

## Technical distinction

This is not a list of saved snapshots. The graph encodes verified ancestry and reversible execution semantics. Branch comparison identifies divergence from a shared verified state, while recovery is allowed only when the target is a verified ancestor, every traversed checkpoint is reversible, and the rollback cost fits the declared budget. Receipts remain deterministic and content-addressed.

## Current boundary

This is an independent reference implementation. It does not integrate proprietary Cognition systems or claim production use. The next depth gate is binding checkpoints to real worktree/container snapshots and task-run receipts so recovery can materialize the exact verified environment represented by each digest.
