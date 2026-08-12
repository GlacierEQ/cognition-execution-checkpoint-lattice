# Execution Checkpoint Lattice

Independent GlacierEQ portfolio exhibit aligned to **Cognition** operating themes.

> **Not affiliated.** This repository is not affiliated with, endorsed by, employed by, or deployed at Cognition. No proprietary access, production deployment, customer impact, or company partnership is claimed.

## Problem

Long-horizon software tasks fail expensively when environment setup, hidden dependencies, ambiguous requirements, and partial failures compound. Restarting the entire trajectory wastes work; merely saving a label or hash is not enough if the exact state cannot later be reconstructed and re-verified.

## System

**Execution Checkpoint Lattice** is a persistent, content-addressed execution-state graph with real capture, branch comparison, self-contained materialization, and bounded recovery.

A verified checkpoint binds:

- checkpoint/subgoal identity and verified parent ancestry;
- exact git worktree file bytes, HEAD, dirty-status digest, dependency manifests, and bounded environment identity;
- exact verification commands, exit codes, stdout/stderr digests, and aggregate verification digest;
- explicit reversibility and recovery cost;
- a self-contained Git bundle plus archived tracked/untracked working-tree state;
- immutable persistent metadata and artifact identities.

## What it can do

- **capture** a real worktree only after its verification command plan passes;
- **persist** checkpoint metadata in SQLite WAL storage and immutable content-addressed blobs;
- **compare** divergent verified branches and locate their nearest common checkpoint;
- **plan recovery** only to verified ancestors and refuse irreversible or over-budget paths;
- **materialize** a checkpoint into a fresh isolated git workspace from its embedded bundle, even after the original repository path is removed;
- **execute recovery** by materializing the target, rerunning the target verification commands, and proving the recovered snapshot digest equals the target state digest;
- **audit store integrity** across checkpoint metadata, parent references, proofs, state digests, and workspace archives.

Checkpoint ids are immutable. Exact replay is idempotent; conflicting content under an existing id is refused.

## Install

```bash
python -m pytest -q
python -m pip install build
python -m build
python -m pip install dist/*.whl
```

Operate an in-memory serialized lattice:

```bash
execution-checkpoint-lattice examples/recovery_plan.json \
  --budget 3.0 --output recovery-plan.json
```

Operate the persistent store:

```bash
execution-checkpoint-store --store .checkpoint-store integrity
execution-checkpoint-store --store .checkpoint-store list
```

Run the full persistent recovery demonstration:

```bash
python examples/persistent_recovery_demo.py --work /tmp/checkpoint-demo
```

That demonstration creates a repository, captures a verified root checkpoint, advances and captures a distinct child, reopens the persistent store, recovers child → root from the embedded bundle, reruns verification, and proves the recovered state digest equals the original root digest.

## Proof surface

| Capability | Implementation | Behavioral proof |
|---|---|---|
| Checkpoint DAG / compare / recovery planning | `src/execution_checkpoint_lattice.py` | `tests/test_execution_checkpoint_lattice.py` |
| Real worktree state identity | `src/worktree_snapshot.py` | `tests/test_real_checkpoint_capture.py` |
| Exact command verification receipts | `src/execution_receipt.py` | `tests/test_real_checkpoint_capture.py` |
| Self-contained Git-bundle archive | `src/workspace_archive.py` | `tests/test_persistent_recovery.py` |
| Durable checkpoint/artifact store | `src/checkpoint_store.py` | `tests/test_persistent_recovery.py` |
| Verified materialization | `src/checkpoint_materializer.py` | `tests/test_persistent_recovery.py` |
| Recovery execution + re-verification | `src/recovery_executor.py` | `tests/test_persistent_recovery.py` |
| Installed operational CLI | `src/checkpoint_store_cli.py` | `.github/workflows/tests.yml` |
| Full executable scenario | `examples/persistent_recovery_demo.py` | `.github/workflows/tests.yml` |

## Current boundary

This is an independent local-first execution/recovery system. It does not integrate proprietary Cognition infrastructure and makes no production-use claim. Its self-contained archive intentionally captures tracked and untracked non-ignored worktree state plus reachable Git history; ignored caches or external services require their own purpose-specific checkpoint adapters if a workload depends on them. The crystallization manifests define the material capability set, and terminal `CRYSTALLIZED` status is earned only from exact-head behavioral/build/runtime proof.
