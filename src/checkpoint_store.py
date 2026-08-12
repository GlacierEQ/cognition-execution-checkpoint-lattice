"""Durable local-first checkpoint store with content-addressed artifacts.

Metadata lives in SQLite using WAL + immediate write transactions; workspace
archives live as immutable digest-addressed blobs. Checkpoint ids are immutable:
replaying the exact record is idempotent, while conflicting content under an
existing id is refused. Verified children may only be stored when every parent
already exists and is itself verified.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from execution_checkpoint_lattice import Checkpoint, CheckpointStatus
from execution_receipt import execute_verification_plan, verification_digest
from workspace_archive import WorkspaceArchive, capture_workspace_archive


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class CheckpointStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = self.root / "blobs"
        self.blobs.mkdir(exist_ok=True)
        self.db = self.root / "checkpoints.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    checkpoint_digest TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL,
                    proof_digest TEXT NOT NULL,
                    proof_json TEXT NOT NULL,
                    archive_digest TEXT NOT NULL,
                    archive_blob_digest TEXT,
                    state_digest TEXT NOT NULL,
                    verification_digest TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(checkpoints)").fetchall()
            }
            if "archive_blob_digest" not in columns:
                connection.execute("ALTER TABLE checkpoints ADD COLUMN archive_blob_digest TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_checkpoints_state_digest ON checkpoints(state_digest)"
            )

    def put_blob(self, data: bytes) -> str:
        digest = _sha(data)
        path = self.blobs / digest
        if path.exists():
            if _sha(path.read_bytes()) != digest:
                raise ValueError("stored_blob_corrupt")
            return digest
        temp = self.blobs / f".{digest}.{os.getpid()}.tmp"
        temp.write_bytes(data)
        if _sha(temp.read_bytes()) != digest:
            temp.unlink(missing_ok=True)
            raise ValueError("blob_write_verification_failed")
        try:
            os.replace(temp, path)
        except FileExistsError:
            temp.unlink(missing_ok=True)
        return digest

    def get_blob(self, digest: str) -> bytes:
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("blob_digest_invalid")
        path = self.blobs / digest
        if not path.is_file():
            raise KeyError(f"blob_missing:{digest}")
        data = path.read_bytes()
        if _sha(data) != digest:
            raise ValueError("stored_blob_corrupt")
        return data

    def _validate_parents(self, connection: sqlite3.Connection, checkpoint: Checkpoint) -> None:
        for parent_id in checkpoint.parent_ids:
            row = connection.execute(
                "SELECT checkpoint_json FROM checkpoints WHERE checkpoint_id=?",
                (parent_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"parent_missing:{parent_id}")
            parent = Checkpoint.from_dict(json.loads(row["checkpoint_json"]))
            if parent.status is not CheckpointStatus.VERIFIED:
                raise ValueError(f"parent_not_verified:{parent_id}")

    def save(self, checkpoint: Checkpoint, proof: Mapping[str, Any], archive: WorkspaceArchive) -> str:
        archive.validate()
        if checkpoint.status is not CheckpointStatus.VERIFIED:
            raise ValueError("store_requires_verified_checkpoint")
        if checkpoint.state_digest != archive.snapshot_digest:
            raise ValueError("checkpoint_state_archive_mismatch")
        if checkpoint.verification_digest is None:
            raise ValueError("checkpoint_verification_digest_missing")
        if checkpoint.artifacts.get("workspace_archive") != archive.archive_digest:
            raise ValueError("checkpoint_archive_artifact_mismatch")
        if checkpoint.artifacts.get("verification_plan") != checkpoint.verification_digest:
            raise ValueError("checkpoint_verification_artifact_mismatch")
        proof_doc = dict(proof)
        if proof_doc.get("checkpoint") != checkpoint.as_dict():
            raise ValueError("checkpoint_proof_mismatch")

        checkpoint_doc = checkpoint.as_dict()
        checkpoint_json = json.dumps(checkpoint_doc, sort_keys=True, separators=(",", ":"))
        checkpoint_digest = _digest(checkpoint_doc)
        proof_json = json.dumps(proof_doc, sort_keys=True, separators=(",", ":"), allow_nan=False)
        proof_digest = _sha(proof_json.encode("utf-8"))
        archive_bytes = archive.to_bytes()
        archive_blob_digest = self.put_blob(archive_bytes)
        if archive_blob_digest != _sha(archive_bytes):
            raise ValueError("archive_blob_digest_internal_error")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_parents(connection, checkpoint)
            existing = connection.execute(
                "SELECT checkpoint_digest, proof_digest, archive_digest, archive_blob_digest "
                "FROM checkpoints WHERE checkpoint_id=?",
                (checkpoint.checkpoint_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["checkpoint_digest"] == checkpoint_digest
                    and existing["proof_digest"] == proof_digest
                    and existing["archive_digest"] == archive.archive_digest
                    and (existing["archive_blob_digest"] in {None, archive_blob_digest})
                ):
                    if existing["archive_blob_digest"] is None:
                        connection.execute(
                            "UPDATE checkpoints SET archive_blob_digest=? WHERE checkpoint_id=?",
                            (archive_blob_digest, checkpoint.checkpoint_id),
                        )
                    connection.commit()
                    return checkpoint_digest
                raise ValueError("checkpoint_id_conflict")
            connection.execute(
                """
                INSERT INTO checkpoints(
                    checkpoint_id, checkpoint_digest, checkpoint_json,
                    proof_digest, proof_json, archive_digest, archive_blob_digest,
                    state_digest, verification_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint_digest,
                    checkpoint_json,
                    proof_digest,
                    proof_json,
                    archive.archive_digest,
                    archive_blob_digest,
                    checkpoint.state_digest,
                    checkpoint.verification_digest,
                ),
            )
            connection.commit()
            return checkpoint_digest
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT checkpoint_json FROM checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"checkpoint_missing:{checkpoint_id}")
        return Checkpoint.from_dict(json.loads(row["checkpoint_json"]))

    def get_proof(self, checkpoint_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT proof_json, proof_digest FROM checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"checkpoint_missing:{checkpoint_id}")
        if _sha(row["proof_json"].encode("utf-8")) != row["proof_digest"]:
            raise ValueError("checkpoint_proof_corrupt")
        value = json.loads(row["proof_json"])
        if not isinstance(value, dict):
            raise ValueError("checkpoint_proof_invalid")
        return value

    def get_archive(self, checkpoint_id: str) -> WorkspaceArchive:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT archive_digest, archive_blob_digest FROM checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"checkpoint_missing:{checkpoint_id}")
        wanted_archive_digest = row["archive_digest"]
        blob_digest = row["archive_blob_digest"]
        if blob_digest:
            archive = WorkspaceArchive.from_bytes(self.get_blob(blob_digest))
            if archive.archive_digest != wanted_archive_digest:
                raise ValueError("archive_metadata_digest_mismatch")
            return archive

        # Compatibility recovery for an older store created before direct blob
        # addressing was added. Once found, bind the direct blob digest.
        for path in self.blobs.iterdir():
            if not path.is_file() or len(path.name) != 64:
                continue
            data = self.get_blob(path.name)
            try:
                archive = WorkspaceArchive.from_bytes(data)
            except Exception:
                continue
            if archive.archive_digest == wanted_archive_digest:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE checkpoints SET archive_blob_digest=? WHERE checkpoint_id=?",
                        (path.name, checkpoint_id),
                    )
                return archive
        raise KeyError(f"archive_missing:{wanted_archive_digest}")

    def list_checkpoints(self) -> tuple[Checkpoint, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT checkpoint_json FROM checkpoints ORDER BY checkpoint_id"
            ).fetchall()
        return tuple(Checkpoint.from_dict(json.loads(row["checkpoint_json"])) for row in rows)

    def integrity_report(self) -> dict[str, Any]:
        errors: list[str] = []
        checkpoints = self.list_checkpoints()
        ids = {checkpoint.checkpoint_id for checkpoint in checkpoints}
        for checkpoint in checkpoints:
            try:
                proof = self.get_proof(checkpoint.checkpoint_id)
                archive = self.get_archive(checkpoint.checkpoint_id)
                if proof.get("checkpoint") != checkpoint.as_dict():
                    errors.append(f"proof_mismatch:{checkpoint.checkpoint_id}")
                if archive.snapshot_digest != checkpoint.state_digest:
                    errors.append(f"archive_state_mismatch:{checkpoint.checkpoint_id}")
                for parent_id in checkpoint.parent_ids:
                    if parent_id not in ids:
                        errors.append(f"parent_missing:{checkpoint.checkpoint_id}:{parent_id}")
            except Exception as exc:
                errors.append(f"{checkpoint.checkpoint_id}:{type(exc).__name__}:{exc}")
        core = {
            "checkpoint_count": len(checkpoints),
            "errors": errors,
            "status": "PASS" if not errors else "FAIL",
        }
        return {**core, "integrity_digest": _digest(core)}


def capture_checkpoint_to_store(
    store: CheckpointStore,
    *,
    checkpoint_id: str,
    subgoal: str,
    parent_ids: Iterable[str],
    repo: str | Path,
    verification_commands: Iterable[Mapping[str, Any]],
    reversible: bool = True,
    recovery_cost_units: float = 1.0,
) -> tuple[Checkpoint, dict[str, Any]]:
    archive = capture_workspace_archive(repo)
    receipts = execute_verification_plan(verification_commands, cwd=repo)
    failed = [receipt for receipt in receipts if receipt.status != "PASS"]
    if failed:
        raise ValueError("checkpoint_verification_failed:" + ",".join(receipt.name for receipt in failed))
    verify_digest = verification_digest(receipts)
    checkpoint = Checkpoint(
        checkpoint_id=checkpoint_id,
        subgoal=subgoal,
        parent_ids=tuple(parent_ids),
        state_digest=archive.snapshot_digest,
        verification_digest=verify_digest,
        status=CheckpointStatus.VERIFIED,
        reversible=reversible,
        recovery_cost_units=recovery_cost_units,
        artifacts={
            "workspace_archive": archive.archive_digest,
            "verification_plan": verify_digest,
        },
    )
    proof = {
        "schema": "glaciereq.execution-checkpoint-store-record.v1",
        "checkpoint": checkpoint.as_dict(),
        "archive": archive.as_dict(include_content=False),
        "verification_receipts": [receipt.as_dict() for receipt in receipts],
    }
    store.save(checkpoint, proof, archive)
    return checkpoint, proof
