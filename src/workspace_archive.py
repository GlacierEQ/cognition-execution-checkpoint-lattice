"""Self-contained deterministic archives for recoverable git worktrees.

Each archive carries:
- the exact tracked and untracked, non-ignored working-tree entries used by
  `worktree_snapshot.capture_worktree`;
- the exact captured HEAD;
- a content-addressed Git bundle containing that commit and its reachable
  history, so recovery does not depend on the original remote still existing;
- the original source repository only as provenance/fallback metadata.

Materialization clones the embedded bundle, checks out the exact captured HEAD,
restores the archived working tree, and verifies the resulting snapshot digest.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from worktree_snapshot import capture_worktree

DEFAULT_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_BUNDLE_BYTES = 250 * 1024 * 1024


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
    if not path or rel.is_absolute() or ".." in rel.parts or path == ".":
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
    depth = 0
    for part in parent.joinpath(PurePosixPath(target)).parts:
        if part == "..":
            depth -= 1
        elif part not in {"", "."}:
            depth += 1
        if depth < 0:
            raise ValueError("archive_symlink_target_escapes_root")


def _capture_bundle(repo: Path, head_sha: str, max_bundle_bytes: int) -> bytes:
    if not isinstance(max_bundle_bytes, int) or max_bundle_bytes <= 0:
        raise ValueError("bundle_max_bytes_invalid")
    fd, name = tempfile.mkstemp(prefix="checkpoint-", suffix=".bundle")
    os.close(fd)
    bundle = Path(name)
    try:
        # HEAD is named explicitly so clone can resolve the bundle; the exact
        # captured SHA remains the checkout authority after clone.
        proc = subprocess.run(
            ["git", "bundle", "create", str(bundle), "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError("git_bundle_create_failed:" + (proc.stderr or proc.stdout)[:500])
        data = bundle.read_bytes()
        if not data:
            raise ValueError("git_bundle_empty")
        if len(data) > max_bundle_bytes:
            raise ValueError("git_bundle_size_limit_exceeded")
        verify = subprocess.run(
            ["git", "bundle", "verify", str(bundle)],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if verify.returncode != 0:
            raise ValueError("git_bundle_verify_failed:" + (verify.stderr or verify.stdout)[:500])
        # Ensure the exact head we claim is contained in the bundle's advertised refs.
        heads = subprocess.run(
            ["git", "bundle", "list-heads", str(bundle)],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if heads.returncode != 0 or head_sha not in heads.stdout:
            raise ValueError("git_bundle_missing_captured_head")
        return data
    finally:
        bundle.unlink(missing_ok=True)


@dataclass(frozen=True)
class WorkspaceArchive:
    source_repository: str
    head_sha: str
    snapshot_digest: str
    entries: tuple[dict[str, Any], ...]
    content_bytes: int
    git_bundle_sha256: str
    git_bundle_bytes: int
    git_bundle_base64: str
    archive_digest: str

    def as_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        entries = []
        for row in self.entries:
            item = dict(row)
            if not include_content:
                item.pop("content_base64", None)
            entries.append(item)
        value = {
            "schema": "glaciereq.workspace-archive.v2",
            "source_repository": self.source_repository,
            "head_sha": self.head_sha,
            "snapshot_digest": self.snapshot_digest,
            "entries": entries,
            "content_bytes": self.content_bytes,
            "git_bundle_sha256": self.git_bundle_sha256,
            "git_bundle_bytes": self.git_bundle_bytes,
            "archive_digest": self.archive_digest,
        }
        if include_content:
            value["git_bundle_base64"] = self.git_bundle_base64
        return value

    def to_bytes(self) -> bytes:
        return (json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "WorkspaceArchive":
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != "glaciereq.workspace-archive.v2":
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
            git_bundle_sha256=str(payload.get("git_bundle_sha256") or ""),
            git_bundle_bytes=int(payload.get("git_bundle_bytes", 0)),
            git_bundle_base64=str(payload.get("git_bundle_base64") or ""),
            archive_digest=str(payload.get("archive_digest") or ""),
        )
        archive.validate()
        return archive

    def bundle_bytes_value(self) -> bytes:
        try:
            data = base64.b64decode(self.git_bundle_base64, validate=True)
        except Exception as exc:
            raise ValueError("git_bundle_content_invalid") from exc
        if len(data) != self.git_bundle_bytes:
            raise ValueError("git_bundle_size_mismatch")
        if _sha(data) != self.git_bundle_sha256:
            raise ValueError("git_bundle_digest_mismatch")
        return data

    def validate(self) -> None:
        if not self.source_repository:
            raise ValueError("archive_source_missing")
        if len(self.head_sha) != 40 or any(ch not in "0123456789abcdef" for ch in self.head_sha):
            raise ValueError("archive_head_sha_invalid")
        if len(self.snapshot_digest) != 64:
            raise ValueError("archive_snapshot_digest_invalid")
        if len(self.git_bundle_sha256) != 64 or self.git_bundle_bytes <= 0:
            raise ValueError("git_bundle_metadata_invalid")
        self.bundle_bytes_value()

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
                    content = base64.b64decode(encoded, validate=True)
                except Exception as exc:
                    raise ValueError("archive_file_content_invalid") from exc
                if raw.get("sha256") != _sha(content):
                    raise ValueError("archive_file_digest_mismatch")
                total += len(content)
                canonical_entries.append({"path": path, "kind": kind, "mode": mode, "size": len(content), "sha256": _sha(content)})
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
            "git_bundle_sha256": self.git_bundle_sha256,
            "git_bundle_bytes": self.git_bundle_bytes,
        }
        if _digest(core) != self.archive_digest:
            raise ValueError("archive_digest_mismatch")


def capture_workspace_archive(
    repo: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> WorkspaceArchive:
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
            content = path.read_bytes()
            total += len(content)
            if total > max_bytes:
                raise ValueError("archive_size_limit_exceeded")
            digest = _sha(content)
            entries.append({
                "path": rel,
                "kind": "file",
                "mode": mode,
                "size": len(content),
                "sha256": digest,
                "content_base64": base64.b64encode(content).decode("ascii"),
            })
            canonical_entries.append({"path": rel, "kind": "file", "mode": mode, "size": len(content), "sha256": digest})

    bundle = _capture_bundle(root, snapshot.head_sha, max_bundle_bytes)
    bundle_digest = _sha(bundle)
    source_repository = _source(root)
    core = {
        "source_repository": source_repository,
        "head_sha": snapshot.head_sha,
        "snapshot_digest": snapshot.snapshot_digest,
        "entries": canonical_entries,
        "content_bytes": total,
        "git_bundle_sha256": bundle_digest,
        "git_bundle_bytes": len(bundle),
    }
    archive = WorkspaceArchive(
        source_repository=source_repository,
        head_sha=snapshot.head_sha,
        snapshot_digest=snapshot.snapshot_digest,
        entries=tuple(entries),
        content_bytes=total,
        git_bundle_sha256=bundle_digest,
        git_bundle_bytes=len(bundle),
        git_bundle_base64=base64.b64encode(bundle).decode("ascii"),
        archive_digest=_digest(core),
    )
    archive.validate()
    return archive


def materialize_workspace(archive: WorkspaceArchive, destination: str | Path) -> dict[str, Any]:
    archive.validate()
    dest = Path(destination).resolve()
    if dest.exists() and any(dest.iterdir()):
        raise ValueError("materialization_destination_not_empty")
    if dest.exists():
        dest.rmdir()
    dest.parent.mkdir(parents=True, exist_ok=True)

    fd, name = tempfile.mkstemp(prefix="materialize-", suffix=".bundle", dir=str(dest.parent))
    os.close(fd)
    bundle_path = Path(name)
    bundle_path.write_bytes(archive.bundle_bytes_value())
    try:
        proc = subprocess.run(
            ["git", "clone", "--no-checkout", "--quiet", str(bundle_path), str(dest)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError("materialization_bundle_clone_failed:" + (proc.stderr or proc.stdout)[:500])
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
                content = base64.b64decode(entry["content_base64"], validate=True)
                if _sha(content) != entry["sha256"]:
                    raise ValueError("materialization_file_digest_mismatch")
                path.write_bytes(content)
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
            "git_bundle_sha256": archive.git_bundle_sha256,
            "source_repository": archive.source_repository,
            "verified": True,
        }
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        bundle_path.unlink(missing_ok=True)
