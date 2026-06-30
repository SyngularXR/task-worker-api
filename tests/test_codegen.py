"""Tests for the TypeScript codegen tool (tools/gen_typescript.py).

Guards against the duplicate-interface bug: when two TaskType keys share one
Pydantic model via an alias (GS4D_BUILD -> Gs4dBuildParams = GsBuildParams),
the generated TS must emit `export interface GsBuildParams` exactly once —
a duplicate would fail TS compilation in upstream consumers.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_GEN_TS = Path(__file__).resolve().parent.parent / "tools" / "gen_typescript.py"


@pytest.fixture(scope="module")
def gen_module():
    """Load gen_typescript.py as a module (it lives outside the package)."""
    spec = importlib.util.spec_from_file_location("gen_typescript", _GEN_TS)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def generated_ts(gen_module) -> str:
    return gen_module.generate()


def test_gs4d_build_in_task_params_by_type(generated_ts):
    """Both gs_build and gs4d_build must appear in the dispatch map."""
    assert "'gs_build': GsBuildParams;" in generated_ts
    assert "'gs4d_build': GsBuildParams;" in generated_ts


def test_no_duplicate_interface_for_alias(generated_ts):
    """The alias bug: GS4D_BUILD -> Gs4dBuildParams = GsBuildParams means
    model_cls.__name__ is 'GsBuildParams' for both registry entries. Emitting
    per-entry would produce two `export interface GsBuildParams` blocks."""
    count = generated_ts.count("export interface GsBuildParams")
    assert count == 1, f"expected exactly one GsBuildParams interface, got {count}"


def test_every_registry_interface_emitted_once(generated_ts):
    """Generalize the dedup guard: each distinct interface name appears once."""
    import re

    names = re.findall(r"^export interface (\w+)", generated_ts, re.MULTILINE)
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate interface declarations: {dupes}"


def test_gs4d_build_in_task_type_enum(generated_ts):
    assert "| 'gs4d_build'" in generated_ts
    assert "GS4D_BUILD: 'gs4d_build' as const" in generated_ts
