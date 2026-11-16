# -*- coding: utf-8 -*-
"""Trace 的纯读取工具（编辑器 / 控制台共用）。

只读 ``outputs/traces/*.jsonl``，不写 trace、不构建场景、不调用模型。写入仍由
``core.observability.tracer.TraceCallbackHandler`` 负责；这里给控制台提供
与编辑器完全一致的只读视图。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from core.paths import output_path

# Resolved at import from AH_OUTPUT_ROOT (workers set their own tree before
# importing core.paths).  Tests/console monkeypatch this module attribute.
TRACE_DIR = output_path("traces")


def trace_dir() -> Path:
    return TRACE_DIR


def _trace_files(limit: int | None = None) -> list[Path]:
    directory = trace_dir()
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("trace_*.jsonl"), reverse=True)
    if limit is not None:
        files = files[: max(0, int(limit))]
    return files


def iter_events(limit_files: int | None = None) -> Iterator[dict[str, Any]]:
    """按文件从新到旧读取事件；坏行直接跳过。"""
    for path in _trace_files(limit_files):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def read_trace_events(trace_id: str | None, limit_files: int | None = None) -> list[dict[str, Any]]:
    """返回指定 trace 的事件；空 trace / 文件不存在返回空列表。"""
    text = str(trace_id or "").strip()
    if not text:
        return []
    return [row for row in iter_events(limit_files) if str(row.get("trace_id") or "") == text]


def recent_trace_rows(limit_files: int = 14, max_events: int | None = None) -> list[dict[str, Any]]:
    """运行列表用的最近 trace 事件（保持文件新→旧、行内顺序）。"""
    events = list(iter_events(limit_files))
    if max_events is not None:
        return events[: max(0, int(max_events))]
    return events
