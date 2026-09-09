import os
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from task_worker_api.admission_supervisor import run_cycle
from task_worker_api.host_reporter import physical_boot_id
from task_worker_api.resources import AdmissionError, AttemptOwnership
from task_worker_api.windows_job import WindowsAppContainer
from task_worker_api.windows_supervisor import WindowsSupervisor
from .test_resources import cpu_profile
from .windows_helpers import stage_python

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='real native supervisor checks')


def setup_supervisor(tmp_path):
    root = tmp_path/'work'
    root.mkdir()
    host = uuid4()
    boot = physical_boot_id(host, Path('/proc'))
    binding = dict(authority_id=uuid4(), epoch=1, host_id=host, boot_id=boot, execution_scope='native-test')
    claim = SimpleNamespace(host_id=host, boot_id=boot, execution_scope='native-test', state='reserved',
        ownership=AttemptOwnership(worker_instance_id=uuid4(), attempt_id=uuid4(), generation=1, token='t'*32),
        profile=cpu_profile(host_ram_mib=128, execution_ram_mib=128, cpu_millicores=os.cpu_count()*1000),
        input_digest='a'*64, gpu_uuid=None)
    return WindowsSupervisor(tmp_path/'supervisor.sqlite', root, **binding), claim, root, binding


@pytest.mark.asyncio
async def test_native_supervisor_cycle_preserves_operations_and_releases_after_cleanup(tmp_path):
    supervisor, claim, root, binding = setup_supervisor(tmp_path)
    runtime = tmp_path/'runtime'
    stage_python(runtime)
    worker_directory = tmp_path/'worker-journal'
    worker_directory.mkdir()
    journal = SimpleNamespace(path=worker_directory/'claim.sqlite')
    with sqlite3.connect(journal.path) as db:
        db.execute('CREATE TABLE operation (value TEXT)')
    released = []
    journal.acknowledge_release = released.append
    journal.pending = lambda: (None, None)
    command = [str(runtime/'python.exe'), '-I', '-S', '-c',
        'import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); '
        'db.execute("INSERT INTO operation VALUES (\'durable\')"); db.commit(); db.close()', str(journal.path)]
    operations = []

    class Client:
        state = 'reserved'

        async def resource_claim(self, *args):
            return claim, 0

        async def resource_status(self, *args):
            return SimpleNamespace(state=self.state, cancelled=False)

        resource_heartbeat = resource_status

        async def resource_recover_operations(self, journal):
            assert not (root/str(claim.ownership.attempt_id)).exists()
            with sqlite3.connect(journal.path) as db:
                assert db.execute('SELECT value FROM operation').fetchone() == ('durable',)

        async def resource_operation(self, journal, kind, payload, **options):
            operations.append(kind)
            if kind == 'decline':
                self.state = 'releasing'
            else:
                assert kind == 'release'
                proof = options['cleanup'].observation.evidence
                assert proof == supervisor.cleanup(claim)
                assert proof.processes_stopped and proof.models_evicted and proof.scratch_cleaned
                self.state = 'released'
            return await self.resource_status()

        async def resource_ready(self, *args):
            assert released == [claim.ownership.attempt_id]

    async def report():
        return None

    launch = dict(command=command, read_only_paths=[runtime], network=False,
                  environment={key: os.environ[key] for key in ('SystemRoot', 'LOCALAPPDATA')})
    await run_cycle(Client(), journal, claim.ownership.worker_instance_id, ['render'],
                    supervisor, launch, report, b'k'*32)
    assert operations == ['decline', 'release']
    assert supervisor._row(claim)['phase'] == 'access_cleaned'
    env = supervisor._row(claim)['plan']['environment']
    assert env['CUDA_VISIBLE_DEVICES'] == '-1' and env['NVIDIA_VISIBLE_DEVICES'] == 'void'
    assert env['TEMP'] == env['TMP'] == str(root/str(claim.ownership.attempt_id))
    assert not supervisor.apps and not supervisor.jobs
    restarted = WindowsSupervisor(supervisor.journal.path, root, **binding)
    assert restarted.cleanup(claim) == supervisor.cleanup(claim)


def test_preparing_recovery_cannot_resume_and_profile_conflict_stays_unknown(tmp_path):
    supervisor, claim, root, binding = setup_supervisor(tmp_path)
    plan = supervisor.journal.prepare(claim)
    Path(plan['cwd']).mkdir()
    with WindowsAppContainer(plan['app_container']):
        restarted = WindowsSupervisor(supervisor.journal.path, root, **binding)
        assert restarted.cleanup(claim).processes_stopped
        with pytest.raises(AdmissionError, match='launch_requires_reconciliation'):
            restarted.journal.prepare(claim)
    claim.ownership = claim.ownership.model_copy(update={'attempt_id': uuid4()})
    name = 'surgiclaw.' + claim.ownership.attempt_id.hex
    with WindowsAppContainer(name):
        with pytest.raises(OSError, match='CreateAppContainerProfile'):
            supervisor.launch(claim, command=[], read_only_paths=[], journal_directory=tmp_path,
                              environment={}, network=False)
        assert supervisor._row(claim)['phase'] == 'profile_unknown'
        with pytest.raises(AdmissionError, match='cleanup_unverified'):
            supervisor.sign_cleanup(claim, b'k'*32)
        with pytest.raises(OSError, match='CreateAppContainerProfile'):
            WindowsAppContainer(name)  # The conflicting profile was not removed.


@pytest.mark.asyncio
async def test_native_operator_configuration_excludes_signing_state(tmp_path, monkeypatch):
    from task_worker_api import admission_supervisor

    for name in ('private', 'report', 'credentials', 'runtime'):
        (tmp_path/name).mkdir()
    (tmp_path/'private/key').write_bytes(b'k'*32)
    (tmp_path/'credentials/worker').write_text('test-only')
    (tmp_path/'runtime/python.exe').write_bytes(b'config-test-placeholder')
    config = dict(launcher='windows', authority_id=str(uuid4()), host_id=str(uuid4()), boot_id=str(uuid4()),
        epoch=1, execution_scope='native-test', supervisor_journal='private/supervisor.sqlite', work_root='work',
        worker_journal_directory='worker-journal', worker_instance_id=str(uuid4()), worker_id='native-test',
        backend_url='http://test/api/v1', worker_backend_url='http://appcontainer/api/v1', credential_file='credentials/worker', report_file='report/snapshot.json',
        signing_key_file='private/key', python_executable='runtime/python.exe', read_only_paths=['runtime'],
        handlers={'render': 'src.services.resource_render_worker:run_admitted'})
    path = tmp_path/'config.json'
    path.write_text(json.dumps(config))

    class Client:
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def resource_ready(self, *args):
            pass

    async def cycle(client, journal, instance, types, supervisor, launch, read_report, key):
        assert isinstance(supervisor, WindowsSupervisor) and types == ['render'] and key == b'k'*32
        command = launch['command']
        assert command[:4] == [str(tmp_path/'runtime/python.exe'), '-I', '-m', 'task_worker_api.admitted_worker']
        assert launch['network'] is True
        assert str(tmp_path/'private/key') not in launch['read_only_paths']
        assert str(tmp_path/'private') not in launch['read_only_paths']
        worker = json.loads(Path(command[4]).read_text())
        assert worker['backend_url'] == 'http://appcontainer/api/v1'
        assert worker['work_dir'] == '.' and worker['journal_file'] == str(journal.path)
        assert worker['credential_file'] == str(tmp_path/'credentials/worker')
        assert worker['report_file'] == str(tmp_path/'report/snapshot.json')
        raise asyncio.CancelledError()

    monkeypatch.setattr('task_worker_api.client.BackendClient', Client)
    monkeypatch.setattr(admission_supervisor, 'run_cycle', cycle)
    with pytest.raises(asyncio.CancelledError):
        await admission_supervisor.run(path)
    config['read_only_paths'] = ['runtime', 'private']
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='signing key'):
        await admission_supervisor.run(path)


@pytest.mark.parametrize('backend', ['cuda', 'vulkan', 'dx12'])
def test_native_gpu_assignment_is_claim_owned(tmp_path, monkeypatch, backend):
    supervisor, claim, root, binding = setup_supervisor(tmp_path)
    claim.gpu_uuid = 'GPU-test'
    claim.profile = claim.profile.model_copy(update={'gpu_backend': backend, 'gpu_count': 1, 'gpu_vram_mib': 128})
    for name in ('CUDA_VISIBLE_DEVICES', 'cuda_visible_devices', 'SPECTRA_GPU_ID', 'NVIDIA_VISIBLE_DEVICES', 'TEMP'):
        with pytest.raises(ValueError, match='supervisor-owned'):
            supervisor.launch(claim, command=[], read_only_paths=[], journal_directory=tmp_path,
                              environment={name: 'override'}, network=False)
    with pytest.raises(AdmissionError, match='launch_unknown'):
        supervisor._row(claim)
    captured = {}
    def launch(claim, job, **options):
        captured.update(options['environment'])
        return None
    monkeypatch.setattr(supervisor.journal, 'launch', launch)
    supervisor.launch(claim, command=[], read_only_paths=[], journal_directory=tmp_path, environment={}, network=False)
    assert captured['CUDA_VISIBLE_DEVICES'] == captured['NVIDIA_VISIBLE_DEVICES'] == claim.gpu_uuid
    if backend != 'cuda':
        assert captured['SPECTRA_GPU_BACKEND'] == backend and captured['SPECTRA_GPU_ID'] == claim.gpu_uuid
    supervisor.cleanup(claim)
