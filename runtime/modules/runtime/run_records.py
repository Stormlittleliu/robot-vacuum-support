# -*- coding: utf-8 -*-
"""公共的只读运行快照存储（编辑器 / 控制台共用）。

只写运行时目录，不碰 ``data/specs``、不切 active、不保存活对象。一次 run 一个
JSON 文件，包含发起时的文档深拷贝、配置 hash、trace/thread、答案和用量；
审批续跑更新同一份记录，保证"结果属于当时那份配置"。

控制台只读这里，不经过编辑器 HTTP；``modules/editor/run_store.py`` 只是兼容
re-export shim。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.paths import runtime_path

RUN_ROOT = runtime_path("editor_runs")
RUN_ID_PATTERN = re.compile(r"^run-[0-9a-f]{32}$")
STATUSES = {"running", "interrupted", "completed", "error"}


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_PATTERN.fullmatch(str(run_id or "")))


def _path(run_id: str) -> Path:
    if not valid_run_id(run_id):
        raise ValueError("无效的运行标识")
    return Path(RUN_ROOT) / f"{run_id}.json"


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def config_hash(name: str, workflow: str, document: dict[str, Any] | None) -> str:
    payload = json.dumps(
        {"scenario": name, "workflow": workflow, "document": document},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write(record: dict[str, Any]) -> Path:
    path = _path(str(record["run_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path


def create_run(
    name: str,
    workflow: str,
    *,
    base_revision: int | None,
    input_text: str,
    document: dict[str, Any] | None,
    source: str = "editor",
    run_id: str | None = None,
) -> dict[str, Any]:
    """在真正执行前落一份运行快照。``document`` 必须已被深拷贝。"""
    run_id = run_id or new_run_id()
    if not valid_run_id(run_id):
        raise ValueError("无效的运行标识")
    now = _now()
    snapshot = copy.deepcopy(document) if document is not None else None
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "scenario": name,
        "workflow": workflow,
        "base_revision": base_revision,
        "input": input_text,
        "document": _json_copy(snapshot),
        "config_hash": config_hash(name, workflow, snapshot),
        "source": source,
        "created_at": now,
        "updated_at": now,
        "status": "running",
        "trace_id": None,
        "thread_id": None,
        "answer": "",
        "error": None,
        "usage": None,
    }
    _write(record)
    return _json_copy(record)


def get_run(run_id: str, *, include_document: bool = True) -> dict[str, Any] | None:
    try:
        path = _path(run_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if not include_document:
        record = {key: value for key, value in record.items() if key != "document"}
    return record


def update_run(run_id: str, **changes: Any) -> dict[str, Any] | None:
    record = get_run(run_id)
    if record is None:
        return None
    if "status" in changes and changes["status"] not in STATUSES:
        raise ValueError(f"未知运行状态：{changes['status']}")
    if "document" in changes:
        changes["document"] = _json_copy(changes["document"])
    record.update(changes)
    record["updated_at"] = _now()
    _write(record)
    return record


def find_by_thread(thread_id: str) -> dict[str, Any] | None:
    if not thread_id:
        return None
    root = Path(RUN_ROOT)
    if not root.is_dir():
        return None
    for path in root.glob("run-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("thread_id") == thread_id:
            return record
    return None


def list_runs() -> list[dict[str, Any]]:
    root = Path(RUN_ROOT)
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("run-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        record = get_run(path.stem, include_document=False)
        if record is not None:
            rows.append(record)
    return rows
