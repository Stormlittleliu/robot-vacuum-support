# -*- coding: utf-8 -*-
"""Small, dependency-free redaction for persisted diagnostics.

Product traces and session error rows must stay useful for diagnosis without
copying provider credentials or authentication headers into logs, reports or the
SSE stream.  This module is deliberately conservative: it only masks explicit
credential shapes (API keys, bearer tokens, Authorization/api-key pairs) and
leaves ordinary prose alone.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
REDACTED_KEY = "[REDACTED_KEY]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer <token> / Authorization=<token>
    (
        re.compile(r"(?i)(\bauthorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
        r"\1" + REDACTED,
    ),
    # Bearer <token> on its own.
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer " + REDACTED),
    # api_key=..., DEEPSEEK_API_KEY=..., X-Api-Key: ...
    (
        re.compile(
            r"(?i)(\b(?:api[_-]?key|deepseek_api_key|openai_api_key|dashscope_api_key|x-api-key)\s*[:=]\s*)"
            r"(\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
        ),
        r"\1" + REDACTED,
    ),
    # Common provider key prefix.
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{8,}\b"), REDACTED_KEY),
    # Provider prose such as "Your api key: ****-key is invalid".
    (re.compile(r"(?i)(your\s+api\s+key\s*:\s*)[^\s,;)]+"), r"\1" + REDACTED),
    # Explicit environment assignment.
    (
        re.compile(r"(?im)^(\s*[A-Z0-9_]*API_KEY\s*=\s*).+$"),
        r"\1" + REDACTED,
    ),
)


def redact_text(value: Any) -> str:
    text = str(value or "")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact strings in a JSON-like value."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value
