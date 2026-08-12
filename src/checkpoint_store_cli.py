"""Operational CLI for the persistent Execution Checkpoint Lattice store."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from checkpoint_materializer import materialize_checkpoint
from checkpoint_store import CheckpointStore, capture_checkpoint_to_store
from recovery_executor import execute_recovery


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("config_must_be_object")
    return payload


def _write(value: dict, output: Path | None) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persist, materialize, verify, and recover execution checkpoints")
    parser.add_argument("--store", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture")
    capture.add_argument("config", type=Path)
    capture.add_argument("--repo", type=Path, default=Path.cwd())
    capture.add_argument("--output", type=Path)

    listing = sub.add_parser("list")
    listing.add_argument("--output", type=Path)

    integrity = sub.add_parser("integrity")
    integrity.add_argument("--output", type=Path)

    materialize = sub.add_parser("materialize")
    materialize.add_argument("checkpoint_id")
    materialize.add_argument("destination", type=Path)
    materialize.add_argument("--output", type=Path)

    recover = sub.add_parser("recover")
    recover.add_argument("current_id")
    recover.add_argument("target_id")
    recover.add_argument("destination", type=Path)
    recover.add_argument("--budget", type=float, required=True)
    recover.add_argument("--output", type=Path)

    args = parser.parse_args(argv)
    try:
        store = CheckpointStore(args.store)
        if args.command == "capture":
            config = _load(args.config)
            checkpoint, proof = capture_checkpoint_to_store(
                store,
                checkpoint_id=config.get("checkpoint_id"),
                subgoal=config.get("subgoal"),
                parent_ids=config.get("parent_ids") or [],
                repo=args.repo,
                verification_commands=config.get("verification_commands") or [],
                reversible=config.get("reversible", True),
                recovery_cost_units=config.get("recovery_cost_units", 1.0),
            )
            _write({"checkpoint": checkpoint.as_dict(), "proof": proof}, args.output)
            return 0
        if args.command == "list":
            _write({"checkpoints": [checkpoint.as_dict() for checkpoint in store.list_checkpoints()]}, args.output)
            return 0
        if args.command == "integrity":
            report = store.integrity_report()
            _write(report, args.output)
            return 0 if report["status"] == "PASS" else 2
        if args.command == "materialize":
            result = materialize_checkpoint(store, args.checkpoint_id, args.destination)
            _write(result, args.output)
            return 0
        if args.command == "recover":
            result = execute_recovery(
                store,
                current_id=args.current_id,
                target_id=args.target_id,
                destination=args.destination,
                budget=args.budget,
            )
            _write(result, args.output)
            return 0
        raise ValueError("command_invalid")
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        sys.stderr.write(json.dumps({"status": "REFUSE", "reason": str(exc)}, sort_keys=True) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
