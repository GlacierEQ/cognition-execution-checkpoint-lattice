# DEV_UP_INSTRUCTIONS — implementation record

**Repository:** `GlacierEQ/cognition-execution-checkpoint-lattice`  
**Independent company lens:** Cognition  
**Innovation:** Execution Checkpoint Lattice

## Mission

Carry long-horizon software tasks through verified intermediate states so an execution agent can branch, compare, and recover without repeating an entire trajectory.

## Implemented

`src/execution_checkpoint_lattice.py` now provides a content-addressed checkpoint DAG rather than a placeholder allow/refuse shell.

A checkpoint binds subgoal identity, parent checkpoints, state digest, verification digest, verification state, reversibility, recovery cost, and optional artifact digests. The lattice enforces verified ancestry and exposes:

- serialized DAG verification;
- verified checkpoint advancement;
- divergent branch comparison with nearest common checkpoint;
- bounded recovery to a verified ancestor;
- irreversible-path refusal;
- recovery-budget refusal;
- deterministic snapshot, comparison, checkpoint, and recovery-plan digests.

`src/checkpoint_lattice_cli.py` exposes the mechanism as an installable command, and `examples/recovery_plan.json` demonstrates a three-stage verified trajectory recovering to its root checkpoint.

## Verification contract

Behavioral tests now cover DAG verification, divergence comparison, successful recovery, sibling recovery refusal, budget refusal, irreversible rollback refusal, checkpoint advancement, unverified-parent refusal, unresolved ancestry, and malformed digests. Existing adversarial coverage remains intact.

The repository is packaged as a wheel with the `execution-checkpoint-lattice` console command. Exact-source promotion remains governed by the existing Helix authority path and must not be inferred from this document.

## Truth boundary

No Cognition affiliation, proprietary access, production deployment, customer impact, or company partnership is claimed. The next useful depth step is binding checkpoint digests to real worktree/container snapshots and execution receipts, not adding another abstract governance layer.
