import json
import asyncio
import httpx
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from task_worker_api.docker_supervisor import DockerSupervisor
from task_worker_api.resources import AdmissionError, AttemptOwnership


@pytest.mark.asyncio
@pytest.mark.parametrize("child_started", [False, True])
@pytest.mark.parametrize("heartbeat_fenced", [False, True])
async def test_supervisor_renews_reservation_during_child_startup(tmp_path, monkeypatch, child_started, heartbeat_fenced):
    from task_worker_api.admission_supervisor import run_cycle

    state = dict(launched=False, polls=0, renewed_after_launch=0, running_renewals=0, phase="reserved")
    claim = SimpleNamespace(ownership=SimpleNamespace(attempt_id=uuid4()))
    sleep = asyncio.sleep

    async def short_sleep(seconds):
        await sleep(0.001 if seconds == 5 else 0.01)

    monkeypatch.setattr(asyncio, "sleep", short_sleep)

    class Client:
        async def resource_claim(self, *args):
            return claim, 0
        async def resource_heartbeat(self, *args):
            if state["launched"]:
                state["renewed_after_launch"] += 1
                if heartbeat_fenced:
                    response = httpx.Response(409, request=httpx.Request('POST', 'http://test/workers/heartbeat'))
                    response.raise_for_status()
            if state["phase"] == "running":
                state["running_renewals"] += 1
            return SimpleNamespace(state=state["phase"], cancelled=False)
        async def resource_status(self, *args):
            return SimpleNamespace(state=state["phase"], cancelled=False)
        async def resource_recover_operations(self, *args):
            pass
        async def resource_operation(self, journal, operation, *args, **kwargs):
            state["phase"] = "released" if operation == "release" else "releasing"
            return SimpleNamespace(state=state["phase"])
        async def resource_ready(self, *args):
            pass

    class Supervisor:
        def _row(self, *args):
            raise AdmissionError("launch_unknown")
        def launch(self, *args, **kwargs):
            state["launched"] = True
        def running(self, *args):
            state["polls"] += 1
            if child_started:
                state["phase"] = "running"
            return state["polls"] < 4
        def cleanup(self, *args):
            state['cleaned'] = True
        def sign_cleanup(self, *args):
            return None

    async def report():
        return None

    journal = SimpleNamespace(path=tmp_path / "journal", acknowledge_release=lambda attempt: None)
    await run_cycle(Client(), journal, uuid4(), ["gs_build"], Supervisor(), {}, report, b"key")
    assert state['cleaned']
    if heartbeat_fenced:
        assert state['renewed_after_launch'] == 1
    elif child_started:
        assert state["running_renewals"] == 1
    else:
        assert state["renewed_after_launch"] >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_failures", [0, 5])
async def test_backend_supervisor_builds_private_finalizer_configuration(tmp_path, monkeypatch, startup_failures):
    from task_worker_api import admission_supervisor

    for name in ("private", "report", "publication", "artifacts", "credentials"):
        (tmp_path / name).mkdir()
    for name in ("credentials/worker", "credentials/response", "credentials/database", "report/snapshot"):
        (tmp_path / name).write_text("test-only")
    (tmp_path / "private/signing").write_bytes(b"s" * 32)
    config = dict(authority_id=str(uuid4()), host_id=str(uuid4()), boot_id=str(uuid4()), epoch=1,
        execution_scope="backend", cgroup_parent="", network_id="d" * 64, supervisor_journal="private/supervisor.sqlite",
        work_root="work", worker_journal_directory="worker-journal", image_id="sha256:" + "b" * 64,
        worker_instance_id=str(uuid4()), worker_id="test-finalizer", backend_url="http://test/api/v1", worker_backend_url="http://backend-container/api/v1",
        credential_file="credentials/worker", report_file="report/snapshot", signing_key_file="private/signing",
        backend_finalizer=dict(publication_directory="publication", artifact_root="artifacts",
            response_key_file="credentials/response", database_url_file="credentials/database"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))

    ready_calls = 0
    retry_delays = []

    async def sleep(delay):
        retry_delays.append(delay)

    monkeypatch.setattr(admission_supervisor.asyncio, "sleep", sleep)

    class Client:
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def resource_ready(self, *args):
            nonlocal ready_calls
            ready_calls += 1
            if ready_calls <= startup_failures:
                raise httpx.ConnectError("Backend still starting")

    async def cycle(client, journal, instance, types, supervisor, launch, read_report, key):
        assert ready_calls == startup_failures + 1
        assert types == ["finalize_spatial", "finalize_gs", "finalize_render", "finalize_segment", "finalize_synthetic", "finalize_gs4d", "finalize_model", "finalize_cinematic", "finalize_deploy", "finalize_deploy_prep"]
        assert launch["command"][2] == "src.services.resource_finalizer_worker"
        assert launch["publication_directory"] == tmp_path / "publication"
        assert launch["environment"]["SHARED_DATA_PATH"] == "/app/shared"
        assert str(tmp_path / "private/signing") not in launch["read_only_mounts"]
        worker_config = json.loads((tmp_path / f"private/worker-{instance}.json").read_text())
        assert worker_config["backend_url"] == "http://backend-container/api/v1"
        assert worker_config["database_url_file"] == "/run/backend-database-url"
        assert worker_config["response_key_file"] == "/run/backend-response-key"
        assert worker_config["artifact_root"] == "/run/committed-artifacts"
        assert "handlers" not in worker_config
        raise asyncio.CancelledError()

    monkeypatch.setattr("task_worker_api.client.BackendClient", Client)
    monkeypatch.setattr(admission_supervisor, "run_cycle", cycle)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(admission_supervisor.run(path), timeout=5)
    assert retry_delays == [5, 10, 20, 40, 40][:startup_failures]


def test_supervisor_singleton_releases_lock_on_exit(tmp_path):
    from task_worker_api.admission_supervisor import _singleton

    path = tmp_path / "supervisor.lock"
    with _singleton(path):
        with pytest.raises(OSError):
            with _singleton(path):
                pytest.fail("second supervisor acquired the same lock")
    with _singleton(path):
        pass


def supervisor(tmp_path, monkeypatch):
    host, boot = uuid4(), uuid4()
    monkeypatch.setattr("task_worker_api.docker_supervisor.physical_boot_id", lambda *args: boot)
    instance = DockerSupervisor(tmp_path / "supervisor.sqlite", tmp_path / "work", authority_id=uuid4(),
                                host_id=host, boot_id=boot, epoch=1, execution_scope="test-scope", cgroup_parent="", network_id="d" * 64)
    claim = SimpleNamespace(host_id=host, boot_id=boot, state="reserved", gpu_uuid="GPU-test", execution_scope="test-scope",
        staging_deadline=datetime.now(timezone.utc) + timedelta(seconds=300),
        ownership=AttemptOwnership(worker_instance_id=uuid4(), attempt_id=uuid4(), generation=1, token="x" * 32),
        profile=SimpleNamespace(execution_ram_mib=100, cpu_millicores=1000, gpu_backend="cuda"))
    return instance, claim


@pytest.mark.parametrize("backend,changed", [("cuda", None), ("vulkan", None),
    ("vulkan", "NVIDIA_DRIVER_CAPABILITIES"), ("vulkan", "SPECTRA_GPU_ID"), ("vulkan", "SPECTRA_GPU_BACKEND"),
    ("vulkan", "runtime")])
@pytest.mark.parametrize("windows_host", [False, True])
def test_launch_is_durable_gpu_pinned_and_never_restarted(tmp_path, monkeypatch, backend, changed, windows_host):
    instance, claim = supervisor(tmp_path, monkeypatch)
    claim.profile.gpu_backend = backend
    monkeypatch.setattr("task_worker_api.docker_supervisor.os.getuid", lambda: 1000, raising=False)
    monkeypatch.setattr("task_worker_api.docker_supervisor.os.getgid", lambda: 1001, raising=False)
    if windows_host:
        monkeypatch.delattr("task_worker_api.docker_supervisor.os.getuid")
    calls = []
    container = "a" * 64

    def docker(*args, timeout=30):
        calls.append(args)
        if args[0] == "create":
            assert 290 < timeout <= 300
            assert instance._row(claim)[1] == "creating"
            assert args[args.index("--gpus") + 1] == "device=GPU-test"
            assert args[args.index("--network") + 1] == instance.network_id
            assert args[args.index("--user") + 1] == ("1000:1000" if windows_host else "1000:1001")
            assert "USER=worker" in args and "HOME=/work" in args
            assert "XDG_CACHE_HOME=/work/.cache" in args
            assert ("NVIDIA_DRIVER_CAPABILITIES=graphics,utility" in args) == (backend == "vulkan")
            if backend == "vulkan":
                assert "SPECTRA_GPU_BACKEND=vulkan" in args and "SPECTRA_GPU_ID=GPU-test" in args
                assert "--runtime=nvidia" in args
            return container
        if args[0] == "inspect":
            plan = instance._row(claim)[0]
            environment = dict(plan["environment"])
            if changed:
                environment[changed] = "altered"
            return json.dumps([dict(Id=container, Image=plan["image"], Config={"User": plan["user"], "Labels": {"synpusher.launch": plan["id"]},
                "Env": [f"{key}={value}" for key, value in environment.items()]},
                NetworkSettings={"Networks": {"test": {"NetworkID": plan["network_id"]}}},
                HostConfig=dict(Runtime="nvidia" if backend == "vulkan" and changed != "runtime" else "runc",
                    NetworkMode=plan["network_id"], Privileged=False, PidMode="", CapAdd=None, Devices=[], RestartPolicy={"Name": "no"}, CgroupParent="",
                    DeviceRequests=[{"DeviceIDs": ["GPU-test"], "Count": 0}], Memory=100 * 1024 * 1024, NanoCpus=1_000_000_000,
                    ReadonlyRootfs=True, CapDrop=["ALL"], SecurityOpt=["no-new-privileges"]))])
        assert args == ("start", container)
        assert instance._row(claim)[1:3] == ("starting", container)
        raise TimeoutError("lost start response")

    monkeypatch.setattr(instance, "_docker", docker)
    with pytest.raises(AdmissionError if changed else TimeoutError):
        instance.launch(claim, image="sha256:" + "b" * 64, command=["python", "-V"], read_only_mounts={})
    with pytest.raises(AdmissionError, match="reconciliation"):
        instance.launch(claim, image="sha256:" + "b" * 64, command=["python", "-V"], read_only_mounts={})
    assert [call[0] for call in calls] == (["create", "inspect"] if changed else ["create", "inspect", "start"])


def test_docker_rejects_dx12_and_vulkan_capability_overrides(tmp_path, monkeypatch):
    instance, claim = supervisor(tmp_path, monkeypatch)
    options = dict(image="sha256:" + "b" * 64, command=["python", "-V"], read_only_mounts={})
    claim.profile.gpu_backend = "dx12"
    with pytest.raises(AdmissionError, match="unsupported_gpu_backend"):
        instance.launch(claim, **options)
    claim.profile.gpu_backend = "vulkan"
    with pytest.raises(ValueError, match="supervisor-owned"):
        instance.launch(claim, **options, environment={"NVIDIA_DRIVER_CAPABILITIES": "all"})
    with pytest.raises(AdmissionError, match="launch_unknown"):
        instance._row(claim)


def test_operator_model_settings_cannot_override_gpu_or_authority(tmp_path, monkeypatch):
    instance, claim = supervisor(tmp_path, monkeypatch)
    for name in ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "SYNPUSHER_TARGETS", "SPECTRA_GPU_BACKEND", "SPECTRA_GPU_ID"):
        with pytest.raises(ValueError, match="supervisor-owned"):
            instance.launch(claim, image="sha256:" + "a" * 64, command=["python", "-V"],
                            read_only_mounts={}, environment={name: "all"})


@pytest.mark.parametrize("finalizer_type", ["finalize_spatial", "finalize_gs", "finalize_render", "finalize_segment", "finalize_synthetic", "finalize_gs4d", "finalize_model", "finalize_cinematic", "finalize_deploy", "finalize_deploy_prep"])
def test_publication_mount_is_backend_cpu_only_and_inspected(tmp_path, monkeypatch, finalizer_type):
    instance, claim = supervisor(tmp_path, monkeypatch)
    publication = tmp_path / "publication"
    publication.mkdir()
    command = ["python", "-m", "src.services.resource_finalizer_worker", "/run/worker-config.json"]
    options = dict(image="sha256:" + "b" * 64, command=command, read_only_mounts={}, publication_directory=publication)
    with pytest.raises(ValueError, match="backend CPU"):
        instance.launch(claim, **options)
    claim.gpu_uuid = None
    claim.profile.gpu_count = 0
    claim.task = SimpleNamespace(task_type="spatial_recon")
    with pytest.raises(ValueError, match="backend CPU"):
        instance.launch(claim, **options)
    claim.task.task_type = finalizer_type
    container = "c" * 64
    mounted_source = publication
    changed_user = None
    extra_network = False

    def docker(*args, timeout=30):
        if args[0] == "create":
            assert "--gpus" not in args and "--runtime=runc" in args
            assert f"type=bind,source={publication},target=/app/shared" in args
            return container
        if args[0] == "inspect":
            plan = instance._row(claim)[0]
            return json.dumps([dict(Id=container, Image=plan["image"], Config={"User": changed_user or plan["user"], "Labels": {"synpusher.launch": plan["id"]}},
                State={"Running": True}, Mounts=[dict(Type="bind", Source=str(mounted_source), Destination="/app/shared", RW=True)],
                NetworkSettings={"Networks": {"test": {"NetworkID": plan["network_id"]},
                    **({"foreign": {"NetworkID": "e" * 64}} if extra_network else {})}},
                HostConfig=dict(NetworkMode=plan["network_id"], Privileged=False, PidMode="", CapAdd=None, Devices=[], RestartPolicy={"Name": "no"}, CgroupParent="",
                    DeviceRequests=[], Memory=100 * 1024 * 1024, NanoCpus=1_000_000_000,
                    ReadonlyRootfs=True, CapDrop=["ALL"], SecurityOpt=["no-new-privileges"]))])
        assert args == ("start", container)
        return container

    monkeypatch.setattr(instance, "_docker", docker)
    assert instance.launch(claim, **options) == container
    assert instance.running(claim)
    extra_network = True
    with pytest.raises(AdmissionError, match="configuration_changed"):
        instance.running(claim)
    extra_network = False
    original_network = instance.network_id
    instance.network_id = "e" * 64
    with pytest.raises(AdmissionError, match="attempt_fenced"):
        instance.running(claim)
    instance.network_id = original_network
    changed_user = "2000:2000"
    with pytest.raises(AdmissionError, match="configuration_changed"):
        instance.running(claim)
    changed_user = None
    mounted_source = tmp_path
    with pytest.raises(AdmissionError, match="configuration_changed"):
        instance.running(claim)


def test_ambiguous_removal_reconciles_exact_id_before_proof(tmp_path, monkeypatch):
    instance, claim = supervisor(tmp_path, monkeypatch)
    plan = dict(id="unique-launch", ownership=claim.ownership.model_dump(mode="json"), root=str(instance.root),
                host=str(instance.host_id), boot=str(instance.boot_id), authority=str(instance.authority_id), epoch=1,
                scope="test-scope", cgroup_parent="", network_id=instance.network_id)
    import sqlite3
    with sqlite3.connect(instance.journal) as db:
        db.execute("INSERT INTO launches VALUES(?,?,'removing',?,NULL)", [str(claim.ownership.attempt_id), json.dumps(plan), "c" * 64])
    directory = instance.root / str(claim.ownership.attempt_id)
    directory.mkdir()
    (directory / "scratch").write_text("old attempt")
    calls = []

    def docker(*args, timeout=30):
        calls.append(args)
        assert args[:3] == ("container", "ls", "--all")
        assert "id=" + "c" * 64 in args
        return ""

    monkeypatch.setattr(instance, "_docker", docker)
    proof = instance.cleanup(claim)
    assert proof.processes_stopped and proof.models_evicted and proof.scratch_cleaned
    assert not directory.exists()
    assert instance.cleanup(claim) == proof and len(calls) == 1
    with pytest.raises(AdmissionError, match="reconciliation"):
        instance._record(claim, "starting", "c" * 64)


@pytest.mark.parametrize("network", ["host", "bridge", "none", "container:abc", "friendly-network", ""])
def test_supervisor_requires_immutable_network_identity(tmp_path, network):
    with pytest.raises(ValueError, match="network ID"):
        DockerSupervisor(tmp_path / "private.sqlite", tmp_path / "work", authority_id=uuid4(),
                         host_id=uuid4(), boot_id=uuid4(), epoch=1, execution_scope="/", cgroup_parent="", network_id=network)
    assert not (tmp_path / "private.sqlite").exists()


def test_oom_evidence_survives_ambiguous_removal(tmp_path, monkeypatch):
    import sqlite3

    instance, claim = supervisor(tmp_path, monkeypatch)
    plan = dict(id="launch", ownership=claim.ownership.model_dump(mode="json"), root=str(instance.root),
                host=str(instance.host_id), boot=str(instance.boot_id), authority=str(instance.authority_id), epoch=1,
                scope="test-scope", cgroup_parent="", network_id=instance.network_id)
    container = "c" * 64
    with sqlite3.connect(instance.journal) as db:
        db.execute("INSERT INTO launches VALUES(?,?,'started',?,NULL)",
                   [str(claim.ownership.attempt_id), json.dumps(plan), container])
    directory = instance.root / str(claim.ownership.attempt_id)
    directory.mkdir()
    monkeypatch.setattr(instance, "_inspect", lambda *args: {
        "Id": container, "State": {"Running": False, "Pid": 0, "OOMKilled": True}})

    def docker(*args, timeout=30):
        if args[0] == "rm":
            raise TimeoutError("removal succeeded but response lost")
        assert args[:3] == ("container", "ls", "--all")
        return ""

    monkeypatch.setattr(instance, "_docker", docker)
    with pytest.raises(TimeoutError):
        instance.cleanup(claim)
    assert directory.exists()
    assert json.loads(instance._row(claim)[3])["out_of_memory"]
    proof = instance.cleanup(claim)
    assert proof.out_of_memory and proof.scratch_cleaned and not directory.exists()
    assert instance.cleanup(claim) == proof


def test_expired_staging_never_creates_launch_intent(tmp_path, monkeypatch):
    instance, claim = supervisor(tmp_path, monkeypatch)
    claim.staging_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    monkeypatch.setattr(instance, "_docker", lambda *args, **kwargs: pytest.fail("expired launch reached Docker"))
    with pytest.raises(AdmissionError, match="staging_deadline_exceeded"):
        instance.launch(claim, image="sha256:" + "b" * 64, command=["python", "-V"], read_only_mounts={})
    with pytest.raises(AdmissionError, match="launch_unknown"):
        instance._row(claim)
