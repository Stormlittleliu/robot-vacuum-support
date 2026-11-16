# -*- coding: utf-8 -*-
"""Startup profiling contract tests.

These tests are deliberately offline: they use fake specs/tools and never call
a provider, a model, a tool, or retrieval.  They cover the startup sub-span
shape, delayed-import error propagation, the fact that scenario builds are not
memoized across turns, and that the toolbox cache is process-local (the worker
is one process per session), not a cross-user shared-state store.
"""
from __future__ import annotations

import builtins
import io
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT))

from core.observability import latency  # noqa: E402
from core.scenarios import generic, pools, registry  # noqa: E402


@pytest.fixture()
def recording(tmp_path, monkeypatch):
    monkeypatch.setenv("RVS_LATENCY_DIR", str(tmp_path / "spans"))
    monkeypatch.setenv("RVS_LATENCY_PROCESS", "test")
    monkeypatch.setattr(latency, "_ENABLED", True)
    monkeypatch.setattr(latency, "_PROCESS_ROLE", "test")
    monkeypatch.setattr(latency, "_WRITER", None)
    return tmp_path / "spans"


def _spans(span_dir):
    return latency.read_spans(span_dir)


def test_scenario_build_emits_real_subspans_and_keeps_contract(recording, monkeypatch, tmp_path):
    chat_model = object()
    fake_factory = types.ModuleType("core.llm.factory")
    fake_factory.chat_model = chat_model
    monkeypatch.setitem(sys.modules, "core.llm.factory", fake_factory)
    spec = SimpleNamespace(tools=["rag_retrieve"], response_format=None, name="demo")
    box = SimpleNamespace(
        name="rag",
        tools=[SimpleNamespace(name="rag_retrieve")],
        runtime=None,
        defaults={},
    )
    sentinel = SimpleNamespace(name="assembled")
    captured: dict = {}

    monkeypatch.setattr(generic, "ensure_spec_editable", lambda *a, **k: None)
    monkeypatch.setattr(generic, "load_active_spec", lambda *a, **k: spec)
    monkeypatch.setattr(generic.pools, "toolbox", lambda *a, **k: box)
    monkeypatch.setattr(generic, "load_book", lambda *a, **k: object())
    monkeypatch.setattr(generic, "resolve_cases_path", lambda *a, **k: None)
    monkeypatch.setattr(generic, "resolve_schema", lambda *a, **k: None)

    def fake_build_from_spec(*args, **kwargs):
        captured["model"] = kwargs.get("model")
        captured["tools"] = kwargs.get("tools")
        return sentinel

    monkeypatch.setattr(generic, "build_from_spec", fake_build_from_spec)

    with latency.bind_context(request_id="startup-1", cold=True):
        with latency.span("worker.scenario_resolve") as parent:
            result = generic.build_generic_spec_scenario("demo", root=tmp_path)

    assert result is sentinel
    assert captured["model"] is chat_model
    assert [tool.name for tool in captured["tools"]] == ["rag_retrieve"]

    rows = _spans(recording)
    by_stage = {row["stage"]: row for row in rows}
    expected = {
        "worker.model_import_config",
        "worker.spec_read_validate",
        "worker.toolbox_build",
        "worker.prompt_book_load",
        "worker.scenario_assemble",
    }
    assert expected <= set(by_stage)
    for stage in expected:
        assert by_stage[stage]["parent_span_id"] == parent.span_id
        assert by_stage[stage]["status"] == "ok"
        assert by_stage[stage]["cold"] is True


def test_delayed_model_import_error_surfaces(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "core.llm.factory":
            raise ImportError("delayed factory import failed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    with pytest.raises(ImportError, match="delayed factory import failed"):
        generic.build_generic_spec_scenario("demo", root=tmp_path)


def test_scenario_lookup_is_not_memoized(monkeypatch):
    monkeypatch.setattr(registry, "_spec_names", lambda: ["demo"])
    builds: list[object] = []

    def fake_build(name):
        item = object()
        builds.append(item)
        return item

    fake_generic = types.ModuleType("core.scenarios.generic")
    fake_generic.build_generic_spec_scenario = fake_build
    monkeypatch.setitem(sys.modules, "core.scenarios.generic", fake_generic)
    first = registry.get_scenario("demo")
    second = registry.get_scenario("demo")
    assert first is builds[0]
    assert second is builds[1]
    assert first is not second


def test_toolbox_cache_is_process_local_and_clearable(monkeypatch):
    monkeypatch.setitem(pools._BUILDERS, "probe", lambda: object())
    pools.clear_cache()
    try:
        first = pools.toolbox("probe")
        second = pools.toolbox("probe")
        assert first is second  # same worker process / same scenario build
        pools.clear_cache()
        third = pools.toolbox("probe")
        assert third is not first  # explicit reset; no cross-process shared store
        assert pools.toolbox.cache_info().maxsize is None
    finally:
        pools.clear_cache()


def test_workflow_graphs_are_per_runtime_not_shared():
    from modules.runtime.workflow import WorkflowRuntime

    class FakeScenario:
        name = "demo"

        def resolve_execution(self):
            return SimpleNamespace(
                workflow_factory=lambda scenario: {"agent": object()}
            )

    first = WorkflowRuntime(FakeScenario())
    second = WorkflowRuntime(FakeScenario())
    assert first.graphs["agent"] is not second.graphs["agent"]


def test_worker_bootstrap_import_is_a_separate_span(recording, monkeypatch):
    from modules.instances.runtime import worker

    monkeypatch.setattr(worker, "_execution_module", lambda: SimpleNamespace())
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(""))
    with worker.latency.bind_context(request_id="boot-1", cold=True):
        assert worker.main() == 0

    rows = _spans(recording)
    by_stage = {row["stage"]: row for row in rows}
    assert "worker.process_start" in by_stage
    assert "worker.bootstrap_import" in by_stage
    assert "worker.ready" in by_stage
    assert by_stage["worker.bootstrap_import"]["status"] == "ok"
    assert by_stage["worker.bootstrap_import"]["cold"] is True
    # Bootstrap is outside the first request's scenario_resolve tree.
    assert by_stage["worker.bootstrap_import"]["parent_span_id"] is None


def test_importtime_parser_never_adds_nested_cumulative():
    from scripts.profile_startup import _parse_importtime

    text = "\n".join(
        [
            "import time:       100 |        100 | a",
            "import time:       200 |        300 | b",
            "import time:        50 |        350 | c",
            "import time:        10 |        360 | core.scenarios.registry",
        ]
    )
    parsed = _parse_importtime(text)
    assert [row["module"] for row in parsed["top_self"]] == ["b", "a", "c", "core.scenarios.registry"]
    assert [row["module"] for row in parsed["top_cumulative"]] == [
        "core.scenarios.registry",
        "c",
        "b",
        "a",
    ]
    assert parsed["top_cumulative"][0]["cumulative_ms"] == pytest.approx(0.36)
    assert parsed["top_self"][0]["self_ms"] == pytest.approx(0.2)
    assert "not additive" in parsed["note"]
