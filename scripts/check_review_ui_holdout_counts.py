# -*- coding: utf-8 -*-
"""Headless regression for dynamic holdout counts in the existing review UI.

Serves the real index.html/app.js with a tiny fixture API (counts 0 and 16) and
uses Chrome --headless=new --dump-dom. No real holdout case text is read, no
review store is touched, and no model is called.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "eval" / "review_ui"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
REPORT = ROOT / "outputs" / "action-state-answer-review-holdout-ui.json"


class Handler(BaseHTTPRequestHandler):
    count = 0

    def log_message(self, *_args):
        return

    def _json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            data = (UI / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/static/app.js":
            data = (UI / "app.js").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/meta":
            self._json({
                "ok": True,
                "version": "manual-review/v2",
                "reviewerDefault": "fixture",
                "suite": {
                    "suiteId": "fixture-holdout-counts",
                    "revision": "v1",
                    "schemaVersion": "robot-support-eval/v1",
                    "counts": {"development": 2, "holdout": Handler.count},
                    "casesSha256": "0" * 64,
                    "splitSha256": "0" * 64,
                    "suiteSha256": "0" * 64,
                    "caseCount": 2,
                },
                "holdout": {"hiddenByDefault": True, "exposed": False, "firstExposure": None, "note": ""},
                "store": {"counts": {"reviewCount": 0, "exposureCount": 0}},
                "scopes": [{"value": "case_definition", "label": "案例定义"}],
                "statuses": [{"value": "passed", "label": "通过"}],
                "boundaries": {},
            })
            return
        if path == "/api/cases":
            self._json({
                "ok": True,
                "split": "development",
                "scope": "case_definition",
                "filter": "all",
                "suiteRevision": "v1",
                "rows": [],
                "totalBeforeFilter": 0,
                "filteredTotal": 0,
                "progress": {
                    "case_definition": {"total": 0, "reviewed": 0, "unreviewed": 0},
                    "run_answer": {"total": 0, "reviewed": 0, "unreviewed": 0},
                },
                "holdout": {"exposed": False},
            })
            return
        self._json({"ok": False, "error": "not_found"}, 404)


def chrome_dom(url: str) -> str:
    with tempfile.TemporaryDirectory(prefix="holdout-ui-") as profile:
        result = subprocess.run(
            [
                str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", "--window-size=1200,900",
                "--virtual-time-budget=8000", f"--user-data-dir={profile}",
                "--dump-dom", url,
            ],
            text=True, encoding="utf-8", capture_output=True, timeout=45,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-1000:])
        return result.stdout


def run_case(count: int) -> dict:
    Handler.count = count
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/#split=holdout"
        dom = chrome_dom(url)
    finally:
        server.shutdown()
        server.server_close()
    hidden_tab = bool(re.search(r'id="holdout-tab"[^>]*hidden', dom))
    hidden_reveal = bool(re.search(r'id="holdout-reveal"[^>]*hidden', dom))
    hidden_gate = bool(re.search(r'id="holdout-gate"[^>]*hidden', dom))
    count_text = re.search(r'id="holdout-count">(\d+)<', dom)
    dev_active = bool(re.search(r'class="split-tab active" data-split="development"', dom))
    return {
        "count": count,
        "url": url,
        "hidden_tab": hidden_tab,
        "hidden_reveal": hidden_reveal,
        "hidden_gate": hidden_gate,
        "count_text": count_text.group(1) if count_text else None,
        "development_active_after_hash_fallback": dev_active,
        "dom": dom,
    }


def main() -> None:
    zero = run_case(0)
    sixteen = run_case(16)
    checks = {
        "zero_hides_tab": zero["hidden_tab"],
        "zero_hides_reveal": zero["hidden_reveal"],
        "zero_hides_gate": zero["hidden_gate"],
        "zero_count_text": zero["count_text"] == "0",
        "zero_hash_falls_back_development": zero["development_active_after_hash_fallback"],
        "sixteen_shows_tab": not sixteen["hidden_tab"],
        "sixteen_shows_reveal": not sixteen["hidden_reveal"],
        "sixteen_count_text": sixteen["count_text"] == "16",
        "sixteen_gate_preserved": 'id="holdout-gate"' in sixteen["dom"] and not sixteen["hidden_reveal"],
    }
    report = {
        "schema_version": "action-state-answer-review-holdout-ui/v1",
        "method": "real index.html/app.js + fixture /api/meta counts + Chrome --headless=new",
        "no_holdout_case_text_read": True,
        "no_real_review_store_written": True,
        "checks": checks,
        "zero": {key: value for key, value in zero.items() if key != "dom"},
        "sixteen": {key: value for key, value in sixteen.items() if key != "dom"},
    }
    assert all(checks.values()), checks
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(REPORT), "checks": checks}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
