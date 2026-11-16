# -*- coding: utf-8 -*-
"""Approval checkpoint 的最小生命周期管理。

checkpoint 本身由 LangGraph SqliteSaver 写；这里只记录 thread 的创建时间，
用于清理过期的待审批 thread，避免 SQLite 无限增长。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.paths import runtime_path

DEFAULT_TTL_DAYS = 7


def _paths(root: str | Path | None = None) -> tuple[Path, Path]:
    base = Path(root) if root is not None else runtime_path()
    return base / "approval_checkpoints.sqlite", base / "approval_threads.json"


def _load_meta(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember_thread(thread_id: str, root: str | Path | None = None) -> None:
    _, meta_path = _paths(root)
    meta = _load_meta(meta_path)
    meta[str(thread_id)] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cleanup_expired(
    root: str | Path | None = None,
    *,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> list[str]:
    """删除超过 TTL 的 approval thread checkpoint，返回被清理的 thread。"""
    db_path, meta_path = _paths(root)
    meta = _load_meta(meta_path)
    if not meta:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    expired: list[str] = []
    for thread_id, raw in list(meta.items()):
        try:
            created = datetime.fromisoformat(str(raw))
        except ValueError:
            created = cutoff - timedelta(seconds=1)
        if created < cutoff:
            expired.append(thread_id)
    if expired and db_path.is_file():
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                for thread_id in expired:
                    conn.execute("delete from writes where thread_id = ?", (thread_id,))
                    conn.execute("delete from checkpoints where thread_id = ?", (thread_id,))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            return []
    if expired:
        for thread_id in expired:
            meta.pop(thread_id, None)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return expired
