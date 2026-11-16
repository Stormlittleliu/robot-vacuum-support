# -*- coding: utf-8 -*-
"""Server-authoritative session store for instance products.

Metadata and transcript live under ``storage_root/sessions/<sid>/``.  The UI
never has to supply chat history; the server rebuilds it after a restart.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SESSION_SCHEMA_VERSION = 1
_SID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")


class SessionStoreError(RuntimeError):
    pass


class SessionNotFound(SessionStoreError):
    pass


class SessionBindingError(SessionStoreError):
    pass


class SessionEnded(SessionStoreError):
    pass


def valid_session_id(session_id: str) -> bool:
    return bool(_SID_RE.fullmatch(str(session_id or "")))


class SessionStore:
    def __init__(self, config):
        self.instance_id = str(config.instance_id)
        self.artifact_id = str(config.artifact_id)
        self.scenario = str(config.scenario)
        self.root = Path(getattr(config, "sessions_root", Path(config.storage_root) / "sessions"))
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.RLock] = {}

    # ------------------------------------------------------------ paths
    def session_root(self, session_id: str) -> Path:
        if not valid_session_id(session_id):
            raise SessionNotFound("无效会话标识")
        return self.root / session_id

    def workspace_path(self, session_id: str) -> Path:
        return self.session_root(session_id) / "workspace"

    def data_path(self, session_id: str) -> Path:
        return self.session_root(session_id) / "data"

    def _metadata_path(self, session_id: str) -> Path:
        return self.session_root(session_id) / "session.json"

    def _transcript_path(self, session_id: str) -> Path:
        return self.session_root(session_id) / "transcript.jsonl"

    def _support_state_path(self, session_id: str) -> Path:
        return self.session_root(session_id) / "support_state.json"

    def _lock_for(self, session_id: str) -> threading.RLock:
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[session_id] = lock
            return lock

    # ------------------------------------------------------------ metadata
    def _write_metadata(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        root = self.session_root(session_id)
        root.mkdir(parents=True, exist_ok=True)
        path = self._metadata_path(session_id)
        tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return payload

    def read_metadata(self, session_id: str) -> dict[str, Any] | None:
        path = self._metadata_path(session_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _validate_binding(self, metadata: dict[str, Any]) -> dict[str, Any]:
        if metadata.get("instance_id") != self.instance_id:
            raise SessionBindingError("会话不属于当前实例")
        if metadata.get("artifact_id") != self.artifact_id:
            raise SessionBindingError("会话绑定的是其它 artifact")
        if metadata.get("scenario") not in (None, self.scenario):
            raise SessionBindingError("会话绑定的是其它场景")
        return metadata

    def get(self, session_id: str) -> dict[str, Any]:
        metadata = self.read_metadata(session_id)
        if metadata is None:
            raise SessionNotFound("会话不存在")
        return self._validate_binding(metadata)

    def list(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return rows
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or not valid_session_id(directory.name):
                continue
            metadata = self.read_metadata(directory.name)
            if metadata is None:
                continue
            try:
                self._validate_binding(metadata)
            except SessionBindingError:
                continue
            metadata["session_id"] = directory.name
            metadata["workspace"] = str(directory / "workspace")
            rows.append(metadata)
        rows.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
        return rows

    def create(self, workflow: str | None = None) -> dict[str, Any]:
        session_id = "s-" + uuid.uuid4().hex[:16]
        now = time.time()
        metadata = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": session_id,
            "instance_id": self.instance_id,
            "artifact_id": self.artifact_id,
            "scenario": self.scenario,
            "workflow": workflow,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "thread_id": None,
        }
        root = self.session_root(session_id)
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        (root / "data").mkdir(parents=True, exist_ok=True)
        self._write_metadata(session_id, metadata)
        self._transcript_path(session_id).touch()
        return metadata

    def update(self, session_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock_for(session_id):
            metadata = self.get(session_id)
            metadata.update(fields)
            metadata["updated_at"] = time.time()
            return self._write_metadata(session_id, metadata)

    def mark(self, session_id: str, status: str, **fields: Any) -> dict[str, Any]:
        if status not in {"active", "ended"}:
            raise ValueError("无效会话状态")
        return self.update(session_id, status=status, **fields)

    def require_active(self, session_id: str) -> dict[str, Any]:
        metadata = self.get(session_id)
        if metadata.get("status") == "ended":
            raise SessionEnded("会话已结束，请先重新打开")
        return metadata

    # ------------------------------------------------------------ support state
    def load_support_state(self, session_id: str) -> dict[str, Any] | None:
        """Read the project-owned support state; missing/corrupt = legacy lazy init."""
        self.get(session_id)
        path = self._support_state_path(session_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def save_support_state(self, session_id: str, state: dict[str, Any]) -> dict[str, Any]:
        """Atomically persist support state under the same per-session lock.

        The caller holds the existing worker busy reservation; this lock only
        serialises file writes so a state file can never be half-written.
        """
        if not isinstance(state, dict):
            raise ValueError("support state must be an object")
        with self._lock_for(session_id):
            self.get(session_id)
            path = self._support_state_path(session_id)
            tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
            tmp.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
        return state

    # ------------------------------------------------------------ transcript
    def append_message(
        self, session_id: str, role: str, content: str, **extra: Any
    ) -> dict[str, Any]:
        if not content:
            return {}
        record = {
            "role": str(role),
            "content": str(content),
            "at": time.time(),
            "message_id": "m-" + uuid.uuid4().hex[:12],
        }
        record.update(extra)
        with self._lock_for(session_id):
            self.get(session_id)
            path = self._transcript_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def transcript(self, session_id: str) -> list[dict[str, Any]]:
        self.get(session_id)
        path = self._transcript_path(session_id)
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("content"):
                    rows.append(item)
        except OSError:
            return rows
        return rows

    def history(self, session_id: str, limit: int = 24) -> list[dict[str, str]]:
        rows = self.transcript(session_id)
        history = [
            {"role": str(item.get("role")), "content": str(item.get("content"))}
            for item in rows
            if item.get("role") in {"user", "assistant"} and not item.get("interrupted")
        ]
        return history[-limit:]

    # ------------------------------------------------------------ files
    def workspace_files(self, session_id: str, limit: int = 500) -> list[dict[str, Any]]:
        self.get(session_id)
        root = self.workspace_path(session_id).resolve()
        files: list[dict[str, Any]] = []
        if not root.is_dir():
            return files
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
                relative = resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            files.append({"path": relative.as_posix(), "bytes": path.stat().st_size})
            if len(files) >= limit:
                break
        return files

    def resolve_workspace_file(self, session_id: str, relative: str) -> Path:
        self.get(session_id)
        root = self.workspace_path(session_id).resolve()
        target = (root / str(relative or "")).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise FileNotFoundError("文件不存在或不属于当前工作区")
        return target
