# -*- coding: utf-8 -*-
"""Unit checks for the deployment preflight helper (no Docker daemon needed)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "scripts" / "docker_deploy.py"
    spec = importlib.util.spec_from_file_location("rvs_docker_deploy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_read_env_file_handles_quotes_and_comments(tmp_path):
    module = _load_module()
    env_file = tmp_path / ".env.docker"
    env_file.write_text(
        "# comment\nPUBLIC_PORT=18080\nexport RVS_ADMIN_TOKEN='admin-value'\n"
        'RVS_ACCESS_TOKEN="access-value"\nEMPTY=\n',
        encoding="utf-8",
    )
    values = module.read_env_file(env_file)
    assert values["PUBLIC_PORT"] == "18080"
    assert values["RVS_ADMIN_TOKEN"] == "admin-value"
    assert values["RVS_ACCESS_TOKEN"] == "access-value"
    assert values["EMPTY"] == ""


def test_preflight_rejects_identical_tokens_without_echo(tmp_path, monkeypatch):
    module = _load_module()
    shared = "same-test-token-value-1234567890"
    env_file = tmp_path / ".env.docker"
    env_file.write_text(
        f"PUBLIC_PORT=18080\nRVS_ADMIN_TOKEN={shared}\nRVS_ACCESS_TOKEN={shared}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module,
        "_check_assets",
        lambda: {"knowledge": True, "models": True, "rag_index": True},
    )
    monkeypatch.setattr(
        module,
        "_check_ignore_files",
        lambda: {"gitignore_env": True, "dockerignore_env": True},
    )
    monkeypatch.setattr(module, "_port_free", lambda port: True)
    monkeypatch.setattr(
        module,
        "_run",
        lambda command, *, timeout=30, env=None: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )
    monkeypatch.setattr(
        module,
        "_compose",
        lambda project, env_file, *args, env=None: subprocess.CompletedProcess(
            ["docker", "compose", *args], 0, "", ""
        ),
    )
    result = module.preflight(env_file, project="rvssmoke", port=18080)
    assert result["ok"] is False
    assert "tokens_distinct" in result["failed"]
    assert shared not in json.dumps(result, ensure_ascii=False)


def test_preflight_accepts_distinct_tokens(tmp_path, monkeypatch):
    module = _load_module()
    env_file = tmp_path / ".env.docker"
    env_file.write_text(
        "PUBLIC_PORT=18080\n"
        "RVS_ADMIN_TOKEN=admin-test-token-value-1234567890\n"
        "RVS_ACCESS_TOKEN=access-test-token-value-0987654321\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module,
        "_check_assets",
        lambda: {"knowledge": True, "models": True, "rag_index": True},
    )
    monkeypatch.setattr(
        module,
        "_check_ignore_files",
        lambda: {"gitignore_env": True, "dockerignore_env": True},
    )
    monkeypatch.setattr(module, "_port_free", lambda port: True)
    monkeypatch.setattr(
        module,
        "_run",
        lambda command, *, timeout=30, env=None: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )
    monkeypatch.setattr(
        module,
        "_compose",
        lambda project, env_file, *args, env=None: subprocess.CompletedProcess(
            ["docker", "compose", *args], 0, "", ""
        ),
    )
    result = module.preflight(env_file, project="rvssmoke", port=18080)
    assert result["ok"] is True
    assert result["failed"] == []
