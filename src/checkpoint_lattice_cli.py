"""CLI for verifying, comparing, advancing, and recovering checkpoint lattices."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from execution_checkpoint_lattice import Decision, ExecutionCheckpointLattice, ExecutionCheckpointLatticeRequest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operate a content-addressed execution checkpoint lattice")
    parser.add_argument("input", type=Path, help="JSON containing subject_id, checkpoints, and operation")
    parser.add_argument("--budget", type=float, default=0.0, help="recovery budget units")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("input_must_be_object")
        receipt = ExecutionCheckpointLattice().evaluate(
            ExecutionCheckpointLatticeRequest(
                subject_id=payload.get("subject_id"),
                payload={"checkpoints": payload.get("checkpoints", []), "operation": payload.get("operation")},
                budget=args.budget,
            )
        )
        rendered = json.dumps(receipt.as_dict(), indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        sys.stdout.write(rendered)
        return 0 if receipt.decision is Decision.ALLOW else 2
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        sys.stderr.write(json.dumps({"decision": "ERROR", "reason": str(exc)}, sort_keys=True) + "\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
