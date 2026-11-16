# -*- coding: utf-8 -*-
"""Compatibility shim for the earlier provider trace helper.

The first latency slice refused a custom transport whenever a proxy was
present.  That was correct about not replacing the transport, but it also left
provider phases unmeasured.  The current implementation lives in
:mod:`core.observability.provider_transport`: it keeps the SDK's real
``httpx``/``httpx2`` client and proxy mounts, and injects
``request.extensions["trace"]`` instead.  This module re-exports the public
names plus the small stream helper used by the latency span tests.
"""
from __future__ import annotations

from typing import Any

from core.observability.provider_transport import (  # noqa: F401
    ProviderRequestRecorder,
    _TracedResponseStream,
    build_http_client as _build_http_client,
    enabled,
    proxy_env_present,
    recent_records,
    scope,
    sdk_versions,
    status,
)


def build_http_client(*, timeout: float | None = None, base_url: str | None = None) -> Any | None:
    """Backward-compatible wrapper; product code uses provider_transport."""
    return _build_http_client(timeout=timeout, base_url=base_url)


__all__ = [
    "ProviderRequestRecorder",
    "_TracedResponseStream",
    "build_http_client",
    "enabled",
    "proxy_env_present",
    "recent_records",
    "scope",
    "sdk_versions",
    "status",
]
