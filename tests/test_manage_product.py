# -*- coding: utf-8 -*-
"""manage_product 的固定实例生命周期测试。

不启动真实服务、不访问网络、不发送终止信号。只验证错误身份拒绝停止、
命令不匹配拒绝停止、已停止幂等，以及 start 对同一实例幂等采纳不重启。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "manage_product.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("manage_product", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_config(tmp_path: Path) -> tuple[dict, Path]:
    release = tmp_path / "release"
    artifact = tmp_path / "artifact-v4-demo"
    artifact.mkdir(parents=True)
    (artifact / "serve.py").write_text("# dummy\n", encoding="utf-8")
    config_path = release / "instance-8902.json"
    release.mkdir(parents=True)
    config = {
        "schema_version": 1,
        "instance_id": "robot-vacuum-support-release-v1",
        "artifact_id": "robot_vacuum-v4-demo",
        "artifact_root": str(artifact),
        "name": "demo",
        "scenario": "robot_vacuum",
        "version": 4,
        "workflow": "agent",
        "host": "127.0.0.1",
        "port": 8902,
        "storage_root": str(tmp_path / "storage"),
        "environment_file": str(tmp_path / "env"),
        "asset_bindings": {},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config, config_path


def test_start_adopts_matching_instance_without_restart(tmp_path, monkeypatch, capsys):
    module = _load_module()
    config, config_path = _make_config(tmp_path)
    module.RELEASE_DIR = tmp_path / "release"
    module.CONFIG = config_path
    module.SERVICE_DIR = tmp_path / "service"
    module.PID_FILE = module.SERVICE_DIR / "service.json"
    module.LOG_FILE = module.SERVICE_DIR / "service.log"
    state = {
        "running": True,
        "api_ok": True,
        "port_listening": True,
        "identity_ok": True,
        "command_ok": True,
        "pid": 4242,
        "health": {"ready": True},
        "instance": {"status": "ok"},
    }
    monkeypatch.setattr(module, "_running_state", lambda _config: state)

    def _no_spawn(*args, **kwargs):
        raise AssertionError("幂等 start 不应再次启动进程")

    monkeypatch.setattr(module.subprocess, "Popen", _no_spawn)

    assert module.main(["start"]) == 0
    recorded = json.loads(module.PID_FILE.read_text(encoding="utf-8"))
    assert recorded["pid"] == 4242
    assert recorded["adopted"] is True
    assert "未重启" in capsys.readouterr().out


def test_start_refuses_mismatched_running_instance(tmp_path, monkeypatch, capsys):
    module = _load_module()
    config, config_path = _make_config(tmp_path)
    module.RELEASE_DIR = tmp_path / "release"
    module.CONFIG = config_path
    module.SERVICE_DIR = tmp_path / "service"
    module.PID_FILE = module.SERVICE_DIR / "service.json"
    module.LOG_FILE = module.SERVICE_DIR / "service.log"
    state = {
        "running": True,
        "api_ok": True,
        "port_listening": True,
        "identity_ok": False,
        "command_ok": True,
        "pid": 4242,
        "health": {"ready": True},
        "instance": {"status": "ok"},
    }
    monkeypatch.setattr(module, "_running_state", lambda _config: state)
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应启动")),
    )

    assert module.main(["start"]) == 2
    assert "拒绝启动" in capsys.readouterr().err


def test_stop_refuses_wrong_identity_without_signal(tmp_path, monkeypatch, capsys):
    module = _load_module()
    config, config_path = _make_config(tmp_path)
    module.RELEASE_DIR = tmp_path / "release"
    module.CONFIG = config_path
    state = {
        "running": True,
        "api_ok": True,
        "port_listening": True,
        "identity_ok": False,
        "command_ok": True,
        "pid": 4242,
        "health": {"ready": True},
        "instance": {"status": "ok"},
    }
    monkeypatch.setattr(module, "_running_state", lambda _config: state)
    monkeypatch.setattr(
        module,
        "_kill_tree",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应终止进程")),
    )

    assert module.main(["stop"]) == 2
    assert "身份不匹配" in capsys.readouterr().err


def test_stop_refuses_command_mismatch_without_signal(tmp_path, monkeypatch, capsys):
    module = _load_module()
    config, config_path = _make_config(tmp_path)
    module.RELEASE_DIR = tmp_path / "release"
    module.CONFIG = config_path
    state = {
        "running": True,
        "api_ok": True,
        "port_listening": True,
        "identity_ok": True,
        "command_ok": False,
        "pid": 4242,
        "health": {"ready": True},
        "instance": {"status": "ok"},
    }
    monkeypatch.setattr(module, "_running_state", lambda _config: state)
    monkeypatch.setattr(
        module,
        "_kill_tree",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应终止进程")),
    )

    assert module.main(["stop"]) == 2
    assert "命令" in capsys.readouterr().err


def test_stop_is_idempotent_when_not_running(tmp_path, monkeypatch, capsys):
    module = _load_module()
    config, config_path = _make_config(tmp_path)
    module.RELEASE_DIR = tmp_path / "release"
    module.CONFIG = config_path
    state = {
        "running": False,
        "api_ok": False,
        "port_listening": False,
        "identity_ok": False,
        "command_ok": False,
        "pid": None,
        "health": None,
        "instance": None,
    }
    monkeypatch.setattr(module, "_running_state", lambda _config: state)
    monkeypatch.setattr(
        module,
        "_kill_tree",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应终止进程")),
    )

    assert module.main(["stop"]) == 0
    assert "已停止" in capsys.readouterr().out


def test_command_match_normalizes_windows_slashes(tmp_path):
    module = _load_module()
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "serve.py").write_text("# dummy\n", encoding="utf-8")
    config = {
        "_artifact_root": str(artifact),
        "_config_path": str(tmp_path / "release" / "instance-8902.json"),
    }
    command = (
        f'"C:/python.exe" {str(artifact).replace(chr(92), "/")}/serve.py '
        f"--instance-config {config['_config_path']}"
    )
    assert module._command_matches(command, config) is True
    assert module._command_matches("python other.py", config) is False
