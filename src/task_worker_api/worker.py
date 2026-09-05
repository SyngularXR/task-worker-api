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
import json
import logging
import math
import os
import random
import re
import shutil
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

from .cancel import CancelGuard
from .client import BackendClient, _DEFAULT_BACKOFF_MAX_S, _JITTER_SPREAD
from .context import ClaimedTask, TaskContext
from .enums import TaskType
from .errors import ProtocolError, TaskCancelled, TaskParamsError
from .files import prepare_inputs, upload_outputs
from .payload_log import PayloadLogger, sanitize_worker_id
from .progress import ProgressReporter
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

# A full day is well beyond the fleet's longest configured task timeout (the
# 3-hour cinematic_baking job), so a quiet tree this old cannot be a healthy
# attempt. Operators can raise this floor for unusually slow local storage.
_DEFAULT_WORKDIR_CLEANUP_MIN_AGE_S = 24 * 60 * 60
_TASK_WORKDIR_RE = re.compile(r"task_\d+")

#: Cap on how many poll cycles a connect-failing foreign target is skipped.
#: 32 cycles at the default 5s poll interval ≈ 2.7 min between attempts —
#: enough to stop a dead target from consuming a retry burst every cycle,
#: short enough that a recovered box is picked back up within minutes.
_FOREIGN_BACKOFF_MAX_CYCLES = 32

#: Exponent ceiling for that backoff. ``min()`` already clamps the *result*,
#: but not the exponentiation: ``failures`` keeps incrementing against a
#: permanently dead box, so ``2 ** failures`` builds a wider throwaway integer
#: every cycle for a worker left running for days. ``bit_length()`` is the
#: first exponent whose power exceeds the ceiling, so clamping there keeps the
#: arithmetic O(1) with no change to any skip value.
_FOREIGN_BACKOFF_MAX_EXP = _FOREIGN_BACKOFF_MAX_CYCLES.bit_length()

#: Bare fleet-default worker ids (``blender-worker-1``). Fine on a single
#: box; ambiguous the moment two boxes' fleets poll the same target — the
#: target's registry and ownership checks key on the worker_id string.
_DEFAULT_WORKER_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-worker-\d+$")


@dataclass
class ForeignTarget:
    """One additional SynPusher box this worker helps when otherwise idle.

    Parsed from ``SYNPUSHER_TARGETS`` (``url|api_key|task_types`` entries,
    semicolon-separated) or built directly in tests with an injected
    ``client``. Foreign targets are strictly additive: ``SYNPUSHER_URL`` /
    ``WORKER_API_KEY`` keep their exact meaning as the *home* box — the only
    target whose shared-volume paths this worker may trust.
    """

    url: str
    api_key: str
    task_types: list[TaskType]
    client: Optional[BackendClient] = None  # tests inject a fake


def parse_synpusher_targets(raw: Optional[str]) -> list[ForeignTarget]:
    """Parse ``SYNPUSHER_TARGETS`` — fail fast on any malformed entry.

    A silently skipped target is indistinguishable from "no work available",
    so misconfiguration raises :class:`ProtocolError` (consumers exit
    non-zero and container crash-loop detection fires) instead of degrading.
    """
    targets: list[ForeignTarget] = []
    if not raw or not raw.strip():
        return targets
    for idx, entry in enumerate(raw.split(";")):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split("|")]
        if len(parts) != 3 or not all(parts):
            raise ProtocolError(
                f"SYNPUSHER_TARGETS entry {idx} is malformed: {entry!r}. "
                "Expected 'url|api_key|task_types' with all three fields "
                "non-empty (entries separated by ';')."
            )
        url, api_key, types_str = parts
        types: list[TaskType] = []
        for t in types_str.split(","):
            t = t.strip()
            if not t:
                continue
            try:
                types.append(TaskType(t))
            except ValueError:
                raise ProtocolError(
                    f"SYNPUSHER_TARGETS entry {idx} names unknown task type "
                    f"{t!r}. Known: "
                    f"{', '.join(sorted(m.value for m in TaskType))}"
                )
        if not types:
            raise ProtocolError(
                f"SYNPUSHER_TARGETS entry {idx} has an empty task_types field."
            )
        targets.append(ForeignTarget(url=url, api_key=api_key, task_types=types))
    return targets


#: Ports httpx omits from the connection key because they are implied by the
#: scheme — two URLs differing only by one of these reach the same backend.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _canonical_url(url: str) -> str:
    """Return *url* in the form two spellings of one backend share.

    "Same backend" has to mean "httpx dials the same address and sends the
    same request target", so the canonical form is *derived from* ``httpx.URL``
    rather than re-derived alongside it. httpx lower-cases the scheme,
    lower-cases and IDNA-encodes the host, resolves ``.``/``..`` path segments,
    and never puts the fragment on the wire — so ``http://far/a/../api/v1``,
    ``http://far/api/v1#frag`` and ``http://FÄR``-style spellings all reach the
    one place a plain ``http://far/api/v1`` does. Restating those rules by hand
    is what previously let exactly those pairs past the home-box and duplicate
    guards below, reinstating the double-polling they exist to stop.

    Input httpx cannot parse, or that names no scheme and host, is returned
    trailing-slash stripped, keeping the comparison exactly as strict as it was
    before rather than collapsing unrelated junk together.

    Verified identical across the supported ``httpx>=0.23`` range: 0.23.3 (what
    the SynPusher backend pins) and 0.28 agree on every rule used here, despite
    the URL-parser rewrite in 0.24. They differ only in how they *reject* a bad
    port — 0.28 raises ``InvalidURL``, 0.23 yields an empty host — and both of
    those land on the verbatim fallback below.
    """
    trimmed = url.rstrip("/")
    try:
        parsed = httpx.URL(trimmed)
        scheme, port = parsed.scheme, parsed.port
        # Both are ASCII once httpx has accepted the URL: the host is
        # IDNA-encoded and the path percent-encoded. raw_path is the wire
        # request target — dot segments resolved, query kept, fragment dropped.
        host = parsed.raw_host.decode("ascii")
        path = parsed.raw_path.decode("ascii")
    except httpx.InvalidURL:  # bad port, un-encodable host — compare verbatim
        return trimmed
    if not scheme or not host:
        return trimmed
    # 0.28 leaves an explicitly spelled default port on the URL when the scheme
    # was upper-cased, but connects to the same address either way.
    if port == _DEFAULT_PORTS.get(scheme):
        port = None
    netloc = f"[{host}]" if ":" in host else host  # httpx strips IPv6 [ ]
    if port is not None:
        netloc = f"{netloc}:{port}"
    # Strip again: resolving dot segments can reintroduce a trailing slash.
    return f"{scheme}://{netloc}{path}".rstrip("/")


@dataclass
class _Target:
    """Runtime view of one pollable box (home or foreign)."""

    client: "BackendClient"
    task_types: list[TaskType]
    is_home: bool
    base_url: str
    api_key: str
    label: str
    failures: int = field(default=0)
    skip_cycles: int = field(default=0)


def _positive_finite_s(name: str, value: float) -> float:
    """Validate a worker pacing knob, or raise ``ValueError``.

    These knobs are the worker's only pacing, and every degenerate value
    breaks it in a way that is silent at construction and expensive in
    production — against the one backend the whole fleet shares:

    * ``poll_interval_s`` / ``claim_backoff_max_s``: ``0``/negative spins the
      claim loop at full speed, ``inf`` makes the first idle wait never end
      (the worker stops polling for good), and ``NaN`` fails every comparison
      in ``_claim_wait_s`` — so the cap never matches, the doubling loop runs
      once per accumulated failure, and ``asyncio.wait_for(timeout=nan)``
      waits forever.
    * ``heartbeat_interval_s``: ``<= 0`` (and ``NaN``, which fails the sleep's
      own clamp) turns ``ProgressReporter._heartbeat_loop``'s sleep into a
      no-op, hammering ``PUT /tasks/{id}/progress`` as fast as the backend
      answers; ``inf`` never heartbeats at all, so the task looks stale.
    * ``cancel_poll_interval_s``: the same two failures against ``CancelGuard``
      and ``GET /tasks/{id}/cancel-status``.
    * ``timeout_grace_s``: ``NaN`` makes ``TaskWatchdog._wait`` return
      instantly at both grace phases (``end = now + nan``, so the loop never
      runs), collapsing SIGTERM → grace → SIGKILL → grace → hard-exit into an
      immediate container kill on the first deadline; ``<= 0`` does the same,
      and ``inf`` means the escalation never reaches the hard exit.

    Rejecting them here is what makes the capped-and-still-polling guarantee
    real.
    """
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{name} must be a finite positive number of seconds "
            f"(got {value!r})"
        )
    return float(value)


def _finite_task_timeout_s(name: str, value: float) -> float:
    """Validate a task-timeout value, or raise ``ValueError``.

    Unlike the pacing knobs, ``<= 0`` is legal here — it is the documented
    "no timeout" escape hatch for a known-unbounded task type. Non-finite
    values defeat the timeout silently instead of loudly: ``NaN`` fails
    ``_run_one``'s ``timeout_s > 0`` check, so the watchdog is never started
    and a wedged handler runs unbounded with no log line saying why; ``inf``
    starts a watchdog whose deadline never arrives — the same unbounded run,
    one thread more expensive.
    """
    if not math.isfinite(value):
        raise ValueError(
            f"{name} must be a finite number of seconds (got {value!r})"
        )
    return float(value)


def _make_sync_fail(
    base_url: str, api_key: str, task_id: int, worker_id: str, *, timeout_s: float = 3.0,
    attempts: int = 3, retry_sleep_s: float = 2.0,
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
    """
    url = (
        f"{base_url.rstrip('/')}/tasks/{task_id}/fail"
        f"?worker_id={urllib.parse.quote(worker_id, safe='')}"
    )

    def _sync_fail(error: str) -> None:
        data = json.dumps({"error": error}).encode("utf-8")
        last_exc: Exception | None = None
        for attempt in range(attempts):
            req = urllib.request.Request(
                url, data=data, method="PUT",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
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


def _newest_tree_mtime(path: Path) -> float:
    """Return the newest mtime in ``path`` without following symlinks."""
    newest = path.stat(follow_symlinks=False).st_mtime
    pending = [path]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                newest = max(
                    newest,
                    entry.stat(follow_symlinks=False).st_mtime,
                )
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
    return newest


def _sweep_orphaned_workdirs(
    work_dir: Path,
    active_task_dir: Optional[Path],
    min_age_s: float,
) -> None:
    """Best-effort removal of old SDK-owned task dirs. Runs in a thread."""
    cutoff = time.time() - min_age_s
    try:
        candidates = list(work_dir.iterdir())
    except Exception as e:  # noqa: BLE001 — cleanup must never stop polling
        log.warning("workdir cleanup: could not scan %s: %s", work_dir, e)
        return

    for task_dir in candidates:
        if not _TASK_WORKDIR_RE.fullmatch(task_dir.name):
            continue
        try:
            if (
                task_dir == active_task_dir
                or task_dir.is_symlink()
                or not task_dir.is_dir()
            ):
                continue
            if _newest_tree_mtime(task_dir) >= cutoff:
                continue
            shutil.rmtree(task_dir)
            log.info("workdir cleanup: removed orphaned %s", task_dir)
        except Exception as e:  # noqa: BLE001 — cleanup must never stop polling
            log.warning("workdir cleanup: could not remove %s: %s", task_dir, e)


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
        foreign_targets: Optional[list[ForeignTarget]] = None,
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
        self.heartbeat_interval_s = _positive_finite_s(
            "heartbeat_interval_s", heartbeat_interval_s,
        )
        self.heartbeat_warn_threshold = heartbeat_warn_threshold
        self.cancel_poll_interval_s = _positive_finite_s(
            "cancel_poll_interval_s", cancel_poll_interval_s,
        )
        self.task_timeout_s = _finite_task_timeout_s(
            "task_timeout_s", task_timeout_s,
        )
        self.task_timeouts = {
            t: _finite_task_timeout_s(f"task_timeouts[{t}]", v)
            for t, v in (task_timeouts or {}).items()
        }
        self.timeout_grace_s = _positive_finite_s(
            "timeout_grace_s", timeout_grace_s,
        )
        self._on_hard_exit = on_hard_exit or (lambda: os._exit(75))
        self._timeout_env = parse_timeouts_env(os.environ.get("WORKER_TASK_TIMEOUTS"))
        self._watchdog_factory = _watchdog_factory
        self._active_task_dir: Optional[Path] = None
        self._workdir_cleanup_lock = asyncio.Lock()
        workdir_age_raw = os.environ.get(
            "WORKER_WORKDIR_CLEANUP_MIN_AGE_S",
            str(_DEFAULT_WORKDIR_CLEANUP_MIN_AGE_S),
        )
        try:
            self._workdir_cleanup_min_age_s = float(workdir_age_raw)
            if (
                not math.isfinite(self._workdir_cleanup_min_age_s)
                or self._workdir_cleanup_min_age_s <= 0
            ):
                raise ValueError
        except (ValueError, TypeError):
            log.warning(
                "workdir cleanup: WORKER_WORKDIR_CLEANUP_MIN_AGE_S=%r is "
                "invalid; falling back to %d seconds",
                workdir_age_raw, _DEFAULT_WORKDIR_CLEANUP_MIN_AGE_S,
            )
            self._workdir_cleanup_min_age_s = float(
                _DEFAULT_WORKDIR_CLEANUP_MIN_AGE_S
            )

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
            client_norm = _canonical_url(str(client_base_url))
            worker_norm = _canonical_url(self.backend_url)
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

        # ----- cross-box targets (SYNPUSHER_TARGETS) ------------------
        # SYNPUSHER_URL stays the home box with unchanged semantics — the only
        # target trusted for shared-volume paths. Foreign targets are strictly
        # additive and always remote-mode; parsed from env unless tests pass
        # them explicitly.
        specs = (
            foreign_targets
            if foreign_targets is not None
            else parse_synpusher_targets(os.environ.get("SYNPUSHER_TARGETS"))
        )
        self._foreign_targets: list[_Target] = []
        #: Rotating start index into ``_foreign_targets`` (see ``_claim``).
        self._foreign_cursor = 0
        home_norm = _canonical_url(self.backend_url)
        seen_urls: set[str] = set()
        for idx, spec in enumerate(specs):
            spec_url = _canonical_url(spec.url or "")
            if spec_url and spec_url == home_norm:
                raise ProtocolError(
                    f"SYNPUSHER_TARGETS lists the home box ({spec.url!r}); "
                    "foreign targets are additive — remove the home URL from "
                    "the list."
                )
            if spec_url and spec_url in seen_urls:
                raise ProtocolError(
                    f"SYNPUSHER_TARGETS entry {idx} repeats target URL "
                    f"{spec.url!r}. Each foreign target must be a distinct "
                    "backend — a repeat doubles that box's claim traffic and "
                    "weights it twice in the round-robin; remove the "
                    "duplicate."
                )
            seen_urls.add(spec_url)
            usable = [t for t in spec.task_types if t in self.handlers]
            dropped = [t.value for t in spec.task_types if t not in self.handlers]
            if dropped:
                log.warning(
                    "foreign target %s lists task types with no local handler "
                    "(%s); they will not be claimed.",
                    spec.url, ", ".join(dropped),
                )
            if not usable:
                raise ProtocolError(
                    f"foreign target {spec.url!r} has no task type this "
                    "worker handles; remove the entry or fix its task_types."
                )
            tgt_client = spec.client
            if tgt_client is None:
                tgt_client = BackendClient(
                    spec.url, spec.api_key, timeout_s=request_timeout_s,
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
            self._foreign_targets.append(_Target(
                client=tgt_client,
                task_types=usable,
                is_home=False,
                base_url=spec.url or str(getattr(tgt_client, "base_url", "")),
                api_key=spec.api_key,
                label=spec.url or type(tgt_client).__name__,
            ))
        if self._foreign_targets and _DEFAULT_WORKER_ID_RE.match(self.worker_id):
            log.warning(
                "WORKER_ID %r looks like a bare fleet default. With "
                "SYNPUSHER_TARGETS set, worker ids must be globally unique "
                "across boxes (the target's registry and ownership checks "
                "key on this string) — prefix it with the box name, e.g. "
                "'3dpo-%s'.",
                self.worker_id, self.worker_id,
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

    @property
    def _home_target(self) -> _Target:
        """Live home-target view over ``self._client``.

        A property (not captured at construction) so test seams and hybrid
        consumers that swap ``worker._client`` keep home binding coherent —
        the home task's progress/complete/files must follow the current
        client, exactly as they did before targets existed.
        """
        return _Target(
            client=self._client,
            task_types=[],  # home claims use self.task_types (live view)
            is_home=True,
            base_url=self.backend_url,
            api_key=self.api_key,
            label="home",
        )

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
        await self._run_cleanup()

        cleanup_raw = os.environ.get("WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S", "3600")
        try:
            cleanup_interval_s = float(cleanup_raw)
            if not math.isfinite(cleanup_interval_s) or cleanup_interval_s <= 0:
                raise ValueError(
                    f"cleanup interval must be a finite positive number of "
                    f"seconds, got {cleanup_interval_s}"
                )
        except (ValueError, TypeError):
            log.warning(
                "payload_log: WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S=%r is "
                "invalid; falling back to 3600 seconds",
                cleanup_raw,
            )
            cleanup_interval_s = 3600.0
        cleanup_task = asyncio.create_task(
            self._periodic_cleanup_loop(cleanup_interval_s)
        )

        # Startup checks belong inside the try: a fatal one (box affinity)
        # must still run the finally, or its ProtocolError surfaces buried in
        # "Task was destroyed but it is pending" and unclosed-socket noise.
        try:
            if self._foreign_targets:
                log.info(
                    "cross-box mode: %d foreign target(s): %s",
                    len(self._foreign_targets),
                    ", ".join(t.label for t in self._foreign_targets),
                )
                await self._verify_home_affinity()

            while not self._stop.is_set():
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
                task, target = claimed
                await self._run_one(task, target)
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            self._payload_logger.close()
            await self._client.close()
            for tgt in self._foreign_targets:
                try:
                    await tgt.client.close()
                except Exception:  # noqa: BLE001 — shutdown is best-effort
                    log.warning("failed to close client for %s", tgt.label)
            log.info("task-worker-api Worker stopped: id=%s", self.worker_id)

    async def _verify_home_affinity(self) -> None:
        """Volume-affinity check: is SYNPUSHER_URL really the box whose volume
        this worker mounts?

        A typo'd home URL would designate a FOREIGN box as trusted-for-paths
        and recreate the silent wrong-file hazard cross-box mode exists to
        close. The home backend writes its identity to
        ``{shared_volume}/.box-id`` and serves the same value at
        ``GET /tasks/box-id``; a mismatch is fatal. Missing endpoint (old
        backend) or missing sentinel only warns — the check must not break
        rollout ordering — and without a shared volume there is nothing to
        verify.
        """
        if not self.shared_volume_path:
            return
        sentinel = Path(self.shared_volume_path) / ".box-id"
        try:
            volume_id = sentinel.read_text(encoding="utf-8").strip()
        except OSError:
            log.warning(
                "box-affinity: no readable %s on the shared volume; cannot "
                "verify SYNPUSHER_URL points at the box this volume belongs "
                "to. Update the home backend to one that writes .box-id.",
                sentinel,
            )
            return
        get_box_id = getattr(self._client, "get_box_id", None)
        if get_box_id is None:
            log.warning(
                "box-affinity: client %s has no get_box_id; skipping check.",
                type(self._client).__qualname__,
            )
            return
        try:
            home_id = await get_box_id()
        except Exception as e:  # noqa: BLE001 — startup must tolerate a blip
            log.warning(
                "box-affinity: could not fetch home box id from %s (%s); "
                "continuing without verification.",
                self.backend_url, e,
            )
            return
        if home_id is None:
            log.warning(
                "box-affinity: home backend %s predates GET /tasks/box-id; "
                "continuing without verification.",
                self.backend_url,
            )
            return
        if home_id != volume_id:
            raise ProtocolError(
                f"box-affinity check failed: home backend {self.backend_url} "
                f"reports box id {home_id!r} but the mounted shared volume "
                f"belongs to box {volume_id!r}. SYNPUSHER_URL points at a "
                "box whose volume this worker does NOT mount — fix "
                "SYNPUSHER_URL (or the volume mount) before enabling "
                "cross-box targets."
            )
        log.info("box-affinity: verified home %s == volume box id", home_id)

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
                await self._run_cleanup()
        except asyncio.CancelledError:
            raise

    async def _run_cleanup(self) -> None:
        """Run all worker-local retention off the event loop."""
        await asyncio.to_thread(self._payload_logger.cleanup_old_files)
        # Serializing only the sweep against task activation closes the race
        # where an old path is selected, then reclaimed and recreated for a
        # new attempt before rmtree starts. Waiting here never blocks the loop.
        async with self._workdir_cleanup_lock:
            await asyncio.to_thread(
                _sweep_orphaned_workdirs,
                self.work_dir,
                self._active_task_dir,
                self._workdir_cleanup_min_age_s,
            )

    async def run_one(self) -> bool:
        """Process exactly one claim cycle. Returns True iff a task ran.

        Test seam — production code uses ``run_forever``.
        """
        claimed = await self._claim()
        if claimed is None:
            return False
        task, target = claimed
        await self._run_one(task, target)
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

    async def _claim(self) -> Optional[tuple[ClaimedTask, _Target]]:
        """Home-first claim across all targets.

        Home gets first refusal every cycle — "help only when idle" falls out
        of the loop structure, since a worker only polls at all when it has no
        task. Foreign targets are then swept in round-robin order: the sweep
        returns on the first successful claim, so a fixed order lets one
        target with a standing backlog claim every cycle and the targets
        after it are never polled at all — not merely deprioritised.
        Advancing the start index one step per cycle gives every target first
        pick every ``len(targets)`` cycles. One that fails to connect is
        skipped for exponentially more cycles (capped) so a dead target can't
        starve the ones after it or consume a retry burst every cycle. That
        backoff is charged per poll cycle, before the sweep, rather than when
        the sweep happens to reach the target: the sweep returns on the first
        successful claim, so a target sitting behind a backlogged one is never
        reached, and an in-sweep decrement would stall its counter — an
        N-cycle backoff would take up to ``N * len(targets)`` cycles to
        expire, scaling the ceiling with target count. Home failures keep
        driving the existing global idle-wait escalation; foreign failures
        never touch it.
        """
        claimed = await self._claim_home()
        if claimed is not None:
            return claimed, self._home_target
        targets = self._foreign_targets
        if not targets:
            return None
        start = self._foreign_cursor
        # Modulo on store, not on read: the cursor must stay small for the
        # same reason the backoff exponent does.
        self._foreign_cursor = (start + 1) % len(targets)
        due = []
        for tgt in targets[start:] + targets[:start]:
            if tgt.skip_cycles > 0:
                tgt.skip_cycles -= 1
            else:
                due.append(tgt)
        for tgt in due:
            if self._stop.is_set():
                return None
            try:
                foreign_claim = await tgt.client.claim_next(
                    tgt.task_types, worker_id=self.worker_id,
                )
            except Exception as e:  # noqa: BLE001
                tgt.failures += 1
                tgt.skip_cycles = min(
                    2 ** min(tgt.failures, _FOREIGN_BACKOFF_MAX_EXP),
                    _FOREIGN_BACKOFF_MAX_CYCLES,
                )
                log.warning(
                    "foreign claim failed against %s (%d consecutive; "
                    "skipping %d cycles): %s",
                    tgt.label, tgt.failures, tgt.skip_cycles, e,
                )
                continue
            tgt.failures = 0
            if foreign_claim is not None:
                log.info(
                    "claimed cross-box task %s (%s) from %s",
                    foreign_claim.id, foreign_claim.task_type.value, tgt.label,
                )
                return foreign_claim, tgt
        return None

    async def _claim_home(self) -> Optional[ClaimedTask]:
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

    async def _run_one(self, task: ClaimedTask, target: _Target) -> None:
        """Heartbeat → stage inputs → run handler → publish.

        Every backend interaction for this task — heartbeat, cancel poll,
        file transfer, watchdog last-resort fail, and the terminal report —
        goes through ``target`` (the box the task was claimed from), never
        through worker-level home state: a foreign task's timeout or
        completion must land on the foreign box.

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
        """
        task_dir = self.work_dir / f"task_{task.id}"
        progress = ProgressReporter(
            target.client, task.id,
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
                    target.base_url, target.api_key, task.id, self.worker_id,
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

            # A periodic sweep may already be removing this old path. Wait for
            # it off-loop, then mark the path active before staging any files.
            async with self._workdir_cleanup_lock:
                self._active_task_dir = task_dir

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
                target.client, task.id,
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
                    task, target.client, task_dir, cancelled=cancelled,
                    foreign=not target.is_home,
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
                        task, target.client, file_ctx, output_files,
                        self.shared_volume_path, cancelled=cancelled,
                        foreign=not target.is_home,
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
                    # ``terminal`` mirrors the branch the try-block will take, so
                    # the except handler can name the call that failed even when
                    # the exception fires before the method returns. ``fired`` wins
                    # over outcome because a timeout overrides a late completion.
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
                        terminal = "fail"
                    elif outcome[0] == "complete":
                        terminal = "complete"
                    else:
                        terminal = "fail"
                    try:
                        if fired:
                            await target.client.fail(
                                task.id, f"timeout: exceeded {timeout_s:.0f}s",
                            )
                            log.warning(
                                "task %s timed out (%s)",
                                task.id, task.task_type.value,
                            )
                        elif outcome[0] == "complete":
                            await target.client.complete(task.id, outcome[1])
                            log.info(
                                "task %s completed (%s)",
                                task.id, task.task_type.value,
                            )
                        else:
                            await target.client.fail(task.id, outcome[1])
                            if outcome[1] == "cancelled by user":
                                log.info("task %s cancelled by user", task.id)
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
                        log.error(
                            "task %s: terminal %s report failed after retries; "
                            "backend did not record outcome=%r: %s",
                            task.id, terminal, outcome[1], report_exc,
                        )
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
            try:
                await asyncio.to_thread(
                    shutil.rmtree, task_dir, ignore_errors=True,
                )
            finally:
                # This assignment must not await: cancellation during cleanup
                # must not leave a dead task protected from future sweeps.
                self._active_task_dir = None


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
