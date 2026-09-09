"""Trusted native adapter for admission_supervisor.run_cycle.

The operator holds the supervisor singleton lock for this object's lifetime.
All calls are serialized; private state and signing keys must be outside grants.
"""
import os
from pathlib import Path
import re

from .resources import AdmissionError
from .windows_job import WindowsAppContainer, WindowsJob
from .windows_launch import WindowsLaunchJournal


class WindowsSupervisor:
    def __init__(self, journal, work_root, **binding):
        self.journal = WindowsLaunchJournal(journal, work_root=work_root, **binding)
        self.jobs = {}
        self.apps = {}

    def _row(self, claim):
        return self.journal.row(claim)

    def launch(self, claim, *, command, read_only_paths, journal_directory, environment, network):
        environment = dict(environment)
        if len({name.upper() for name in environment}) != len(environment):
            raise ValueError('Duplicate Windows environment variable')
        for name, value in environment.items():
            if (not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) or not isinstance(value, str)
                    or name.upper() in ('CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES', 'SYNPUSHER_TARGETS',
                                        'SPECTRA_GPU_BACKEND', 'SPECTRA_GPU_ID', 'TEMP', 'TMP')):
                raise ValueError('invalid or supervisor-owned environment variable')
        environment['CUDA_VISIBLE_DEVICES'] = claim.gpu_uuid or '-1'
        environment['NVIDIA_VISIBLE_DEVICES'] = claim.gpu_uuid or 'void'
        if claim.profile.gpu_backend in ('vulkan', 'dx12'):
            environment['SPECTRA_GPU_BACKEND'] = claim.profile.gpu_backend
            environment['SPECTRA_GPU_ID'] = claim.gpu_uuid
        cpus = os.cpu_count()
        if not cpus:
            raise AdmissionError('hardware_report_stale')
        rate = min(10000, claim.profile.cpu_millicores * 10 // cpus)
        if rate < 1:
            raise AdmissionError('insufficient_cpu')
        plan = self.journal.prepare(claim)
        attempt = str(claim.ownership.attempt_id)
        directory = Path(plan['cwd'])
        directory.mkdir()  # Never reuse an existing attempt directory.
        environment['TEMP'] = environment['TMP'] = str(directory)
        try:
            app = WindowsAppContainer(plan['app_container'], network=network)
        except BaseException:
            # In particular, an existing profile is not ours to remove.
            self.journal._record(claim, 'preparing', 'profile_unknown')
            raise
        self.apps[attempt] = app
        job = WindowsJob(memory_bytes=claim.profile.execution_ram_mib * 1024**2,
                         cpu_rate=rate, process_limit=128)
        self.jobs[attempt] = job
        # The log is retained in private supervisor storage, never worker-readable.
        return self.journal.launch(claim, job, app_container=app, command=command,
            cwd=directory, environment=environment, log_path=self.journal.path.parent/(attempt+'.log'),
            read_only_paths=read_only_paths, journal_directory=journal_directory)

    def running(self, claim):
        row = self._row(claim)
        if row['phase'] != 'started':
            return False
        job = self.jobs.get(str(claim.ownership.attempt_id))
        if job is None:
            raise AdmissionError('cleanup_unverified')
        # A surviving descendant after root exit is stopped during cleanup.
        return any(process.wait(0) is None for process in job.processes)

    def cleanup(self, claim):
        proof = self.journal.cleanup(claim)
        attempt = str(claim.ownership.attempt_id)
        job = self.jobs.pop(attempt, None)
        if job is not None:
            job.close()
        app = self.apps.pop(attempt, None)
        if app is not None:
            app.close()
        return proof

    def sign_cleanup(self, claim, key):
        self.cleanup(claim)
        return self.journal.sign_cleanup(claim, key)
