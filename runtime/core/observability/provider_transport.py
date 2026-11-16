# -*- coding: utf-8 -*-
"""Provider transport observation that keeps the SDK's real proxy path.

What this module does
---------------------
The product calls an OpenAI-compatible provider through LangChain's
``ChatOpenAI``.  The installed ``openai`` (3.37.0) uses an ``httpx2.Client`` and
LangChain's default sync client is ``openai.DefaultHttpxClient`` (an
``httpx2.Client``).  This module *does not* replace that transport.  The
product builds its normal client first; :func:`instrument_client` then wraps
that exact instance's ``build_request``/``send`` and adds one response hook so
every request carries ``request.extensions["trace"]``.  ``httpx2``/``httpcore2``
already forward that extension to the connection pool, proxy tunnel and HTTP/1
or HTTP/2 connection, so real proxy mounts, ``trust_env``, TLS verification and
connection pooling are untouched.  ``build_http_client`` is a probe/test
convenience that constructs the same default-shaped client and instruments it.

What it records
---------------
- event existence and duration for ``connection.connect_tcp``,
  ``connection.start_tls``, ``connection.retry``,
  ``http11.send_request_headers``, ``http11.receive_response_headers``,
  ``http11.receive_response_body`` (and the HTTP/2 equivalents);
- whether a TCP connection was opened during the request (connection reuse),
  and whether proxy control/tunnel events were observed;
- retry/backoff events and the time between provider attempts;
- the first response-body chunk and whether a provider token stream exists.

What it deliberately does not record
------------------------------------
No query string, URL path, request/response body, header value, cookie, API key
or proxy credential is stored.  Only the provider host, port, HTTP method,
status code and durations are kept.  A ``connect_tcp`` event whose destination
is not the provider host is labelled as a proxy/other connection; its address is
not recorded and its duration is not presented as provider-direct connect time.

What cannot be claimed
----------------------
``receive_response_headers`` is one inclusive wait: provider queueing, prefill,
generation and public-network RTT are not separable from it.  The product path
uses non-streaming ``invoke``, so the first body chunk is not first-token time;
``ttft_available`` is always false and ``ttft_reason`` says why.

Scope
-----
Sync only.  ``ChatOpenAI.invoke`` is the product path.  ``http_async_client`` /
``astream`` is not wrapped by this module and is reported as unavailable rather
than silently assumed covered.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Iterator

from core.observability import latency

SCHEMA_VERSION = "provider-transport/v1"
RECORD_SCHEMA_VERSION = "provider-transport-record/v1"
TRUTHY = {"1", "true", "yes", "on"}
RECORD_DIR_ENV = "RVS_PROVIDER_TRANSPORT_DIR"
TRACE_ENV = "RVS_PROVIDER_TRACE"

#: Expected httpcore/httpcore2 event names (prefix.event).  Missing entries are
#: reported as not observed, never as zero-duration events.
EXPECTED_EVENTS = (
    "connection.connect_tcp",
    "connection.start_tls",
    "connection.retry",
    "http11.send_request_headers",
    "http11.receive_response_headers",
    "http11.receive_response_body",
    "http2.send_request_headers",
    "http2.receive_response_headers",
    "http2.receive_response_body",
)

_NEVER_RECORDED = (
    "url_query",
    "url_path",
    "request_body",
    "response_body",
    "header_values",
    "cookies",
    "authorization",
    "api_key",
    "proxy_credentials",
)

_RECENT: deque[dict[str, Any]] = deque(maxlen=64)
_RECORD_LOCK = threading.Lock()
_LAST_BUILD_INFO: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Capability / configuration helpers (offline; never opens a socket)
# ---------------------------------------------------------------------------
def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def enabled() -> bool:
    """Opt-in only.  Observation does not probe the network by itself."""
    return _truthy(os.getenv(TRACE_ENV))


def proxy_env_present() -> bool:
    """Whether httpx would see a proxy from env or OS settings.

    Only a boolean is exposed; proxy URLs (which may contain credentials) are
    never returned or logged.
    """
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        if os.environ.get(name, "").strip():
            return True
    try:
        return bool(urllib.request.getproxies())
    except Exception:
        return False


def _module_version(name: str) -> str | None:
    try:
        module = __import__(name)
        return str(getattr(module, "__version__", "") or "") or None
    except Exception:
        return None


def sdk_versions() -> dict[str, str | None]:
    return {
        "httpx": _module_version("httpx"),
        "httpcore": _module_version("httpcore"),
        "httpx2": _module_version("httpx2"),
        "httpcore2": _module_version("httpcore2"),
        "openai": _module_version("openai"),
        "langchain_openai": _module_version("langchain_openai"),
    }


def scope() -> dict[str, Any]:
    return {
        "sync_supported": True,
        "async_supported": False,
        "async_reason": (
            "only the sync httpx client is wrapped; ChatOpenAI.invoke is the "
            "product path.  AsyncClient/astream keeps its default client and is "
            "reported as not covered, not as measured."
        ),
        "streaming_ttft_supported": False,
    }


def status() -> dict[str, Any]:
    """Describe what can be observed without sending any request."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "available" if enabled() else "disabled",
        "reason": (
            "RVS_PROVIDER_TRACE is on; sync SDK client hook is installed when "
            "a model client is built"
            if enabled()
            else "RVS_PROVIDER_TRACE is off; the product keeps its default client"
        ),
        "injection": "request.extensions['trace'] via httpx client build_request hook",
        "transport_replaced": False,
        "trust_env_preserved": True,
        "mounts_preserved": True,
        "proxy_env_present": proxy_env_present(),
        "scope": scope(),
        "sdk_versions": sdk_versions(),
        "expected_events": list(EXPECTED_EVENTS),
        "recording": {
            "dir_env": RECORD_DIR_ENV,
            "dir_set": bool(os.getenv(RECORD_DIR_ENV, "").strip()),
            "note": "records are kept in a bounded in-process deque even when no dir is set",
        },
        "ttft": {
            "available": False,
            "reason": (
                "product path uses non-streaming invoke; a first body chunk is "
                "not a first-token timestamp"
            ),
        },
        "read_headers": {
            "attributable_to_remote_inference_or_network": False,
            "reason": (
                "receive_response_headers is one inclusive wait covering send, "
                "proxy/public RTT, provider queueing, prefill and generation"
            ),
        },
        "redaction": {
            "provider_host_visible": True,
            "never_recorded": list(_NEVER_RECORDED),
        },
        "last_build": dict(_LAST_BUILD_INFO),
    }


@contextmanager
def _parseable_no_proxy_env() -> Iterator[dict[str, Any]]:
    """Temporarily drop bracketed IPv6 NO_PROXY entries for client construction.

    httpx 0.28.1 and httpx2 2.13.1 build a URLPattern for every NO_PROXY token.
    A token such as ``[::1]`` becomes ``all://[[::1]]`` and raises
    ``InvalidURL: Invalid port: ':1]'`` before any request is sent.  The host
    exports such a token.  The product already applies the same workaround in
    its worker/interpreter.  This context manager only hides the bracketed form
    while the client builds its mounts and restores the process environment
    afterwards; it does not change the proxy URL, TLS verification or any OS
    setting.  The unbracketed ``::1`` entry is kept, so loopback bypass is not
    removed for ordinary IPv6 loopback.
    """
    saved: dict[str, str] = {}
    removed = 0
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if raw is None:
            continue
        parts = [part.strip() for part in raw.split(",")]
        kept = [
            part
            for part in parts
            if part and not (part.startswith("[") and part.endswith("]"))
        ]
        if len(kept) != len([part for part in parts if part]):
            removed += len([part for part in parts if part]) - len(kept)
            saved[name] = raw
            os.environ[name] = ",".join(kept)
    try:
        yield {
            "bracketed_ipv6_entries_ignored": removed,
            "process_env_restored": True,
        }
    finally:
        for name, raw in saved.items():
            os.environ[name] = raw


#: Public alias for callers that need to build a baseline client under the same
#: parseable NO_PROXY view (probe/preflight only).  The environment is restored
#: on exit.
parseable_no_proxy_env = _parseable_no_proxy_env


def _client_classes() -> tuple[Any, Any, str]:
    """Return ``(default_client_class, sync_byte_stream_class, backend)``.

    Preference order matches the installed SDK: OpenAI's default client (which
    may be httpx2 or legacy httpx), then httpx2.Client, then httpx.Client.
    """
    try:
        import openai

        base = openai.DefaultHttpxClient
        mro_modules = {
            str(getattr(item, "__module__", "")) for item in getattr(base, "__mro__", ())
        }
        if any("httpx2" in module for module in mro_modules):
            import httpx2

            return base, httpx2.SyncByteStream, "httpx2"
        if any(module == "httpx._client" or module.startswith("httpx.") for module in mro_modules):
            import httpx

            return base, httpx.SyncByteStream, "httpx"
    except Exception:
        pass
    try:
        import httpx2

        return httpx2.Client, httpx2.SyncByteStream, "httpx2"
    except Exception:
        pass
    import httpx

    return httpx.Client, httpx.SyncByteStream, "httpx"


# ---------------------------------------------------------------------------
# Trace extension injection
# ---------------------------------------------------------------------------
def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("ascii", errors="replace")
        except Exception:
            return None
    return str(value)


def _norm_host(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    text = text.strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return text


def _safe_request_info(info: dict[str, Any]) -> dict[str, Any]:
    """Extract only routing metadata, never headers/body/URL query."""
    out: dict[str, Any] = {}
    request = info.get("request")
    if request is not None:
        method = getattr(request, "method", None)
        if method is not None:
            out["method"] = _text(method)
        url = getattr(request, "url", None)
        if url is not None:
            out["url_host"] = _norm_host(getattr(url, "host", None))
            port = getattr(url, "port", None)
            if port is not None:
                out["url_port"] = int(port)
    host = info.get("host")
    if host is not None:
        out["host"] = _norm_host(host)
    port = info.get("port")
    if port is not None:
        try:
            out["port"] = int(port)
        except (TypeError, ValueError):
            pass
    server_hostname = info.get("server_hostname")
    if server_hostname is not None:
        out["server_hostname"] = _norm_host(server_hostname)
    exception = info.get("exception")
    if exception is not None:
        out["error_type"] = type(exception).__name__
    return out


def _split_trace_name(name: str) -> tuple[str, str, str]:
    text = str(name or "")
    status = "unknown"
    if "." in text:
        prefix, _, status = text.rpartition(".")
        if status not in {"started", "complete", "failed"}:
            prefix, status = text, "unknown"
    else:
        prefix, status = text, "unknown"
    if "." in prefix:
        logger_part, _, event = prefix.partition(".")
        return logger_part, event, status
    return "", prefix, status


def _classify(event: str, info: dict[str, Any], provider_host: str | None) -> str:
    safe = _safe_request_info(info)
    method = (safe.get("method") or "").upper()
    url_host = safe.get("url_host")
    event_host = safe.get("host")
    server_hostname = safe.get("server_hostname")

    if event == "connect_tcp":
        if provider_host and event_host == provider_host:
            return "provider"
        if event_host:
            return "proxy_or_other"
        return "unknown"
    if event == "start_tls":
        if provider_host and server_hostname == provider_host:
            return "origin_tls"
        if server_hostname:
            return "proxy_tls"
        return "unknown"
    if event in {"send_request_headers", "receive_response_headers", "receive_response_body"}:
        if method == "CONNECT":
            return "proxy_tunnel"
        if provider_host and url_host == provider_host:
            return "provider"
        if url_host:
            # A forward HTTP proxy rewrites the request URL host to the proxy.
            return "provider_via_proxy"
        return "unknown"
    if event == "retry":
        return "connection"
    return "unknown"


def _is_provider_exchange(role: str) -> bool:
    """Provider request events, including a forward HTTP proxy rewrite."""
    return role in {"provider", "provider_via_proxy"}


def _span_stage(event: str, role: str) -> str:
    if event == "connect_tcp":
        return "provider.connect_tcp" if role == "provider" else "provider.proxy.connect_tcp"
    if event == "start_tls":
        return "provider.start_tls" if role == "origin_tls" else "provider.proxy.start_tls"
    if event == "send_request_headers":
        return "provider.send_headers" if _is_provider_exchange(role) else "provider.proxy.send_headers"
    if event == "receive_response_headers":
        return "provider.read_headers" if _is_provider_exchange(role) else "provider.proxy.read_headers"
    if event == "receive_response_body":
        return "provider.body"
    if event == "retry":
        return "provider.retry_backoff"
    return "provider.transport_event"


def _interval_union_ms(intervals: Iterable[tuple[int, int]]) -> float | None:
    items = sorted((start, end) for start, end in intervals if end >= start)
    if not items:
        return None
    merged: list[list[int]] = [[items[0][0], items[0][1]]]
    for start, end in items[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return round(sum(end - start for start, end in merged) / 1_000_000.0, 3)


# ---------------------------------------------------------------------------
# Per-request recorder
# ---------------------------------------------------------------------------
class ProviderRequestRecorder:
    """Collects safe transport events for one HTTP request.

    One instance is attached to one ``httpx`` request.  It is intentionally
    defensive: every callback catches its own exceptions so a diagnostic bug
    cannot turn a provider response into a business failure.
    """

    def __init__(
        self,
        request: Any,
        *,
        backend: str = "httpx2",
        build_info: dict[str, Any] | None = None,
        stream_base: Any = None,
    ) -> None:
        self.request_id = latency.new_id("ptx")
        self.backend = backend
        self.build_info = dict(build_info or {})
        self.stream_base = stream_base
        self.provider_host = _norm_host(getattr(getattr(request, "url", None), "host", None))
        try:
            self.provider_port = int(getattr(request.url, "port", 0) or 0) or None
        except Exception:
            self.provider_port = None
        self.method = _text(getattr(request, "method", None)) or "UNKNOWN"
        self.call_id = latency.current_call_id()
        self.call_span_id = latency.current_call_span() or latency.active_span_id()
        self.created_mono_ns = time.perf_counter_ns()
        self.created_wall_ns = time.time_ns()
        self.send_start_mono_ns: int | None = None
        self.send_end_mono_ns: int | None = None
        self.end_wall_ns: int | None = None
        self._request_span = None
        self._events: list[dict[str, Any]] = []
        # Started/complete callbacks do not share the same ``info`` payload
        # (httpcore puts kwargs on start and return_value on complete), so the
        # matching key deliberately excludes the role.  The role is classified
        # once at ``started`` and reused when the matching completion arrives.
        self._open: dict[tuple[str, str], dict[str, Any]] = {}
        self._first_chunk_ns: int | None = None
        self._first_chunk_bytes: int | None = None
        self._body_complete = False
        self._status_code: int | None = None
        self._error_type: str | None = None
        self._stream_mode = False
        self._finished = False
        self._finish_lock = threading.Lock()
        self._first_body_read_seen = False

    # ---- lifecycle -----------------------------------------------------
    def begin_send(self) -> None:
        try:
            if self.send_start_mono_ns is not None:
                return
            self.send_start_mono_ns = time.perf_counter_ns()
            self._request_span = latency.open_span(
                "provider.request",
                parent_span_id=self.call_span_id,
                host=self.provider_host,
                method=self.method,
                transport=f"{self.backend}_client_hook",
            )
        except Exception:
            self._request_span = None

    def end_send(
        self,
        *,
        response: Any = None,
        error: BaseException | None = None,
        stream: bool = False,
    ) -> None:
        try:
            self.send_end_mono_ns = time.perf_counter_ns()
            self._stream_mode = bool(stream)
            if response is not None:
                self._status_code = getattr(response, "status_code", None)
            if error is not None:
                self._error_type = type(error).__name__
            if error is not None or not stream:
                self.finish(status="error" if error is not None else "ok")
            # For stream=True the body may still arrive; the wrapped stream
            # finishes the record on EOF/close.
        except Exception:
            pass

    def note_response(self, response: Any) -> None:
        try:
            self._status_code = getattr(response, "status_code", None)
        except Exception:
            pass

    def note_first_body_chunk(self, size: int | None = None) -> None:
        try:
            if self._first_chunk_ns is not None:
                return
            self._first_chunk_ns = time.perf_counter_ns()
            self._first_chunk_bytes = int(size) if size is not None else None
            parent = getattr(self._request_span, "span_id", None) or self.call_span_id
            latency.point(
                "provider.first_chunk",
                parent_span_id=parent,
                host=self.provider_host,
                first_chunk_bytes=self._first_chunk_bytes,
                note="first response body chunk, not first token",
            )
        except Exception:
            pass

    def note_body_complete(self, *, completed: bool = True) -> None:
        try:
            if completed:
                self._body_complete = True
            if self._stream_mode:
                self.finish(status="ok" if self._error_type is None else "error")
        except Exception:
            pass

    def note_body_error(self, error: BaseException | None = None) -> None:
        try:
            self._error_type = type(error).__name__ if error is not None else "StreamError"
        except Exception:
            pass

    # ---- trace extension ----------------------------------------------
    def wrap_trace(self, existing: Any = None) -> Callable[[str, dict[str, Any]], None]:
        """Chain the SDK's existing trace extension instead of overwriting it."""

        def _trace(name: str, info: dict[str, Any]) -> None:
            # Preserve any existing extension's exception behaviour first.
            if callable(existing):
                existing(name, info)
            try:
                self.on_event(name, info)
            except Exception:
                # Diagnostics must never break the provider call.
                pass

        return _trace

    def on_event(self, name: str, info: dict[str, Any] | None = None) -> None:
        info = info or {}
        logger_part, event, status = _split_trace_name(name)
        now = time.perf_counter_ns()
        key = (logger_part, event)

        if status == "started":
            previous = self._open.pop(key, None)
            if previous is not None:
                self._close_event(
                    previous,
                    now=now,
                    status="superseded",
                    duration_unavailable="superseded_by_new_start",
                )
            role = _classify(event, info, self.provider_host)
            span = None
            try:
                if self._should_span(event, role):
                    span = latency.open_span(
                        _span_stage(event, role),
                        parent_span_id=getattr(self._request_span, "span_id", None)
                        or self.call_span_id,
                        event=event,
                        role=role,
                        host=self.provider_host if role in {"provider", "origin_tls"} else None,
                    )
            except Exception:
                span = None
            self._open[key] = {
                "event": event,
                "role": role,
                "status": status,
                "start_mono_ns": now,
                "span": span,
                "info": _safe_event_attrs(event, role, info, self.provider_host),
            }
            if event == "receive_response_body" and not self._first_body_read_seen:
                self._first_body_read_seen = True
            return

        item = self._open.pop(key, None)
        if item is None:
            # A complete/failed without a matching start: keep the fact that
            # the event exists, but do not invent a duration.  The role cannot
            # be recovered from completion payloads, so mark it unknown.
            role = _classify(event, info, self.provider_host)
            self._events.append(
                {
                    "event": event,
                    "role": role,
                    "status": status,
                    "start_offset_ms": None,
                    "end_offset_ms": _offset_ms(now, self._t0()),
                    "duration_ms": None,
                    "duration_unavailable": "missing_start",
                    "attrs": _safe_event_attrs(event, role, info, self.provider_host),
                }
            )
            return
        self._close_event(
            item,
            now=now,
            status=status,
            extra=_safe_event_attrs(
                event, str(item.get("role") or "unknown"), info, self.provider_host
            ),
        )

    def _t0(self) -> int:
        return self.send_start_mono_ns or self.created_mono_ns

    def _should_span(self, event: str, role: str) -> bool:
        if event in {
            "connect_tcp",
            "start_tls",
            "send_request_headers",
            "receive_response_headers",
            "retry",
        }:
            return True
        # Only the first body read is interesting; per-chunk spans would be
        # noisy and would duplicate the record.
        return event == "receive_response_body" and not self._first_body_read_seen

    def _close_event(
        self,
        item: dict[str, Any],
        *,
        now: int,
        status: str,
        extra: dict[str, Any] | None = None,
        duration_unavailable: str | None = None,
    ) -> None:
        start = int(item.get("start_mono_ns") or now)
        duration_ms = round(max(0, now - start) / 1_000_000.0, 3)
        attrs = dict(item.get("info") or {})
        attrs.update(extra or {})
        row = {
            "event": item.get("event"),
            "role": item.get("role"),
            "status": status,
            "start_offset_ms": _offset_ms(start, self._t0()),
            "end_offset_ms": _offset_ms(now, self._t0()),
            "duration_ms": duration_ms,
            "duration_unavailable": duration_unavailable,
            "attrs": attrs,
        }
        self._events.append(row)
        span = item.get("span")
        if span is not None:
            try:
                span.end(
                    status="error" if status == "failed" else "ok",
                    error_type=attrs.get("error_type"),
                    event=item.get("event"),
                    role=item.get("role"),
                )
            except Exception:
                pass

    # ---- serialisation -------------------------------------------------
    def finish(self, *, status: str | None = None) -> None:
        with self._finish_lock:
            if self._finished:
                return
            self._finished = True
        try:
            now = time.perf_counter_ns()
            if self.end_wall_ns is None:
                self.end_wall_ns = time.time_ns()
            # Close any start without complete as open_at_request_end; do not
            # invent a duration for it.
            for item in list(self._open.values()):
                self._close_event(
                    item,
                    now=now,
                    status="open_at_request_end",
                    duration_unavailable="open_at_request_end",
                )
            self._open.clear()
            record = self._record(status or ("error" if self._error_type else "ok"), now)
            if self._request_span is not None:
                try:
                    self._request_span.end(
                        status="error" if record.get("status") == "error" else "ok",
                        status_code=record.get("status_code"),
                        time_to_headers_ms=record.get("time_to_headers_ms"),
                        connection_setup_union_ms=record.get("connection_setup_union_ms"),
                        connect_tcp_count=record.get("events_seen", {}).get("connect_tcp", 0),
                        start_tls_count=record.get("events_seen", {}).get("start_tls", 0),
                        retry_count=record.get("retry_count"),
                        connection_reuse=record.get("connection_reuse"),
                        via_proxy_observed=record.get("proxy_observed"),
                        first_body_chunk_ms=record.get("first_body_chunk_ms"),
                        ttft_available=False,
                        error_type=record.get("error_type"),
                    )
                except Exception:
                    pass
            _store_record(record)
        except Exception:
            pass

    def _record(self, status: str, now: int) -> dict[str, Any]:
        t0 = self._t0()
        elapsed_ms = round(max(0, now - t0) / 1_000_000.0, 3)
        events = list(self._events)
        seen: dict[str, int] = {}
        phases: dict[str, list[float]] = {
            "connect_tcp_ms": [],
            "start_tls_ms": [],
            "send_headers_ms": [],
            "read_headers_ms": [],
            "body_read_ms": [],
            "retry_backoff_ms": [],
        }
        setup_intervals: list[tuple[int, int]] = []
        first_headers_end: int | None = None
        first_headers_duration: float | None = None
        send_headers_duration: float | None = None
        first_body_start: int | None = None
        first_body_duration: float | None = None
        connect_roles: set[str] = set()
        proxy_observed = False
        retry_count = 0
        retry_backoff_total = 0.0

        for row in events:
            event = str(row.get("event") or "")
            role = str(row.get("role") or "")
            duration = row.get("duration_ms")
            seen[event] = seen.get(event, 0) + 1
            if "proxy" in role:
                proxy_observed = True
            if event == "connect_tcp":
                connect_roles.add(role)
                if duration is not None:
                    phases["connect_tcp_ms"].append(float(duration))
            elif event == "start_tls":
                if duration is not None:
                    phases["start_tls_ms"].append(float(duration))
            elif event == "send_request_headers":
                if (
                    duration is not None
                    and send_headers_duration is None
                    and _is_provider_exchange(role)
                ):
                    send_headers_duration = float(duration)
                if _is_provider_exchange(role) and duration is not None:
                    phases["send_headers_ms"].append(float(duration))
            elif event == "receive_response_headers":
                if _is_provider_exchange(role) and duration is not None:
                    phases["read_headers_ms"].append(float(duration))
                    if first_headers_duration is None:
                        first_headers_duration = float(duration)
                if _is_provider_exchange(role) and first_headers_end is None:
                    end_offset = row.get("end_offset_ms")
                    if end_offset is not None:
                        first_headers_end = t0 + int(float(end_offset) * 1_000_000)
            elif event == "receive_response_body" and _is_provider_exchange(role):
                if duration is not None:
                    phases["body_read_ms"].append(float(duration))
                if first_body_start is None and row.get("start_offset_ms") is not None:
                    first_body_start = t0 + int(float(row["start_offset_ms"]) * 1_000_000)
                if first_body_duration is None and duration is not None:
                    first_body_duration = float(duration)
            elif event == "retry":
                retry_count += 1
                if duration is not None:
                    retry_backoff_total += float(duration)
                    phases["retry_backoff_ms"].append(float(duration))

            is_setup = event in {"connect_tcp", "start_tls"} or (
                role == "proxy_tunnel"
                and event in {"send_request_headers", "receive_response_headers"}
            )
            if (
                is_setup
                and row.get("start_offset_ms") is not None
                and row.get("end_offset_ms") is not None
                and row.get("duration_ms") is not None
            ):
                start = t0 + int(float(row["start_offset_ms"]) * 1_000_000)
                end = t0 + int(float(row["end_offset_ms"]) * 1_000_000)
                setup_intervals.append((start, end))

        trace_available = bool(events)
        provider_headers_seen = any(
            r.get("event") == "receive_response_headers"
            and _is_provider_exchange(str(r.get("role") or ""))
            for r in events
        )
        provider_send_seen = any(
            r.get("event") == "send_request_headers"
            and _is_provider_exchange(str(r.get("role") or ""))
            for r in events
        )
        if provider_send_seen and not provider_headers_seen:
            provider_reuse_basis = "provider send_headers observed but no response headers"
        else:
            provider_reuse_basis = ""
        connect_seen = seen.get("connect_tcp", 0) > 0
        if not trace_available:
            connection_reuse = "unknown"
            connection_reuse_known = False
            connection_reuse_basis = "no trace events observed"
        elif connect_seen:
            connection_reuse = "new_connection"
            connection_reuse_known = True
            connection_reuse_basis = "connect_tcp event observed during this request"
        elif provider_headers_seen:
            connection_reuse = "reused"
            connection_reuse_known = True
            connection_reuse_basis = "provider headers observed without connect_tcp"
        else:
            connection_reuse = "unknown"
            connection_reuse_known = False
            connection_reuse_basis = (
                provider_reuse_basis or "no provider headers and no connect_tcp"
            )

        first_chunk_offset = (
            round((self._first_chunk_ns - t0) / 1_000_000.0, 3)
            if self._first_chunk_ns is not None
            else None
        )
        record = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "request_id": self.request_id,
            "provider_host": self.provider_host,
            "provider_port": self.provider_port,
            "method": self.method,
            "backend": self.backend,
            "sdk": sdk_versions(),
            "proxy_env_present": proxy_env_present(),
            "build_info": dict(self.build_info),
            "call_id": self.call_id,
            "call_span_id": self.call_span_id,
            "start_wall_ns": self.created_wall_ns,
            "end_wall_ns": self.end_wall_ns,
            "elapsed_ms": elapsed_ms,
            "status": status,
            "status_code": self._status_code,
            "error_type": self._error_type,
            "trace_events_available": trace_available,
            "trace_event_count": len(events),
            "events_seen": seen,
            "missing_events": list(
                dict.fromkeys(
                    event_name
                    for event_name in (
                        name.split(".", 1)[1] for name in EXPECTED_EVENTS
                    )
                    if seen.get(event_name, 0) == 0
                )
            ),
            "events": events,
            "phases": phases,
            "connection_setup_union_ms": _interval_union_ms(setup_intervals),
            "time_to_headers_ms": (
                round((first_headers_end - t0) / 1_000_000.0, 3)
                if first_headers_end is not None
                else None
            ),
            "send_headers_duration_ms": send_headers_duration,
            "read_headers_wait_ms": first_headers_duration,
            "read_headers_attribution": {
                "decomposable_into_remote_inference_and_pure_rtt": False,
                "reason": (
                    "receive_response_headers is one inclusive wait; it covers "
                    "proxy/public RTT, provider queueing, prefill and generation"
                ),
            },
            "first_body_read_started_offset_ms": (
                round((first_body_start - t0) / 1_000_000.0, 3)
                if first_body_start is not None
                else None
            ),
            "first_body_read_ms": first_body_duration,
            "first_body_chunk_ms": first_chunk_offset,
            "first_body_chunk_bytes": self._first_chunk_bytes,
            "body_complete": self._body_complete,
            "connection_reuse": connection_reuse,
            "connection_reuse_known": connection_reuse_known,
            "connection_reuse_basis": connection_reuse_basis,
            "connect_tcp_destination": (
                "provider"
                if "provider" in connect_roles
                else ("non_provider" if connect_roles else "unknown")
            ),
            "connect_tcp_roles": sorted(connect_roles),
            "proxy_observed": proxy_observed,
            "retry_count": retry_count,
            "retry_backoff_ms": round(retry_backoff_total, 3) if retry_count else None,
            "ttft_available": False,
            "ttft_reason": (
                "product path uses non-streaming invoke; first body chunk is not "
                "a first-token timestamp"
            ),
            "stream_mode": self._stream_mode,
            "redaction": {
                "provider_host_visible": True,
                "never_recorded": list(_NEVER_RECORDED),
                "header_values_stored": False,
                "url_query_or_path_stored": False,
                "body_stored": False,
                "credentials_stored": False,
            },
        }
        return record


def _offset_ms(mono_ns: int, t0: int) -> float:
    return round(max(0, mono_ns - t0) / 1_000_000.0, 3)


def _safe_event_attrs(
    event: str,
    role: str,
    info: dict[str, Any],
    provider_host: str | None,
) -> dict[str, Any]:
    safe = _safe_request_info(info)
    attrs: dict[str, Any] = {}
    if role in {"provider", "origin_tls"} and provider_host:
        attrs["host"] = provider_host
    if "error_type" in safe:
        attrs["error_type"] = safe["error_type"]
    if event in {"send_request_headers", "receive_response_headers", "send_request_body"}:
        method = safe.get("method")
        if method:
            attrs["method"] = method
    if event == "retry":
        # Backoff delay is in kwargs; it is not secret.  Keep it as a number.
        delay = info.get("delay")
        if isinstance(delay, (int, float)):
            attrs["delay_s"] = round(float(delay), 3)
    return attrs


# ---------------------------------------------------------------------------
# Response stream wrapper (first body chunk + close finalisation)
# ---------------------------------------------------------------------------
def _make_recording_stream_class(stream_base: Any):
    class _RecordingStream(stream_base):
        def __init__(self, inner: Any, recorder: ProviderRequestRecorder) -> None:
            self._inner = inner
            self._recorder = recorder
            self._first = False
            self._closed = False

        def __iter__(self) -> Iterator[bytes]:
            try:
                for chunk in self._inner:
                    self._observe(chunk)
                    yield chunk
            except BaseException as exc:  # noqa: BLE001
                self._recorder.note_body_error(exc)
                raise
            else:
                self._recorder.note_body_complete(completed=True)

        def read(self, *args: Any, **kwargs: Any) -> bytes:
            inner_read = getattr(self._inner, "read", None)
            try:
                if callable(inner_read):
                    data = inner_read(*args, **kwargs)
                else:
                    data = b"".join(self._inner)
            except BaseException as exc:  # noqa: BLE001
                self._recorder.note_body_error(exc)
                raise
            self._observe(data)
            self._recorder.note_body_complete(completed=True)
            return data

        def _observe(self, chunk: Any) -> None:
            if self._first or not chunk:
                return
            self._first = True
            try:
                size = len(chunk)
            except Exception:
                size = None
            self._recorder.note_first_body_chunk(size)

        def close(self) -> Any:
            if self._closed:
                return None
            self._closed = True
            try:
                return self._inner.close()
            finally:
                self._recorder.note_body_complete(completed=False)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> Any:
            self.close()
            return False

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    return _RecordingStream


# ---------------------------------------------------------------------------
# Client instrumentation
# ---------------------------------------------------------------------------
def _backend_for_client(client: Any) -> tuple[str, Any]:
    mro_modules = {
        str(getattr(item, "__module__", "")) for item in getattr(type(client), "__mro__", ())
    }
    if any("httpx2" in module for module in mro_modules):
        import httpx2

        return "httpx2", httpx2.SyncByteStream
    import httpx

    return "httpx", httpx.SyncByteStream


def instrument_client(client: Any) -> bool:
    """Attach trace injection to an already-built real SDK client.

    This is the strictest proxy-preserving option: LangChain/OpenAI build their
    normal client (including socket options and proxy mounts), then only
    ``build_request`` / ``send`` and one response hook are wrapped in place.
    The transport object is not replaced, ``trust_env`` is untouched, and
    existing request/response hooks are preserved.

    Returns ``True`` when the client is instrumented (or was already), and
    ``False`` when tracing is off or the client cannot be wrapped.  Failures are
    recorded in ``_LAST_BUILD_INFO`` and never raise to the caller.
    """
    global _LAST_BUILD_INFO
    if client is None:
        _LAST_BUILD_INFO = {
            "status": "unavailable_no_client",
            "reason": "no SDK client instance was available to instrument",
        }
        return False
    if not enabled():
        return False
    if getattr(client, "_rvs_provider_transport_instrumented", False):
        return True

    try:
        backend, stream_base = _backend_for_client(client)
        recording_stream_cls = _make_recording_stream_class(stream_base)
        build_info: dict[str, Any] = {
            "status": "available",
            "backend": backend,
            "client_class": f"{type(client).__module__}.{type(client).__name__}",
            "transport_replaced": False,
            "trust_env_preserved": bool(getattr(client, "trust_env", True)),
            "mounts_preserved": True,
            "injection": "in-place build_request override",
        }
        original_build_request = client.build_request
        original_send = client.send

        def _build_request(*args: Any, **kwargs: Any) -> Any:
            request = original_build_request(*args, **kwargs)
            if not enabled():
                return request
            try:
                recorder = ProviderRequestRecorder(
                    request,
                    backend=backend,
                    build_info=dict(_LAST_BUILD_INFO),
                    stream_base=stream_base,
                )
                request.extensions["rvs_provider_recorder"] = recorder
                request.extensions["trace"] = recorder.wrap_trace(
                    request.extensions.get("trace")
                )
            except Exception:
                # Never break request construction for diagnostics.
                pass
            return request

        def _send(request: Any, **kwargs: Any) -> Any:
            recorder = None
            if enabled():
                try:
                    recorder = request.extensions.get("rvs_provider_recorder")
                except Exception:
                    recorder = None
            if recorder is not None:
                try:
                    recorder.begin_send()
                except Exception:
                    pass
            try:
                response = original_send(request, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                if recorder is not None:
                    try:
                        recorder.end_send(error=exc, stream=bool(kwargs.get("stream")))
                    except Exception:
                        pass
                raise
            if recorder is not None:
                try:
                    recorder.note_response(response)
                    recorder.end_send(
                        response=response,
                        stream=bool(kwargs.get("stream")),
                    )
                except Exception:
                    pass
            return response

        def _response_hook(response: Any) -> None:
            try:
                request = getattr(response, "request", None)
                recorder = (
                    request.extensions.get("rvs_provider_recorder")
                    if request is not None
                    else None
                )
                if recorder is None:
                    return
                recorder.note_response(response)
                stream = getattr(response, "stream", None)
                if stream is not None and not isinstance(stream, recording_stream_cls):
                    response.stream = recording_stream_cls(stream, recorder)
            except Exception:
                # Diagnostics must never break a response.
                pass

        client.build_request = _build_request
        client.send = _send
        try:
            hooks = dict(getattr(client, "_event_hooks", {}) or {})
            response_hooks = list(hooks.get("response", []) or [])
            response_hooks.append(_response_hook)
            hooks["response"] = response_hooks
            hooks.setdefault("request", [])
            client._event_hooks = hooks
            build_info["event_hooks"] = {
                "response_hooks": len(response_hooks),
                "request_hooks_preserved": True,
            }
        except Exception:
            build_info["event_hooks"] = {"installed": False}
        client._rvs_provider_transport_instrumented = True
        _LAST_BUILD_INFO = dict(build_info)
        return True
    except Exception as exc:  # noqa: BLE001
        _LAST_BUILD_INFO = {
            "status": "unavailable_instrument_failed",
            "reason": "could not instrument the real SDK client",
            "error_type": type(exc).__name__,
        }
        return False


def build_http_client(
    *,
    timeout: float | None = None,
    base_url: str | None = None,
    transport: Any = None,
    trust_env: bool | None = None,
) -> Any | None:
    """Build a default-shaped sync client and instrument it in place.

    This is used by the standalone probe and by local tests.  The product
    factory uses :func:`instrument_client` on the client LangChain already
    built, so this function is not on the product path; that keeps the product's
    socket options, proxy mounts and async client exactly as LangChain made
    them.

    ``transport`` / ``trust_env`` are for local mock tests only.  When tracing
    is disabled, ``None`` is returned.
    """
    global _LAST_BUILD_INFO
    if not enabled():
        _LAST_BUILD_INFO = {"status": "disabled", "reason": "RVS_PROVIDER_TRACE is off"}
        return None

    base_cls, _, _ = _client_classes()
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if base_url:
        kwargs["base_url"] = base_url
    if transport is not None:
        # Test-only.  Product call sites never pass this.
        kwargs["transport"] = transport
    if trust_env is not None:
        # Test-only.  Product call sites never pass this; default is True.
        kwargs["trust_env"] = trust_env

    try:
        with _parseable_no_proxy_env() as parse_info:
            client = base_cls(**kwargs)
        instrument_client(client)
        _LAST_BUILD_INFO.setdefault("build", {}).update(parse_info)
        _LAST_BUILD_INFO["built_for_probe_or_test"] = True
        return client
    except Exception as exc:  # noqa: BLE001
        _LAST_BUILD_INFO = {
            "status": "unavailable_build_failed",
            "reason": "could not build a tracing client; caller keeps its default client",
            "error_type": type(exc).__name__,
        }
        return None


# ---------------------------------------------------------------------------
# Record export
# ---------------------------------------------------------------------------
def _store_record(record: dict[str, Any]) -> None:
    with _RECORD_LOCK:
        _RECENT.append(record)
        directory = os.getenv(RECORD_DIR_ENV, "").strip()
        if not directory:
            return
        try:
            from pathlib import Path

            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            with (path / "records.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            # Diagnostics must never break a turn.
            pass


def recent_records() -> list[dict[str, Any]]:
    with _RECORD_LOCK:
        return [dict(item) for item in _RECENT]


def clear_recent_records() -> None:
    with _RECORD_LOCK:
        _RECENT.clear()


# ---------------------------------------------------------------------------
# Backwards-compatible response stream used by the older latency test
# ---------------------------------------------------------------------------
class _TracedResponseStream:
    """Wrap an iterable and emit ``provider.body``/``provider.first_chunk``.

    Kept for the existing latency-span contract test.  The new SDK hook uses
    ``_RecordingStream`` above; this small class is transport-agnostic.
    """

    def __init__(self, inner: Any, *, request_span_id: str | None, host: str | None) -> None:
        self._inner = inner
        self._request_span_id = request_span_id
        self._host = host
        self._first = False
        self._ended = False
        self._body_span = latency.open_span(
            "provider.body",
            parent_span_id=request_span_id,
            host=host,
        )

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self._inner:
                self._observe(chunk)
                yield chunk
        except BaseException as exc:  # noqa: BLE001
            self._finish("error", type(exc).__name__)
            raise
        else:
            self._finish("ok")

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        try:
            data = self._inner.read(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            self._finish("error", type(exc).__name__)
            raise
        self._observe(data)
        self._finish("ok")
        return data

    def _observe(self, chunk: Any) -> None:
        if self._first or not chunk:
            return
        self._first = True
        latency.point(
            "provider.first_chunk",
            parent_span_id=self._body_span.span_id if self._body_span else self._request_span_id,
            host=self._host,
            note="first body chunk, not first token; product path uses non-streaming invoke",
        )

    def _finish(self, status: str, error: str | None = None) -> None:
        if self._ended:
            return
        self._ended = True
        if self._body_span is not None:
            self._body_span.end(status=status, error=error)

    def close(self) -> Any:
        try:
            return self._inner.close()
        finally:
            self._finish("ok")

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *args: Any) -> Any:
        self.close()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
