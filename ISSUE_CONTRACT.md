# Issue contract — Execution Checkpoint Lattice

## Problem
reliably carrying software tasks across long horizons where environment setup, hidden dependencies, ambiguous requirements, and partial failures compound

## Desired outcome
A bounded, open, testable implementation of **Execution Checkpoint Lattice** that demonstrates Persist verified intermediate states and reversible checkpoints keyed to task subgoals, so the agent can branch, compare, and recover without repeating entire trajectories.

## Non-goals
- Cognition affiliation or proprietary integration
- Portfolio-wide scale/performance claims
- UI marketing site

## Acceptance
1. Mechanism module implements allow + refuse with structured receipts
2. pytest behavioral suite green
3. operate.py cold-start produces JSON receipt
4. Non-affiliation disclaimer preserved
