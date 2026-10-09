"""Private borrowing holds and immutable incident/clear history, outside model mounts."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
from uuid import uuid4

from .errors import ProtocolError
from .borrowing_protocol import (
    BorrowingBinding, BorrowingIncident, BorrowingInspection, SignedBorrowingAck, SignedBorrowingClear,
    SignedBorrowingOwners, fresh_observation, verify_borrowing, sign_borrowing, policy_digest,
)


def incident_digest(incident):
    return hashlib.sha256(incident.model_dump_json().encode()).hexdigest()


class BorrowingJournal:
    def __init__(self, path: Path, binding: BorrowingBinding):
        self.path, self.binding = path, binding
        self._mutex = threading.RLock()
        self._write_failed = False
        self._failed_connection = None

    def _connect(self, *, check_binding=True):
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ProtocolError("borrowing journal must be an owned private regular file")
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0, check_same_thread=False)
        try:
            db.execute("PRAGMA synchronous=FULL")
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise ProtocolError("borrowing journal mode changed; rollback DELETE is required")
            row = db.execute("SELECT payload FROM binding WHERE id=1").fetchone()
            previous = BorrowingBinding.model_validate_json(row[0]) if row else None
            if (previous is None or (previous.host_id, previous.gpu_uuid) != (self.binding.host_id, self.binding.gpu_uuid)
                    or (check_binding and previous != self.binding)):
                raise ProtocolError("borrowing journal binding changed")
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def _write(self, *, check_binding=True):
        with self._mutex:
            if self._write_failed:
                raise ProtocolError("borrowing journal writer failed; no new proof is permitted")
            db = None
            try:
                db = self._connect(check_binding=check_binding)
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except sqlite3.Error:
                self._write_failed = True
                if db is not None:
                    try:
                        db.rollback()
                        db.execute("BEGIN EXCLUSIVE")
                        self._failed_connection, db = db, None
                    except sqlite3.Error:
                        pass
                raise
            finally:
                if db is not None:
                    db.close()

    def prepare(self):
        """Trusted initial preparation only; absent observation is ineligible, not a fault."""
        parent = self.path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077 or parent.st_uid != os.getuid():
            raise ProtocolError("borrowing state parent must be owned and private")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path)) as db, db:
            if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ProtocolError("borrowing preparation requires rollback DELETE mode")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE binding(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)")
            db.execute("CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1),generation INTEGER NOT NULL,owners TEXT,inspection TEXT,inspected_at TEXT,primary_owner INTEGER NOT NULL,established INTEGER NOT NULL,received_owners TEXT,run_id TEXT,run_pid INTEGER,run_start INTEGER,running INTEGER NOT NULL,proof_run_id TEXT)")
            db.execute("CREATE TABLE incidents(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,ack TEXT)")
            db.execute("CREATE TABLE clears(id TEXT PRIMARY KEY,payload TEXT NOT NULL,history_only INTEGER NOT NULL CHECK(history_only IN (0,1)))")
            db.execute("CREATE TABLE incident_clear(incident TEXT PRIMARY KEY REFERENCES incidents(id),clear_id TEXT NOT NULL REFERENCES clears(id))")
            db.execute("INSERT INTO binding VALUES(1,?)", [self.binding.model_dump_json()])
            db.execute("INSERT INTO state VALUES(1,0,NULL,NULL,NULL,1,0,NULL,NULL,NULL,NULL,0,NULL)")

    def begin_run(self):
        from .borrowing_observer import process_identity
        pid=os.getpid()
        start,_=process_identity(pid)
        with self._write() as db:
            established,running,run_id,owners=db.execute("SELECT established,running,run_id,owners FROM state WHERE id=1").fetchone()
            if established and running:
                old=SignedBorrowingOwners.model_validate_json(owners).observation
                self._fault(db,old.report_sequence,"ownership_unverified",
                    policy_digest({"condition":"observer_unclean_restart","run_id":run_id}),datetime.now(timezone.utc))
            db.execute("UPDATE state SET run_id=?,run_pid=?,run_start=?,running=1,proof_run_id=NULL WHERE id=1",
                [str(uuid4()),pid,start])

    def finish_run(self):
        if self._write_failed:
            return  # Unclean marker remains; no durable incident is asserted for failed persistence.
        with self._write() as db:
            owners=db.execute("SELECT owners FROM state WHERE id=1").fetchone()[0]
            if self._pending(db) or not owners:
                return
            view=SignedBorrowingOwners.model_validate_json(owners).observation
            if (not fresh_observation(datetime.now(timezone.utc),view.server_time)
                    or any(owner.role=="inference" for owner in view.owners)):
                return
            db.execute("UPDATE state SET running=0 WHERE id=1")

    @staticmethod
    def _pending(db):
        return db.execute("SELECT i.id,i.payload,i.digest,i.ack FROM incidents i LEFT JOIN incident_clear c ON c.incident=i.id WHERE c.incident IS NULL ORDER BY i.rowid").fetchall()

    def pending(self):
        with self._mutex, closing(self._connect()) as db:
            return [BorrowingIncident.model_validate_json(row[1]) for row in self._pending(db)]

    def awaiting_delivery(self):
        with self._mutex, closing(self._connect(check_binding=False)) as db:
            return [BorrowingIncident.model_validate_json(row[1]) for row in self._pending(db) if row[3] is None]

    def has_observation(self):
        with self._mutex, closing(self._connect()) as db:
            return db.execute("SELECT established FROM state WHERE id=1").fetchone()[0] == 1

    def _fault(self,db,sequence,reason,evidence_digest,observed_at):
        for row in self._pending(db):
            old=BorrowingIncident.model_validate_json(row[1])
            if old.reason==reason and old.evidence_digest==evidence_digest:
                return old
        generation=db.execute("SELECT generation FROM state WHERE id=1").fetchone()[0]+1
        incident=BorrowingIncident(binding=self.binding,incident_id=uuid4(),hold_generation=generation,
            observed_sequence=sequence,observed_at=observed_at,reason=reason,evidence_digest=evidence_digest)
        db.execute("INSERT INTO incidents VALUES(?,?,?,NULL)",[str(incident.incident_id),incident.model_dump_json(),incident_digest(incident)])
        db.execute("UPDATE state SET generation=? WHERE id=1",[generation])
        return incident

    def fault(self,sequence,reason,evidence_digest,*,observed_at=None):
        with self._write() as db:
            return self._fault(db,sequence,reason,evidence_digest,observed_at or datetime.now(timezone.utc))

    def record_owner_view(self, signed, key, *, now=None):
        now = now or datetime.now(timezone.utc)
        view = verify_borrowing(signed,key,self.binding)
        if not fresh_observation(now,view.server_time) or not fresh_observation(now,view.report_captured_at):
            raise ProtocolError("authority owner ACK is stale")
        with self._write() as db:
            old = db.execute("SELECT received_owners FROM state WHERE id=1").fetchone()[0]
            if old:
                previous = SignedBorrowingOwners.model_validate_json(old).observation
                if view.report_sequence < previous.report_sequence or view.server_time < previous.server_time:
                    raise ProtocolError("authority owner ACK was replayed")
            db.execute("UPDATE state SET received_owners=? WHERE id=1",[signed.model_dump_json()])

    def owner_view(self):
        with self._mutex, closing(self._connect()) as db:
            row=db.execute("SELECT received_owners FROM state WHERE id=1").fetchone()[0]
        return SignedBorrowingOwners.model_validate_json(row) if row else None

    def observe(self, signed: SignedBorrowingOwners, key, inspection: dict, *, primary_owner: bool, now=None):
        now = now or datetime.now(timezone.utc)
        view = verify_borrowing(signed, key, self.binding)
        if not view.complete or not fresh_observation(now, view.server_time) or not fresh_observation(now, view.report_captured_at):
            raise ProtocolError("borrowing owner observation is incomplete or stale")
        with self._write() as db:
            previous = db.execute("SELECT owners FROM state WHERE id=1").fetchone()[0]
            if previous:
                old = SignedBorrowingOwners.model_validate_json(previous).observation
                if view.report_sequence < old.report_sequence or view.server_time < old.server_time:
                    raise ProtocolError("borrowing owner observation was replayed")
                if view.report_sequence == old.report_sequence and view.report_captured_at != old.report_captured_at:
                    raise ProtocolError("borrowing owner sequence conflicts")
            db.execute("UPDATE state SET owners=?,inspection=?,inspected_at=?,primary_owner=?,established=established OR ?,proof_run_id=run_id WHERE id=1",
                [signed.model_dump_json(), json.dumps(inspection, sort_keys=True, separators=(",", ":")), now.isoformat(), primary_owner, not primary_owner])

    def observation_time(self,key,*,after,attempt_id=None,now=None):
        now=now or datetime.now(timezone.utc)
        try:
            with self._mutex, closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                if self._pending(db):
                    return None
                row=db.execute("SELECT owners,inspected_at,primary_owner,established,run_id,run_pid,run_start,running,proof_run_id FROM state WHERE id=1").fetchone()
            owners,inspected_at,primary,established,run_id,pid,start,running,proof_run=row
            if not established or not owners or not inspected_at or primary or not running or proof_run!=run_id:
                return None
            from .borrowing_observer import process_identity
            if process_identity(pid)[0]!=start:
                return None
            view=verify_borrowing(SignedBorrowingOwners.model_validate_json(owners),key,self.binding)
            if any(owner.role == "inference" and (owner.state not in ("reserved","running")
                    or owner.reclaim_deadline is not None or owner.lease_expires_at <= now) for owner in view.owners):
                return None
            if attempt_id is not None and not any(owner.role == "inference" and owner.attempt_id == attempt_id
                    and owner.worker_instance_id == self.binding.worker_instance_id
                    and owner.service_id == self.binding.service_id for owner in view.owners):
                return None
            inspected=datetime.fromisoformat(inspected_at)
            if (view.complete and not view.disabled and inspected>after and fresh_observation(now,view.server_time)
                    and fresh_observation(now,view.report_captured_at) and fresh_observation(now,inspected)):
                return inspected
            return None
        except (OSError,sqlite3.Error,ValueError,ProtocolError):
            return None

    def eligible(self,key,*,after,now=None):
        return self.observation_time(key,after=after,now=now) is not None

    def acknowledge(self, signed: SignedBorrowingAck, key):
        ack = verify_borrowing(signed, key, signed.observation.incident.binding)
        with self._write(check_binding=False) as db:
            row = db.execute("SELECT payload,ack FROM incidents WHERE id=?", [str(ack.incident.incident_id)]).fetchone()
            if row is None or BorrowingIncident.model_validate_json(row[0]) != ack.incident:
                raise ProtocolError("borrowing ACK does not match retained incident")
            if row[1] is not None and SignedBorrowingAck.model_validate_json(row[1]) != signed:
                raise ProtocolError("borrowing ACK changed")
            db.execute("UPDATE incidents SET ack=? WHERE id=? AND ack IS NULL", [signed.model_dump_json(), str(ack.incident.incident_id)])

    def inspection(self, owners, reporter_key, physical: dict, *, primary_owner, now=None):
        """Trusted finite requalification inspection; no journal rebind or clear."""
        now = now or datetime.now(timezone.utc)
        view = verify_borrowing(owners, reporter_key, self.binding)
        if (primary_owner or view.owners or physical != {"gpu_uuid":self.binding.gpu_uuid,"processes":[]}
                or not view.complete or not fresh_observation(now, view.server_time)
                or not fresh_observation(now, view.report_captured_at)):
            raise ProtocolError("requalification requires fresh complete idle ownership")
        with self._mutex, closing(self._connect(check_binding=False)) as db:
            db.execute("BEGIN")
            generation = db.execute("SELECT generation FROM state WHERE id=1").fetchone()[0]
            previous = BorrowingBinding.model_validate_json(db.execute("SELECT payload FROM binding WHERE id=1").fetchone()[0])
            pending = {row[0]: row[2] for row in self._pending(db)}
        return sign_borrowing(BorrowingInspection(binding=self.binding,
            previous_binding_digest=policy_digest(previous.model_dump(mode="json")),
            hold_generation=generation, incidents=pending, owners=owners, inspected_at=now,
            inspection_digest=policy_digest(physical), idle=True), reporter_key)

    def apply_clear(self, signed: SignedBorrowingClear, key, reporter_key,
                    owners: SignedBorrowingOwners, physical: dict, *, primary_owner, now=None):
        clear = verify_borrowing(signed, key, self.binding)
        now = now or datetime.now(timezone.utc)
        view = verify_borrowing(owners, reporter_key, self.binding)
        if (not fresh_observation(now, clear.response_at) or primary_owner or view.owners
                or physical != {"gpu_uuid":self.binding.gpu_uuid,"processes":[]} or not view.complete
                or not fresh_observation(now, view.server_time) or not fresh_observation(now, view.report_captured_at)
                or view.report_sequence < clear.report_sequence or policy_digest(physical) != clear.inspection_digest):
            raise ProtocolError("borrowing clear lacks matching fresh idle inspection")
        with self._write(check_binding=False) as db:
            generation = db.execute("SELECT generation FROM state WHERE id=1").fetchone()[0]
            previous = BorrowingBinding.model_validate_json(db.execute("SELECT payload FROM binding WHERE id=1").fetchone()[0])
            if generation != clear.hold_generation:
                raise ProtocolError("borrowing clear misses a newer local incident")
            prior = db.execute("SELECT payload FROM clears WHERE id=?", [str(clear.clear_id)]).fetchone()
            if prior:
                original = SignedBorrowingClear.model_validate_json(prior[0]).observation
                if original.model_dump(exclude={"response_at"}) != clear.model_dump(exclude={"response_at"}):
                    raise ProtocolError("borrowing clear identity conflicts")
                return
            if policy_digest(previous.model_dump(mode="json")) != clear.previous_binding_digest:
                raise ProtocolError("borrowing clear previous binding differs")
            pending = {row[0]: row[2] for row in self._pending(db)}
            if pending != {str(k): v for k, v in clear.incidents.items()}:
                raise ProtocolError("borrowing clear does not match exact retained incidents")
            db.execute("INSERT INTO clears VALUES(?,?,0)", [str(clear.clear_id), signed.model_dump_json()])
            for incident in pending:
                db.execute("INSERT INTO incident_clear VALUES(?,?)", [incident, str(clear.clear_id)])
            # The signed clear confirms authority delivery even when the original ACK was lost.
            db.execute("UPDATE binding SET payload=? WHERE id=1", [self.binding.model_dump_json()])
            db.execute("UPDATE state SET owners=?,inspection=?,inspected_at=?,primary_owner=0,established=1,running=0,proof_run_id=NULL WHERE id=1",
                [owners.model_dump_json(), json.dumps(physical,sort_keys=True,separators=(",", ":")), now.isoformat()])

    def reconcile_clear_history(self,signed: SignedBorrowingClear,key):
        """Confirm an older strict subset only; never rebind or clear the newer hold."""
        clear = verify_borrowing(signed,key,signed.observation.binding)
        if (clear.binding.authority_id,clear.binding.host_id,clear.binding.gpu_uuid) != (self.binding.authority_id,self.binding.host_id,self.binding.gpu_uuid):
            raise ProtocolError("historical clear differs from retained host GPU")
        named = {str(k):v for k,v in clear.incidents.items()}
        with self._write(check_binding=False) as db:
            for identity,digest in named.items():
                row = db.execute("SELECT payload,digest FROM incidents WHERE id=?",[identity]).fetchone()
                if row is None or row[1] != digest:
                    raise ProtocolError("historical clear does not match retained incident")
                incident = BorrowingIncident.model_validate_json(row[0])
                if (incident.hold_generation > clear.hold_generation
                        or (incident.binding.authority_id,incident.binding.host_id,incident.binding.gpu_uuid)
                        != (clear.binding.authority_id,clear.binding.host_id,clear.binding.gpu_uuid)):
                    raise ProtocolError("historical clear incident binding differs")
            prior = db.execute("SELECT payload,history_only FROM clears WHERE id=?",[str(clear.clear_id)]).fetchone()
            if prior:
                original = SignedBorrowingClear.model_validate_json(prior[0]).observation
                if (prior[1] != 1 or original.model_dump(exclude={"response_at"}) != clear.model_dump(exclude={"response_at"})):
                    raise ProtocolError("historical clear identity conflicts")
                return
            generation = db.execute("SELECT generation FROM state WHERE id=1").fetchone()[0]
            pending = {row[0]:row[2] for row in self._pending(db)}
            if generation <= clear.hold_generation or not set(named) < set(pending) or any(pending[k] != v for k,v in named.items()):
                raise ProtocolError("historical clear must leave a newer retained hold")
            db.execute("INSERT INTO clears VALUES(?,?,1)",[str(clear.clear_id),signed.model_dump_json()])
            for identity in named:
                db.execute("INSERT INTO incident_clear VALUES(?,?)",[identity,str(clear.clear_id)])
