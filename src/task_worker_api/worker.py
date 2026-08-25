"""The Worker class — claim → run handler → complete.

Drops ~500 lines of boilerplate from every worker repo. A handler is just
``async def run(ctx: TaskContext, params: TypedParamsModel) -> dict``;
the Worker class owns claim, stage-inputs, heartbeat, cancel guard,
publish-outputs, error handling, and the polling loop.

Two modes of use:

1. Pure worker — ``asyncio.run(Worker(...).run_forever())``.
2. Hybrid — ``await run_hybrid(uvicorn.Server.serve(), worker)`` when the
   process also runs a FastAPI app (Neural-Canvas pattern).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import random
import shutil
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

from .cancel import CancelGuard
from .client import (
    BackendClient,
    _DEFAULT_BACKOFF_MAX_S,
    _JITTER_SPREAD,
    _TERMINAL_EXTRA_TRANSIENT,
    _is_transient_status,
)
from .context import ClaimedTask, TaskContext
from .enums import TaskType
from .errors import ProtocolError, TaskCancelled, TaskParamsError
from .files import prepare_inputs, upload_outputs
from .payload_log import PayloadLogger, sanitize_worker_id
from .progress import ProgressReporter
from .reports import TerminalReport, UnconfirmedReports
from .schemas import TASK_PARAMS_SCHEMAS, TaskParamsBase
from .timeouts import DEFAULT_TASK_TIMEOUT_S, parse_timeouts_env, resolve_task_timeout
from .watchdog import TaskWatchdog, TerminalGuard, list_descendants

log = logging.getLogger(__name__)

HandlerFn = Callable[[TaskContext, TaskParamsBase], Awaitable[dict]]

# Default ceiling for the escalated idle wait between poll cycles after
# consecutive claim failures. ``BackendClient._retry`` already backs off
# *within* one claim call, but that schedule resets every cycle: during a
# backend outage every fleet worker re-hammered the struggling backend with a
# fresh retry burst every ``poll_interval_s``, so aggregate load never decayed
# and a restarting backend got no room to recover. 60s matches the client's
# ``_DEFAULT_BACKOFF_MAX_S`` — long enough to thin the fleet's request rate by
# ~an order of magnitude, short enough that a recovered backend is picked up
# within a minute.
_DEFAULT_CLAIM_BACKOFF_MAX_S = _DEFAULT_BACKOFF_MAX_S


def _positive_finite_s(name: str, value: float) -> float:
    """Validate a poll-loop delay knob, or raise ``ValueError``.

    These two knobs are the poll loop's only pacing, and every degenerate
    value breaks it in a way that is silent at construction and expensive in
    production: ``0``/negative spins the claim loop at full speed against the
    backend, ``inf`` makes the first idle wait never end (the worker stops
    polling for good), and ``NaN`` fails every comparison in ``_claim_wait_s``
    — so the cap never matches, the doubling loop runs once per accumulated
    failure, and ``asyncio.wait_for(timeout=nan)`` waits forever. Rejecting
    them here is what makes the capped-and-still-polling guarantee real.
    """
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{name} must be a finite positive number of seconds "
            f"(got {value!r})"
        )
    return float(value)


def _make_sync_fail(
    base_url: str, api_key: str, task_id: int, worker_id: str, *, timeout_s: float = 3.0,
    attempts: int = 3, retry_sleep_s: float = 2.0,
    idempotency_key: Optional[str] = None,
):
    """Build a synchronous ``fail(error)`` callable for the watchdog thread.

    The async BackendClient is bound to the (possibly blocked) event loop, so
    the watchdog's last-resort report uses plain stdlib urllib with an explicit
    short timeout — it must never become a second wedge. Matches the wire
    format of ``BackendClient.fail``: PUT /tasks/{id}/fail {"error": ...}.

    Retries a few times with a short sleep (bounded ~15s total with the
    defaults): this is the only report path for a watchdog-fired timeout, and
    a single attempt against a momentarily unavailable backend (restart, DB
    blip) would silently orphan the task as RUNNING until the sweeper.

    Those retries re-PUT a report that may already have been applied (a lost
    response looks exactly like a lost write from here), so the attempt's
    ``idempotency_key`` rides along on the same ``Idempotency-Key`` header
    ``BackendClient.fail`` sends — the async and watchdog paths report the
    same logical failure under the same name.
    """
    url = (
        f"{base_url.rstrip('/')}/tasks/{task_id}/fail"
        f"?worker_id={urllib.parse.quote(worker_id, safe='')}"
    )

    def _sync_fail(error: str) -> None:
        data = json.dumps({"error": error}).encode("utf-8")
        last_exc: Exception | None = None
        for attempt in range(attempts):
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
            try:
                urllib.request.urlopen(req, timeout=timeout_s).close()
                return
            except Exception as e:  # noqa: BLE001 — retried, re-raised on exhaustion
                last_exc = e
                if attempt < attempts - 1:
                    time.sleep(retry_sleep_s)
        assert last_exc is not None
        raise last_exc

    return _sync_fail


def _clear_workdir(task_dir: Path) -> None:
    """Remove a leftover attempt's workdir, or raise. Runs in a thread.

    Fails *closed*: the caller runs this before staging inputs, and the whole
    point is that nothing from a dead attempt survives into this one. A
    best-effort delete (``ignore_errors=True``) would let a permission error,
    a filesystem fault, or a partial deletion leave stale ``out/`` files in
    place while the attempt runs on to publish them by name as its own fresh
    result — the exact data-integrity bug this removal exists to prevent. So
    an unremovable leftover aborts the attempt instead: the task fails, is
    re-queued, and an operator sees the error rather than a silently wrong
    output.

    A missing directory is the normal first-attempt case, not a failure. The
    explicit ``exists`` check afterwards covers deletions that *don't* raise:
    ``rmtree`` can hit ``FileNotFoundError`` on one entry that vanished
    mid-walk and leave every sibling behind, so "no exception" alone is not
    evidence the tree is gone.
    """
    try:
        shutil.rmtree(task_dir)
    except FileNotFoundError:
        pass
    if task_dir.exists():
        raise RuntimeError(
            f"stale workdir {task_dir} survived removal; refusing to run this "
            "attempt in a dead attempt's directory (its outputs would be "
            "published as this attempt's results)"
        )


#: Client overrides already warned about (one WARNING per process, per name).
_warned_legacy_terminal: set = set()


def _idempotency_kwarg(report_fn, key: str) -> dict:
    """``{"idempotency_key": key}`` if ``report_fn`` accepts it, else ``{}``.

    Mirrors ``files._cancel_kwarg``, for the same reason and against the same
    kind of consumer: someone who subclassed ``BackendClient`` (or wrote a test
    double) against the older ``complete(task_id, result)`` /
    ``fail(task_id, error)`` signature must keep working across this upgrade.
    A ``TypeError`` raised here would break the one call whose entire job is to
    make sure a finished task's outcome gets recorded — the worst possible
    place to demand a lockstep migration.

    Such an override only loses the dedupe *name*: the report itself is
    unchanged, and SynPusher's guarded terminal transitions already collapse a
    duplicate terminal write. One WARNING per process names the override so it
    can be updated.
    """
    try:
        params = inspect.signature(report_fn).parameters
    except (TypeError, ValueError):  # pragma: no cover — unintrospectable
        # Can't tell; assume the current signature rather than silently
        # dropping the key for a client that does support it.
        return {"idempotency_key": key}
    if "idempotency_key" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    ):
        return {"idempotency_key": key}
    name = getattr(report_fn, "__qualname__", str(report_fn))
    if name not in _warned_legacy_terminal:
        _warned_legacy_terminal.add(name)
        log.warning(
            "%s has no 'idempotency_key' parameter; terminal reports from this "
            "worker will not carry an Idempotency-Key header, so a re-sent "
            "report cannot be deduped by key at the backend. Add "
            "'idempotency_key: Optional[str] = None' as a keyword-only "
            "parameter and forward it.",
            name,
        )
    return {}


def _result_encode_error(result: object) -> Optional[str]:
    """The error ``complete()`` would raise encoding ``result``, or ``None``.

    Asks httpx itself — building a request encodes the body, and building one
    transmits nothing — rather than re-implementing its JSON encoding. The
    encoder's flags have moved across the httpx 0.23-0.28 range this SDK
    supports (0.28 passes ``allow_nan=False``, so a NaN metric that older
    httpx would have put on the wire is now rejected), and a check stricter
    than the real encoder would fail a task the backend would have accepted.
    """
    try:
        httpx.Request(
            "PUT", "http://encode-check.invalid/", json={"result": result},
        )
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return None


class Worker:
    """Glues everything together. One instance per worker process.

    Construction:
        worker = Worker(
            backend_url="http://backend:5000/api/v1",
            api_key=os.environ["WORKER_API_KEY"],
            worker_id="blender-worker-1",
            handlers={
                TaskType.DETECT_CUT_PLANES: detect_cut_planes.run,
                TaskType.MODEL_INITIALIZING: model_initializing.run,
            },
        )
        await worker.run_forever()
    """

    def __init__(
        self,
        *,
        backend_url: str,
        api_key: str,
        worker_id: str,
        handlers: dict[TaskType, HandlerFn],
        work_dir: Optional[str] = None,
        shared_volume_path: Optional[str] = None,
        poll_interval_s: float = 5.0,
        claim_backoff_max_s: float = _DEFAULT_CLAIM_BACKOFF_MAX_S,
        heartbeat_interval_s: float = 10.0,
        heartbeat_warn_threshold: int = 3,
        cancel_poll_interval_s: float = 2.0,
        request_timeout_s: float = 30.0,
        file_timeout_s: float = 300.0,
        cancel_timeout_s: float = 5.0,
        lifecycle_timeout_s: float = 15.0,
        max_retries: int = 4,
        retry_backoff_s: float = 2.0,
        retry_backoff_max_s: Optional[float] = _DEFAULT_BACKOFF_MAX_S,
        retry_sleep_budget_s: Optional[float] = None,
        retry_jitter: bool = True,
        task_timeout_s: float = DEFAULT_TASK_TIMEOUT_S,
        task_timeouts: Optional[dict] = None,
        timeout_grace_s: float = 15.0,
        on_hard_exit: Optional[Callable[[], None]] = None,
        client: Optional[BackendClient] = None,
        _watchdog_factory: Callable[..., object] = TaskWatchdog,
    ):
        self.backend_url = backend_url
        self.api_key = api_key
        self.worker_id = worker_id
        self.handlers = handlers
        self.work_dir = Path(
            work_dir or os.environ.get("WORKER_WORKDIR") or tempfile.gettempdir()
        )
        self.shared_volume_path = shared_volume_path
        self.poll_interval_s = _positive_finite_s(
            "poll_interval_s", poll_interval_s,
        )
        self.claim_backoff_max_s = _positive_finite_s(
            "claim_backoff_max_s", claim_backoff_max_s,
        )
        # Same flag the BackendClient uses for its per-call retry delays, and
        # for the same reason: the fleet's workers restart together (one deploy
        # rolls all three) and would otherwise share one deterministic
        # schedule. Tests that assert exact delays pass retry_jitter=False.
        self.retry_jitter = retry_jitter
        self.heartbeat_interval_s = heartbeat_interval_s
        self.heartbeat_warn_threshold = heartbeat_warn_threshold
        self.cancel_poll_interval_s = cancel_poll_interval_s
        self.task_timeout_s = task_timeout_s
        self.task_timeouts = task_timeouts or {}
        self.timeout_grace_s = timeout_grace_s
        self._on_hard_exit = on_hard_exit or (lambda: os._exit(75))
        self._timeout_env = parse_timeouts_env(os.environ.get("WORKER_TASK_TIMEOUTS"))
        self._watchdog_factory = _watchdog_factory

        self._payload_logger = self._build_payload_logger()

        if client is None:
            self._client = BackendClient(
                backend_url, api_key, timeout_s=request_timeout_s,
                worker_id=worker_id,
                file_timeout_s=file_timeout_s,
                cancel_timeout_s=cancel_timeout_s,
                lifecycle_timeout_s=lifecycle_timeout_s,
                max_retries=max_retries,
                retry_backoff_s=retry_backoff_s,
                retry_backoff_max_s=retry_backoff_max_s,
                retry_sleep_budget_s=retry_sleep_budget_s,
                retry_jitter=retry_jitter,
                payload_logger=self._payload_logger,
            )
        else:
            # Externally-supplied client (e.g. FakeBackendClient in tests) is
            # used as-is; we don't reach in and rewrite its state.
            self._client = client
        self._stop = asyncio.Event()
        # Consecutive failed claim round-trips; drives the escalating idle
        # wait in run_forever. Reset by any claim that reaches the backend.
        self._claim_failures = 0
        # Terminal outcomes the backend never confirmed. The poll loop
        # re-sends them once the backend is answering again; a re-delivery of
        # the same task drops the stale one and runs the handler. See
        # task_worker_api.reports.
        self._unconfirmed = UnconfirmedReports()

        # Fail fast on misconfiguration: an empty handlers dict makes
        # task_types=[] in run_forever's poll loop, so the worker silently
        # polls forever without ever processing work. Operators only notice
        # via the absence of activity. Same shape of misconfiguration as
        # the missing-schema case below.
        if not self.handlers:
            raise ProtocolError(
                "handlers is empty; Worker has nothing to claim. "
                "Register at least one TaskType→handler mapping."
            )

        # Fail fast on misconfiguration: any handler whose TaskType has no
        # registered params schema would only blow up after claim_next pulls
        # the first matching task off the queue, burning retry budget. Same
        # check lives in _run_one as defense-in-depth for code paths that
        # mutate handlers post-construction.
        missing = [t.value for t in self.handlers if t not in TASK_PARAMS_SCHEMAS]
        if missing:
            raise ProtocolError(
                "no schema registered for task_type "
                f"{', '.join(sorted(missing))}; "
                "update task-worker-api or register one locally"
            )

        # Fail fast on misconfiguration: when an external client is supplied,
        # its base URL must match (equal or be a prefix of) the worker's
        # backend_url. A mismatch silently routes every request to the wrong
        # endpoint — claim, complete, and file transfer all hit a host that
        # doesn't know this worker_id, so tasks are claimed-but-never-completed
        # or completed-against-the-wrong-tenant. Real BackendClient always
        # carries base_url; test doubles (FakeBackendClient) may not, and are
        # skipped — they don't make real HTTP calls, so URL provenance is moot.
        client_base_url = getattr(self._client, "base_url", None)
        if client_base_url is not None:
            client_norm = str(client_base_url).rstrip("/")
            worker_norm = self.backend_url.rstrip("/")
            if not (
                client_norm == worker_norm
                or worker_norm.startswith(client_norm + "/")
                or client_norm.startswith(worker_norm + "/")
            ):
                log.warning(
                    "backend_url %r and client base_url %r disagree; "
                    "requests will hit the wrong endpoint.",
                    self.backend_url, client_base_url,
                )
                raise ProtocolError(
                    f"backend_url {self.backend_url!r} and the supplied "
                    f"client's base_url {client_base_url!r} are not "
                    f"compatible; the client must target the same backend. "
                    f"Pass a client whose base_url matches backend_url."
                )

    def _build_payload_logger(self) -> PayloadLogger:
        """Construct a PayloadLogger from env + shared_volume_path.

        When shared_volume_path is None the logger is disabled (no place to
        write). When the env var WORKER_PAYLOAD_LOG_ENABLED is "false" it's
        disabled regardless. Bad WORKER_PAYLOAD_LOG_RETENTION_DAYS values
        fall back to 14 days with a WARNING.
        """
        env_enabled = (
            os.environ.get("WORKER_PAYLOAD_LOG_ENABLED", "true").lower() != "false"
        )
        enabled = bool(self.shared_volume_path) and env_enabled

        retention_raw = os.environ.get("WORKER_PAYLOAD_LOG_RETENTION_DAYS", "14")
        try:
            retention = int(retention_raw)
            if retention < 1:
                raise ValueError(f"retention must be >= 1, got {retention}")
        except (ValueError, TypeError):
            log.warning(
                "payload_log: WORKER_PAYLOAD_LOG_RETENTION_DAYS=%r is invalid; "
                "falling back to 14 days",
                retention_raw,
            )
            retention = 14

        if self.shared_volume_path:
            root = (
                Path(self.shared_volume_path)
                / "_worker_payloads"
                / sanitize_worker_id(self.worker_id)
            )
        else:
            # Placeholder; logger is disabled so it's never touched.
            root = Path("/__payload_log_disabled__")

        return PayloadLogger(
            root=root,
            worker_id=self.worker_id,
            retention_days=retention,
            enabled=enabled,
        )

    @property
    def task_types(self) -> list[TaskType]:
        """Types this worker can claim, derived from registered handlers."""
        return list(self.handlers.keys())

    async def shutdown(self) -> None:
        """Ask the polling loop to exit after the current task finishes."""
        self._stop.set()

    async def run_forever(self) -> None:
        """Main polling loop. Returns when shutdown() is called."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "task-worker-api Worker starting: id=%s url=%s types=%s",
            self.worker_id, self.backend_url,
            ",".join(t.value for t in self.task_types),
        )
        if self._payload_logger.enabled:
            log.info(
                "payload logging: enabled, root=%s, retention=%dd",
                self._payload_logger.root, self._payload_logger.retention_days,
            )
        else:
            log.info(
                "payload logging: disabled (shared_volume_path=%r, env=%r)",
                self.shared_volume_path,
                os.environ.get("WORKER_PAYLOAD_LOG_ENABLED", "true"),
            )
        self._payload_logger.cleanup_old_files()

        cleanup_interval_s = float(
            os.environ.get("WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S", "3600")
        )
        cleanup_task = asyncio.create_task(
            self._periodic_cleanup_loop(cleanup_interval_s)
        )

        try:
            while not self._stop.is_set():
                # Settle outcomes the backend never confirmed, on *every*
                # cycle rather than only idle ones. A worker whose queue never
                # empties never sees an idle cycle, so an idle-only flush
                # would hold those reports until the bounded ledger evicted
                # them — re-creating the dropped-outcome bug the ledger exists
                # to prevent, on exactly the busy fleet that can least afford
                # to recompute a task. Here at the top of the cycle the worker
                # holds no claim and no heartbeat: the previous task is
                # terminal and the next is not claimed yet, so a slow re-send
                # can't strand a task the sweeper would then reclaim.
                if len(self._unconfirmed) and not self._claim_failures:
                    await self._flush_unconfirmed_reports()
                claimed = await self._claim()
                if claimed is None:
                    wait_s = self._claim_wait_s()
                    if self._claim_failures:
                        log.info(
                            "claim has failed %d consecutive times; "
                            "backing off %.1fs before the next poll",
                            self._claim_failures, wait_s,
                        )
                    try:
                        # Waiting on _stop (rather than sleeping) is what keeps
                        # shutdown responsive: an escalated 60s wait would
                        # otherwise delay process exit by up to a minute.
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=wait_s,
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._run_one(claimed)
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            self._payload_logger.close()
            if len(self._unconfirmed):
                # Last chance to say it out loud: the process is going away
                # and the ledger with it, so these outcomes are lost for good
                # and their tasks will be swept as stale and recomputed.
                log.error(
                    "worker stopping with %d terminal report(s) the backend "
                    "never confirmed (task ids: %s); those outcomes are lost",
                    len(self._unconfirmed),
                    ", ".join(
                        str(r.task_id) for r in self._unconfirmed.pending()
                    ),
                )
            await self._client.close()
            log.info("task-worker-api Worker stopped: id=%s", self.worker_id)

    async def _periodic_cleanup_loop(self, interval_s: float) -> None:
        """Background timer that re-runs cleanup so idle workers stay honest.

        Without this, a worker that wrote files 30 days ago and then sat
        dormant would never clean up — rollover-triggered cleanup only fires
        on the next write. The timer fires every interval_s seconds; when
        shutdown is requested, the loop returns immediately.
        """
        try:
            while True:
                await asyncio.sleep(interval_s)
                if self._stop.is_set():
                    return
                self._payload_logger.cleanup_old_files()
        except asyncio.CancelledError:
            raise

    async def run_one(self) -> bool:
        """Process exactly one claim cycle. Returns True iff a task ran.

        Test seam — production code uses ``run_forever``.
        """
        claimed = await self._claim()
        if claimed is None:
            return False
        await self._run_one(claimed)
        return True

    # ----- internals ----------------------------------------------

    def _claim_wait_s(self) -> float:
        """Idle wait before the next poll, escalated by claim failures.

        ``poll_interval_s`` while claims are landing, then doubling per
        consecutive failure up to ``claim_backoff_max_s`` — so a fleet's
        request rate against a down backend decays exponentially instead of
        holding steady at one burst per worker per poll interval.

        Escalated waits are jittered (±25%, shared with the client's retry
        schedule). Decay alone doesn't fix the herd: the fleet's workers are
        deployed and restarted together, so they enter the outage in lockstep
        and a *deterministic* schedule keeps them there — every worker's 8th
        failure lands on the same instant, and a backend that comes back mid-
        backoff is hit by the whole fleet at once instead of a spread. Jitter
        is what decorrelates them; the doubling only thins the average rate.

        Doubling iteratively (rather than ``poll_interval_s * 2 ** failures``)
        keeps a pathological failure count — a worker left running for days
        against a dead backend — from overflowing the float and yielding
        ``inf``: the loop stops as soon as the cap is reached. The returned
        wait — jitter included — always lands in ``[poll_interval_s, cap]``,
        where the cap is floored at ``poll_interval_s`` so a consumer that
        configures a ``claim_backoff_max_s`` below their poll interval never
        polls *faster* while failing than while healthy.
        """
        if self._claim_failures <= 0:
            # Healthy polling is exactly poll_interval_s — unchanged, and no
            # herd to break up: these claims are landing.
            return self.poll_interval_s
        cap = max(self.claim_backoff_max_s, self.poll_interval_s)
        delay = self.poll_interval_s
        for _ in range(self._claim_failures):
            delay *= 2
            if delay >= cap:
                delay = cap
                break
        if not self.retry_jitter:
            return delay
        spread = delay * _JITTER_SPREAD
        return random.uniform(
            max(delay - spread, self.poll_interval_s),
            min(delay + spread, cap),
        )

    async def _claim(self) -> Optional[ClaimedTask]:
        try:
            claimed = await self._client.claim_next(
                self.task_types, worker_id=self.worker_id,
            )
        except Exception as e:  # noqa: BLE001
            # BackendClient._retry already exhausted its per-call attempts, so
            # this is a backend that stayed unreachable across the whole retry
            # window. Count it so the poll loop widens its idle wait.
            self._claim_failures += 1
            log.warning(
                "claim failed against %s (%d consecutive): %s",
                self.backend_url, self._claim_failures, e,
            )
            return None
        # A completed round-trip — including an empty queue (None) — means the
        # backend is answering, so the escalated wait collapses back to
        # poll_interval_s immediately.
        self._claim_failures = 0
        return claimed

    async def _put_terminal(self, report: TerminalReport) -> None:
        """Send one terminal report through the client. Raises on failure.

        The single place the async paths call either terminal route, so every
        send — first attempt and poll-loop re-send alike — carries the
        report's ``Idempotency-Key`` and nothing else has to remember which
        client method goes with which outcome. A client override predating the
        key still gets its report; see :func:`_idempotency_kwarg`.
        """
        report_fn = (
            self._client.complete if report.kind == "complete"
            else self._client.fail
        )
        await report_fn(
            report.task_id, report.payload,
            **_idempotency_kwarg(report_fn, report.idempotency_key),
        )

    async def _flush_unconfirmed_reports(self) -> None:
        """Re-send terminal reports the backend never confirmed. Never raises.

        Driven from the top of every poll cycle — busy or idle — but only
        while the last claim round-trip reached the backend: one re-send costs
        up to ``_TERMINAL_MIN_ATTEMPTS`` × ``lifecycle_timeout_s`` plus
        backoff, so firing it into a dead backend would stall the loop *and*
        undo what the claim backoff exists for — thinning the fleet's request
        rate while the backend is struggling.

        Per entry:

        * accepted → drop it; the task is terminal at last.
        * refused with a *permanent* 4xx → the backend answered and will not
          take this report from this worker (almost always the ownership
          check, after the sweeper handed the task to someone else). Drop it:
          whoever owns the task now reports its outcome, and re-sending on a
          timer would just repeat the same rejection every poll cycle while
          holding a slot in the bounded ledger.
        * anything else (transport error, a 5xx that outlived the client's own
          retries, or a *transient* 4xx — 408/429 — whose retry budget the
          client merely exhausted) → the backend is degraded again. Keep the
          entry and end the flush for this cycle rather than walking the rest
          of the ledger into the same wall.
        """
        for report in self._unconfirmed.pending():
            try:
                await self._put_terminal(report)
            except httpx.HTTPStatusError as exc:
                # Only a status that says "this task is not yours" retires an
                # entry. 408/429 are 4xx but transient — the client ran out of
                # retry budget, not out of ownership — so they take the
                # degraded path below and stay in the ledger; retiring a
                # rate-limited report here would drop the outcome for good,
                # which is the failure this ledger exists to prevent.
                if 400 <= exc.response.status_code < 500 and not (
                    _is_transient_status(exc, _TERMINAL_EXTRA_TRANSIENT)
                ):
                    self._unconfirmed.discard(report.task_id)
                    log.warning(
                        "task %s: backend refused the unconfirmed %s report "
                        "with HTTP %d; it is no longer ours to report, so the "
                        "outcome is dropped — whoever owns the task now "
                        "reports it",
                        report.task_id, report.kind,
                        exc.response.status_code,
                    )
                    continue
                log.warning(
                    "task %s: re-sending the unconfirmed %s report failed "
                    "(HTTP %d); will retry on a later poll cycle",
                    report.task_id, report.kind, exc.response.status_code,
                )
                break
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "task %s: re-sending the unconfirmed %s report failed "
                    "(%s); will retry on a later poll cycle",
                    report.task_id, report.kind, exc,
                )
                break
            else:
                self._unconfirmed.discard(report.task_id)
                log.info(
                    "task %s: unconfirmed %s report accepted on re-send; the "
                    "backend has the outcome",
                    report.task_id, report.kind,
                )

    async def _run_one(self, task: ClaimedTask) -> None:
        """Heartbeat → stage inputs → run handler → publish.

        The heartbeat is started *before* ``prepare_inputs`` so the backend's
        ``updated_at`` keeps ticking during the (potentially multi-minute)
        remote input download. Without it, a long download of GB-scale
        inputs (colmap-splat PLY files, Neural-Canvas splats) leaves the task
        row unchanged since claim time, and the stale-task sweeper can
        reclaim it while the worker is still fetching — the worker then
        processes and completes a task the backend no longer owns.

        The heartbeat also stays up *through* the terminal complete()/fail()
        report and is only stopped afterwards, for the same reason: those
        calls retry inside BackendClient and can span minutes against a
        degraded backend, and a task whose ``updated_at`` freezes for that
        window is exactly what the sweeper reclaims.

        The CancelGuard is also started *before* ``prepare_inputs`` and its
        ``cancelled`` event is threaded into both the download and upload
        loops. Remote-mode ``prepare_inputs`` can spend minutes streaming
        GB-scale inputs; a user cancel during that window must not wait for
        every remaining file to finish — the guard's poll detects the cancel
        and ``prepare_inputs`` raises ``TaskCancelled`` before downloading the
        next file. The same guard stays active through the handler *and* the
        ``upload_outputs`` phase: remote-mode uploads of GB-scale outputs
        can likewise take minutes, and a cancel during output publishing
        must abort between uploads rather than streaming every remaining
        file to a task the user already cancelled.

        Every attempt starts from a clean ``task_<id>`` workdir, so a task
        re-queued after a mid-task kill never inherits the dead attempt's
        staged inputs or outputs (see the removal below). That removal fails
        closed: a leftover workdir that cannot be removed aborts the attempt
        (reported as a failure) rather than letting it run — and publish —
        on top of another attempt's files.

        A per-task watchdog (when ``timeout`` > 0) enforces a wall-clock
        deadline off the event loop. Terminal reporting goes through a single
        TerminalGuard so a timeout and a near-simultaneous normal completion
        report exactly once (first resolver wins).

        A terminal report the backend never confirmed is kept (see
        :mod:`task_worker_api.reports`) so the poll loop can re-send it, but a
        re-delivery of that task always runs the handler — see below.
        """
        superseded = self._unconfirmed.take(task.id)
        if superseded is not None:
            # This delivery is a *new attempt*, and the run starting now owns
            # the outcome. Nothing in the claim envelope says otherwise: the
            # worker protocol carries no backend-issued attempt or lease id
            # (see ClaimedTask), so "the task whose report we lost came back"
            # is indistinguishable from "the backend legitimately re-queued
            # it" — a completion that committed while its response was lost,
            # then a requeue for a genuine re-run. Answering the second case
            # with the stored result would hand back a stale outcome and skip
            # the work that was actually asked for, so the stale report is
            # dropped (``take`` already removed it) and the handler runs.
            #
            # Keeping it would be worse than useless: a superseded fail()
            # re-sent later stamps ``failed`` over whatever this attempt
            # produces.
            #
            # Replaying a stored completion here — cheap as it looks when the
            # work really was already done — needs the backend to say which
            # attempt this is: an attempt/lease token in the claim envelope
            # that the report can be matched against, or an explicit backend
            # guarantee that a re-delivery implies the earlier report never
            # committed. Until one of those exists, re-running is the only
            # answer that is right in both cases.
            log.warning(
                "task %s: discarding the unconfirmed %s report from an earlier "
                "attempt; this delivery is a new attempt and supersedes it",
                task.id, superseded.kind,
            )

        # One identity per attempt, one key per terminal route. Every send of
        # this attempt's report — the client's own retries, the watchdog's
        # sync fail, a re-send from the ledger cycles later — carries the same
        # key, so a backend that dedupes on it applies the report once. A
        # later attempt on the same task (re-queued after a failure) mints a
        # fresh identity, so its report is not mistaken for a duplicate of
        # this one.
        attempt_id = uuid.uuid4().hex
        complete_key = f"task-{task.id}-complete-{attempt_id}"
        fail_key = f"task-{task.id}-fail-{attempt_id}"

        task_dir = self.work_dir / f"task_{task.id}"
        progress = ProgressReporter(
            self._client, task.id,
            heartbeat_interval_s=self.heartbeat_interval_s,
            heartbeat_warn_threshold=self.heartbeat_warn_threshold,
        )

        timeout_s = resolve_task_timeout(
            task.task_type,
            default_s=self.task_timeout_s,
            per_type=self.task_timeouts,
            env=self._timeout_env,
        )
        guard = TerminalGuard()
        wd = None
        if timeout_s > 0:
            log.info(
                "task %s: %s timeout=%.0fs",
                task.id, task.task_type.value, timeout_s,
            )
            wd = self._watchdog_factory(
                timeout_s=timeout_s,
                grace_s=self.timeout_grace_s,
                guard=guard,
                sync_fail=_make_sync_fail(
                    self.backend_url, self.api_key, task.id, self.worker_id,
                    idempotency_key=fail_key,
                ),
                on_hard_exit=self._on_hard_exit,
                children_before=list_descendants(os.getpid()),
            )
            wd.start()

        outcome: tuple[str, object] = ("fail", "unknown")
        try:
            # Capture BEFORE schema validation so malformed payloads — exactly
            # the bugs most worth replaying — still produce a typed-stream
            # record. record() never raises (see PayloadLogger contract).
            self._payload_logger.record(task)

            handler = self.handlers.get(task.task_type)
            if handler is None:
                raise ProtocolError(
                    f"no handler registered for task_type {task.task_type.value}"
                )

            params_schema = TASK_PARAMS_SCHEMAS.get(task.task_type)
            if params_schema is None:
                raise ProtocolError(
                    f"no schema registered for task_type {task.task_type.value}; "
                    "update task-worker-api or register one locally"
                )

            try:
                typed_params = params_schema(**task.params)
            except Exception as e:  # noqa: BLE001
                raise TaskParamsError(
                    f"task.params failed schema validation on claim: {e}"
                ) from e

            # Start the heartbeat *before* staging inputs. Remote-mode
            # prepare_inputs can spend minutes downloading GB-scale files
            # (colmap-splat PLYs, Neural-Canvas splats); without heartbeat
            # ticks during that window the backend's stale-task sweeper can
            # reclaim the task while we're still fetching it. The finally
            # block below always calls progress.stop(), so a failure in
            # prepare_inputs still tears the heartbeat down cleanly.
            await progress.start_heartbeat()

            # Each attempt starts from a clean workdir. The finally block
            # below removes ``task_dir`` after every attempt, but a mid-task
            # kill (OOM killer, SIGKILL, host reboot) never runs it and the
            # container filesystem survives the restart — so when the backend
            # re-queues the task and this worker claims it again, it computes
            # the *same* ``task_<id>`` path and finds the dead attempt's tree
            # still there. prepare_inputs would then stage this attempt's
            # inputs alongside stale ``in/`` files, and upload_outputs
            # publishes ``out/<filename>`` by name without checking who wrote
            # it — so any output the retry's handler doesn't (re)write is
            # published as the retry's fresh result from the dead attempt's
            # bytes. Unlike the best-effort cleanup in ``finally``, this one
            # fails closed (see _clear_workdir): if the leftover can't be
            # removed we abort the attempt rather than run on top of it. Off
            # the loop for the same reason as the ``finally`` cleanup, and
            # after start_heartbeat so a GB-scale leftover delete doesn't
            # stall the heartbeat into the stale-task sweeper's window. On a
            # first attempt the dir doesn't exist and this is a no-op.
            await asyncio.to_thread(_clear_workdir, task_dir)

            # Start the CancelGuard *before* prepare_inputs so a user cancel
            # during the (potentially multi-minute) input download is
            # detected instead of burning bandwidth to completion. The guard
            # yields its ``cancelled`` event; prepare_inputs checks it
            # between batch downloads and raises TaskCancelled. The same
            # guard then covers the handler and upload phases.
            async with CancelGuard(
                self._client, task.id,
                poll_interval_s=self.cancel_poll_interval_s,
            ) as cancelled:
                # Link the CancelGuard's ``cancelled`` event into the
                # ProgressReporter so handlers that poll
                # ``ctx.progress.is_cancelled`` (Neural-Canvas's
                # segmentation pipeline, colmap-splat's gs_build watcher)
                # learn of a cancel at the guard's poll latency
                # (cancel_poll_interval_s, default 2s) instead of the
                # heartbeat's (heartbeat_interval_s, default 10s). The
                # CancelGuard polls /tasks/{id}/cancel-status on its own
                # schedule; without this link its faster detection is
                # invisible to cooperative handlers that check
                # is_cancelled between blocking ops but don't hit an
                # await point where the guard raises TaskCancelled.
                progress.link_cancelled(cancelled)
                file_ctx = await prepare_inputs(
                    task, self._client, task_dir, cancelled=cancelled,
                )
                ctx = TaskContext(task=task, files=file_ctx, progress=progress)

                result = await handler(ctx, typed_params)

                # Publish outputs *inside* the CancelGuard so a user cancel
                # during the (potentially multi-minute) output upload is
                # detected instead of burning bandwidth to completion.
                # Remote-mode upload_outputs can spend minutes streaming
                # GB-scale outputs (colmap-splat PLYs, Neural-Canvas
                # splats); a cancel during that window must not wait for
                # every remaining file to finish uploading to a task the
                # user already cancelled. upload_outputs checks the guard's
                # ``cancelled`` event between uploads and raises
                # TaskCancelled before the next file starts — mirroring the
                # cancel-during-download guard in prepare_inputs.
                output_files = (result or {}).get("output_files") or {}
                if output_files:
                    delivered = await upload_outputs(
                        task, self._client, file_ctx, output_files,
                        self.shared_volume_path, cancelled=cancelled,
                    )
                    result = {**result, "output_files": delivered}
            outcome = ("complete", result or {})

        except TaskCancelled:
            outcome = ("fail", "cancelled by user")
        except (TaskParamsError, ProtocolError) as e:
            log.error("task %s protocol error: %s", task.id, e)
            outcome = ("fail", str(e))
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error("task %s failed: %s\n%s", task.id, e, tb)
            outcome = ("fail", f"{type(e).__name__}: {e}\n{tb}")
        finally:
            fired = wd.stop() if wd is not None else False
            # The heartbeat keeps ticking *through* the terminal report and is
            # only stopped in the finally below. complete()/fail() go through
            # BackendClient._retry, so against a degraded backend one report
            # can span minutes of retries and backoff. Stopping the heartbeat
            # first froze the task's ``updated_at`` for that whole window, and
            # the backend's stale-task sweeper reads a stale ``updated_at`` as
            # abandonment: it reclaims and re-queues the task mid-report, a
            # second worker claims it, and both compute and publish the same
            # outcome in parallel. Ticks during the report are what tell the
            # sweeper this worker is still alive and finishing the task.
            try:
                # Single terminal report. If the watchdog fired, the deadline
                # won; otherwise report the handler outcome. The guard makes
                # this exactly-once even against the watchdog's hard-exit path.
                if guard.claim():
                    # Determine the terminal method + payload up front so the
                    # except handler can log *which* report failed and on which
                    # task — a bare ``except: pass`` here was silently swallowing
                    # failures of the complete()/fail() call itself. BackendClient
                    # already retries transient transport/5xx errors inside
                    # _retry, so an exception reaching this block means retries
                    # were exhausted (backend down longer than the retry window)
                    # or a non-transient error surfaced. Either way the backend
                    # never learns the task's terminal status: a completed task
                    # stays "in_progress" until the sweeper marks it stale, and
                    # the operator had zero visibility. Surface it at ERROR so
                    # it's not invisible, while keeping the non-raising contract
                    # (the polling loop must keep running other tasks).
                    # The report is built as a value *before* it is sent, so
                    # the except handler can name the call that failed even
                    # when the exception fires before the method returns — and
                    # so a report that never lands can be kept and re-sent
                    # verbatim, under the same key, later. ``fired`` wins over
                    # outcome because a timeout overrides a late completion.
                    #
                    # A result the wire can't encode is a terminal report that
                    # can never land: complete() raises while *building* the
                    # request, so nothing is ever sent, and the task orphans in
                    # in_progress until the backend's sweeper reclaims and
                    # *recomputes* it — hours of GPU work redone over a stray
                    # numpy scalar, Path or datetime left in a handler's dict.
                    # Catch it here and report the failure instead, so the task
                    # lands terminal carrying the real cause.
                    #
                    # Deciding this *before* the request is what makes it safe,
                    # and is why there is no post-hoc "complete() raised, so
                    # fail() it" fallback in the handler below. Once a complete
                    # request has gone out, its failure is ambiguous — the write
                    # may have committed with only the response lost — and no
                    # amount of reading the task back closes that window: the
                    # write can still commit (or a cancel/requeue land) between
                    # the read and the fail(), which would overwrite a real
                    # outcome with a bogus failure. Ruling that out needs an
                    # atomic conditional transition (CAS) or a backend
                    # idempotency contract this SDK ships independently of and
                    # can't verify at runtime. Here there is nothing to race:
                    # no complete request was ever transmitted, and the fail()
                    # below is the same single terminal report this worker
                    # already owed the task.
                    if not fired and outcome[0] == "complete":
                        encode_error = _result_encode_error(outcome[1])
                        if encode_error is not None:
                            log.error(
                                "task %s: handler result is not JSON-"
                                "serializable (%s); reporting the task failed "
                                "instead — complete() could never have been "
                                "sent, and leaving it unreported orphans the "
                                "task in_progress",
                                task.id, encode_error,
                            )
                            outcome = (
                                "fail",
                                "handler succeeded but its result could not be "
                                f"encoded for the complete report: {encode_error}",
                            )
                    if fired:
                        report = TerminalReport(
                            task.id, "fail",
                            f"timeout: exceeded {timeout_s:.0f}s", fail_key,
                        )
                    elif outcome[0] == "complete":
                        report = TerminalReport(
                            task.id, "complete", outcome[1], complete_key,
                        )
                    else:
                        report = TerminalReport(
                            task.id, "fail", outcome[1], fail_key,
                        )
                    terminal = report.kind
                    try:
                        await self._put_terminal(report)
                    except Exception as report_exc:  # noqa: BLE001
                        # The terminal status call itself failed after exhausting
                        # the client's own retries. Log at ERROR (not WARNING) —
                        # this is a task whose handler outcome is *lost*: the
                        # backend will leave it in_progress and eventually sweep
                        # it as stale. The handler result was computed but never
                        # delivered, so an operator needs to know which task and
                        # which terminal method failed. We intentionally do NOT
                        # re-raise: a single failed terminal report must not
                        # kill the polling loop and strand every subsequent task.
                        #
                        # And we do NOT retry the other terminal route here (see
                        # the encode pre-check above): the request is already on
                        # the wire, so this failure can't tell a lost write from
                        # a lost *response*, and a second terminal write would
                        # risk stamping ``failed`` over a completion that landed.
                        #
                        # What we *do* keep is this same report, under the key
                        # it was already sent with. Re-sending it is not a
                        # second terminal write: it is the same one, named, so
                        # the ambiguous case collapses to "the backend already
                        # has it". The poll loop retries it once the backend is
                        # answering again (_flush_unconfirmed_reports) — which
                        # beats leaving a finished task to be swept as stale
                        # and recomputed from scratch. It is dropped, not
                        # re-sent, if the task is delivered here again: that
                        # delivery is a new attempt (see _run_one's head).
                        self._unconfirmed.record(report)
                        log.error(
                            "task %s: terminal %s report failed after retries; "
                            "backend did not record outcome=%r: %s — kept for "
                            "re-send on a later poll cycle",
                            task.id, terminal, report.payload, report_exc,
                        )
                    else:
                        if fired:
                            log.warning(
                                "task %s timed out (%s)",
                                task.id, task.task_type.value,
                            )
                        elif report.kind == "complete":
                            log.info(
                                "task %s completed (%s)",
                                task.id, task.task_type.value,
                            )
                        elif report.payload == "cancelled by user":
                            log.info("task %s cancelled by user", task.id)
            finally:
                # Always tear the heartbeat down, even if the report block
                # raised something the except above doesn't catch (guard.claim
                # blowing up) or the worker task was cancelled mid-report — a
                # leaked heartbeat outlives the task and double-starts the next
                # one (start_heartbeat rejects a double start).
                await progress.stop()
            # Off the event loop: a finished task's workdir holds its staged
            # inputs *and* its outputs (colmap-splat PLYs, Neural-Canvas
            # splats), so a synchronous rmtree freezes the loop for the whole
            # delete — in hybrid mode that stalls the FastAPI app, and in any
            # mode it delays the next claim. ``ignore_errors=True`` keeps this
            # non-raising, so the semantics are unchanged.
            await asyncio.to_thread(shutil.rmtree, task_dir, ignore_errors=True)


async def run_hybrid(
    app_coro: Awaitable[None],
    worker: Worker,
) -> None:
    """Run an awaitable (e.g. uvicorn.Server.serve()) and a Worker concurrently.

    If either exits, the other is cancelled cleanly. Used by Neural-
    Canvas where the FastAPI server and the task worker share one
    process + event loop.

    Implemented with `asyncio.wait` + cancel (not `asyncio.TaskGroup`)
    to keep Python 3.10 compatibility. Neural-Canvas currently runs
    3.10 and can't easily jump to 3.11; raising the SDK's floor would
    strand that consumer.
    """
    app_task = asyncio.ensure_future(app_coro)
    worker_task = asyncio.ensure_future(worker.run_forever())
    try:
        done, pending = await asyncio.wait(
            {app_task, worker_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Cancel whichever sibling is still running so we don't leak.
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Re-raise the first exception from the completed side so the
        # caller sees it (TaskGroup behavior equivalent).
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
    finally:
        for t in (app_task, worker_task):
            if not t.done():
                t.cancel()
