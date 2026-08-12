"""Content-address a real git worktree and execution environment.

The snapshot digest is derived from exact file bytes, git HEAD, dirty-status
bytes, selected dependency manifests, and a bounded runtime environment
fingerprint. It is suitable for checkpoint identity and later materialization
verification; it is not a claim that container/process state has been frozen.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _git_bytes(repo: Path, *args: str, check: bool = True) -> bytes:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    if check and proc.returncode != 0:
        raise ValueError((proc.stderr or proc.stdout or b"git failed").decode("utf-8", errors="replace")[:1000])
    return proc.stdout or b""


def _head(repo: Path) -> str | None:
    proc = subprocess.run(["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=repo, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value if len(value) == 40 else None


def _listed_files(repo: Path) -> list[str]:
    raw = _git_bytes(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    paths = [chunk.decode("utf-8", errors="strict") for chunk in raw.split(b"\0") if chunk]
    return sorted(set(paths))


def _file_record(repo: Path, rel: str) -> dict[str, Any]:
    path = repo / rel
    if path.is_symlink():
        target = os.readlink(path)
        return {"path": rel, "kind": "symlink", "target": target, "sha256": _sha_bytes(target.encode("utf-8"))}
    if not path.is_file():
        return {"path": rel, "kind": "missing", "sha256": _sha_bytes(b"")}
    data = path.read_bytes()
    return {"path": rel, "kind": "file", "size": len(data), "sha256": _sha_bytes(data)}


@dataclass(frozen=True)
class WorktreeSnapshot:
    repository: str
    head_sha: str | None
    files: tuple[dict[str, Any], ...]
    git_status_sha256: str
    dependency_manifest_sha256: dict[str, str]
    environment: dict[str, str]
    environment_digest: str
    snapshot_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "head_sha": self.head_sha,
            "files": [dict(row) for row in self.files],
            "git_status_sha256": self.git_status_sha256,
            "dependency_manifest_sha256": dict(self.dependency_manifest_sha256),
            "environment": dict(self.environment),
            "environment_digest": self.environment_digest,
            "snapshot_digest": self.snapshot_digest,
        }


def capture_worktree(repo: str | Path) -> WorktreeSnapshot:
    root = Path(repo).resolve()
    if not (root / ".git").exists():
        raise ValueError("repository_not_git_worktree")
    files = tuple(_file_record(root, rel) for rel in _listed_files(root))
    status_bytes = _git_bytes(root, "status", "--porcelain=v1", "-z")
    manifests: dict[str, str] = {}
    for rel in (
        "requirements.txt", "pyproject.toml", "poetry.lock", "uv.lock",
        "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock",
        "go.sum", "Pipfile.lock",
    ):
        path = root / rel
        if path.is_file():
            manifests[rel] = _sha_bytes(path.read_bytes())
    environment = {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "executable_name": Path(sys.executable).name,
    }
    environment_digest = _digest(environment)
    identity = {
        "head_sha": _head(root),
        "files": files,
        "git_status_sha256": _sha_bytes(status_bytes),
        "dependency_manifest_sha256": manifests,
        "environment_digest": environment_digest,
    }
    return WorktreeSnapshot(
        repository=str(root),
        head_sha=identity["head_sha"],
        files=files,
        git_status_sha256=identity["git_status_sha256"],
        dependency_manifest_sha256=manifests,
        environment=environment,
        environment_digest=environment_digest,
        snapshot_digest=_digest(identity),
    )


def verify_worktree(repo: str | Path, expected_snapshot_digest: str) -> bool:
    if not isinstance(expected_snapshot_digest, str) or len(expected_snapshot_digest) != 64:
        raise ValueError("expected_snapshot_digest_invalid")
    return capture_worktree(repo).snapshot_digest == expected_snapshot_digest
