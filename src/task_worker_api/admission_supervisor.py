"""One durable claim/execute/reconcile/release cycle for the trusted supervisor.

The caller owns configuration, readiness, polling/backoff and periodic reports.
Only the worker claim journal is shared with the child; Docker launch history and
the signing key stay private. Backend replay is authoritative after every restart.
"""
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import sys
from time import monotonic
from uuid import UUID

import httpx

from .resources import AdmissionError
from .errors import ProtocolError


async def _log_resources(claim, read_report, event, started):
    """Best-effort host observations, never task peaks or cleanup evidence."""
    try:
        report = (await read_report()).report
        logging.getLogger(__name__).info("admission_resources %s", json.dumps({
            "event": event, "task_id": claim.task_id,
            "attempt_id": str(claim.ownership.attempt_id),
            "profile_id": claim.profile.profile_id, "revision": claim.profile.revision,
            "elapsed_seconds": round(monotonic() - started, 3),
            "observation": report.model_dump(mode="json"),
        }))
    except Exception:
        logging.getLogger(__name__).warning("Admission resource observation unavailable")


async def run_cycle(client, journal, worker_instance_id, task_types, supervisor, launch, read_report, signing_key):
    claim, delay = await client.resource_claim(journal, worker_instance_id, task_types, await read_report())
    if claim is None:
        journal.acknowledge_no_work(journal.pending()[0].claim_request_id)
        return delay
    started = monotonic()
    await _log_resources(claim, read_report, "claimed", started)
    last_sample = started
    try:
        supervisor._row(claim)
        recovered = True
    except AdmissionError as exc:
        if exc.code != "launch_unknown":
            raise
        recovered = False
    if not recovered:
        async def startup_heartbeat():
            while True:
                try:
                    state = await client.resource_heartbeat(claim)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 409:
                        return  # Terminal/fenced attempts proceed to owned cleanup.
                    raise
                if state.state != "reserved" or state.cancelled:
                    return
                await asyncio.sleep(5)

        heartbeat = asyncio.create_task(startup_heartbeat())
        try:
            await asyncio.to_thread(supervisor.launch, claim, **launch, journal_directory=journal.path.parent)
            while await asyncio.to_thread(supervisor.running, claim):
                if monotonic() - last_sample >= 30:
                    await _log_resources(claim, read_report, "active", started)
                    last_sample = monotonic()
                state = await client.resource_status(claim)
                if state.state not in ("reserved", "running") or state.cancelled:
                    break
                await asyncio.sleep(2)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
    # Recovery never resumes an old handler, even if its container is still alive.
    await asyncio.to_thread(supervisor.cleanup, claim)
    await _log_resources(claim, read_report, "cleanup_finished", started)
    try:
        await client.resource_recover_operations(journal)
    except httpx.HTTPStatusError as exc:
        state = await client.resource_status(claim)
        if exc.response.status_code != 409 or state.state not in ("releasing", "recovering", "released"):
            raise
        # Terminal/quarantined state fences any delayed old write. Preserve the
        # requests until release acknowledgement; never rerun their handler.
    state = await client.resource_status(claim)
    if state.state == "reserved":
        await client.resource_operation(journal, "decline", {"reason": "supervised process exited before start"})
    elif state.state == "running":
        await client.resource_operation(journal, "fail", {"error": "supervised process exited without an outcome"})
    proof = await asyncio.to_thread(supervisor.sign_cleanup, claim, signing_key)
    state = await client.resource_operation(journal, "release", {}, host_report=await read_report(), cleanup=proof)
    if state.state != "released":
        raise AdmissionError("cleanup_unverified")
    journal.acknowledge_release(claim.ownership.attempt_id)
    await _log_resources(claim, read_report, "released", started)
    await client.resource_ready(journal, worker_instance_id)
    return 0


@contextlib.contextmanager
def _singleton(path):
    with path.open("a+b") as file:
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield  # Closing the handle releases the OS lock, including on process exit.


async def run(config_path):
    """Operator-only CONFIG; periodic host_reporter is a separate service.

    Required: authority_id, host_id, boot_id, epoch, execution_scope, cgroup_parent, network_id,
    supervisor_journal, work_root, worker_journal_directory, image_id,
    worker_instance_id, worker_id, backend_url, worker_backend_url, credential_file, report_file,
    signing_key_file, handlers (task type -> module:function).
    All filesystem paths resolve relative to CONFIG. backend_url is reached from
    the host; worker_backend_url is reached from the child network namespace.
    Optional read_only_mounts maps host paths
    to container paths for model assets; no signing state may be included.
    Optional environment supplies the measured model/config/cache settings.
    A trusted backend CPU supervisor replaces handlers with backend_finalizer:
    publication_directory, artifact_root, response_key_file, database_url_file.
    It accepts backend CPU publication tasks and mounts the publication store read-write.
    Native Windows compute uses launcher="windows", python_executable and
    read_only_paths instead of image_id/cgroup_parent/network_id/read_only_mounts.
    Python must have the SDK and handler dependencies installed in its private
    runtime; it runs with -I. worker_backend_url must work from AppContainer.
    """
    from .client import BackendClient
    from .claim_journal import ClaimJournal
    from .docker_supervisor import DockerSupervisor
    from .resource_protocol import SignedHostReport

    config = json.loads(config_path.read_text())
    launcher = config.get('launcher', 'docker')
    if launcher not in ('docker', 'windows'):
        raise ValueError('Unknown supervisor launcher')
    native = launcher == 'windows'
    if native and (os.name != 'nt' or 'backend_finalizer' in config or 'read_only_mounts' in config):
        raise ValueError('Native Windows compute requires read_only_paths and a Windows host')
    def path(name):
        return (config_path.parent / config[name]).resolve()

    private_journal = path("supervisor_journal")
    private_journal.parent.mkdir(parents=True, exist_ok=True)
    worker_directory = path("worker_journal_directory")
    worker_directory.mkdir(parents=True, exist_ok=True)
    report_file, credential_file, key_file = path("report_file"), path("credential_file"), path("signing_key_file")
    worker_config = private_journal.parent / ("worker-" + str(UUID(config["worker_instance_id"])) + ".json")
    mounts = {str(report_file.parent): "/run/host-report", str(credential_file): "/run/worker-credential",
              str(worker_config): "/run/worker-config.json"}
    if native:
        mounts = {source: source for source in mounts}
        for source in config['read_only_paths']:
            resolved = str((config_path.parent / source).resolve(strict=True))
            mounts[resolved] = resolved
        python = path('python_executable')
        if not python.is_file() or not any(python.is_relative_to(Path(source)) for source in mounts):
            raise ValueError('Native Python must be inside a granted runtime path')
    finalizer = config.get("backend_finalizer")
    publication_directory = None
    worker_options = {}
    module = "task_worker_api.admitted_worker"
    if finalizer is not None:
        if "handlers" in config:
            raise ValueError("backend finalizer cannot register compute handlers")
        module = "src.services.resource_finalizer_worker"
        task_types = ["finalize_spatial", "finalize_gs", "finalize_render", "finalize_segment", "finalize_synthetic", "finalize_gs4d", "finalize_model", "finalize_cinematic", "finalize_deploy", "finalize_deploy_prep"]
        publication_directory = (config_path.parent / finalizer["publication_directory"]).resolve(strict=True)
        for name, target in (("artifact_root", "/run/committed-artifacts"),
                             ("response_key_file", "/run/backend-response-key"),
                             ("database_url_file", "/run/backend-database-url")):
            mounts[str((config_path.parent / finalizer[name]).resolve(strict=True))] = target
            worker_options[name] = target
    else:
        task_types = list(config["handlers"])
        worker_options["handlers"] = config["handlers"]
    for source, target in config.get("read_only_mounts", {}).items():
        mounts[str((config_path.parent / source).resolve())] = target
    for mounted in [worker_directory, path("work_root"), *map(Path, mounts), *([publication_directory] if publication_directory else [])]:
        if any(secret == mounted or secret.is_relative_to(mounted) for secret in (private_journal, key_file)):
            raise ValueError("worker mounts must exclude supervisor journal and signing key")
    if len(set(mounts.values())) != len(mounts):
        raise ValueError("duplicate worker mount targets")
    with _singleton(private_journal.with_suffix(".lock")):
        worker_config.write_text(json.dumps(dict(backend_url=config["worker_backend_url"], worker_id=config["worker_id"],
            credential_file=str(credential_file) if native else "/run/worker-credential",
            journal_file=str(worker_directory/'claim.sqlite') if native else "/run/worker-journal/claim.sqlite",
            report_file=str(report_file) if native else "/run/host-report/" + report_file.name,
            work_dir='.' if native else "/work", **worker_options)))
        binding = dict(authority_id=UUID(config["authority_id"]),
            host_id=UUID(config["host_id"]), boot_id=UUID(config["boot_id"]), epoch=config["epoch"],
            execution_scope=config["execution_scope"])
        if native:
            from .windows_supervisor import WindowsSupervisor
            path('work_root').mkdir(parents=True, exist_ok=True)
            supervisor = WindowsSupervisor(private_journal, path('work_root'), **binding)
        else:
            supervisor = DockerSupervisor(private_journal, path("work_root"), **binding,
                cgroup_parent=config["cgroup_parent"], network_id=config["network_id"])
        journal = ClaimJournal(worker_directory / "claim.sqlite")
        key = key_file.read_bytes()
        if len(key) < 32:
            raise ValueError("supervisor signing key requires at least 32 bytes")
        instance = UUID(config["worker_instance_id"])
        environment = dict(config.get("environment", {}))
        if finalizer is not None:
            environment["SHARED_DATA_PATH"] = "/app/shared"
        if native:
            launch = dict(command=[str(python), '-I', '-m', module, str(worker_config)],
                read_only_paths=list(mounts), network=True,
                environment={**{name: os.environ[name] for name in ('SystemRoot', 'LOCALAPPDATA', 'ProgramFiles')}, **environment})
        else:
            launch = dict(image=config["image_id"], command=["python", "-m", module, "/run/worker-config.json"],
                read_only_mounts=mounts, environment=environment, publication_directory=publication_directory)

        async def read_report():
            return SignedHostReport.model_validate_json(report_file.read_text())

        async with BackendClient(config["backend_url"], credential_file.read_text().strip()) as client:
            needs_ready = journal.pending() is None
            failures = 0
            while True:
                try:
                    if needs_ready:
                        await client.resource_ready(journal, instance)
                        needs_ready = False
                    delay = await run_cycle(client, journal, instance, task_types, supervisor, launch, read_report, key)
                    failures = 0
                except ProtocolError:
                    raise
                except Exception:
                    logging.exception("Supervisor cycle failed; retaining journal for reconciliation")
                    failures = min(failures + 1, 4)
                    delay = min(60, 5 * 2 ** (failures - 1))
                await asyncio.sleep(delay)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(Path(sys.argv[1]).resolve()))
