# -*- coding: utf-8 -*-
"""Provider transport hook contract tests.

These tests use a local ``httpx2.MockTransport`` and synthetic httpcore trace
events.  They never open a provider connection, never read credentials and do
not depend on the user's real proxy reachability.  The proxy-preservation test
builds a real ``httpx2`` client with a fake proxy map and checks that the
transport remains the SDK's normal proxy transport.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

httpx2 = pytest.importorskip("httpx2")

from core.observability import latency, provider_transport as pt  # noqa: E402


@pytest.fixture()
def tracing(monkeypatch, tmp_path):
    monkeypatch.setenv("RVS_PROVIDER_TRACE", "1")
    monkeypatch.setenv("RVS_LATENCY_TRACE", "1")
    monkeypatch.setenv("RVS_LATENCY_DIR", str(tmp_path / "spans"))
    monkeypatch.setenv("RVS_PROVIDER_TRANSPORT_DIR", str(tmp_path / "records"))
    monkeypatch.setattr(latency, "_ENABLED", True)
    monkeypatch.setattr(latency, "_PROCESS_ROLE", "test")
    monkeypatch.setattr(latency, "_WRITER", None)
    pt.clear_recent_records()
    yield tmp_path
    pt.clear_recent_records()


def _events(request, *, connect: bool = True, retry: bool = False, sleep: float = 0.012):
    trace = request.extensions.get("trace")
    if trace is None:
        return
    if connect:
        trace("connection.connect_tcp.started", {"host": "api.provider.test", "port": 443})
        time.sleep(sleep)
        trace("connection.connect_tcp.complete", {"host": "api.provider.test", "port": 443})
    trace("http11.send_request_headers.started", {"request": request})
    time.sleep(0.003)
    trace("http11.send_request_headers.complete", {"request": request})
    if retry:
        trace("connection.retry.started", {"request": request})
        time.sleep(sleep)
        trace("connection.retry.complete", {"request": request})
    trace("http11.receive_response_headers.started", {"request": request})
    time.sleep(sleep)
    trace(
        "http11.receive_response_headers.complete",
        {
            "request": request,
            "return_value": (b"HTTP/1.1", 200, b"OK", [(b"Authorization", b"SECRET")]),
        },
    )
    trace("http11.receive_response_body.started", {"request": request})
    time.sleep(0.004)
    trace("http11.receive_response_body.complete", {"request": request})


def _mock_client(handler, *, trust_env: bool = False):
    return pt.build_http_client(
        timeout=5.0,
        base_url="https://api.provider.test",
        transport=httpx2.MockTransport(handler),
        trust_env=trust_env,
    )


def test_proxy_config_is_not_replaced_by_a_custom_transport(monkeypatch):
    """Build the real client; proxy mounts must still belong to httpx2."""
    monkeypatch.setenv("RVS_PROVIDER_TRACE", "1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9")
    fake = {
        "http": "http://127.0.0.1:9",
        "https": "http://127.0.0.1:9",
        "no": "",
        "node_use_env": "1",
    }
    monkeypatch.setattr(httpx2._utils, "getproxies", lambda: dict(fake))

    client = pt.build_http_client(timeout=5.0, base_url="https://api.provider.test")
    try:
        assert client is not None
        assert client.trust_env is True
        assert getattr(client, "_rvs_provider_transport_instrumented", False) is True
        # The transport object is still the SDK's normal HTTPTransport whose
        # pool is the proxy pool; no wrapper/custom transport is substituted.
        transport = client._transport_for_url(httpx2.URL("https://api.provider.test"))
        assert isinstance(transport, httpx2.HTTPTransport)
        assert type(transport._pool).__name__ == "HTTPProxy"
        assert len(client._mounts) >= 2
    finally:
        client.close()


def test_trace_injection_chains_existing_extension(tracing):
    seen = []

    def existing(name, info):
        seen.append(name)

    def handler(request):
        _events(request)
        return httpx2.Response(200, stream=httpx2.ByteStream(b'{"ok":true}'))

    client = _mock_client(handler)
    try:
        request = client.build_request(
            "POST",
            "https://api.provider.test/v1/chat",
            json={"messages": ["x"]},
            extensions={"trace": existing},
        )
        response = client.send(request)
        assert response.read() == b'{"ok":true}'
        assert seen, "the pre-existing trace extension must still be called"
        record = pt.recent_records()[-1]
        assert record["trace_events_available"] is True
    finally:
        client.close()


def test_event_durations_union_and_connection_reuse(tracing):
    def handler(request):
        _events(request, connect=True)
        return httpx2.Response(200, stream=httpx2.ByteStream(b"{}"))

    client = _mock_client(handler)
    try:
        first = client.send(
            client.build_request("POST", "https://api.provider.test/v1/chat")
        )
        first.read()
        record = pt.recent_records()[-1]
        assert record["trace_events_available"] is True
        assert record["events_seen"]["connect_tcp"] == 1
        assert record["events_seen"]["send_request_headers"] == 1
        assert record["events_seen"]["receive_response_headers"] == 1
        assert record["events_seen"]["receive_response_body"] == 1
        assert record["connection_setup_union_ms"] is not None
        assert record["connection_setup_union_ms"] >= 8
        # connect + TLS/proxy events are unioned, not multiplied: the union
        # cannot exceed the sum of the observed setup durations.
        setup_sum = sum(record["phases"]["connect_tcp_ms"]) + sum(
            record["phases"]["start_tls_ms"]
        )
        assert record["connection_setup_union_ms"] <= setup_sum + 2.0
        assert record["read_headers_wait_ms"] >= 8
        assert record["time_to_headers_ms"] >= record["read_headers_wait_ms"]
        assert record["connection_reuse"] == "new_connection"
        assert record["connection_reuse_known"] is True
        assert record["connect_tcp_destination"] == "provider"
        assert record["first_body_chunk_ms"] is not None
        assert record["first_body_chunk_bytes"] == 2
        assert record["body_complete"] is True

        # A second response without connect_tcp means the connection was reused
        # from the pool.  The record says so and does not invent a connect time.
        def reused_handler(request):
            _events(request, connect=False)
            return httpx2.Response(200, stream=httpx2.ByteStream(b"{}"))

        client._transport = httpx2.MockTransport(reused_handler)
        second = client.send(
            client.build_request("POST", "https://api.provider.test/v1/chat")
        )
        second.read()
        reused = pt.recent_records()[-1]
        assert reused["connection_reuse"] == "reused"
        assert reused["connection_reuse_known"] is True
        assert reused["phases"]["connect_tcp_ms"] == []
    finally:
        client.close()


def test_completion_payload_without_start_metadata_matches_the_start(tracing):
    """httpcore completion callbacks carry return_value, not the start kwargs."""

    def handler(request):
        trace = request.extensions["trace"]
        trace("connection.connect_tcp.started", {"host": "api.provider.test", "port": 443})
        time.sleep(0.01)
        trace("connection.connect_tcp.complete", {"return_value": object()})
        trace("http11.send_request_headers.started", {"request": request})
        time.sleep(0.004)
        trace("http11.send_request_headers.complete", {"return_value": None})
        trace("http11.receive_response_headers.started", {"request": request})
        time.sleep(0.01)
        trace("http11.receive_response_headers.complete", {"return_value": None})
        trace("http11.receive_response_body.started", {"request": request})
        time.sleep(0.004)
        trace("http11.receive_response_body.complete", {"return_value": None})
        return httpx2.Response(200, stream=httpx2.ByteStream(b"{}"))

    client = _mock_client(handler)
    try:
        response = client.send(
            client.build_request("POST", "https://api.provider.test/v1/chat")
        )
        response.read()
        record = pt.recent_records()[-1]
        events = {row["event"]: row for row in record["events"]}
        for event in (
            "connect_tcp",
            "send_request_headers",
            "receive_response_headers",
            "receive_response_body",
        ):
            row = events[event]
            assert row["duration_ms"] is not None
            assert row["duration_unavailable"] is None
            assert row["role"] in {"provider", "proxy_or_other"}
        assert record["connection_setup_union_ms"] >= 8
        assert record["read_headers_wait_ms"] >= 8
    finally:
        client.close()


def test_retry_backoff_and_missing_trace_fallback_do_not_break_business(tracing):
    def handler(request):
        _events(request, connect=False, retry=True)
        return httpx2.Response(200, stream=httpx2.ByteStream(b"{}"))

    client = _mock_client(handler)
    try:
        response = client.send(
            client.build_request("POST", "https://api.provider.test/v1/chat")
        )
        assert response.read() == b"{}"
        record = pt.recent_records()[-1]
        assert record["retry_count"] == 1
        assert record["retry_backoff_ms"] >= 8

        # A request built outside the instrumented path has no recorder.  The
        # client must still send it; diagnostics are a no-op, not a failure.
        before = len(pt.recent_records())
        manual = httpx2.Request("POST", "https://api.provider.test/v1/chat")
        manual_response = client.send(manual)
        assert manual_response.read() == b"{}"
        assert len(pt.recent_records()) == before

        # A trace callback that raises is a diagnostic failure only; business
        # must continue.  Our wrapper ignores our own exceptions.
        request = client.build_request("POST", "https://api.provider.test/v1/chat")
        recorder = request.extensions["rvs_provider_recorder"]
        original_on_event = recorder.on_event

        def broken(name, info):
            raise RuntimeError("diagnostic callback broke")

        recorder.on_event = broken
        response = client.send(request)
        assert response.read() == b"{}"
        recorder.on_event = original_on_event
    finally:
        client.close()


def test_network_exception_is_recorded_and_propagated(tracing):
    def handler(request):
        raise httpx2.ConnectError("mock connect failure")

    client = _mock_client(handler)
    try:
        request = client.build_request("POST", "https://api.provider.test/v1/chat")
        with pytest.raises(httpx2.ConnectError):
            client.send(request)
        record = pt.recent_records()[-1]
        assert record["status"] == "error"
        assert record["error_type"] == "ConnectError"
        assert record["trace_events_available"] is False
        assert record["connection_reuse_known"] is False
        assert record["connection_reuse"] == "unknown"
    finally:
        client.close()


def test_secret_redaction_and_ttft_unavailable(tracing):
    secret = "Bearer TOP-SECRET-VALUE"

    def handler(request):
        trace = request.extensions["trace"]
        # Deliberately pass headers, a query string and a body in the fake
        # trace info.  The recorder must not copy any of them.
        trace(
            "connection.connect_tcp.started",
            {
                "host": "api.provider.test",
                "port": 443,
                "headers": {"Authorization": secret},
                "url": "https://api.provider.test/v1/chat?token=" + secret,
                "body": {"secret": secret},
            },
        )
        trace("connection.connect_tcp.complete", {"host": "api.provider.test", "port": 443})
        trace("http11.send_request_headers.started", {"request": request})
        trace("http11.send_request_headers.complete", {"request": request})
        trace("http11.receive_response_headers.started", {"request": request})
        trace("http11.receive_response_headers.complete", {"request": request})
        trace("http11.receive_response_body.started", {"request": request})
        trace("http11.receive_response_body.complete", {"request": request})
        return httpx2.Response(
            200,
            headers={"Authorization": secret, "X-Test": secret},
            stream=httpx2.ByteStream(b"{}"),
        )

    client = _mock_client(handler)
    try:
        response = client.send(
            client.build_request(
                "POST",
                "https://api.provider.test/v1/chat?token=" + secret,
                headers={"Authorization": secret},
                json={"secret": secret},
            )
        )
        response.read()
        record = pt.recent_records()[-1]
        serialized = json.dumps(record, ensure_ascii=False)
        assert "TOP-SECRET-VALUE" not in serialized
        assert record["redaction"]["header_values_stored"] is False
        assert record["redaction"]["url_query_or_path_stored"] is False
        assert record["redaction"]["body_stored"] is False
        assert record["ttft_available"] is False
        assert "first-token" in record["ttft_reason"]
        assert record["first_body_chunk_ms"] is not None
        # A first body chunk is still not first-token/TTFT.
        assert record["ttft_available"] is False
    finally:
        client.close()


def test_disabled_observation_returns_none(monkeypatch):
    monkeypatch.delenv("RVS_PROVIDER_TRACE", raising=False)
    assert pt.build_http_client(timeout=5.0) is None
    assert pt.status()["status"] == "disabled"
    assert pt.status()["transport_replaced"] is False
