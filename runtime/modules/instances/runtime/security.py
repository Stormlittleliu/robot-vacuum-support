# -*- coding: utf-8 -*-
"""Deployment security helpers for the single-tenant instance API.

The defaults keep the historical local mode: an unconfigured deployment is
bound to loopback and has no account system.  A cloud deployment sets explicit
tokens and an explicit Origin allow-list; nothing here trusts ``Host`` or
``X-Forwarded-*`` to *bypass* authentication.

Environment:
  RVS_ADMIN_TOKEN       shared operator bearer token (admin API/page data)
  RVS_ACCESS_TOKEN      optional shared visitor token for ordinary session APIs
  RVS_ALLOWED_ORIGINS   comma-separated explicit CORS/Origin allow-list
  RVS_SERVE_STATIC      "0"/"false" disables backend static hosting
"""
from __future__ import annotations

import hmac
import os
from urllib.parse import urlparse

ADMIN_TOKEN_ENV = "RVS_ADMIN_TOKEN"
ACCESS_TOKEN_ENV = "RVS_ACCESS_TOKEN"
ALLOWED_ORIGINS_ENV = "RVS_ALLOWED_ORIGINS"
SERVE_STATIC_ENV = "RVS_SERVE_STATIC"

ACCESS_COOKIE = "rvs_access_token"


def env_value(name: str) -> str:
    return str(os.getenv(name, "") or "").strip()


def configured_token(name: str) -> str:
    return env_value(name)


def constant_time_equals(left: str | None, right: str | None) -> bool:
    """Compare two UTF-8 strings without an early-exit timing gap."""
    if left is None or right is None:
        return False
    try:
        left_bytes = str(left).encode("utf-8")
        right_bytes = str(right).encode("utf-8")
    except UnicodeError:
        return False
    return hmac.compare_digest(left_bytes, right_bytes)


def bearer_token(header_value: str | None) -> str | None:
    """Parse exactly ``Bearer <token>``; other schemes/empty values fail."""
    value = str(header_value or "").strip()
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def parse_allowed_origins(value: str | None) -> list[str]:
    """Return only absolute http(s) origins, deduplicated, no wildcard."""
    result: list[str] = []
    for raw in str(value or "").split(","):
        item = raw.strip()
        if not item:
            continue
        parsed = urlparse(item)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            continue
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if origin not in result:
            result.append(origin)
    return result


def allowed_origins_from_env() -> list[str]:
    return parse_allowed_origins(os.getenv(ALLOWED_ORIGINS_ENV, ""))


def static_hosting_enabled() -> bool:
    value = env_value(SERVE_STATIC_ENV).lower()
    return value not in {"0", "false", "no", "off"}


def loopback_binding(host: str | None) -> bool:
    value = str(host or "").strip().lower().strip("[]")
    return value in {"127.0.0.1", "localhost", "::1"}


def origin_allowed(request, allowed_origins: list[str]) -> bool:
    """Validate browser Origin without trusting forwarding headers.

    - no Origin: non-browser/local clients are allowed (CSRF still needs a
      browser Origin, so this does not weaken browser writes);
    - ``null`` or malformed Origin: rejected;
    - explicitly allow-listed origin: allowed for a separately hosted UI;
    - same-origin against the request ``Host``: allowed behind a reverse proxy.
      ``Host`` is only used for this comparison, never for auth/loopback.
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    origin = origin.strip().lower()
    if origin == "null":
        return False
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    normalized = f"{parsed.scheme}://{parsed.netloc}"
    if normalized in allowed_origins:
        return True
    host = str(request.headers.get("host") or "").strip().lower()
    return bool(host) and parsed.netloc == host


def access_token_from_request(request) -> str | None:
    token = bearer_token(request.headers.get("authorization"))
    if token:
        return token
    return str(request.cookies.get(ACCESS_COOKIE) or "") or None


def admin_auth_mode() -> str:
    return "bearer_token" if configured_token(ADMIN_TOKEN_ENV) else "loopback_only"


def access_auth_mode() -> str:
    return "bearer_token" if configured_token(ACCESS_TOKEN_ENV) else "none"


__all__ = [
    "ACCESS_COOKIE",
    "ADMIN_TOKEN_ENV",
    "ACCESS_TOKEN_ENV",
    "ALLOWED_ORIGINS_ENV",
    "SERVE_STATIC_ENV",
    "access_auth_mode",
    "access_token_from_request",
    "admin_auth_mode",
    "allowed_origins_from_env",
    "bearer_token",
    "configured_token",
    "constant_time_equals",
    "env_value",
    "loopback_binding",
    "origin_allowed",
    "parse_allowed_origins",
    "static_hosting_enabled",
]
