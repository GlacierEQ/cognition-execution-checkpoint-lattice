"""Capture a verified checkpoint from an actual git worktree."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from checkpoint_capture import capture_verified_checkpoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture a content-addressed verified execution checkpoint")
    parser.add_argument("config", type=Path, help="JSON with checkpoint_id, subgoal, parent_ids, verification_commands")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("config_must_be_object")
        checkpoint, proof = capture_verified_checkpoint(
            checkpoint_id=payload.get("checkpoint_id"),
            subgoal=payload.get("subgoal"),
            parent_ids=payload.get("parent_ids") or [],
            repo=args.repo,
            verification_commands=payload.get("verification_commands") or [],
            reversible=payload.get("reversible", True),
            recovery_cost_units=payload.get("recovery_cost_units", 1.0),
        )
        rendered = json.dumps(proof, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        sys.stdout.write(rendered)
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(json.dumps({"status": "REFUSE", "reason": str(exc)}, sort_keys=True) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
