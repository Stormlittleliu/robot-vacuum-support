# -*- coding: utf-8 -*-
"""demo_readiness 的最小单元测试。

全部使用 tmp_path 里的 dummy 资产/模型/索引/artifact，不依赖真实大数据、
不访问网络、不启动服务、不调用模型。只复用 source 的 artifact manifest
校验库来生成和校验 dummy manifest。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "demo_readiness.py"
RUNTIME_ROOT = REPO_ROOT / "runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from modules.artifacts.manifest import compute_content_hash, hash_tree  # noqa: E402

KNOWLEDGE_NAMES = (
    "故障排除.txt",
    "扫地机器人100问.pdf",
    "扫地机器人100问2.txt",
    "扫拖一体机器人100问.txt",
    "维护保养.txt",
    "选购指南.txt",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("demo_readiness", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)


def _make_source(root: Path) -> None:
    project = {
        "schema": 1,
        "name": "robot_vacuum",
        "scenario": "robot_vacuum",
        "project_name": "robot-vacuum-support",
        "spec_root": ".",
        "needs_configuration": [
            "model_credentials: DEEPSEEK_API_KEY (instance environment_file, not copied)"
        ],
        "asset_source": {
            "knowledge": ["assets/knowledge"],
            "models": ["assets/models"],
            "rag_index": ["assets/rag_index"],
        },
    }
    _write(root / "project.json", json.dumps(project))
    _write(
        root / "robot_vacuum" / "state.json",
        json.dumps({"name": "robot_vacuum", "owner": "spec", "code_hash": None}),
    )
    _write(
        root / "robot_vacuum" / "active.json",
        json.dumps({"version": 4, "file": "v4.json"}),
    )
    _write(root / "robot_vacuum" / "v4.json", json.dumps({"schema_version": 2}))
    for name in KNOWLEDGE_NAMES:
        _write(root / "assets" / "knowledge" / name, b"dummy knowledge")
    _write(
        root / "assets" / "models" / "bge-small-zh-v1.5" / "model.safetensors",
        b"dummy model",
    )
    _write(
        root / "assets" / "models" / "bge-small-zh-v1.5" / "config.json",
        b"{}",
    )
    _write(
        root / "assets" / "models" / "bge-small-zh-v1.5" / "tokenizer.json",
        b"{}",
    )
    _write(root / "assets" / "rag_index" / "chroma.sqlite3", b"dummy sqlite")
    _write(
        root / "assets" / "rag_index" / "seg-1" / "data_level0.bin",
        b"dummy index",
    )


def _make_artifact(root: Path) -> Path:
    artifact = root / "artifact"
    files: dict[str, bytes] = {
        "run.py": b"# dummy cli\n",
        "serve.py": b"# dummy server\n",
        "requirements.txt": b"# none\n",
        "runtime/__init__.py": b"# dummy runtime\n",
        "product_ui/index.html": b"<html></html>\n",
        "robot_vacuum/active.json": json.dumps(
            {"version": 4, "file": "v4.json"}
        ).encode(),
        "robot_vacuum/v4.json": b"{}\n",
        "robot_vacuum/state.json": b'{"name":"robot_vacuum","owner":"spec"}\n',
    }
    for relative, content in files.items():
        _write(artifact / relative, content)

    checksums = {
        relative: hashlib.sha256(content).hexdigest()
        for relative, content in sorted(files.items())
    }
    runtime_hash = hash_tree(artifact / "runtime")
    manifest = {
        "schema_version": 2,
        "artifact_id": "robot_vacuum-v4-dummy0001",
        "name": "robot_vacuum",
        "version": 4,
        "workflows": ["agent"],
        "entry": {
            "cli": "run.py",
            "server": "serve.py",
            "default_workflow": "agent",
        },
        "frontend": {"root": "product_ui", "api_version": "v1"},
        "runtime": {
            "included": True,
            "python": ">=3.10",
            "root": "runtime",
            "requirements": "requirements.txt",
            "hash": runtime_hash,
        },
        "source": {"scenario": "robot_vacuum", "version": 4},
        "files": sorted(files),
        "checksums": checksums,
        "content_hash": compute_content_hash(checksums),
    }
    _write(artifact / "manifest.json", json.dumps(manifest))
    return artifact


def _make_env(root: Path) -> Path:
    env_file = root / "demo.env"
    _write(env_file, "DEEPSEEK_API_KEY=dummy-value-do-not-print\n")
    return env_file


def test_live_ready_with_dummy_assets_and_no_secret_leak(tmp_path, capsys):
    module = _load_module()
    _make_source(tmp_path)
    artifact = _make_artifact(tmp_path)
    env_file = _make_env(tmp_path)
    output = tmp_path / "report.json"

    code = module.main(
        [
            "--root",
            str(tmp_path),
            "--artifact",
            str(artifact),
            "--env-file",
            str(env_file),
            "--mode",
            "live",
            "--output",
            str(output),
        ]
    )
    stdout = capsys.readouterr().out
    assert code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["not_a_live_test"] is True
    assert report["live_model_invoked"] is False
    assert report["service_started"] is False
    assert report["source_read_only"] is True
    assert report["ready_for_offline"] is True
    assert report["ready_for_live"] is True
    assert report["checks"]["artifact"]["service_ready"] is True
    assert report["checks"]["artifact"]["runtime_hash_ok"] is True
    assert report["checks"]["model_config"]["configured"] is True
    assert "DEEPSEEK_API_KEY" in stdout
    assert "dummy-value-do-not-print" not in stdout
    assert "dummy-value-do-not-print" not in output.read_text(encoding="utf-8")
    assert str(env_file) not in stdout


def test_missing_artifact_is_not_guessed_by_mtime(tmp_path, capsys):
    module = _load_module()
    _make_source(tmp_path)
    # 故意放一个更高 mtime 的“旧产物”，检查脚本不会把它当 latest。
    decoy = tmp_path / "built" / "robot_vacuum-v9-deadbeef"
    decoy.mkdir(parents=True)
    _write(decoy / "manifest.json", b"{}")

    code = module.main(["--root", str(tmp_path), "--mode", "offline"])
    stdout = capsys.readouterr().out
    report = json.loads(stdout)
    assert code == 2
    assert report["status"] == "not_ready"
    assert report["checks"]["artifact"]["specified"] is False
    assert any("artifact 待指定" in item for item in report["blocking"])
    assert "v9-deadbeef" not in stdout


def test_live_mode_requires_explicit_env_but_offline_can_pass(
    tmp_path, capsys, monkeypatch
):
    module = _load_module()
    _make_source(tmp_path)
    artifact = _make_artifact(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    code = module.main(
        ["--root", str(tmp_path), "--artifact", str(artifact), "--mode", "live"]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["ready_for_offline"] is True
    assert report["ready_for_live"] is False
    assert report["checks"]["model_config"]["configured"] is False
    assert any("live 模式" in item for item in report["blocking"])

    code = module.main(
        ["--root", str(tmp_path), "--artifact", str(artifact), "--mode", "offline"]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["status"] == "ready"
    assert report["ready_for_offline"] is True
    assert report["ready_for_live"] is False


def test_wrong_spec_owner_blocks(tmp_path, capsys):
    module = _load_module()
    _make_source(tmp_path)
    artifact = _make_artifact(tmp_path)
    _write(
        tmp_path / "robot_vacuum" / "state.json",
        json.dumps({"name": "robot_vacuum", "owner": "code", "code_hash": "bad"}),
    )

    code = module.main(
        ["--root", str(tmp_path), "--artifact", str(artifact), "--mode", "offline"]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["checks"]["spec"]["ok"] is False
    assert report["checks"]["spec"]["owner"] == "code"
    assert any("spec owner" in item for item in report["blocking"])
