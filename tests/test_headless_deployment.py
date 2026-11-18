# -*- coding: utf-8 -*-
"""Contract tests for the headless WSL2 deployment helper and override."""
from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "headless_docker.py"
OVERRIDE = ROOT / "deploy" / "compose.headless.yml"


def load_helper():
    spec = importlib.util.spec_from_file_location("headless_docker", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_override_requires_explicit_isolation_values():
    text = OVERRIDE.read_text(encoding="utf-8")
    for variable in (
        "RVS_BACKEND_IMAGE:?",
        "RVS_DATA_VOLUME:?",
        "RVS_ASSETS_HOST_ROOT:?",
        "PUBLIC_PORT:?",
        "RVS_INSTANCE_ID:?",
    ):
        assert "${" + variable in text
    # The generic cloud default tag must never be inherited by this override.
    assert "rvs-backend:smoke" not in text


def test_override_uses_linux_asset_bindings_and_pins_volume_name():
    text = OVERRIDE.read_text(encoding="utf-8")
    assert "/knowledge:/assets/knowledge:ro" in text
    assert "/models/bge-small-zh-v1.5:/assets/models/bge-small-zh-v1.5:ro" in text
    assert "/rag_index:/assets/rag_index:ro" in text
    assert "name: ${RVS_DATA_VOLUME:?RVS_DATA_VOLUME must be an explicit volume name}" in text


def test_helper_defaults_pin_project_and_streaming_build_is_namespaced():
    module = load_helper()
    assert module.DEFAULT_PROJECT == "rvs-p1"
    assert module.DEFAULT_DISTRO == "DockerEngine"
    assert module.build_parser().parse_args(["build"]).command == "build"


def test_helper_refuses_public_smoke_tag_without_touching_docker(tmp_path, monkeypatch):
    module = load_helper()
    env_file = tmp_path / "runtime.env"
    env_file.write_text("RVS_BACKEND_IMAGE=rvs-backend:smoke\n", encoding="utf-8")

    called = []
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: called.append(a))

    args = argparse.Namespace(
        distro="DockerEngine",
        project="rvs-p1",
        env_file=str(env_file),
        timeout=10,
    )
    assert module.command_build(args) == 2
    assert called == []


def test_helper_parses_env_file_without_exposing_values(tmp_path):
    module = load_helper()
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "# comment\nPUBLIC_PORT=18080\nexport RVS_ADMIN_TOKEN='quoted-value'\n",
        encoding="utf-8",
    )
    values = module.read_env_file(env_file)
    assert values["PUBLIC_PORT"] == "18080"
    assert values["RVS_ADMIN_TOKEN"] == "quoted-value"


def test_sse_event_parser_keeps_only_data_frames():
    module = load_helper()
    raw = (
        ": keep-alive\n"
        'data: {"type": "answer", "answer": "ok"}\n\n'
        "event: message\n"
        'data: {"type": "done"}\n\n'
    )
    events = module._sse_events(raw)
    assert [event["type"] for event in events] == ["answer", "done"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drive mapping only")
def test_windows_repo_path_maps_to_mnt_drive():
    module = load_helper()
    mapped = module.wsl_path(ROOT)
    assert mapped.startswith("/mnt/")
    assert "\\" not in mapped
