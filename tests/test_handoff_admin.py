# -*- coding: utf-8 -*-
"""Human handoff/admin regressions: routing, idempotency, concurrency, ACL.

These tests do not call a model: the handoff path must work with only transcript
storage, and the one "bot resumed" test replaces the worker with a fake.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "runtime", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from modules.instances.runtime import server as server_module  # noqa: E402
from modules.instances.runtime.handoff import HandoffConflict  # noqa: E402
from modules.instances.runtime.server import create_app  # noqa: E402


class FakeConfig:
    instance_id = "inst-handoff"
    artifact_id = "art-handoff"
    scenario = "robot_vacuum"
    version = 4
    name = "handoff admin test"
    port = 8948
    workflows = ["agent"]
    default_workflow = "agent"
    issues = []
    manifest = {}

    def __init__(self, root: Path, host: str = "127.0.0.1"):
        self.host = host
        self.sessions_root = root / "sessions"
        self.storage_root = root
        self.product_ui_root = ROOT / "product_ui"


def make_client(tmp_path: Path, host: str = "127.0.0.1"):
    app = create_app(FakeConfig(tmp_path, host=host))
    return app, TestClient(app)


def sse_events(response) -> list[dict]:
    events = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def test_full_handoff_lifecycle_is_idempotent_and_restores_bot(tmp_path, monkeypatch):
    app, client = make_client(tmp_path)
    with client:
        store = app.state.store
        session_id = store.create(workflow="agent")["session_id"]

        first = client.post(
            f"/api/sessions/{session_id}/handoff/request",
            json={"request_id": "apply-1"},
        )
        assert first.status_code == 200, first.text
        ticket = first.json()["ticket"]
        assert ticket["status"] == "waiting"
        assert first.json()["created"] is True

        duplicate = client.post(
            f"/api/sessions/{session_id}/handoff/request",
            json={"request_id": "apply-1"},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True
        assert duplicate.json()["ticket"]["ticket_id"] == ticket["ticket_id"]

        # The normal chat entry is now a human queue write: no worker/model call.
        calls = []

        def forbidden_reserve(*args, **kwargs):
            calls.append("reserve")
            raise AssertionError("agent entry must not run while a ticket is open")

        monkeypatch.setattr(server_module, "reserve_session", forbidden_reserve)
        sent = client.post(
            f"/api/sessions/{session_id}/messages", json={"prompt": "人工客服在吗？"}
        )
        assert sent.status_code == 200, sent.text
        events = sse_events(sent)
        assert events[0]["type"] == "handoff"
        assert events[-1]["type"] == "done"
        assert calls == []
        transcript = store.transcript(session_id)
        assert transcript[-1]["role"] == "user"
        assert transcript[-1]["author"]["kind"] == "user"
        assert transcript[-1]["ticket_id"] == ticket["ticket_id"]

        listing = client.get("/api/admin/handoff?status=waiting")
        assert listing.status_code == 200
        rows = listing.json()["rows"]
        assert len(rows) == 1
        assert rows[0]["queued_count"] == 1

        detail = client.get(f"/api/admin/handoff/{session_id}")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["handoff"]["summary"].startswith("【转人工摘要")
        assert payload["ticket"]["status"] == "waiting"
        assert payload["queued_messages"][0]["handled"] is False

        accepted = client.post(f"/api/admin/handoff/{session_id}/accept", json={})
        assert accepted.status_code == 200
        assert accepted.json()["ticket"]["status"] == "active"

        reply = client.post(
            f"/api/admin/handoff/{session_id}/reply",
            json={"message": "我是人工客服，请先确认充电座指示灯。", "request_id": "reply-1"},
        )
        assert reply.status_code == 200, reply.text
        assert reply.json()["duplicate"] is False
        human_rows = [row for row in store.transcript(session_id) if row["role"] == "human"]
        assert len(human_rows) == 1
        assert human_rows[0]["author"] == {"kind": "human", "display_name": "本机人工客服"}
        assert human_rows[0]["request_id"] == "reply-1"

        replay = client.post(
            f"/api/admin/handoff/{session_id}/reply",
            json={"message": "我是人工客服，请先确认充电座指示灯。", "request_id": "reply-1"},
        )
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True
        assert len([row for row in store.transcript(session_id) if row["role"] == "human"]) == 1

        polled = client.get(f"/api/sessions/{session_id}/handoff/messages?after=0")
        assert polled.status_code == 200
        poll_payload = polled.json()
        assert [row["message_id"] for row in poll_payload["messages"]] == [
            human_rows[0]["message_id"]
        ]
        assert poll_payload["messages"][0]["author"]["kind"] == "human"
        assert poll_payload["cursor"] == len(store.transcript(session_id))

        # Model-safe history never exposes the human role; it is clearly labelled.
        history = store.history(session_id)
        assert all(row["role"] in {"user", "assistant"} for row in history)
        assert any(row["content"].startswith("[人工客服] 我是人工客服") for row in history)

        closed = client.post(
            f"/api/admin/handoff/{session_id}/close",
            json={"note": "已电话确认恢复", "request_id": "close-1"},
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["ticket"]["status"] == "closed"
        assert closed.json()["ticket"]["close_note"] == "已电话确认恢复"
        closed_again = client.post(
            f"/api/admin/handoff/{session_id}/close",
            json={"note": "已电话确认恢复", "request_id": "close-1"},
        )
        assert closed_again.status_code == 200
        assert closed_again.json()["duplicate"] is True

        # Closed ticket: the normal bot entry must run again (fake worker, no LLM).
        ran = []

        class FakeReservation:
            def __init__(self):
                self.busy = threading.Lock()
                self.session_id = session_id

        def fake_reserve(manager, store_, sid, resume=False):
            reservation = FakeReservation()
            reservation.busy.acquire()
            return reservation

        def fake_run(self, sid, body, event_sink=None, reservation=None):
            ran.append(body.get("prompt"))
            reservation.busy.release()
            return {"answer": "机器人已恢复回答", "status": "completed"}

        monkeypatch.setattr(server_module, "reserve_session", fake_reserve)
        monkeypatch.setattr(
            app.state.manager, "run", types.MethodType(fake_run, app.state.manager)
        )
        monkeypatch.setenv("RVS_TURN_INTERPRETER", "off")
        resumed = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"prompt": "关闭人工后机器人还能回答吗"},
        )
        assert resumed.status_code == 200, resumed.text
        assert ran == ["关闭人工后机器人还能回答吗"]
        resumed_events = sse_events(resumed)
        assert any(event.get("type") == "answer" for event in resumed_events)

        # Re-apply after close creates a fresh ticket and keeps the closed one in history.
        reapplied = client.post(
            f"/api/sessions/{session_id}/handoff/request",
            json={"request_id": "apply-2"},
        )
        assert reapplied.status_code == 200
        new_ticket = reapplied.json()["ticket"]
        assert new_ticket["status"] == "waiting"
        assert new_ticket["ticket_id"] != ticket["ticket_id"]
        assert ticket["ticket_id"] in new_ticket["previous_ticket_ids"]


def test_agent_busy_returns_409_and_old_sessions_stay_compatible(tmp_path, monkeypatch):
    app, client = make_client(tmp_path)
    with client:
        store = app.state.store
        session_id = store.create(workflow="agent")["session_id"]
        store.append_message(session_id, "user", "旧会话问题", turn=1)
        store.append_message(session_id, "assistant", "旧会话回答", turn=1)

        # Legacy session without handoff.json: no ticket, still viewable.
        assert client.get(f"/api/sessions/{session_id}/handoff").json()["status"] == "none"
        assert client.get(f"/api/admin/handoff/{session_id}").status_code == 404
        assert client.get(f"/api/sessions/{session_id}/handoff-summary").status_code == 200

        class BusySession:
            closing = False
            running = True
            pending_thread_id = None
            busy = threading.Lock()

        monkeypatch.setattr(app.state.manager, "peek", lambda sid: BusySession())
        busy = client.post(f"/api/sessions/{session_id}/handoff/request", json={})
        assert busy.status_code == 409
        assert busy.json()["detail"]["code"] == "session_busy"

        monkeypatch.undo()
        ok = client.post(f"/api/sessions/{session_id}/handoff/request", json={})
        assert ok.status_code == 200
        assert ok.json()["ticket"]["status"] == "waiting"

        # An old human row (if present) is converted for model input, not passed raw.
        store.append_message(
            session_id,
            "human",
            "旧人工回复",
            author={"kind": "human", "display_name": "人工客服"},
        )
        history = store.history(session_id)
        assert all(row["role"] in {"user", "assistant"} for row in history)
        assert {"role": "user", "content": "[人工客服] 旧人工回复"} in history


def test_admin_is_loopback_only_and_rejects_cross_origin_writes(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        session_id = app.state.store.create(workflow="agent")["session_id"]
        ok = client.post(f"/api/admin/handoff/{session_id}/reply", json={})
        assert ok.status_code in {404, 409, 422}

    remote_app = create_app(FakeConfig(tmp_path / "remote", host="0.0.0.0"))
    with TestClient(remote_app) as remote:
        for headers in (
            {},
            {"X-Forwarded-Host": "127.0.0.1:8948", "Forwarded": "for=127.0.0.1"},
        ):
            blocked = remote.get("/api/admin/handoff", headers=headers)
            assert blocked.status_code == 403
            assert blocked.json()["detail"]["code"] == "admin_local_only"
        page = remote.get("/admin.html")
        assert page.status_code == 403
        assert remote.get("/ADMIN.HTML").status_code != 200

    local_app, local = make_client(tmp_path / "local")
    with local:
        sid = local_app.state.store.create(workflow="agent")["session_id"]
        local.post(f"/api/sessions/{sid}/handoff/request", json={})
        local.post(f"/api/admin/handoff/{sid}/accept", json={})
        evil = local.post(
            f"/api/admin/handoff/{sid}/reply",
            json={"message": "跨站写入", "request_id": "x"},
            headers={"Origin": "http://evil.example"},
        )
        assert evil.status_code == 403
        assert evil.json()["detail"]["code"] == "origin_rejected"
        same = local.post(
            f"/api/admin/handoff/{sid}/reply",
            json={"message": "同源写入", "request_id": "same-origin-1"},
            headers={"Origin": "http://testserver"},
        )
        assert same.status_code == 200, same.text

        user_cross = local.post(
            f"/api/sessions/{sid}/handoff/request",
            json={},
            headers={"Origin": "http://evil.example"},
        )
        assert user_cross.status_code == 403

        page = local.get("/admin.html")
        assert page.status_code == 200
        assert "人工坐席后台" in page.text


def test_concurrent_reply_idempotency_and_close_are_serialised(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        store = app.state.store
        handoff = app.state.handoff
        session_id = store.create(workflow="agent")["session_id"]
        handoff.request(session_id)
        handoff.accept(session_id)

        barrier = threading.Barrier(2)

        def same_reply():
            barrier.wait()
            try:
                result = handoff.reply(
                    session_id, "并发人工回复", request_id="same-concurrent"
                )
                return result["duplicate"]
            except HandoffConflict as exc:
                return "conflict:" + str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: same_reply(), range(2)))
        assert sorted(str(item) for item in results) == ["False", "True"]
        human_rows = [row for row in store.transcript(session_id) if row["role"] == "human"]
        assert len(human_rows) == 1

        second_id = store.create(workflow="agent")["session_id"]
        handoff.request(second_id)
        handoff.accept(second_id)
        barrier = threading.Barrier(2)

        def do_reply():
            barrier.wait()
            try:
                handoff.reply(second_id, "与结案并发", request_id="race-reply")
                return "replied"
            except HandoffConflict:
                return "conflict"

        def do_close():
            barrier.wait()
            return handoff.close(second_id, note="并发结案", request_id="race-close")

        with ThreadPoolExecutor(max_workers=2) as pool:
            reply_future = pool.submit(do_reply)
            close_future = pool.submit(do_close)
            reply_result = reply_future.result()
            close_result = close_future.result()
        assert close_result["ticket"]["status"] == "closed"
        assert reply_result in {"replied", "conflict"}
        rows = [row for row in store.transcript(second_id) if row["role"] == "human"]
        assert len(rows) == (1 if reply_result == "replied" else 0)


def test_session_lifecycle_and_ticket_lifecycle_stay_independent(tmp_path):
    app, client = make_client(tmp_path)
    with client:
        store = app.state.store
        session_id = store.create(workflow="agent")["session_id"]
        requested = client.post(f"/api/sessions/{session_id}/handoff/request", json={})
        ticket_id = requested.json()["ticket"]["ticket_id"]

        store.mark(session_id, "ended")
        # Ending the chat session does not silently close/delete the ticket.
        status = client.get(f"/api/sessions/{session_id}/handoff").json()
        assert status["ticket"]["ticket_id"] == ticket_id
        assert status["ticket"]["status"] == "waiting"
        detail = client.get(f"/api/admin/handoff/{session_id}").json()
        assert detail["session"]["status"] == "ended"
        assert detail["ticket"]["status"] == "waiting"

        # A user cannot open a new human request on an ended chat session.
        ended = client.post(f"/api/sessions/{session_id}/handoff/request", json={})
        assert ended.status_code == 409
        assert ended.json()["detail"]["code"] == "session_ended"

        # The local agent can still take over and reply; the ticket is its own state.
        accepted = client.post(f"/api/admin/handoff/{session_id}/accept", json={})
        assert accepted.status_code == 200
        assert accepted.json()["ticket"]["status"] == "active"
        reply = client.post(
            f"/api/admin/handoff/{session_id}/reply",
            json={"message": "会话结束后工单仍可处理", "request_id": "ended-reply"},
        )
        assert reply.status_code == 200
        assert client.get(f"/api/sessions/{session_id}/handoff").json()["ticket"]["status"] == "active"
