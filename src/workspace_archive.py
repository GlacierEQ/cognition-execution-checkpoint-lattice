"""Deterministic content-addressed archives for recoverable git worktrees.

The archive stores the exact tracked and untracked, non-ignored working-tree
entries used by `worktree_snapshot.capture_worktree`, plus the exact HEAD and a
clone source. It intentionally excludes `.git` internals and ignored build
caches. Materialization recreates a fresh git checkout at the captured HEAD,
cleans the checkout, restores archived entries, and can then be verified against
the checkpoint snapshot digest.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from worktree_snapshot import capture_worktree

DEFAULT_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise ValueError((proc.stderr or proc.stdout or "git command failed")[:1000])
    return proc.stdout.strip()


def _list_paths(repo: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError("git_ls_files_failed")
    return sorted({chunk.decode("utf-8") for chunk in proc.stdout.split(b"\0") if chunk})


def _safe_rel(path: str) -> PurePosixPath:
    rel = PurePosixPath(path)
    if not path or rel.is_absolute() or ".." in rel.parts or "." == path:
        raise ValueError("archive_path_unsafe")
    if rel.parts and rel.parts[0] == ".git":
        raise ValueError("archive_git_internal_forbidden")
    return rel


def _source(repo: Path) -> str:
    origin = _git(repo, "remote", "get-url", "origin", check=False)
    return origin or str(repo)


def _safe_symlink_target(entry_path: str, target: str) -> None:
    if not target or os.path.isabs(target):
        raise ValueError("archive_symlink_target_unsafe")
    parent = PurePosixPath(entry_path).parent
    combined = parent.joinpath(PurePosixPath(target))
    depth = 0
    for part in combined.parts:
        if part == "..":
            depth -= 1
        elif part not in {"", "."}:
            depth += 1
        if depth < 0:
            raise ValueError("archive_symlink_target_escapes_root")


@dataclass(frozen=True)
class WorkspaceArchive:
    source_repository: str
    head_sha: str
    snapshot_digest: str
    entries: tuple[dict[str, Any], ...]
    content_bytes: int
    archive_digest: str

    def as_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        entries = []
        for row in self.entries:
            item = dict(row)
            if not include_content:
                item.pop("content_base64", None)
            entries.append(item)
        return {
            "schema": "glaciereq.workspace-archive.v1",
            "source_repository": self.source_repository,
            "head_sha": self.head_sha,
            "snapshot_digest": self.snapshot_digest,
            "entries": entries,
            "content_bytes": self.content_bytes,
            "archive_digest": self.archive_digest,
        }

    def to_bytes(self) -> bytes:
        return (json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "WorkspaceArchive":
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != "glaciereq.workspace-archive.v1":
            raise ValueError("workspace_archive_schema_invalid")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise ValueError("workspace_archive_entries_invalid")
        archive = cls(
            source_repository=str(payload.get("source_repository") or ""),
            head_sha=str(payload.get("head_sha") or ""),
            snapshot_digest=str(payload.get("snapshot_digest") or ""),
            entries=tuple(dict(row) for row in entries),
            content_bytes=int(payload.get("content_bytes", 0)),
            archive_digest=str(payload.get("archive_digest") or ""),
        )
        archive.validate()
        return archive

    def validate(self) -> None:
        if not self.source_repository:
            raise ValueError("archive_source_missing")
        if len(self.head_sha) != 40 or any(ch not in "0123456789abcdef" for ch in self.head_sha):
            raise ValueError("archive_head_sha_invalid")
        if len(self.snapshot_digest) != 64:
            raise ValueError("archive_snapshot_digest_invalid")
        total = 0
        canonical_entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in self.entries:
            path = str(raw.get("path") or "")
            _safe_rel(path)
            if path in seen:
                raise ValueError("archive_duplicate_path")
            seen.add(path)
            kind = raw.get("kind")
            mode = raw.get("mode")
            if not isinstance(mode, int) or mode < 0:
                raise ValueError("archive_mode_invalid")
            if kind == "file":
                encoded = raw.get("content_base64")
                if not isinstance(encoded, str):
                    raise ValueError("archive_file_content_missing")
                try:
                    data = base64.b64decode(encoded, validate=True)
                except Exception as exc:
                    raise ValueError("archive_file_content_invalid") from exc
                if raw.get("sha256") != _sha(data):
                    raise ValueError("archive_file_digest_mismatch")
                total += len(data)
                canonical_entries.append({"path": path, "kind": kind, "mode": mode, "size": len(data), "sha256": _sha(data)})
            elif kind == "symlink":
                target = raw.get("target")
                if not isinstance(target, str):
                    raise ValueError("archive_symlink_target_missing")
                _safe_symlink_target(path, target)
                if raw.get("sha256") != _sha(target.encode("utf-8")):
                    raise ValueError("archive_symlink_digest_mismatch")
                canonical_entries.append({"path": path, "kind": kind, "mode": mode, "target": target, "sha256": raw.get("sha256")})
            else:
                raise ValueError("archive_entry_kind_invalid")
        if total != self.content_bytes:
            raise ValueError("archive_content_size_mismatch")
        core = {
            "source_repository": self.source_repository,
            "head_sha": self.head_sha,
            "snapshot_digest": self.snapshot_digest,
            "entries": canonical_entries,
            "content_bytes": total,
        }
        if _digest(core) != self.archive_digest:
            raise ValueError("archive_digest_mismatch")


def capture_workspace_archive(repo: str | Path, *, max_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES) -> WorkspaceArchive:
    root = Path(repo).resolve()
    if not (root / ".git").exists():
        raise ValueError("repository_not_git_worktree")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("archive_max_bytes_invalid")
    snapshot = capture_worktree(root)
    if snapshot.head_sha is None:
        raise ValueError("archive_requires_git_head")
    entries: list[dict[str, Any]] = []
    total = 0
    canonical_entries: list[dict[str, Any]] = []
    for rel in _list_paths(root):
        _safe_rel(rel)
        path = root / rel
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            target = os.readlink(path)
            _safe_symlink_target(rel, target)
            digest = _sha(target.encode("utf-8"))
            entries.append({"path": rel, "kind": "symlink", "mode": mode, "target": target, "sha256": digest})
            canonical_entries.append({"path": rel, "kind": "symlink", "mode": mode, "target": target, "sha256": digest})
        elif path.is_file():
            data = path.read_bytes()
            total += len(data)
            if total > max_bytes:
                raise ValueError("archive_size_limit_exceeded")
            digest = _sha(data)
            entries.append({
                "path": rel,
                "kind": "file",
                "mode": mode,
                "size": len(data),
                "sha256": digest,
                "content_base64": base64.b64encode(data).decode("ascii"),
            })
            canonical_entries.append({"path": rel, "kind": "file", "mode": mode, "size": len(data), "sha256": digest})
    source_repository = _source(root)
    core = {
        "source_repository": source_repository,
        "head_sha": snapshot.head_sha,
        "snapshot_digest": snapshot.snapshot_digest,
        "entries": canonical_entries,
        "content_bytes": total,
    }
    return WorkspaceArchive(
        source_repository=source_repository,
        head_sha=snapshot.head_sha,
        snapshot_digest=snapshot.snapshot_digest,
        entries=tuple(entries),
        content_bytes=total,
        archive_digest=_digest(core),
    )


def materialize_workspace(archive: WorkspaceArchive, destination: str | Path) -> dict[str, Any]:
    archive.validate()
    dest = Path(destination).resolve()
    if dest.exists() and any(dest.iterdir()):
        raise ValueError("materialization_destination_not_empty")
    if dest.exists():
        dest.rmdir()
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", "--no-checkout", "--quiet", archive.source_repository, str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError("materialization_clone_failed:" + (proc.stderr or proc.stdout)[:500])
    try:
        checkout = subprocess.run(
            ["git", "checkout", "--quiet", archive.head_sha],
            cwd=dest,
            capture_output=True,
            text=True,
            check=False,
        )
        if checkout.returncode != 0:
            raise ValueError("materialization_checkout_failed:" + (checkout.stderr or checkout.stdout)[:500])
        for child in list(dest.iterdir()):
            if child.name == ".git":
                continue
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child)
        for entry in archive.entries:
            rel = _safe_rel(str(entry["path"]))
            path = dest.joinpath(*rel.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            if entry["kind"] == "file":
                data = base64.b64decode(entry["content_base64"], validate=True)
                if _sha(data) != entry["sha256"]:
                    raise ValueError("materialization_file_digest_mismatch")
                path.write_bytes(data)
                os.chmod(path, int(entry["mode"]))
            else:
                target = str(entry["target"])
                _safe_symlink_target(str(rel), target)
                path.symlink_to(target)
        snapshot = capture_worktree(dest)
        if snapshot.snapshot_digest != archive.snapshot_digest:
            raise ValueError("materialization_snapshot_mismatch")
        return {
            "destination": str(dest),
            "head_sha": snapshot.head_sha,
            "snapshot_digest": snapshot.snapshot_digest,
            "archive_digest": archive.archive_digest,
            "verified": True,
        }
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
