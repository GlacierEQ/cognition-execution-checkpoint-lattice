#!/usr/bin/env python3
"""Executable end-to-end persistent checkpoint recovery demonstration."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from checkpoint_store import CheckpointStore, capture_checkpoint_to_store
from recovery_executor import execute_recovery


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout)[:1000])
    return proc.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    repo = work / "source"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "demo@example.invalid")
    git(repo, "config", "user.name", "Checkpoint Demo")
    (repo / "engine.py").write_text("VALUE = 'root'\n", encoding="utf-8")
    (repo / "verify.py").write_text(
        "from pathlib import Path\n"
        "text=Path('engine.py').read_text()\n"
        "assert 'VALUE' in text\n"
        "print('verified')\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "root")

    store = CheckpointStore(work / "store")
    verification = [{"name": "runtime", "argv": [sys.executable, "verify.py"]}]
    root, _ = capture_checkpoint_to_store(
        store,
        checkpoint_id="root",
        subgoal="verified root",
        parent_ids=[],
        repo=repo,
        verification_commands=verification,
        recovery_cost_units=0.5,
    )

    (repo / "engine.py").write_text("VALUE = 'child'\n", encoding="utf-8")
    (repo / "child.txt").write_text("later trajectory state\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "child")
    child, _ = capture_checkpoint_to_store(
        store,
        checkpoint_id="child",
        subgoal="later verified state",
        parent_ids=["root"],
        repo=repo,
        verification_commands=verification,
        recovery_cost_units=1.0,
    )

    # Reopen the store to prove persistence rather than in-memory continuity.
    reopened = CheckpointStore(work / "store")
    integrity = reopened.integrity_report()
    if integrity["status"] != "PASS":
        raise RuntimeError("persistent store integrity failed")
    receipt = execute_recovery(
        reopened,
        current_id="child",
        target_id="root",
        destination=work / "recovered",
        budget=1.0,
    )
    if receipt["recovered_state_digest"] != root.state_digest:
        raise RuntimeError("recovered state does not match root")
    if child.state_digest == root.state_digest:
        raise RuntimeError("demo did not create a distinct child state")
    output = {
        "schema": "glaciereq.persistent-recovery-demo.v1",
        "store_integrity": integrity,
        "root_state_digest": root.state_digest,
        "child_state_digest": child.state_digest,
        "recovery": receipt,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
