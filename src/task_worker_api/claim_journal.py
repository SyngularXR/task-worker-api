"""One durable logical claim per serial worker, independent of HTTP retries."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

from .resources import AdmissionError, ClaimRequest, ClaimResult


class ClaimJournal:
    """Use a private persistent worker directory, never an attempt workdir.

SQLite FULL synchronization commits the request before callers can send it.
The worker must reconcile an existing entry on restart before requesting work.
"""

    def __init__(self, path: Path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS claim (id INTEGER PRIMARY KEY CHECK(id=1), request TEXT NOT NULL, response TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS operation (kind TEXT PRIMARY KEY, operation_id TEXT NOT NULL, payload TEXT NOT NULL, response TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS operation_request (kind TEXT PRIMARY KEY, operation_id TEXT NOT NULL, body TEXT NOT NULL)")

    def pending(self) -> tuple[ClaimRequest, dict | None] | None:
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT request,response FROM claim WHERE id=1").fetchone()
        if row is None:
            return None
        return ClaimRequest.model_validate_json(row[0]), json.loads(row[1]) if row[1] else None

    def prepare(self, worker_instance_id: UUID, task_types: frozenset[str]) -> ClaimRequest:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request FROM claim WHERE id=1").fetchone()
            if row:
                request = ClaimRequest.model_validate_json(row[0])
                if request.worker_instance_id != worker_instance_id or request.task_types != task_types:
                    raise AdmissionError("previous_claim_unresolved")
            else:
                request = ClaimRequest(protocol_version=2, worker_instance_id=worker_instance_id,
                                       claim_request_id=uuid4(), task_types=task_types)
                db.execute("INSERT INTO claim VALUES(1,?,NULL)", [request.model_dump_json()])
        return request

    def record_response(self, request_id: UUID, result: ClaimResult | None) -> None:
        payload = {"claim": result.model_dump(mode="json") if result else None}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request,response FROM claim WHERE id=1").fetchone()
            if not row or ClaimRequest.model_validate_json(row[0]).claim_request_id != request_id:
                raise AdmissionError("claim_request_unknown")
            if row[1]:
                old = json.loads(row[1])["claim"]
                new = payload["claim"]
                if old is None or new is None:
                    if old != new:
                        raise AdmissionError("idempotency_conflict")
                elif ({k: v for k, v in old.items() if k not in ("state", "lease_expires_at")}
                      != {k: v for k, v in new.items() if k not in ("state", "lease_expires_at")}):
                    raise AdmissionError("idempotency_conflict")
            db.execute("UPDATE claim SET response=? WHERE id=1", [json.dumps(payload)])

    def acknowledge_no_work(self, request_id: UUID) -> None:
        """Claimed attempts stay journaled until the lifecycle integration reconciles them."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request,response FROM claim WHERE id=1").fetchone()
            if not row or ClaimRequest.model_validate_json(row[0]).claim_request_id != request_id:
                raise AdmissionError("claim_request_unknown")
            if row[1] is None or json.loads(row[1]) != {"claim": None}:
                raise AdmissionError("previous_claim_unresolved")
            db.execute("DELETE FROM claim WHERE id=1")

    def prepare_operation(self, kind: str, payload: dict, *, request: dict) -> UUID:
        """One unresolved logical operation per kind in the current serial attempt."""
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            claim = db.execute("SELECT response FROM claim WHERE id=1").fetchone()
            if not claim or not claim[0] or not json.loads(claim[0])["claim"]:
                raise AdmissionError("claim_request_unknown")
            row = db.execute("SELECT operation_id,payload,response FROM operation WHERE kind=?", [kind]).fetchone()
            if row:
                rejected = row[2] and json.loads(row[2]).get("rejected") in ("hardware_report_stale", "hardware_report_replayed", "cleanup_evidence_stale")
                if rejected:
                    # Backend explicitly rejected before committing the operation.
                    # The next observation can safely form a new logical request.
                    db.execute("DELETE FROM operation WHERE kind=?", [kind])
                    db.execute("DELETE FROM operation_request WHERE kind=?", [kind])
                elif json.loads(row[1]) != payload:
                    if kind != "release" or not row[2] or json.loads(row[2]).get("state") != "recovering":
                        raise AdmissionError("idempotency_conflict")
                    # The old release was acknowledged as incomplete cleanup.
                    # New evidence is a new operation; ambiguous writes never rotate.
                    db.execute("DELETE FROM operation WHERE kind=?", [kind])
                    db.execute("DELETE FROM operation_request WHERE kind=?", [kind])
                else:
                    return UUID(row[0])
            operation_id = uuid4()
            db.execute("INSERT INTO operation VALUES(?,?,?,NULL)", [kind, str(operation_id), encoded])
            body = json.dumps({**request, "operation_id": str(operation_id)}, sort_keys=True, allow_nan=False)
            db.execute("INSERT INTO operation_request VALUES(?,?,?)", [kind, str(operation_id), body])
        return operation_id

    def operation_request(self, kind: str, operation_id: UUID) -> dict:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT body FROM operation_request WHERE kind=? AND operation_id=?",
                             [kind, str(operation_id)]).fetchone()
        if row is None:
            raise AdmissionError("operation_request_missing")
        return json.loads(row[0])

    def unresolved_operations(self) -> list[tuple[str, UUID]]:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT kind,operation_id FROM operation WHERE response IS NULL ORDER BY rowid").fetchall()
        return [(kind, UUID(operation_id)) for kind, operation_id in rows]

    def record_operation(self, kind: str, operation_id: UUID, response: dict) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            updated = db.execute("UPDATE operation SET response=? WHERE kind=? AND operation_id=?",
                                 [json.dumps(response), kind, str(operation_id)])
            if updated.rowcount != 1:
                raise AdmissionError("operation_unknown")

    def acknowledge_release(self, attempt_id: UUID) -> None:
        """Only an acknowledged backend release permits a new logical claim."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT response FROM operation WHERE kind='release'").fetchone()
            if not row or not row[0]:
                raise AdmissionError("previous_claim_unresolved")
            response = json.loads(row[0])
            if response.get("state") != "released" or response.get("attempt_id") != str(attempt_id):
                raise AdmissionError("previous_claim_unresolved")
            claim = json.loads(db.execute("SELECT response FROM claim WHERE id=1").fetchone()[0])["claim"]
            if claim["ownership"]["attempt_id"] != str(attempt_id):
                raise AdmissionError("attempt_fenced")
            db.execute("DELETE FROM operation")
            db.execute("DELETE FROM operation_request")
            db.execute("DELETE FROM claim")
