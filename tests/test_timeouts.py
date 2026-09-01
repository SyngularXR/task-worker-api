from __future__ import annotations

from task_worker_api.enums import TaskType
from task_worker_api.timeouts import (
    DEFAULT_TASK_TIMEOUT_S,
    parse_timeouts_env,
    resolve_task_timeout,
)


def test_parse_env_basic():
    assert parse_timeouts_env("default=1800,gs_build=7200") == {
        "default": 1800.0, "gs_build": 7200.0,
    }


def test_parse_env_empty_and_none():
    assert parse_timeouts_env("") == {}
    assert parse_timeouts_env(None) == {}


def test_parse_env_skips_malformed(caplog):
    with caplog.at_level("WARNING"):
        out = parse_timeouts_env("default=1800, oops, render=abc , gs_build=60")
    assert out == {"default": 1800.0, "gs_build": 60.0}
    assert any("WORKER_TASK_TIMEOUTS" in r.message for r in caplog.records)


def test_parse_env_skips_non_finite(caplog):
    # float() accepts these, but nan fails _run_one's `timeout_s > 0` (watchdog
    # never starts) and inf never fires — skip them so the default applies.
    with caplog.at_level("WARNING"):
        out = parse_timeouts_env("default=nan,render=inf,detect_cut_planes=-inf,gs_build=60")
    assert out == {"gs_build": 60.0}
    non_finite_warnings = [
        r for r in caplog.records if "non-finite" in r.message
    ]
    assert len(non_finite_warnings) == 3


def test_resolution_precedence_env_per_type_wins():
    # env per-type > ctor per-type > env default > ctor default
    t = resolve_task_timeout(
        TaskType.GS_BUILD,
        default_s=1800.0,
        per_type={TaskType.GS_BUILD: 3600.0},
        env={"gs_build": 7200.0, "default": 600.0},
    )
    assert t == 7200.0


def test_resolution_ctor_per_type_then_env_default_then_ctor_default():
    assert resolve_task_timeout(
        TaskType.GS_BUILD, default_s=1800.0,
        per_type={TaskType.GS_BUILD: 3600.0}, env={"default": 600.0},
    ) == 3600.0
    assert resolve_task_timeout(
        TaskType.RENDER, default_s=1800.0, per_type={}, env={"default": 600.0},
    ) == 600.0
    assert resolve_task_timeout(
        TaskType.RENDER, default_s=1800.0, per_type={}, env={},
    ) == 1800.0


def test_zero_disables():
    assert resolve_task_timeout(
        TaskType.RENDER, default_s=1800.0, per_type={}, env={"render": 0.0},
    ) == 0.0


def test_default_constant():
    assert DEFAULT_TASK_TIMEOUT_S == 1800.0
