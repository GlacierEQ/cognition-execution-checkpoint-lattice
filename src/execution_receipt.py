"""Execute verification commands and bind results to deterministic receipts."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _argv(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not value:
        raise ValueError("verification_argv_invalid")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ValueError("verification_argv_invalid")
        out.append(item)
    return tuple(out)


@dataclass(frozen=True)
class CommandReceipt:
    name: str
    argv: tuple[str, ...]
    returncode: int
    status: str
    stdout_sha256: str
    stderr_sha256: str
    stdout_tail: str
    stderr_tail: str
    receipt_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "status": self.status,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "receipt_digest": self.receipt_digest,
        }


def execute_command(
    name: str,
    argv: Sequence[str],
    *,
    cwd: str | Path,
    timeout_seconds: int = 900,
    env: Mapping[str, str] | None = None,
) -> CommandReceipt:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("verification_name_missing")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("verification_timeout_invalid")
    command = _argv(argv)
    root = Path(cwd).resolve()
    if not root.is_dir():
        raise ValueError("verification_cwd_missing")
    merged = os.environ.copy()
    if env:
        for key, value in env.items():
            if not isinstance(key, str) or not key or not isinstance(value, str):
                raise ValueError("verification_env_invalid")
            merged[key] = value
    try:
        proc = subprocess.run(
            list(command),
            cwd=str(root),
            env=merged,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = proc.stdout or b""
        stderr = proc.stderr or b""
        returncode = int(proc.returncode)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8", errors="replace")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8", errors="replace")
        returncode = 124
    status = "PASS" if returncode == 0 else "FAIL"
    core = {
        "name": name.strip(),
        "argv": list(command),
        "returncode": returncode,
        "status": status,
        "stdout_sha256": _sha_bytes(stdout),
        "stderr_sha256": _sha_bytes(stderr),
    }
    return CommandReceipt(
        name=name.strip(),
        argv=command,
        returncode=returncode,
        status=status,
        stdout_sha256=core["stdout_sha256"],
        stderr_sha256=core["stderr_sha256"],
        stdout_tail=stdout.decode("utf-8", errors="replace")[-2000:],
        stderr_tail=stderr.decode("utf-8", errors="replace")[-2000:],
        receipt_digest=_digest(core),
    )


def execute_verification_plan(
    commands: Iterable[Mapping[str, Any]],
    *,
    cwd: str | Path,
) -> tuple[CommandReceipt, ...]:
    receipts: list[CommandReceipt] = []
    names: set[str] = set()
    for row in commands:
        if not isinstance(row, Mapping):
            raise ValueError("verification_definition_invalid")
        name = row.get("name")
        argv = row.get("argv")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("verification_name_missing")
        if name in names:
            raise ValueError("duplicate_verification_name")
        names.add(name)
        receipts.append(
            execute_command(
                name,
                argv,
                cwd=cwd,
                timeout_seconds=int(row.get("timeout_seconds", 900)),
            )
        )
    return tuple(receipts)


def verification_digest(receipts: Iterable[CommandReceipt]) -> str:
    rows = [receipt.as_dict() for receipt in receipts]
    if not rows:
        raise ValueError("verification_receipts_empty")
    return _digest(rows)
