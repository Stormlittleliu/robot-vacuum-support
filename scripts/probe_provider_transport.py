# -*- coding: utf-8 -*-
"""Offline preflight + at most one real provider request for transport diagnosis.

What this script is for
-----------------------
The earlier latency report marked provider transport ``unavailable`` because a
custom transport would have bypassed httpx proxy auto-detection.  This probe
uses the SDK's real client and injects ``request.extensions["trace"]`` in place,
so the current proxy path is kept.  It is a diagnostic, not an agent run.

Safety
------
- Offline by default.  ``--live`` is required for the single real request.
- ``--live`` sends at most one short, non-private chat completion with
  ``max_retries=0``; it is not a full agent turn.
- The explicit environment file is loaded in this process only.  No secret
  value is printed, copied, written to an artifact or put into a request log.
- No system proxy, SSL, network setting or GUI process is touched.
- No subprocess is started by this script.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = Path(r"E:\CODE\Myproject\agent-workspace\my_agent_harness\.env")
DEFAULT_OUT = ROOT / "outputs" / "provider-transport"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
LIVE_MESSAGE = "请只回复：ok"


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _host(url: str) -> str | None:
    try:
        return urllib.parse.urlsplit(url).hostname
    except Exception:
        return None


def _safe_timeout(client: Any) -> Any:
    timeout = getattr(client, "timeout", None)
    try:
        return timeout.as_dict()
    except Exception:
        return repr(timeout)


def _transport_shape(client: Any, url: Any) -> dict[str, Any]:
    try:
        transport = client._transport_for_url(url)
        pool = getattr(transport, "_pool", None)
        return {
            "transport_class": type(transport).__name__,
            "pool_class": type(pool).__name__ if pool is not None else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error_type": type(exc).__name__}


def preflight(base_url: str, timeout: float, out_dir: Path) -> dict[str, Any]:
    """Build real clients and compare the traced one with the default shape."""
    os.environ.setdefault("RVS_PROVIDER_TRACE", "1")
    from core.observability import provider_transport as pt

    try:
        import httpx2
        import openai
    except Exception as exc:  # noqa: BLE001
        return {
            "schema_version": "provider-transport-preflight/v1",
            "status": "failed_import",
            "error_type": type(exc).__name__,
        }

    provider_host = _host(base_url)
    checks: dict[str, Any] = {}
    baseline = None
    traced = None
    try:
        with pt.parseable_no_proxy_env() as parse_info:
            baseline = openai.DefaultHttpxClient(base_url=base_url, timeout=timeout)
        traced = pt.build_http_client(timeout=timeout, base_url=base_url)
        if traced is None:
            return {
                "schema_version": "provider-transport-preflight/v1",
                "status": "failed_no_client",
                "provider_host": provider_host,
                "last_build": dict(pt._LAST_BUILD_INFO),
            }

        proxy_url = httpx2.URL(base_url)
        baseline_shape = _transport_shape(baseline, proxy_url)
        traced_shape = _transport_shape(traced, proxy_url)
        baseline_mounts = len(getattr(baseline, "_mounts", {}) or {})
        traced_mounts = len(getattr(traced, "_mounts", {}) or {})
        baseline_hooks = dict(getattr(baseline, "_event_hooks", {}) or {})
        traced_hooks = dict(getattr(traced, "_event_hooks", {}) or {})

        baseline_request = baseline.build_request(
            "POST", base_url.rstrip("/") + "/chat/completions", json={"messages": []}
        )
        traced_request = traced.build_request(
            "POST", base_url.rstrip("/") + "/chat/completions", json={"messages": []}
        )
        existing_seen: list[str] = []

        def _existing(name: str, info: dict[str, Any]) -> None:
            existing_seen.append(name)

        chained_request = traced.build_request(
            "POST",
            base_url.rstrip("/") + "/chat/completions",
            json={"messages": []},
            extensions={"trace": _existing},
        )
        chained_trace = chained_request.extensions.get("trace")
        if callable(chained_trace):
            chained_trace("custom.noop.started", {})
            chained_trace("custom.noop.complete", {})

        checks = {
            "default_client_class": f"{type(baseline).__module__}.{type(baseline).__name__}",
            "traced_client_instrumented": bool(
                getattr(traced, "_rvs_provider_transport_instrumented", False)
            ),
            "trust_env_equal": bool(getattr(baseline, "trust_env", None))
            == bool(getattr(traced, "trust_env", None)),
            "follow_redirects_equal": getattr(baseline, "follow_redirects", None)
            == getattr(traced, "follow_redirects", None),
            "max_redirects_equal": getattr(baseline, "max_redirects", None)
            == getattr(traced, "max_redirects", None),
            "timeout_equal": _safe_timeout(baseline) == _safe_timeout(traced),
            "mount_count_equal": baseline_mounts == traced_mounts,
            "mount_shape_equal": baseline_shape == traced_shape,
            "transport_not_replaced": traced_shape.get("transport_class")
            == baseline_shape.get("transport_class"),
            "proxy_pool_preserved": traced_shape.get("pool_class") == "HTTPProxy",
            "request_header_count_equal": len(baseline_request.headers)
            == len(traced_request.headers),
            "request_method_equal": baseline_request.method == traced_request.method,
            "request_url_host_equal": _host(str(baseline_request.url))
            == _host(str(traced_request.url)),
            "trace_injected": callable(traced_request.extensions.get("trace")),
            "recorder_attached": "rvs_provider_recorder"
            in traced_request.extensions,
            "existing_trace_preserved": bool(existing_seen),
            "baseline_response_hooks": len(baseline_hooks.get("response", [])),
            "traced_response_hooks": len(traced_hooks.get("response", [])),
            "proxy_env_present": pt.proxy_env_present(),
            "no_proxy_workaround": dict(parse_info),
        }
        critical = [
            "trust_env_equal",
            "follow_redirects_equal",
            "max_redirects_equal",
            "timeout_equal",
            "mount_count_equal",
            "mount_shape_equal",
            "transport_not_replaced",
            "request_header_count_equal",
            "request_method_equal",
            "request_url_host_equal",
            "trace_injected",
            "recorder_attached",
            "existing_trace_preserved",
        ]
        ok = all(bool(checks.get(key)) for key in critical)
        return {
            "schema_version": "provider-transport-preflight/v1",
            "generated_at": _now_iso(),
            "status": "ok" if ok else "failed_checks",
            "provider_host": provider_host,
            "base_url_path_not_recorded": True,
            "sdk_versions": pt.sdk_versions(),
            "scope": pt.scope(),
            "status_detail": pt.status(),
            "checks": checks,
            "critical_checks": critical,
            "failed_checks": [key for key in critical if not checks.get(key)],
            "notes": [
                "build_request/send and a response hook are wrapped in place; the transport object is unchanged",
                "the compare baseline is openai.DefaultHttpxClient with the same base_url/timeout",
                "the probe never sends a request during preflight",
            ],
        }
    finally:
        for client in (baseline, traced):
            try:
                if client is not None:
                    client.close()
            except Exception:
                pass


def _record_summary(record: dict[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {"available": False}
    return {
        "available": True,
        "request_id": record.get("request_id"),
        "provider_host": record.get("provider_host"),
        "provider_port": record.get("provider_port"),
        "method": record.get("method"),
        "backend": record.get("backend"),
        "status": record.get("status"),
        "status_code": record.get("status_code"),
        "error_type": record.get("error_type"),
        "elapsed_ms": record.get("elapsed_ms"),
        "trace_events_available": record.get("trace_events_available"),
        "trace_event_count": record.get("trace_event_count"),
        "events_seen": record.get("events_seen"),
        "missing_events": record.get("missing_events"),
        "connection_setup_union_ms": record.get("connection_setup_union_ms"),
        "time_to_headers_ms": record.get("time_to_headers_ms"),
        "send_headers_duration_ms": record.get("send_headers_duration_ms"),
        "read_headers_wait_ms": record.get("read_headers_wait_ms"),
        "read_headers_attribution": record.get("read_headers_attribution"),
        "first_body_read_started_offset_ms": record.get(
            "first_body_read_started_offset_ms"
        ),
        "first_body_read_ms": record.get("first_body_read_ms"),
        "first_body_chunk_ms": record.get("first_body_chunk_ms"),
        "first_body_chunk_bytes": record.get("first_body_chunk_bytes"),
        "body_complete": record.get("body_complete"),
        "connection_reuse": record.get("connection_reuse"),
        "connection_reuse_known": record.get("connection_reuse_known"),
        "connection_reuse_basis": record.get("connection_reuse_basis"),
        "connect_tcp_destination": record.get("connect_tcp_destination"),
        "connect_tcp_roles": record.get("connect_tcp_roles"),
        "proxy_observed": record.get("proxy_observed"),
        "retry_count": record.get("retry_count"),
        "retry_backoff_ms": record.get("retry_backoff_ms"),
        "ttft_available": record.get("ttft_available"),
        "ttft_reason": record.get("ttft_reason"),
        "phases": record.get("phases"),
        "redaction": record.get("redaction"),
    }


def live_probe(
    *,
    env_file: Path,
    base_url: str,
    model: str,
    timeout: float,
    out_dir: Path,
) -> dict[str, Any]:
    """Send at most one short provider request; secrets stay in this process."""
    os.environ.setdefault("RVS_PROVIDER_TRACE", "1")
    os.environ["RVS_PROVIDER_TRANSPORT_DIR"] = str(out_dir / "records")

    from core.observability import provider_transport as pt

    if not env_file.is_file():
        return {
            "schema_version": "provider-transport-live/v1",
            "generated_at": _now_iso(),
            "attempted": False,
            "status": "env_file_missing",
            "env_file": str(env_file),
            "env_values_read_or_logged": False,
        }

    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=str(env_file), override=False)
    except Exception as exc:  # noqa: BLE001
        return {
            "schema_version": "provider-transport-live/v1",
            "generated_at": _now_iso(),
            "attempted": False,
            "status": "env_load_failed",
            "env_file": str(env_file),
            "env_values_read_or_logged": False,
            "error_type": type(exc).__name__,
        }

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return {
            "schema_version": "provider-transport-live/v1",
            "generated_at": _now_iso(),
            "attempted": False,
            "status": "missing_DEEPSEEK_API_KEY",
            "env_file": str(env_file),
            "env_values_read_or_logged": False,
        }

    try:
        import openai
    except Exception as exc:  # noqa: BLE001
        return {
            "schema_version": "provider-transport-live/v1",
            "generated_at": _now_iso(),
            "attempted": False,
            "status": "openai_import_failed",
            "error_type": type(exc).__name__,
        }

    pt.clear_recent_records()
    client = pt.build_http_client(timeout=timeout, base_url=base_url)
    result: dict[str, Any] = {
        "schema_version": "provider-transport-live/v1",
        "generated_at": _now_iso(),
        "attempted": True,
        "status": "not_sent",
        "env_file": str(env_file),
        "env_values_read_or_logged": False,
        "provider_host": _host(base_url),
        "model": model,
        "max_retries": 0,
        "message": LIVE_MESSAGE,
        "message_is_non_private_short_diagnostic": True,
        "full_agent_run": False,
        "http_client_available": client is not None,
        "live_request_count": 0,
        "usage": None,
        "response_chars": None,
        "wall_ms": None,
        "error_type": None,
        "error_phase": None,
    }
    if client is None:
        result["status"] = "client_unavailable"
        result["last_build"] = dict(pt._LAST_BUILD_INFO)
        return result

    sdk_client = None
    try:
        sdk_client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=client,
            max_retries=0,
            timeout=timeout,
        )
        started = time.perf_counter()
        completion = sdk_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": LIVE_MESSAGE}],
            temperature=0,
            max_tokens=8,
        )
        result["wall_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        result["status"] = "ok"
        result["live_request_count"] = 1
        usage = getattr(completion, "usage", None)
        result["usage"] = (
            {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
            if usage is not None
            else None
        )
        try:
            result["response_chars"] = len(completion.choices[0].message.content or "")
        except Exception:
            result["response_chars"] = None
    except BaseException as exc:  # noqa: BLE001
        result["status"] = "error"
        result["error_type"] = type(exc).__name__
        result["error_phase"] = "provider_request"
        result["wall_ms"] = round((time.perf_counter() - started) * 1000.0, 3) if "started" in locals() else None
    finally:
        try:
            if sdk_client is not None:
                sdk_client.close()
        except Exception:
            pass

    records = pt.recent_records()
    result["provider_request_records"] = len(records)
    result["record"] = _record_summary(records[-1] if records else None)
    result["records_file"] = str(out_dir / "records" / "records.jsonl")
    _write_json(out_dir / "records" / "records.json", records)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "records").mkdir(parents=True, exist_ok=True)
    (out_dir / "spans").mkdir(parents=True, exist_ok=True)

    # Enable the opt-in meter before importing provider_transport/latency.  This
    # does not send any request; only --live does.
    os.environ.setdefault("RVS_PROVIDER_TRACE", "1")
    os.environ.setdefault("RVS_LATENCY_TRACE", "1")
    os.environ.setdefault("RVS_LATENCY_DIR", str(out_dir / "spans"))
    os.environ.setdefault("RVS_LATENCY_PROCESS", "provider-probe")
    os.environ["RVS_PROVIDER_TRANSPORT_DIR"] = str(out_dir / "records")

    import sys as _sys

    _sys.path.insert(0, str(ROOT / "runtime"))

    preflight_payload = preflight(args.base_url, args.timeout, out_dir)
    _write_json(out_dir / "preflight.json", preflight_payload)

    live_payload: dict[str, Any] | None = None
    if args.live and not args.preflight_only:
        if preflight_payload.get("status") != "ok":
            live_payload = {
                "schema_version": "provider-transport-live/v1",
                "generated_at": _now_iso(),
                "attempted": False,
                "status": "skipped_preflight_not_ok",
                "env_values_read_or_logged": False,
            }
        else:
            live_payload = live_probe(
                env_file=Path(args.env_file),
                base_url=args.base_url,
                model=args.model,
                timeout=args.timeout,
                out_dir=out_dir,
            )
        _write_json(out_dir / "real-request.json", live_payload)
    elif not args.preflight_only:
        live_payload = {
            "schema_version": "provider-transport-live/v1",
            "generated_at": _now_iso(),
            "attempted": False,
            "status": "not_requested",
            "reason": "offline preflight only; pass --live for one real request",
            "env_values_read_or_logged": False,
        }
        _write_json(out_dir / "real-request.json", live_payload)

    progress = {
        "schema_version": "provider-transport-progress/v1",
        "generated_at": _now_iso(),
        "status": "preflight_done" if live_payload is None else "live_done",
        "preflight_file": str(out_dir / "preflight.json"),
        "real_request_file": str(out_dir / "real-request.json"),
        "preflight_status": preflight_payload.get("status"),
        "live_status": (live_payload or {}).get("status"),
        "env_values_read_or_logged": False,
    }
    _write_json(out_dir / "progress.json", progress)

    summary = {
        "preflight_status": preflight_payload.get("status"),
        "failed_checks": preflight_payload.get("failed_checks"),
        "live_status": (live_payload or {}).get("status"),
        "live_record": (live_payload or {}).get("record"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if preflight_payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
