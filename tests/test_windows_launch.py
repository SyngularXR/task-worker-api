import os
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from task_worker_api.host_reporter import physical_boot_id
from task_worker_api.resources import AdmissionError, AttemptOwnership
from task_worker_api.resource_protocol import observation_signature
from task_worker_api.windows_job import WindowsAppContainer, WindowsJob, WindowsJobProcess
from task_worker_api.windows_launch import WindowsLaunchJournal
from .test_resources import cpu_profile
from .windows_helpers import stage_python

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='real Windows journal launch checks')


@pytest.mark.parametrize('fault', [None, 'same_boot', 'changed_path', 'interrupted'])
def test_reboot_cleanup_removes_only_recorded_native_resources(tmp_path, monkeypatch, fault):
    attempt, host, old_boot = uuid4(), uuid4(), uuid4()
    current_boot = physical_boot_id(host, Path('/proc'))
    root, shared = tmp_path/'work', tmp_path/'shared'
    root.mkdir()
    shared.mkdir()
    directory = root/str(attempt)
    directory.mkdir()
    (directory/'scratch').write_text('owned test scratch')
    (shared/'keep').write_text('shared data')
    journal = WindowsLaunchJournal(tmp_path/'reboot.sqlite', work_root=root, authority_id=uuid4(), epoch=1,
        host_id=host, boot_id=old_boot, execution_scope='native-test')
    with WindowsAppContainer('surgiclaw.'+attempt.hex) as app:
        sid = app.sid_string()
        WindowsAppContainer.path_access(shared, sid, 'RX')
        identity = shared.stat()
        plan = dict(cwd=str(directory), app_container=app.name, sid=sid,
            grants=[dict(path=str(shared), device=identity.st_dev, inode=identity.st_ino + int(fault == 'changed_path'))])
        binding = dict(journal.binding, ownership={'attempt_id': str(attempt)})
        with sqlite3.connect(journal.path) as db:
            db.execute("INSERT INTO windows_launches VALUES(?,?,?,'started',123,456)",
                [str(attempt), json.dumps(binding), json.dumps(plan)])
        before = journal.path.read_bytes()
        if fault in ('same_boot', 'changed_path'):
            with pytest.raises(AdmissionError):
                journal.cleanup_after_reboot(attempt, old_boot if fault == 'same_boot' else current_boot)
            assert (directory/'scratch').exists()
            assert journal.path.read_bytes() == before
        else:
            if fault == 'interrupted':
                with monkeypatch.context() as patch:
                    def fail(*args):
                        raise OSError('test interruption after profile removal')
                    patch.setattr(WindowsAppContainer, 'path_access', fail)
                    with pytest.raises(OSError):
                        journal.cleanup_after_reboot(attempt, current_boot)
                with sqlite3.connect(journal.path) as db:
                    assert db.execute('SELECT phase FROM windows_launches').fetchone()[0] == 'started'
            from task_worker_api.host_recovery import cleanup_windows_after_reboot
            cleanup_windows_after_reboot({"reporter": {"host_id": str(host), "boot_id": str(old_boot),
                "authority_id": journal.binding['authority'], "epoch": 1}, "expected_attempts": [str(attempt)],
                "journals": [{"kind": "windows", "path": str(journal.path), "work_root": str(root)}]})
            journal.cleanup_after_reboot(attempt, current_boot)
            assert not directory.exists()
            acl = subprocess.run(['icacls.exe', str(shared)], capture_output=True, text=True, check=True).stdout
            assert sid not in acl
            with sqlite3.connect(journal.path) as db:
                assert db.execute('SELECT phase FROM windows_launches').fetchone()[0] == 'access_cleaned'
        assert (shared/'keep').read_text() == 'shared data'
        WindowsAppContainer.path_access(shared, sid, None)


@pytest.mark.parametrize('fault', [None, 'grant', 'identity_commit', 'resume', 'started_commit'])
def test_journal_orders_native_launch_and_never_replays_it(tmp_path, monkeypatch, fault):
    attempt = uuid4()
    runtime, work_root = tmp_path/'runtime', tmp_path/'work'
    work_root.mkdir()
    work = work_root/str(attempt)
    stage_python(runtime)
    worker_journal = tmp_path/'worker-journal'
    worker_journal.mkdir()
    host = uuid4()
    boot = physical_boot_id(host, Path('/proc'))
    binding = dict(work_root=work_root, authority_id=uuid4(), epoch=1, host_id=host, boot_id=boot, execution_scope='windows-test')
    journal = WindowsLaunchJournal(tmp_path/'launch.sqlite', **binding)
    claim = SimpleNamespace(host_id=host, boot_id=boot, execution_scope='windows-test', state='reserved',
        ownership=AttemptOwnership(worker_instance_id=uuid4(), attempt_id=attempt, generation=1, token='t'*32),
        profile=cpu_profile(host_ram_mib=128, execution_ram_mib=128, cpu_millicores=os.cpu_count()*1000),
        input_digest='a'*64, gpu_uuid=None)
    marker = work/'runs.txt'
    command = [str(runtime/'python.exe'), '-I', '-S', '-c',
        'import pathlib,sqlite3,sys,time; db=sqlite3.connect(sys.argv[2]); '
        'db.execute("CREATE TABLE checkpoint (value TEXT)"); db.execute("INSERT INTO checkpoint VALUES (\'saved\')"); '
        'db.commit(); db.close(); pathlib.Path(sys.argv[1]).write_text("run"); time.sleep(300)',
        str(marker), str(worker_journal/'claim.sqlite')]
    environment = {key: os.environ[key] for key in ('SystemRoot', 'LOCALAPPDATA')}
    record, resume = journal._record, WindowsJobProcess.resume

    def recording(claim, previous, phase, **identity):
        if phase == 'suspended':
            assert journal.row(claim)['phase'] == 'creating'
            assert not marker.exists()
            if fault == 'identity_commit':
                raise OSError('identity commit failed')
        record(claim, previous, phase, **identity)
        if phase == 'started' and fault == 'started_commit':
            raise OSError('lost commit acknowledgement')

    def resuming(process):
        row = journal.row(claim)
        assert row['phase'] == 'starting'
        assert row['pid'] == process.pid and row['creation_time'] == process.creation_time
        assert not marker.exists()
        resume(process)
        if fault == 'resume':
            raise OSError('lost resume acknowledgement')

    monkeypatch.setattr(journal, '_record', recording)
    monkeypatch.setattr(WindowsJobProcess, 'resume', resuming)
    with WindowsAppContainer('surgiclaw.'+claim.ownership.attempt_id.hex) as app:
        path_access = WindowsAppContainer.path_access
        def granting(path, sid, access):
            if sid == app.sid_string() and access is not None:
                assert journal.row(claim)['phase'] == 'creating'
                path_access(path, sid, access)
                if fault == 'grant':
                    raise OSError('lost grant acknowledgement')
            else:
                path_access(path, sid, access)
        monkeypatch.setattr(WindowsAppContainer, 'path_access', staticmethod(granting))
        options = dict(app_container=app, command=command, cwd=work, environment=environment,
                       log_path=tmp_path/'worker.log', read_only_paths=[runtime], journal_directory=worker_journal)
        if fault is None:
            with WindowsJob(memory_bytes=256*1024**2, cpu_rate=10000, process_limit=8) as oversized:
                with pytest.raises(ValueError, match='claimed limits'):
                    journal.launch(claim, oversized, **options)
                assert not oversized.processes
            with pytest.raises(AdmissionError, match='launch_unknown'):
                journal.row(claim)
        with WindowsJob(memory_bytes=128*1024**2, cpu_rate=10000, process_limit=8) as job:
            journal.prepare(claim)
            work.mkdir()
            spawn = job.spawn
            def spawning(*args, **kwargs):
                row = journal.row(claim)
                assert row['phase'] == 'creating' and row['pid'] is None
                return spawn(*args, **kwargs)
            monkeypatch.setattr(job, 'spawn', spawning)
            if fault:
                with pytest.raises(OSError):
                    journal.launch(claim, job, **options)
            else:
                process = journal.launch(claim, job, **options)
                assert process.wait(0) is None
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert marker.exists(), (tmp_path/'worker.log').read_text()
            # A new journal object stands in for a restarted supervisor.
            restarted = WindowsLaunchJournal(journal.path, **binding)
            with WindowsJob(memory_bytes=128*1024**2, cpu_rate=10000, process_limit=8) as fresh:
                with pytest.raises(AdmissionError, match='launch_requires_reconciliation'):
                    restarted.launch(claim, fresh, **options)
                assert not fresh.processes
            if fault in ('identity_commit', 'grant'):
                assert restarted.row(claim)['phase'] == 'creating'
                assert not restarted.reconcile_root(claim)
                assert not marker.exists()
            else:
                assert restarted.reconcile_root(claim)
                assert restarted.row(claim)['phase'] == 'root_exited'
                with pytest.raises(AdmissionError, match='launch_requires_reconciliation'):
                    restarted._record(claim, 'root_exited', 'starting')
            assert not restarted.reconcile_tree(claim)  # Root exit is insufficient without the retained cohort.
            with pytest.raises(AdmissionError, match='cleanup_unverified'):
                restarted.cleanup_scratch(claim)
            with pytest.raises(AdmissionError, match='cleanup_unverified'):
                restarted.sign_cleanup(claim, b'k'*32)
            assert work.is_dir()
            assert journal.reconcile_tree(claim)
            assert restarted.row(claim)['phase'] == 'tree_exited'
            assert restarted.reconcile_tree(claim)  # Durable proof survives reopening.
            assert restarted.reconcile_root(claim)
            assert job.wait_empty(10000)
            assert all(process.wait(10000) is not None for process in job.processes)
            changed = WindowsLaunchJournal(journal.path, **{**binding, 'epoch': 2})
            with pytest.raises(AdmissionError, match='attempt_fenced'):
                changed.row(claim)
            if fault is None:
                outside = tmp_path/'outside'
                outside.mkdir()
                sentinel = outside/'keep.txt'
                sentinel.write_text('unrelated data')
                # Junction creation does not require an elevated Windows token.
                subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(work/'link'), str(outside)],
                    check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                remove = shutil.rmtree
                def lost_removal_ack(path):
                    assert path.resolve() == work and path.parent == work_root
                    remove(path)
                    raise OSError('lost deletion acknowledgement')
                with monkeypatch.context() as patch:
                    patch.setattr('task_worker_api.windows_launch.shutil.rmtree', lost_removal_ack)
                    with pytest.raises(OSError, match='lost deletion'):
                        restarted.cleanup_scratch(claim)
                assert not work.exists() and sentinel.read_text() == 'unrelated data'
                assert restarted.row(claim)['phase'] == 'tree_exited'
            restarted.cleanup_scratch(claim)
            assert not work.exists()
            assert restarted.row(claim)['phase'] == 'scratch_cleaned'
            restarted.cleanup_scratch(claim)
            if fault is None:
                # A replaced attempt root is never traversed, even on replay.
                subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(work), str(outside)],
                    check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                with pytest.raises(AdmissionError, match='unsafe_cleanup_path'):
                    restarted.cleanup_scratch(claim)
                assert sentinel.read_text() == 'unrelated data'
                os.rmdir(work)  # Remove only the junction created by this test.
            delete_profile = WindowsAppContainer.delete_profile
            def lost_profile_ack(name):
                assert name == app.name
                delete_profile(name)
                raise OSError('lost profile deletion acknowledgement')
            with monkeypatch.context() as patch:
                patch.setattr(WindowsAppContainer, 'delete_profile', staticmethod(lost_profile_ack))
                with pytest.raises(OSError, match='lost profile deletion'):
                    restarted.cleanup_profile(claim)
            assert restarted.row(claim)['phase'] == 'scratch_cleaned'
            restarted = WindowsLaunchJournal(journal.path, **binding)
            restarted.cleanup_profile(claim)
            assert restarted.row(claim)['phase'] == 'profile_cleaned'
            restarted.cleanup_profile(claim)
            assert restarted.reconcile_tree(claim)
            # A fresh creation proves the old profile was removed, not just marked.
            with WindowsAppContainer(app.name):
                pass
            with WindowsAppContainer('surgiclaw.' + uuid4().hex) as other:
                WindowsAppContainer.path_access(runtime, other.sid_string(), 'RX')
                change_access = WindowsAppContainer.path_access
                def lost_access_ack(path, sid, access):
                    assert sid == app.sid_string() and access is None
                    change_access(path, sid, access)
                    raise OSError('lost ACL removal acknowledgement')
                with monkeypatch.context() as patch:
                    patch.setattr(WindowsAppContainer, 'path_access', staticmethod(lost_access_ack))
                    with pytest.raises(OSError, match='lost ACL removal'):
                        restarted.cleanup_access(claim)
                assert restarted.row(claim)['phase'] == 'profile_cleaned'
                restarted = WindowsLaunchJournal(journal.path, **binding)
                restarted.cleanup_access(claim)
                assert restarted.row(claim)['phase'] == 'access_cleaned'
                restarted.cleanup_access(claim)
                # The attempt SID lost executable access; another worker retained it.
                with WindowsAppContainer(app.name) as removed:
                    with WindowsJob(memory_bytes=128*1024**2, cpu_rate=10000, process_limit=8) as probe:
                        try:
                            process = probe.spawn([str(runtime/'python.exe'), '-I', '-S', '-c', 'pass'],
                                cwd=runtime, environment=environment, app_container=removed,
                                log_path=tmp_path/'revoked.log')
                        except OSError as error:
                            assert error.winerror == 5
                        else:
                            # Windows can create the image before the restricted token
                            # attempts to load Python's runtime files.
                            resume(process)
                            assert process.wait(10000) not in (None, 0), (tmp_path/'revoked.log').read_text()
                            assert probe.wait_stopped(10000)
                with WindowsJob(memory_bytes=128*1024**2, cpu_rate=10000, process_limit=8) as probe:
                    process = probe.spawn([str(runtime/'python.exe'), '-I', '-S', '-c', 'pass'],
                        cwd=runtime, environment=environment, app_container=other)
                    resume(process)
                    assert process.wait(10000) == 0
                    assert probe.wait_stopped(10000)
                WindowsAppContainer.path_access(runtime, other.sid_string(), None)
            if fault is None:
                with sqlite3.connect(worker_journal/'claim.sqlite') as db:
                    assert db.execute('SELECT value FROM checkpoint').fetchone() == ('saved',)
            signed = restarted.sign_cleanup(claim, b'k'*32)
            assert signed.observation.evidence == restarted.cleanup(claim)
            assert signed.observation.evidence.scratch_cleaned
            assert signed.signature == observation_signature('cleanup', binding['authority_id'], binding['epoch'],
                signed.observation.model_dump(mode='json'), b'k'*32)
