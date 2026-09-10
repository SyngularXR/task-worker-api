"""Durable native launch, tree-exit evidence and owned scratch/profile cleanup.

The supervisor owns the database, job and AppContainer. Keep private supervisor
state outside every worker grant. Existing attempts are reconciled,
never launched again, including after an ambiguous create/resume outcome.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

from .host_reporter import physical_boot_id
from uuid import UUID

from .resources import AdmissionError, CleanupEvidence
from .resource_protocol import CleanupObservation, SignedCleanup, observation_signature
from .windows_job import WindowsAppContainer, reconcile_windows_process


class WindowsLaunchJournal:
    def __init__(self, path, *, work_root, authority_id, epoch, host_id, boot_id, execution_scope):
        self.path = Path(path).resolve()
        self.root = Path(work_root).resolve(strict=True)
        if (not self.root.is_dir() or self.root == Path(self.root.anchor)
                or self.path.is_relative_to(self.root)):
            raise ValueError('Unsafe Windows attempt storage root')
        self.host_id, self.boot_id = host_id, boot_id
        self.jobs = {}
        self.prepared = set()
        self.binding = dict(authority=str(authority_id), epoch=epoch, host=str(host_id),
                            boot=str(boot_id), scope=execution_scope, root=str(self.root))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS windows_launches (
                attempt TEXT PRIMARY KEY, binding TEXT NOT NULL, plan TEXT NOT NULL,
                phase TEXT NOT NULL, pid INTEGER, creation_time INTEGER)''')

    def _binding(self, claim):
        if (str(claim.host_id) != self.binding['host'] or str(claim.boot_id) != self.binding['boot']
                or claim.execution_scope != self.binding['scope']
                or physical_boot_id(self.host_id, Path('/proc')) != self.boot_id):
            raise AdmissionError('attempt_fenced')
        return json.dumps(dict(**self.binding, ownership=claim.ownership.model_dump(mode='json'),
            profile=claim.profile.model_dump(mode='json'), input_digest=claim.input_digest,
            gpu=claim.gpu_uuid), sort_keys=True)

    def row(self, claim):
        binding = self._binding(claim)
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT binding,plan,phase,pid,creation_time FROM windows_launches WHERE attempt=?',
                             [str(claim.ownership.attempt_id)]).fetchone()
        if row is None:
            raise AdmissionError('launch_unknown')
        if row[0] != binding:
            raise AdmissionError('attempt_fenced')
        return dict(plan=json.loads(row[1]), phase=row[2], pid=row[3], creation_time=row[4])

    def _record(self, claim, previous, phase, *, pid=None, creation_time=None):
        allowed = {'preparing': {'profile_unknown'}, 'creating': {'suspended'}, 'suspended': {'starting', 'stopping'},
                   'starting': {'started', 'stopping'}, 'started': {'stopping'},
                   'stopping': {'root_exited'}, 'root_exited': set(),
                   'tree_exited': {'scratch_cleaned'}, 'scratch_cleaned': {'profile_cleaned'},
                   'profile_cleaned': {'access_cleaned'}, 'access_cleaned': set()}
        if phase not in allowed.get(previous, set()):
            raise AdmissionError('launch_requires_reconciliation')
        binding = self._binding(claim)
        with closing(sqlite3.connect(self.path)) as db, db:
            changed = db.execute('''UPDATE windows_launches SET phase=?,pid=COALESCE(?,pid),
                creation_time=COALESCE(?,creation_time) WHERE attempt=? AND binding=? AND phase=?''',
                [phase, pid, creation_time, str(claim.ownership.attempt_id), binding, previous]).rowcount
            if changed != 1:
                raise AdmissionError('launch_requires_reconciliation')

    def prepare(self, claim):
        """Record ownership before creating scratch or an AppContainer profile."""
        binding = self._binding(claim)
        if claim.state != 'reserved':
            raise AdmissionError('launch_requires_reserved_attempt')
        attempt = str(claim.ownership.attempt_id)
        directory = self.root / attempt
        if directory.resolve() != directory or os.path.lexists(directory):
            raise AdmissionError('unsafe_cleanup_path')
        plan = dict(cwd=str(directory), app_container='surgiclaw.' + claim.ownership.attempt_id.hex,
                    grants=[])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM windows_launches WHERE attempt=?', [attempt]).fetchone():
                raise AdmissionError('launch_requires_reconciliation')
            db.execute("INSERT INTO windows_launches VALUES(?,?,?,'preparing',NULL,NULL)",
                       [attempt, binding, json.dumps(plan, sort_keys=True)])
        self.prepared.add(attempt)
        return plan

    def launch(self, claim, job, *, app_container, command, cwd, environment, log_path,
               read_only_paths, journal_directory):
        binding = self._binding(claim)
        if claim.state != 'reserved':
            raise AdmissionError('launch_requires_reserved_attempt')
        if not isinstance(app_container, WindowsAppContainer) or not app_container.sid:
            raise ValueError('Native admitted launch requires an open AppContainer')
        if app_container.name != 'surgiclaw.' + claim.ownership.attempt_id.hex:
            raise ValueError('AppContainer identity must belong to this attempt')
        if job.processes or job.pids():
            raise ValueError('Native attempt requires a fresh job')
        limits = job.limits()
        cpus = os.cpu_count()
        if (not cpus or limits['memory_bytes'] > claim.profile.execution_ram_mib * 1024 * 1024
                or limits['cpu_rate'] > claim.profile.cpu_millicores * 10 // cpus
                or limits['cpu_flags'] != 5 or limits['flags'] & (0x800 | 0x1000)
                or limits['flags'] & 0x2208 != 0x2208 or limits['process_limit'] > 512):
            raise ValueError('Windows job exceeds or lacks claimed limits')
        cwd = Path(cwd).absolute()
        expected = self.root / str(claim.ownership.attempt_id)
        if cwd != expected or cwd.resolve(strict=True) != expected:
            raise ValueError('Working directory must be the owned attempt directory')
        executable = Path(command[0]).resolve(strict=True)
        grants = []
        if not Path(journal_directory).is_dir():
            raise ValueError('Worker journal requires a persistent directory')
        paths = [(source, 'RX') for source in read_only_paths] + [(journal_directory, 'M')]
        for source, access in paths:
            source = Path(source).absolute()
            if (source.resolve(strict=True) != source or source == Path(source.anchor)
                    or self.path.is_relative_to(source) or self.root.is_relative_to(source)
                    or source.is_relative_to(self.root)):
                raise ValueError('Unsafe worker grant')
            if any(source.is_relative_to(Path(grant['path'])) or Path(grant['path']).is_relative_to(source)
                   for grant in grants):
                raise ValueError('Worker grant paths must not overlap')
            identity = source.stat()
            grants.append(dict(path=str(source), access=access, device=identity.st_dev, inode=identity.st_ino))
        plan = dict(command=list(command), cwd=str(cwd), environment=dict(environment),
            log_path=str(Path(log_path).resolve()), app_container=app_container.name,
            sid=app_container.sid_string(), grants=grants,
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(), limits=limits)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            attempt = str(claim.ownership.attempt_id)
            if attempt not in self.prepared:
                raise AdmissionError('launch_requires_reconciliation')
            changed = db.execute("UPDATE windows_launches SET plan=?,phase='creating' "
                "WHERE attempt=? AND binding=? AND phase='preparing'",
                [json.dumps(plan, sort_keys=True), attempt, binding]).rowcount
            if changed != 1:
                raise AdmissionError('launch_requires_reconciliation')
        self.prepared.remove(attempt)
        self.jobs[str(claim.ownership.attempt_id)] = job
        # No user code runs before the identity is durably committed.
        try:
            for grant in grants:
                WindowsAppContainer.path_access(grant['path'], plan['sid'], grant['access'])
            WindowsAppContainer.path_access(cwd, plan['sid'], 'M')
            process = job.spawn(command, cwd=cwd, environment=environment,
                                log_path=log_path, app_container=app_container)
            self._record(claim, 'creating', 'suspended', pid=process.pid, creation_time=process.creation_time)
            # Commit uncertainty before ResumeThread, not after it might run.
            self._record(claim, 'suspended', 'starting')
            process.resume()
            self._record(claim, 'starting', 'started')
            return process
        except BaseException:
            if job.handle:
                job.terminate()  # A stop request, never cleanup proof.
            raise

    def reconcile_root(self, claim, *, timeout_ms=10000):
        """Stop the recorded root; True is NOT an attempt/tree cleanup proof."""
        row = self.row(claim)
        if row['phase'] == 'root_exited':
            return True
        if row['phase'] in ('tree_exited', 'scratch_cleaned', 'profile_cleaned', 'access_cleaned'):
            return self.reconcile_tree(claim, timeout_ms=timeout_ms)
        if row['pid'] is None:
            # Could have died between CreateProcess and the identity commit.
            return False
        if row['phase'] != 'stopping':
            self._record(claim, row['phase'], 'stopping')
        exited = reconcile_windows_process(host_id=self.host_id, boot_id=self.boot_id,
            pid=row['pid'], creation_time=row['creation_time'], timeout_ms=timeout_ms, terminate=True)
        if exited:
            self._record(claim, 'stopping', 'root_exited')
        return exited

    def reconcile_tree(self, claim, *, timeout_ms=10000):
        """Persist verified cohort exit; lost job/notification state stays unknown.

        This covers processes, not scratch/profile cleanup or reservation release.
        """
        row = self.row(claim)
        if row['phase'] in ('tree_exited', 'scratch_cleaned', 'profile_cleaned', 'access_cleaned'):
            evidence = row['plan'].get('tree_exit')
            if (not isinstance(evidence, dict) or type(evidence.get('total_processes')) is not int
                    or not isinstance(evidence.get('processes'), list)
                    or evidence['total_processes'] != len(evidence['processes'])
                    or any(not isinstance(process, dict) or process.get('exited') is not True
                           or type(process.get('pid')) is not int or process['pid'] <= 0
                           for process in evidence['processes'])
                    or len({process['pid'] for process in evidence['processes']}) != len(evidence['processes'])):
                raise AdmissionError('cleanup_unverified')
            return True
        job = self.jobs.get(str(claim.ownership.attempt_id))
        if row['phase'] == 'preparing':
            observed = {}  # Launch must commit creating before any process can exist.
        else:
            if job is None or not job.handle or not job.wait_stopped(timeout_ms):
                return False
            observed = job.observed
        row['plan']['tree_exit'] = dict(total_processes=len(observed), processes=[
            dict(pid=pid, creation_time=record['creation_time'], exited=record['exited'])
            for pid, record in sorted(observed.items())])
        binding = self._binding(claim)
        with closing(sqlite3.connect(self.path)) as db, db:
            changed = db.execute('''UPDATE windows_launches SET plan=?,phase='tree_exited'
                WHERE attempt=? AND binding=? AND phase=?''', [json.dumps(row['plan'], sort_keys=True),
                str(claim.ownership.attempt_id), binding, row['phase']]).rowcount
            if changed != 1:
                raise AdmissionError('launch_requires_reconciliation')
        self.jobs.pop(str(claim.ownership.attempt_id), None)
        return True

    def cleanup_scratch(self, claim):
        """Remove only this attempt's scratch after verified process-tree exit.

        Filesystem failures preserve tree_exited for an idempotent retry. Profile
        and shared runtime ACL cleanup are separate; this does not release work.
        """
        if not self.reconcile_tree(claim):
            raise AdmissionError('cleanup_unverified')
        row = self.row(claim)
        directory = self.root / str(claim.ownership.attempt_id)
        if (directory.resolve() != directory or directory.parent != self.root
                or Path(row['plan']['cwd']) != directory):
            raise AdmissionError('unsafe_cleanup_path')
        if row['phase'] in ('scratch_cleaned', 'profile_cleaned', 'access_cleaned'):
            if os.path.lexists(directory):
                raise AdmissionError('cleanup_unverified')
            return
        if os.path.lexists(directory):
            # Python >=3.10 removes internal junctions rather than traversing them.
            shutil.rmtree(directory)
        if os.path.lexists(directory):
            raise AdmissionError('cleanup_unverified')
        self._record(claim, 'tree_exited', 'scratch_cleaned')

    def cleanup_profile(self, claim):
        """Remove the exact attempt profile; shared ACL cleanup remains separate."""
        self.cleanup_scratch(claim)
        row = self.row(claim)
        name = 'surgiclaw.' + claim.ownership.attempt_id.hex
        if row['plan']['app_container'] != name:
            raise AdmissionError('cleanup_unverified')
        # Windows returns success for an absent profile, including after a lost commit.
        WindowsAppContainer.delete_profile(name)
        if row['phase'] not in ('profile_cleaned', 'access_cleaned'):
            self._record(claim, 'scratch_cleaned', 'profile_cleaned')

    def cleanup_access(self, claim):
        """Revoke only the recorded attempt's grants after profile cleanup."""
        self.cleanup_profile(claim)
        row = self.row(claim)
        for grant in row['plan']['grants']:
            path = Path(grant['path'])
            identity = path.stat()
            if (path.resolve(strict=True) != path or identity.st_dev != grant['device']
                    or identity.st_ino != grant['inode']):
                raise AdmissionError('unsafe_cleanup_path')
            WindowsAppContainer.path_access(path, row['plan']['sid'], None)
        if row['phase'] != 'access_cleaned':
            self._record(claim, 'profile_cleaned', 'access_cleaned')

    def cleanup_after_reboot(self, attempt_id, new_boot_id):
        """Clean one recorded old-boot attempt; never reinterpret a PID after reboot."""
        attempt_id = UUID(str(attempt_id))
        if (new_boot_id == self.boot_id
                or physical_boot_id(self.host_id, Path('/proc')) != new_boot_id):
            raise AdmissionError('attempt_fenced')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT binding,plan,phase FROM windows_launches WHERE attempt=?',
                             [str(attempt_id)]).fetchone()
            if row is None:
                raise AdmissionError('launch_unknown')
            binding, plan = json.loads(row[0]), json.loads(row[1])
            if (any(binding.get(key) != value for key, value in self.binding.items())
                    or binding['ownership']['attempt_id'] != str(attempt_id)):
                raise AdmissionError('attempt_fenced')
            directory = self.root / str(attempt_id)
            name = 'surgiclaw.' + attempt_id.hex
            if (directory.resolve() != directory or directory.parent != self.root
                    or Path(plan['cwd']) != directory or plan['app_container'] != name):
                raise AdmissionError('unsafe_cleanup_path')
            # Validate every persistent grant before mutating scratch or profiles.
            for grant in plan['grants']:
                path = Path(grant['path'])
                identity = path.stat()
                if (path.resolve(strict=True) != path or identity.st_dev != grant['device']
                        or identity.st_ino != grant['inode']):
                    raise AdmissionError('unsafe_cleanup_path')
            if row[2] == 'access_cleaned':
                if os.path.lexists(directory):
                    raise AdmissionError('cleanup_unverified')
                return
            if os.path.lexists(directory):
                shutil.rmtree(directory)
            WindowsAppContainer.delete_profile(name)
            for grant in plan['grants']:
                WindowsAppContainer.path_access(Path(grant['path']), plan['sid'], None)
            # Each external operation is idempotent. An interruption leaves the
            # old phase, so retry repeats cleanup instead of inventing evidence.
            plan['reboot_cleanup'] = {'previous_boot': str(self.boot_id), 'new_boot': str(new_boot_id)}
            db.execute("UPDATE windows_launches SET plan=?,phase='access_cleaned' WHERE attempt=?",
                       [json.dumps(plan, sort_keys=True), str(attempt_id)])

    def cleanup(self, claim):
        """Build evidence only after process, scratch, profile and grant cleanup."""
        self.cleanup_access(claim)
        row = self.row(claim)
        if row['phase'] != 'access_cleaned':
            raise AdmissionError('cleanup_unverified')
        evidence_id = hashlib.sha256((self._binding(claim) + json.dumps(row, sort_keys=True)).encode()).hexdigest()
        return CleanupEvidence(attempt_id=claim.ownership.attempt_id, boot_id=self.boot_id,
            processes_stopped=True, models_evicted=True, scratch_cleaned=True, evidence_id=evidence_id)

    def sign_cleanup(self, claim, key):
        proof = self.cleanup(claim)
        authority, epoch = UUID(self.binding['authority']), self.binding['epoch']
        observation = CleanupObservation(host_id=self.host_id, issued_at=datetime.now(timezone.utc), evidence=proof)
        return SignedCleanup(authority_id=authority, epoch=epoch, observation=observation,
            signature=observation_signature('cleanup', authority, epoch, observation.model_dump(mode='json'), key))
