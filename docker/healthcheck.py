# -*- coding: utf-8 -*-
"""Stdlib liveness/readiness probe used by Docker/Compose and operators."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def probe(mode: str, base_url: str = "http://127.0.0.1:8000", timeout: float = 4.0) -> tuple[int, dict]:
    path = "/api/health/live" if mode == "live" else "/api/health/ready"
    url = base_url.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 1, {"status": "unreachable", "mode": mode, "error": type(exc).__name__}
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {"status": "invalid_response", "body": body[:200]}
    healthy = status == 200 and (
        mode == "live" or bool(payload.get("ready"))
    )
    return 0 if healthy else 1, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Liveness/readiness probe")
    parser.add_argument("mode", choices=("live", "ready"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=4.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    code, payload = probe(args.mode, args.base_url, args.timeout)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"{args.mode}: {'ok' if code == 0 else 'not ready'}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
