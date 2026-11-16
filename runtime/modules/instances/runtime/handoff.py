# -*- coding: utf-8 -*-
"""Per-session human handoff tickets (independent of chat/session lifecycle).

The chat session status and the human ticket status are deliberately separate:

* session ``active`` / ``ended`` is the normal product lifecycle;
* ticket ``waiting`` / ``active`` / ``closed`` only says where a human request is.

Storage is one ``handoff.json`` per session.  Writes are serialised by a
per-session thread lock plus an OS file lock, then committed with a temp file +
``os.replace``.  A ticket is never inferred from the transcript, so an old
session without a ticket simply has no handoff history.

Message roles written here are:

* user messages routed to the human queue: ``role=user`` with explicit
  ``author={"kind": "user", ...}``;
* human agent replies: ``role=human`` with
  ``author={"kind": "human", "display_name": "人工客服", ...}``.

Existing stores only understand ``user`` / ``assistant``.  ``SessionStore``
converts a human row to a clearly-labelled user row when building model history
instead of passing an unknown role into the model, so the agent never reads a
human answer as its own model fact.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

try:  # package imports (tests / source checkout)
    from .store import SessionNotFound, valid_session_id
except ImportError:  # pragma: no cover - flat artifact runtime
    from store import SessionNotFound, valid_session_id  # type: ignore

TICKET_SCHEMA_VERSION = "support-human-handoff/v1"
OPEN_STATUSES = {"waiting", "active"}
TICKET_STATUSES = {"waiting", "active", "closed"}
MAX_MESSAGE_CHARS = 8000
MAX_NOTE_CHARS = 4000
MAX_QUEUE_ITEMS = 200
MAX_ARCHIVED_TICKETS = 20
DEFAULT_OPERATOR = "本机人工客服"


class HandoffError(RuntimeError):
    pass


class HandoffNotFound(HandoffError):
    pass


class HandoffConflict(HandoffError):
    pass


class HandoffCorrupt(HandoffError):
    pass


def _now() -> float:
    return time.time()


def _new_ticket_id() -> str:
    return "h-" + uuid.uuid4().hex[:12]


def _new_message_id() -> str:
    return "m-" + uuid.uuid4().hex[:12]


def _author(kind: str, display_name: str) -> dict[str, Any]:
    return {"kind": kind, "display_name": display_name}


def _clean_request_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise HandoffConflict("缺少 request_id，无法保证重复回复幂等")
    if len(text) > 200:
        raise HandoffConflict("request_id 过长")
    return text


def ticket_view(ticket: dict[str, Any] | None) -> dict[str, Any] | None:
    """Public-safe projection used by the user-facing product."""
    if not isinstance(ticket, dict):
        return None
    return {
        "schema_version": TICKET_SCHEMA_VERSION,
        "ticket_id": ticket.get("ticket_id"),
        "status": ticket.get("status"),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
        "accepted_at": ticket.get("accepted_at"),
        "closed_at": ticket.get("closed_at"),
        "assigned_to": ticket.get("assigned_to"),
        "previous_ticket_ids": list(ticket.get("previous_ticket_ids") or []),
    }


def admin_ticket_view(ticket: dict[str, Any] | None) -> dict[str, Any] | None:
    """Admin projection: includes queue, replies and close note, but not file locks."""
    if not isinstance(ticket, dict):
        return None
    payload = copy.deepcopy(ticket)
    payload.pop("dedupe", None)
    return payload


class HumanHandoffStore:
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.RLock] = {}
        # A process-wide lock only guards first-use creation of the 1-byte lock
        # file; ticket mutations use the per-session lock below.
        self._lock_file_guard = threading.Lock()

    # ------------------------------------------------------------ paths/io
    def _path(self, session_id: str) -> Path:
        return self.store.session_root(session_id) / "handoff.json"

    def _lock_path(self, session_id: str) -> Path:
        return self.store.session_root(session_id) / "handoff.lock"

    def _thread_lock(self, session_id: str) -> threading.RLock:
        if not valid_session_id(session_id):
            raise SessionNotFound("无效会话标识")
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[session_id] = lock
            return lock

    @contextlib.contextmanager
    def gate(self, session_id: str) -> Iterator[None]:
        """Short server-side gate shared by request/accept/reply/close/messages."""
        lock = self._thread_lock(session_id)
        with lock:
            yield

    @contextlib.contextmanager
    def _file_lock(self, session_id: str) -> Iterator[None]:
        session_root = self.store.session_root(session_id)
        session_root.mkdir(parents=True, exist_ok=True)
        path = self._lock_path(session_id)
        with self._lock_file_guard:
            if not path.exists():
                path.touch()
        handle = path.open("a+b")
        try:
            try:
                if handle.tell() == 0 or path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":  # pragma: no cover - exercised on Windows
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:  # pragma: no cover - POSIX
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == "nt":  # pragma: no cover - exercised on Windows
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover - POSIX
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                raise HandoffError("无法锁定人工工单文件") from exc
        finally:
            handle.close()

    @contextlib.contextmanager
    def _locked(self, session_id: str) -> Iterator[None]:
        with self._thread_lock(session_id):
            with self._file_lock(session_id):
                yield

    def _read_unlocked(self, session_id: str) -> dict[str, Any] | None:
        self.store.get(session_id)  # binding + existence check
        path = self._path(session_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HandoffCorrupt("人工工单文件损坏，拒绝覆盖") from exc
        if not isinstance(payload, dict):
            raise HandoffCorrupt("人工工单文件格式非法")
        if payload.get("session_id") != session_id:
            raise HandoffCorrupt("人工工单与会话不匹配")
        return payload

    def _write_unlocked(self, session_id: str, payload: dict[str, Any]) -> None:
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            path.name
            + ".tmp-{}-{}-{}".format(os.getpid(), threading.get_ident(), uuid.uuid4().hex[:8])
        )
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(path)
        finally:
            with contextlib.suppress(OSError):
                if tmp.exists():
                    tmp.unlink()

    def _load(self, session_id: str) -> dict[str, Any] | None:
        with self._locked(session_id):
            return self._read_unlocked(session_id)

    # ------------------------------------------------------------ read
    def current(self, session_id: str) -> dict[str, Any] | None:
        return self._load(session_id)

    def public_status(self, session_id: str) -> dict[str, Any]:
        ticket = self.current(session_id)
        return {
            "enabled": True,
            "ticket": ticket_view(ticket),
            "status": (ticket or {}).get("status") or "none",
            "can_request": (ticket or {}).get("status") not in OPEN_STATUSES,
        }

    def human_messages(self, session_id: str, after: int = 0) -> dict[str, Any]:
        try:
            after = max(0, int(after))
        except (TypeError, ValueError):
            after = 0
        rows = self.store.transcript(session_id)
        cursor = len(rows)
        messages: list[dict[str, Any]] = []
        for item in rows[after:]:
            if str(item.get("role")) != "human":
                continue
            record = copy.deepcopy(item)
            record.setdefault("author", _author("human", DEFAULT_OPERATOR))
            messages.append(record)
        return {
            "messages": messages,
            "cursor": cursor,
            "ticket": ticket_view(self.current(session_id)),
        }

    # ------------------------------------------------------------ request/accept
    def request(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
        operator: str = DEFAULT_OPERATOR,
    ) -> dict[str, Any]:
        self.store.require_active(session_id)
        request_id = str(request_id or "").strip()[:200] or None
        now = _now()
        with self._locked(session_id):
            ticket = self._read_unlocked(session_id)
            if ticket and ticket.get("status") in OPEN_STATUSES:
                return {
                    "ticket": copy.deepcopy(ticket),
                    "created": False,
                    "duplicate": True,
                }
            previous: list[str] = []
            archived: list[dict[str, Any]] = []
            if ticket:
                previous = list(ticket.get("previous_ticket_ids") or [])
                if ticket.get("ticket_id"):
                    previous.append(str(ticket["ticket_id"]))
                archived = list(ticket.get("archived_tickets") or [])
                archived.append(copy.deepcopy(ticket))
                archived = archived[-MAX_ARCHIVED_TICKETS:]
            new_ticket = {
                "schema_version": TICKET_SCHEMA_VERSION,
                "ticket_id": _new_ticket_id(),
                "session_id": session_id,
                "status": "waiting",
                "created_at": now,
                "updated_at": now,
                "accepted_at": None,
                "closed_at": None,
                "assigned_to": None,
                "close_note": "",
                "last_request_id": request_id,
                "previous_ticket_ids": previous,
                "archived_tickets": archived,
                "queued_messages": [],
                "replies": [],
                "dedupe": {},
                "events": [
                    {
                        "event": "requested",
                        "at": now,
                        "operator": operator,
                        "request_id": request_id,
                        "previous_ticket_id": previous[-1] if previous else None,
                    }
                ],
            }
            self._write_unlocked(session_id, new_ticket)
            return {"ticket": copy.deepcopy(new_ticket), "created": True, "duplicate": False}

    def accept(self, session_id: str, *, operator: str = DEFAULT_OPERATOR) -> dict[str, Any]:
        now = _now()
        with self._locked(session_id):
            ticket = self._read_unlocked(session_id)
            if ticket is None:
                raise HandoffNotFound("该会话没有人工工单")
            status = ticket.get("status")
            if status == "closed":
                raise HandoffConflict("工单已结案；请让用户重新申请人工处理")
            if status == "active":
                return {"ticket": copy.deepcopy(ticket), "changed": False}
            if status != "waiting":
                raise HandoffConflict("工单状态不可接手")
            ticket["status"] = "active"
            ticket["accepted_at"] = now
            ticket["updated_at"] = now
            ticket["assigned_to"] = operator
            ticket.setdefault("events", []).append(
                {"event": "accepted", "at": now, "operator": operator}
            )
            self._write_unlocked(session_id, ticket)
            return {"ticket": copy.deepcopy(ticket), "changed": True}

    # ------------------------------------------------------------ user routing
    def queue_user_message(
        self, session_id: str, prompt: str, *, message_id: str | None = None
    ) -> dict[str, Any] | None:
        """Persist a user message into an open ticket.  ``None`` means no open ticket.

        The caller must have serialised against the agent entry (server gate); this
        method re-checks the ticket under the handoff lock and refuses to route a
        message if the ticket closed in the meantime.
        """
        content = str(prompt or "")
        if not content:
            return None
        now = _now()
        with self._locked(session_id):
            ticket = self._read_unlocked(session_id)
            if ticket is None or ticket.get("status") not in OPEN_STATUSES:
                return None
            record = self.store.append_message(
                session_id,
                "user",
                content,
                turn=None,
                ticket_id=ticket.get("ticket_id"),
                author=_author("user", "用户"),
                handoff_status=ticket.get("status"),
                message_id=message_id or _new_message_id(),
            )
            queue = list(ticket.get("queued_messages") or [])
            queue.append(
                {
                    "message_id": record.get("message_id"),
                    "content": content[:MAX_MESSAGE_CHARS],
                    "at": now,
                    "handled_by": None,
                }
            )
            ticket["queued_messages"] = queue[-MAX_QUEUE_ITEMS:]
            ticket["updated_at"] = now
            ticket.setdefault("events", []).append(
                {
                    "event": "user_message",
                    "at": now,
                    "message_id": record.get("message_id"),
                }
            )
            self._write_unlocked(session_id, ticket)
            return {"ticket": copy.deepcopy(ticket), "message": record}

    # ------------------------------------------------------------ admin reply/close
    def reply(
        self,
        session_id: str,
        message: str,
        *,
        request_id: Any,
        operator: str = DEFAULT_OPERATOR,
    ) -> dict[str, Any]:
        content = str(message or "").strip()
        if not content:
            raise HandoffConflict("人工回复内容不能为空")
        if len(content) > MAX_MESSAGE_CHARS:
            raise HandoffConflict("人工回复过长")
        key = "reply:" + _clean_request_id(request_id)
        now = _now()
        with self._locked(session_id):
            ticket = self._read_unlocked(session_id)
            if ticket is None:
                raise HandoffNotFound("该会话没有人工工单")
            dedupe = ticket.get("dedupe") or {}
            if key in dedupe:
                stored = copy.deepcopy(dedupe[key])
                return {
                    "ticket": copy.deepcopy(ticket),
                    "message": {
                        "message_id": stored.get("message_id"),
                        "role": "human",
                        "content": stored.get("content"),
                        "at": stored.get("at"),
                        "author": stored.get("author")
                        or _author("human", DEFAULT_OPERATOR),
                    },
                    "duplicate": True,
                }
            if ticket.get("status") == "closed":
                raise HandoffConflict("工单已结案，不能继续人工回复")
            if ticket.get("status") != "active":
                raise HandoffConflict("请先接手工单，再进行人工回复")
            message_id = _new_message_id()
            record = self.store.append_message(
                session_id,
                "human",
                content,
                ticket_id=ticket.get("ticket_id"),
                request_id=key.split(":", 1)[1],
                author=_author("human", operator),
                message_id=message_id,
            )
            ticket["status"] = "active"
            ticket["updated_at"] = now
            ticket["assigned_to"] = operator
            replies = list(ticket.get("replies") or [])
            replies.append(
                {
                    "request_id": key.split(":", 1)[1],
                    "message_id": message_id,
                    "content": content,
                    "at": now,
                    "operator": operator,
                }
            )
            ticket["replies"] = replies
            for item in ticket.get("queued_messages") or []:
                if isinstance(item, dict) and not item.get("handled_by"):
                    item["handled_by"] = message_id
            dedupe[key] = {
                "message_id": message_id,
                "content": content,
                "at": now,
                "author": _author("human", operator),
            }
            ticket["dedupe"] = dedupe
            ticket.setdefault("events", []).append(
                {
                    "event": "replied",
                    "at": now,
                    "message_id": message_id,
                    "request_id": key.split(":", 1)[1],
                    "operator": operator,
                }
            )
            self._write_unlocked(session_id, ticket)
            return {
                "ticket": copy.deepcopy(ticket),
                "message": record,
                "duplicate": False,
            }

    def close(
        self,
        session_id: str,
        *,
        note: str = "",
        request_id: Any = None,
        operator: str = DEFAULT_OPERATOR,
    ) -> dict[str, Any]:
        note_text = str(note or "").strip()
        if len(note_text) > MAX_NOTE_CHARS:
            raise HandoffConflict("结案备注过长")
        key = "close:" + _clean_request_id(request_id) if request_id else None
        now = _now()
        with self._locked(session_id):
            ticket = self._read_unlocked(session_id)
            if ticket is None:
                raise HandoffNotFound("该会话没有人工工单")
            dedupe = ticket.get("dedupe") or {}
            if key and key in dedupe:
                return {
                    "ticket": copy.deepcopy(ticket),
                    "duplicate": True,
                    "changed": False,
                }
            if ticket.get("status") == "closed":
                return {
                    "ticket": copy.deepcopy(ticket),
                    "duplicate": True,
                    "changed": False,
                }
            if ticket.get("status") != "active":
                raise HandoffConflict("只有处理中的工单可以结案")
            ticket["status"] = "closed"
            ticket["updated_at"] = now
            ticket["closed_at"] = now
            ticket["close_note"] = note_text
            ticket["assigned_to"] = ticket.get("assigned_to") or operator
            if key:
                dedupe[key] = {"at": now, "note": note_text}
                ticket["dedupe"] = dedupe
            ticket.setdefault("events", []).append(
                {
                    "event": "closed",
                    "at": now,
                    "operator": operator,
                    "note": note_text,
                    "request_id": (key.split(":", 1)[1] if key else None),
                }
            )
            self._write_unlocked(session_id, ticket)
            return {"ticket": copy.deepcopy(ticket), "duplicate": False, "changed": True}


def queued_for_admin(ticket: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for item in (ticket or {}).get("queued_messages") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "message_id": item.get("message_id"),
                "content": item.get("content"),
                "at": item.get("at"),
                "handled": bool(item.get("handled_by")),
            }
        )
    return rows
