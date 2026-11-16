# -*- coding: utf-8 -*-
"""录制前只读就绪检查（不启动服务、不调用模型）。

用途：
- 检查 6 份知识原文、本地 embedding 模型、Chroma 索引是否存在且可读；
- 检查 project.json 与 spec owner（state.json owner=spec）是否仍是当前 source；
- 可选检查 --artifact 的 manifest/checksums/runtime hash，复用
  runtime/modules/artifacts/manifest.py 的 verify_artifact，不另写一套校验；
- 可选检查显式 --env-file 中模型凭据变量名是否存在（只输出 boolean，不输出值，
  也不向上搜索 .env）；
- 输出 JSON，exit 0 表示所选模式就绪，exit 2 表示未就绪。

边界：
- 这不是 live test，不启动 serve.py，不请求 /api/health，不调用模型；
- 只校验“录制前资产/产物/配置就绪”，不证明模型回答质量；
- 不按 mtime 猜最新 artifact；未指定 --artifact 时显示“待指定”；
- 不写 artifact、assets、spec、user notes 或 eval/reviews；只写显式 --output。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

SCHEMA_VERSION = "demo-readiness/v1"
EXPECTED_KNOWLEDGE = (
    "故障排除.txt",
    "扫地机器人100问.pdf",
    "扫地机器人100问2.txt",
    "扫拖一体机器人100问.txt",
    "维护保养.txt",
    "选购指南.txt",
)
_CREDENTIAL_RE = re.compile(r"model_credentials:\s*([A-Za-z_][A-Za-z0-9_]*)")
_ENV_LINE_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$"
)
_PYTHON_MIN_RE = re.compile(r">=\s*(\d+)\.(\d+)")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: cannot read JSON"
    if not isinstance(data, dict):
        return None, "JSON root is not an object"
    return data, None


def _is_readable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.R_OK)


def _check_assets(root: Path) -> dict[str, Any]:
    knowledge_dir = root / "assets" / "knowledge"
    names = sorted(
        path.name for path in knowledge_dir.glob("*") if path.is_file()
    ) if knowledge_dir.is_dir() else []
    readable = {
        name: _is_readable_file(knowledge_dir / name) for name in names
    }
    knowledge_ok = (
        set(names) == set(EXPECTED_KNOWLEDGE)
        and len(names) == len(EXPECTED_KNOWLEDGE)
        and all(readable.values())
    )

    models_root = root / "assets" / "models"
    model_dirs = sorted(
        path.name
        for path in models_root.iterdir()
        if path.is_dir()
    ) if models_root.is_dir() else []
    model_ok = False
    model_files: list[str] = []
    model_name: str | None = None
    for name in model_dirs:
        candidate = models_root / name
        required = ["model.safetensors", "config.json", "tokenizer.json"]
        if all(_is_readable_file(candidate / item) for item in required):
            model_ok = True
            model_name = name
            model_files = sorted(
                path.name for path in candidate.glob("*") if path.is_file()
            )
            break

    index_dir = root / "assets" / "rag_index"
    index_files = sorted(
        path.name for path in index_dir.rglob("*") if path.is_file()
    ) if index_dir.is_dir() else []
    index_ok = (
        index_dir.is_dir()
        and _is_readable_file(index_dir / "chroma.sqlite3")
        and any(name.endswith(".bin") for name in index_files)
    )

    checks = {
        "knowledge": {
            "ok": knowledge_ok,
            "expected_count": len(EXPECTED_KNOWLEDGE),
            "actual_count": len(names),
            "expected_names": list(EXPECTED_KNOWLEDGE),
            "actual_names": names,
            "readable": readable,
            "access": "read_only",
        },
        "models": {
            "ok": model_ok,
            "model_name": model_name,
            "file_count": len(model_files),
            "required_files": ["model.safetensors", "config.json", "tokenizer.json"],
            "access": "read_only",
        },
        "rag_index": {
            "ok": index_ok,
            "file_count": len(index_files),
            "has_sqlite": (index_dir / "chroma.sqlite3").is_file(),
            "has_parquet_or_bin": any(
                name.endswith(".bin") or name.endswith(".parquet")
                for name in index_files
            ),
            "access": "read_only",
        },
    }
    checks["ok"] = bool(
        checks["knowledge"]["ok"]
        and checks["models"]["ok"]
        and checks["rag_index"]["ok"]
    )
    return checks


def _credential_names(project: dict[str, Any]) -> list[str]:
    names: list[str] = []
    needs = project.get("needs_configuration")
    if isinstance(needs, list):
        for item in needs:
            if not isinstance(item, str):
                continue
            match = _CREDENTIAL_RE.search(item)
            if match:
                names.append(match.group(1))
    if not names:
        names.append("DEEPSEEK_API_KEY")
    return sorted(set(names))


def _check_project(root: Path) -> dict[str, Any]:
    project_path = root / "project.json"
    project, error = _load_json(project_path)
    if project is None:
        return {
            "ok": False,
            "error": error or "project.json missing",
            "credential_names": ["DEEPSEEK_API_KEY"],
        }
    scenario = project.get("scenario")
    spec_root = project.get("spec_root")
    ok = scenario == "robot_vacuum" and spec_root == "."
    return {
        "ok": ok,
        "name": project.get("name"),
        "scenario": scenario,
        "spec_root": spec_root,
        "credential_names": _credential_names(project),
    }


def _check_spec_owner(root: Path) -> dict[str, Any]:
    spec_dir = root / "robot_vacuum"
    state, state_error = _load_json(spec_dir / "state.json")
    active, active_error = _load_json(spec_dir / "active.json")
    owner = state.get("owner") if state else None
    active_file = active.get("file") if active else None
    active_version = active.get("version") if active else None
    active_exists = False
    if isinstance(active_file, str):
        candidate = spec_dir / active_file
        active_exists = _is_readable_file(candidate)
    ok = owner == "spec" and active_exists
    return {
        "ok": ok,
        "owner": owner,
        "active_version": active_version,
        "active_file": active_file,
        "active_file_exists": active_exists,
        "state_error": state_error,
        "active_error": active_error,
    }


def _check_model_config(project: dict[str, Any], env_file: str | None) -> dict[str, Any]:
    names = _credential_names(project)
    result: dict[str, Any] = {
        "source": "explicit_env_file" if env_file else "process_env",
        "env_file_provided": bool(env_file),
        "env_file_readable": None,
        "keys": {},
        "configured": False,
        "access": "boolean_only",
    }
    if env_file:
        path = Path(env_file)
        if not path.is_file():
            result["env_file_readable"] = False
            result["keys"] = {
                name: {"present": False, "non_empty": False} for name in names
            }
            return result
        present: set[str] = set()
        non_empty: set[str] = set()
        try:
            content = path.read_text(encoding="utf-8-sig")
        except OSError:
            result["env_file_readable"] = False
            result["keys"] = {
                name: {"present": False, "non_empty": False} for name in names
            }
            return result
        result["env_file_readable"] = True
        for line in content.splitlines():
            match = _ENV_LINE_RE.match(line)
            if not match:
                continue
            key = match.group(1)
            # 只用于判断空/非空，不保留或输出值。
            value = match.group(2).strip()
            present.add(key)
            if value and value not in ("''", '""'):
                non_empty.add(key)
        result["keys"] = {
            name: {
                "present": name in present,
                "non_empty": name in non_empty,
            }
            for name in names
        }
    else:
        result["keys"] = {
            name: {
                "present": name in os.environ,
                "non_empty": bool(os.environ.get(name, "")),
            }
            for name in names
        }
    result["configured"] = all(
        item["present"] and item["non_empty"]
        for item in result["keys"].values()
    )
    return result


def _python_satisfies(spec: str | None) -> bool | None:
    if not isinstance(spec, str):
        return None
    match = _PYTHON_MIN_RE.search(spec)
    if not match:
        return None
    return sys.version_info[:2] >= (int(match.group(1)), int(match.group(2)))


def _check_artifact(artifact: str | None, root: Path) -> dict[str, Any]:
    if not artifact:
        return {
            "specified": False,
            "ok": False,
            "service_ready": False,
            "checksums_ok": False,
            "note": "artifact 待指定；不按 mtime 猜最新，也不启动旧产物",
        }
    path = Path(artifact)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_dir():
        return {
            "specified": True,
            "ok": False,
            "service_ready": False,
            "checksums_ok": False,
            "note": "artifact 目录不存在",
        }
    try:
        from modules.artifacts.manifest import verify_artifact

        report = verify_artifact(path, require_service=True)
    except Exception as exc:  # noqa: BLE001 - 只回传类型，不输出路径内容
        return {
            "specified": True,
            "ok": False,
            "service_ready": False,
            "checksums_ok": False,
            "note": f"manifest 校验库调用失败: {type(exc).__name__}",
            "library": "runtime/modules/artifacts/manifest.py:verify_artifact",
        }
    manifest = report.get("manifest") if isinstance(report.get("manifest"), dict) else {}
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    entry = manifest.get("entry") if isinstance(manifest.get("entry"), dict) else {}
    issues = list(report.get("issues") or [])
    warnings = list(report.get("warnings") or [])
    runtime_python = runtime.get("python")
    runtime_python_satisfied = _python_satisfies(runtime_python)
    if runtime_python_satisfied is False:
        issues.append(
            f"current Python {sys.version_info.major}.{sys.version_info.minor} "
            f"does not satisfy manifest.runtime.python {runtime_python!r}"
        )
    ok = bool(report.get("ok")) and runtime_python_satisfied is not False
    return {
        "specified": True,
        "ok": ok,
        "service_ready": bool(report.get("service_ready")) and ok,
        "checksums_ok": bool(report.get("checksums_ok")),
        "artifact_id": manifest.get("artifact_id"),
        "version": manifest.get("version"),
        "runtime_python": runtime_python,
        "runtime_python_satisfied": runtime_python_satisfied,
        "runtime_hash": runtime.get("hash"),
        "runtime_hash_ok": bool(manifest)
        and not any("runtime hash" in str(item) for item in issues),
        "entry_server": entry.get("server"),
        "workflows": manifest.get("workflows"),
        "issues": issues,
        "warnings": warnings,
        "library": "runtime/modules/artifacts/manifest.py:verify_artifact",
        "note": "离线 manifest/checksum/runtime hash 校验；不启动服务、不调用模型",
    }


def build_report(
    root: Path,
    artifact: str | None,
    env_file: str | None,
    mode: str,
) -> dict[str, Any]:
    assets = _check_assets(root)
    project = _check_project(root)
    spec = _check_spec_owner(root)
    model_config = _check_model_config(project, env_file)
    artifact_check = _check_artifact(artifact, root)

    ready_for_offline = bool(
        assets["ok"]
        and project["ok"]
        and spec["ok"]
        and artifact_check["specified"]
        and artifact_check["ok"]
        and artifact_check["service_ready"]
    )
    ready_for_live = bool(ready_for_offline and model_config["configured"])

    blocking: list[str] = []
    if not assets["ok"]:
        blocking.append("资产检查未通过：6 份知识原文/模型/索引未全部只读存在")
    if not project["ok"]:
        blocking.append("project.json 不是当前 robot_vacuum source 或 spec_root 不是 '.'")
    if not spec["ok"]:
        blocking.append("spec owner 不是 spec，或 active spec 文件不存在")
    if not artifact_check["specified"]:
        blocking.append("artifact 待指定：不猜最新 mtime，也不启动旧产物")
    elif not artifact_check["ok"]:
        blocking.append("artifact manifest/checksum/runtime hash 校验未通过")
    elif not artifact_check["service_ready"]:
        blocking.append("artifact 不是 service-ready（serve.py/product_ui 缺失或校验失败）")

    live_blocking = list(blocking)
    if not model_config["configured"]:
        live_blocking.append(
            "live 模式未检测到显式模型凭据（只检查变量名/非空，不读取值，不向上搜索 .env）"
        )

    selected_blocking = live_blocking if mode == "live" else blocking
    ready = ready_for_live if mode == "live" else ready_for_offline
    status = "ready" if ready else "not_ready"

    notes = [
        "not_a_live_test=true；本检查不启动服务、不请求 /api/health、不调用模型。",
        "artifact 未指定时显示待指定；不会按 mtime 猜最新产物。",
        "模型凭据只输出 boolean；不输出 env 值，不向上搜索 .env。",
        "GUI 8950 是人工审核工具，不是客服产品，也不在本次检查范围内。",
    ]
    if not model_config["configured"]:
        notes.append("当前离线结构演示可继续；真实问答需显式模型配置与隐藏启动。")
    if artifact_check.get("artifact_id"):
        notes.append(
            f"artifact_id={artifact_check['artifact_id']}；"
            "checksum 通过不等于模型/网络/回答质量已就绪。"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "not_a_live_test": True,
        "live_model_invoked": False,
        "service_started": False,
        "source_read_only": True,
        "mode": mode,
        "status": status,
        "status_meaning": "只读录制前就绪检查；不启动服务、不调用模型，不代表回答质量或评测完成。",
        "ready_for_offline": ready_for_offline,
        "ready_for_live": ready_for_live,
        "root": str(root),
        "checks": {
            "assets": assets,
            "project": project,
            "spec": spec,
            "artifact": artifact_check,
            "model_config": model_config,
        },
        "blocking": selected_blocking,
        "live_blocking": live_blocking,
        "notes": notes,
    }


def _write_output(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读录制前就绪检查；不启动服务、不调用模型"
    )
    parser.add_argument(
        "--root",
        default=str(REPO_ROOT),
        help="工程根目录（默认当前仓库）",
    )
    parser.add_argument(
        "--artifact",
        default=None,
        help="显式 artifact 目录；不传则显示待指定，不按 mtime 猜最新",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="显式 env 文件；只检查变量名/非空，不输出值，不向上搜索",
    )
    parser.add_argument(
        "--mode",
        choices=("offline", "live"),
        default="offline",
        help="offline=无网络结构演示；live=真实问答，额外要求模型凭据",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="可选：把 JSON 写入指定路径（仅写这一个报告）",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    report = build_report(root, args.artifact, args.env_file, args.mode)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        _write_output(args.output, report)
    return 0 if report["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
