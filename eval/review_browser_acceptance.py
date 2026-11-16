# -*- coding: utf-8 -*-
"""Headless Chrome acceptance walk for the manual-review GUI.

Uses a throw-away review store under the system temp directory.  It never
writes ``eval/reviews`` or the authoritative dataset.  The script fills a case
definition verdict, saves, refreshes, fills a separate run-answer verdict,
saves, refreshes, reveals the holdout once, exports JSON+CSV, and captures a
screenshot.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import websocket

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8951"
TARGET_CASE = "rsv1-dev-boundary-unknown-model-price-01"
CURRENT_RUN = "v1-live-regrade-development-20261113-033753-69b438"
CASE_NOTES = '案例定义：通过（浏览器验收）\n第二行含 "引号", 逗号; 以及 expected/actual 区分。'
RUN_NOTES = '运行回答：存疑（浏览器验收）\n来源与结论是否语义一致需人工再看；不把 expected 当 actual。'
CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]


class CheckLog:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: Any = None) -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" :: {detail}" if detail not in (None, "") else ""), flush=True)

    def require(self, name: str, ok: bool, detail: Any = None) -> None:
        self.add(name, ok, detail)
        if not ok:
            raise AssertionError(f"{name}: {detail}")

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks)


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(
            ws_url, timeout=30, suppress_origin=True, enable_multithread=False
        )
        self.next_id = 0
        self.events: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.next_id += 1
        message_id = self.next_id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv()
            if not raw:
                raise RuntimeError("CDP websocket closed")
            message = json.loads(raw)
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result") or {}
            self.events.append(message)

    def eval(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(f"JS exception: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def wait_js(self, expression: str, *, timeout: float = 25.0, label: str = "") -> Any:
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                value = self.eval(expression)
                if value:
                    return value
            except Exception as error:  # navigation can destroy the context briefly
                last_error = error
            time.sleep(0.18)
        raise TimeoutError(f"wait_js timeout {label or expression!r}: {last_error}")

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})
        self.wait_js("document.readyState === 'complete'", label="document complete")

    def reload(self) -> None:
        self.call("Page.reload", {"ignoreCache": True})
        time.sleep(0.3)
        self.wait_js("document.readyState === 'complete'", label="document complete after reload")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def wait_http(url: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:
            last = error
            time.sleep(0.2)
    raise RuntimeError(f"HTTP not ready: {url}: {last}")


def chrome_path() -> Path:
    for candidate in CHROME_CANDIDATES:
        if candidate.exists():
            return candidate
    raise RuntimeError("Chrome/Edge not found")


def start_server(store_root: Path, port: int) -> subprocess.Popen:
    log = (store_root / "server.log").open("ab")
    cmd = [
        sys.executable,
        str(ROOT / "eval" / "review_server.py"),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--store",
        str(store_root / "reviews"),
        "--log-file",
        str(store_root / "server.log"),
        "--state-file",
        str(store_root / "server_state.json"),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=log,
        stderr=log,
        creationflags=flags,
    )
    wait_http(f"http://127.0.0.1:{port}/api/health", timeout=40)
    return process


def start_chrome(user_dir: Path, debug_port: int, log_path: Path) -> subprocess.Popen:
    log = log_path.open("ab")
    cmd = [
        str(chrome_path()),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--hide-scrollbars",
        "--window-size=1440,1100",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={debug_port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_dir}",
        "about:blank",
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(cmd, stdout=log, stderr=log, creationflags=flags)


def new_tab(debug_port: int, url: str) -> str:
    request = urllib.request.Request(
        f"http://127.0.0.1:{debug_port}/json/new?{url}", method="PUT"
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["webSocketDebuggerUrl"]


def set_textarea(cdp: CDP, selector: str, value: str) -> None:
    js = f"""
    (() => {{
      const el = document.querySelector({json.dumps(selector)});
      if (!el) return false;
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(el, {json.dumps(value)});
      el.dispatchEvent(new Event('input', {{bubbles: true}}));
      return true;
    }})()
    """
    if not cdp.eval(js):
        raise AssertionError(f"input not found: {selector}")


def click(cdp: CDP, selector: str) -> None:
    if not cdp.eval(f"(() => {{ const el=document.querySelector({json.dumps(selector)}); if(!el) return false; el.click(); return true; }})()"):
        raise AssertionError(f"click target not found: {selector}")


def wait_saved(cdp: CDP, expected_revision: int) -> None:
    cdp.wait_js(
        f"document.querySelector('#save-state').dataset.state === 'saved' && window.__reviewSnapshot().reviewRevision === {expected_revision}",
        label=f"saved revision {expected_revision}",
    )


def snapshot(cdp: CDP) -> dict[str, Any]:
    return cdp.eval("window.__reviewSnapshot()")


def reload_and_wait(cdp: CDP, label: str = "reload") -> None:
    old_boot = cdp.eval("window.__reviewBootId")
    cdp.call("Page.reload", {"ignoreCache": True})
    cdp.wait_js(
        "document.readyState === 'complete' && window.__reviewBootId && window.__reviewBootId !== "
        + json.dumps(old_boot)
        + " && window.__reviewReady === true",
        timeout=40,
        label=label,
    )


def export_in_browser(cdp: CDP, fmt: str) -> dict[str, Any]:
    return cdp.eval(
        f"fetch('/api/export?format={fmt}').then(r => r.arrayBuffer()).then(b => ({{size: b.byteLength, head: Array.from(new Uint8Array(b).slice(0,3)), text: new TextDecoder('utf-8').decode(b)}}))",
        await_promise=True,
    )


def wait_download(directory: Path, suffix: str, timeout: float = 15.0) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = [p for p in directory.glob(f"*{suffix}") if p.is_file() and not p.name.endswith(".crdownload")]
        if files:
            return max(files, key=lambda p: p.stat().st_mtime)
        time.sleep(0.2)
    raise TimeoutError(f"download {suffix} not found in {directory}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8951)
    parser.add_argument("--debug-port", type=int, default=9225)
    parser.add_argument("--output", default=str(ROOT / "outputs" / "manual-review.png"))
    parser.add_argument("--report", default=str(ROOT / "outputs" / "manual-review-browser.json"))
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    temp_root = Path(tempfile.mkdtemp(prefix="mr-browser-"))
    store_root = temp_root / "store"
    store_root.mkdir()
    chrome_profile = temp_root / "chrome-profile"
    downloads = temp_root / "downloads"
    downloads.mkdir()
    log = CheckLog()
    server = None
    chrome = None
    cdp = None
    started = time.time()
    try:
        server = start_server(store_root, args.port)
        log.add("temp server started", True, f"pid={server.pid} store={store_root}")

        before = {
            name: hashlib_sha256(ROOT / "eval" / "datasets" / "v1" / name)
            for name in ("suite.json", "cases.jsonl", "split.json", "audit.json", "evidence_index.json")
        }
        production_store = ROOT / "eval" / "reviews"
        production_before = sorted(
            str(path.relative_to(production_store))
            for path in production_store.rglob("*.json")
        ) if production_store.exists() else []

        chrome = start_chrome(chrome_profile, args.debug_port, temp_root / "chrome.log")
        wait_http(f"http://127.0.0.1:{args.debug_port}/json/version", timeout=40)
        ws_url = new_tab(args.debug_port, f"http://127.0.0.1:{args.port}/")
        cdp = CDP(ws_url)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Network.enable")
        try:
            cdp.call("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(downloads)})
        except Exception:
            cdp.call("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(downloads)})
        cdp.wait_js("window.__reviewReady === true", timeout=40, label="review app ready")
        log.require("headless browser loaded review app", True, cdp.eval("navigator.userAgent"))

        initial = snapshot(cdp)
        log.require("initial split is development", initial["split"] == "development", initial)
        log.require("initial list has 32 development cases", initial["rows"] == 32, initial["rows"])
        log.require("holdout hidden on first load", initial["holdoutExposed"] is False)
        body_initial = cdp.eval("document.body.innerText")
        log.require("no holdout content in initial DOM", "rsv1-hold-" not in body_initial)
        log.require("two review scopes visible", cdp.eval("document.querySelectorAll('.scope-card').length === 2"))

        click(cdp, f'[data-case-id="{TARGET_CASE}"]')
        cdp.wait_js(
            f"window.__reviewSnapshot().selectedId === {json.dumps(TARGET_CASE)} && document.querySelector('#case-id').textContent === {json.dumps(TARGET_CASE)}",
            label="target case selected",
        )
        log.require("case id/category/split rendered", cdp.eval("document.querySelector('#case-id').textContent") == TARGET_CASE)
        log.require("evidence quote rendered", cdp.eval("document.querySelectorAll('.evidence').length") >= 1)
        log.require("expected claims rendered", cdp.eval("document.querySelectorAll('.expected-block').length") >= 1)
        log.require("scope A is case definition", cdp.eval("document.querySelector('#review-target-title').textContent") == "案例定义")
        log.require("run answer is visible as read-only reference", cdp.eval("document.querySelectorAll('.answer-block').length") >= 1)

        click(cdp, '.status-btn[data-status="passed"]')
        set_textarea(cdp, "#notes", CASE_NOTES)
        set_textarea(cdp, "#reviewer", "browser-acceptance")
        log.require("unsaved state visible before save", cdp.eval("document.querySelector('#save-state').dataset.state === 'dirty'"))
        click(cdp, "#save-btn")
        wait_saved(cdp, 1)
        first = snapshot(cdp)
        log.require("case-definition status saved as passed", first["status"] == "passed", first)
        log.require("case-definition notes saved with newline+quotes", first["notes"] == CASE_NOTES)

        reload_and_wait(cdp, "case-definition reload")
        cdp.wait_js(f"window.__reviewSnapshot().selectedId === {json.dumps(TARGET_CASE)}", label="selection restored from hash")
        restored = snapshot(cdp)
        log.require("refresh restores selected case", restored["selectedId"] == TARGET_CASE)
        log.require("refresh restores case-definition status", restored["status"] == "passed", restored)
        log.require("refresh restores notes", restored["notes"] == CASE_NOTES)
        log.require("refresh shows saved revision", restored["reviewRevision"] == 1)
        log.require("refresh has no dirty flag", restored["dirty"] is False)

        click(cdp, "#scope-run_answer")
        cdp.wait_js(
            "window.__reviewSnapshot().scope === 'run_answer' && document.querySelector('#review-target-title').textContent.includes('运行回答')",
            label="run scope",
        )
        cdp.wait_js("document.querySelector('#run-select') && document.querySelector('#run-select').options.length > 0", label="run options")
        cdp.eval(
            f"""(() => {{
              const sel = document.querySelector('#run-select');
              sel.value = {json.dumps(CURRENT_RUN)};
              sel.dispatchEvent(new Event('change', {{bubbles: true}}));
              return sel.value;
            }})()"""
        )
        cdp.wait_js(
            f"window.__reviewSnapshot().runId === {json.dumps(CURRENT_RUN)} && document.querySelectorAll('.answer-block').length > 0",
            label="run answer loaded",
        )
        log.require("scope B is run answer", "运行回答" in cdp.eval("document.querySelector('#review-target-title').textContent"))
        log.require("run answer shows actual answer blocks", cdp.eval("document.querySelectorAll('.answer-block').length") >= 1)
        log.require("run trace summary rendered", cdp.eval("document.querySelectorAll('.trace-box').length") >= 1)
        log.require("run scope has no inherited case verdict", snapshot(cdp)["status"] == "")

        click(cdp, '.status-btn[data-status="uncertain"]')
        set_textarea(cdp, "#notes", RUN_NOTES)
        click(cdp, "#save-btn")
        wait_saved(cdp, 1)
        run_saved = snapshot(cdp)
        log.require("run-answer status saved as uncertain", run_saved["status"] == "uncertain", run_saved)
        log.require("run-answer notes saved", run_saved["notes"] == RUN_NOTES)

        reload_and_wait(cdp, "run-answer reload")
        cdp.wait_js(
            f"window.__reviewSnapshot().selectedId === {json.dumps(TARGET_CASE)} && window.__reviewSnapshot().scope === 'run_answer' && window.__reviewSnapshot().runId === {json.dumps(CURRENT_RUN)}",
            label="run selection restored",
        )
        run_restored = snapshot(cdp)
        log.require("refresh restores run scope + runId", run_restored["scope"] == "run_answer" and run_restored["runId"] == CURRENT_RUN)
        log.require("refresh restores run-answer status", run_restored["status"] == "uncertain")
        log.require("refresh restores run-answer notes", run_restored["notes"] == RUN_NOTES)
        log.require("run answer still visible after refresh", cdp.eval("document.querySelectorAll('.answer-block').length") >= 1)

        # Case-definition and run-answer records must remain separate.
        pair = cdp.eval(
            "Promise.all([fetch('/api/reviews?case_id=' + encodeURIComponent('" + TARGET_CASE + "') + '&scope=case_definition').then(r=>r.json()), fetch('/api/reviews?case_id=' + encodeURIComponent('" + TARGET_CASE + "') + '&scope=run_answer&runId=" + CURRENT_RUN + "').then(r=>r.json())])",
            await_promise=True,
        )
        log.require("separate records: case definition passed", pair[0]["current"]["status"] == "passed", pair[0]["current"]["status"])
        log.require("separate records: run answer uncertain", pair[1]["current"]["status"] == "uncertain", pair[1]["current"]["status"])
        log.require("separate records: distinct reviewIds", pair[0]["current"]["reviewId"] != pair[1]["current"]["reviewId"])

        # Screenshot before revealing the holdout. A taller viewport shows the
        # actual run answer and trace summary together with the review card.
        cdp.eval("window.scrollTo(0,0)")
        cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 2200, "deviceScaleFactor": 1, "mobile": False},
        )
        time.sleep(0.45)
        shot = cdp.call("Page.captureScreenshot", {"format": "png"})
        screenshot_path = Path(args.output)
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        import base64
        screenshot_path.write_bytes(base64.b64decode(shot["data"]))
        log.require("screenshot captured", screenshot_path.exists() and screenshot_path.stat().st_size > 10000, str(screenshot_path))
        cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 1100, "deviceScaleFactor": 1, "mobile": False},
        )

        # Explicit holdout reveal in the real browser.
        click(cdp, '.split-tab[data-split="holdout"]')
        cdp.wait_js("!document.querySelector('#holdout-gate').hidden", label="holdout gate")
        gate_text = cdp.eval("document.querySelector('#holdout-gate').innerText")
        log.require("holdout gate explains exposure", "曝光" in gate_text and "不会自动运行" in gate_text)
        click(cdp, "#holdout-reveal")
        cdp.wait_js(
            "window.__reviewSnapshot().split === 'holdout' && window.__reviewSnapshot().holdoutExposed === true && window.__reviewSnapshot().rows === 16",
            label="holdout exposed with 16 rows",
        )
        exposed = snapshot(cdp)
        log.require("holdout shows 16 cases only after explicit reveal", exposed["rows"] == 16 and exposed["holdoutExposed"])
        exposed_cases = cdp.eval("Array.from(document.querySelectorAll('[data-case-id]')).map(e => e.dataset.caseId)")
        log.require("holdout rows are holdout ids", all(case_id.startswith("rsv1-hold-") for case_id in exposed_cases), exposed_cases[:3])
        meta = cdp.eval("fetch('/api/meta').then(r => r.json())", await_promise=True)
        log.require("first exposure event recorded", meta["holdout"]["exposed"] and meta["holdout"]["firstExposure"]["eventId"] == "holdout-exposure-1")

        # Back to development for export + final state.
        click(cdp, '.split-tab[data-split="development"]')
        cdp.wait_js(
            f"window.__reviewSnapshot().split === 'development' && !!document.querySelector('[data-case-id=\"{TARGET_CASE}\"]')",
            label="development split",
        )
        click(cdp, f'[data-case-id="{TARGET_CASE}"]')
        cdp.wait_js(
            f"window.__reviewSnapshot().selectedId === {json.dumps(TARGET_CASE)} && document.querySelector('#case-id').textContent === {json.dumps(TARGET_CASE)}",
            label="target reselected",
        )
        click(cdp, "#scope-run_answer")
        cdp.wait_js("window.__reviewSnapshot().scope === 'run_answer'", label="run scope after reveal")
        cdp.wait_js(
            f"window.__reviewSnapshot().selectedId === {json.dumps(TARGET_CASE)} && document.querySelectorAll('.answer-block').length > 0",
            label="run answer after reveal",
        )

        # Export via the page's own download buttons.
        click(cdp, "#export-csv")
        csv_path = wait_download(downloads, ".csv")
        csv_raw = csv_path.read_bytes()
        log.require("browser CSV export starts with UTF-8 BOM", csv_raw.startswith(b"\xef\xbb\xbf"), str(csv_path))
        csv_text = csv_raw.decode("utf-8-sig")
        csv_rows = list(csv.DictReader(io.StringIO(csv_text)))
        review_rows = [row for row in csv_rows if row["record_type"] == "review"]
        exposure_rows = [row for row in csv_rows if row["record_type"] == "holdout_exposure"]
        log.require("CSV export contains both review scopes", {row["reviewScope"] for row in review_rows} == {"case_definition", "run_answer"})
        log.require("CSV notes preserve newline and quotes", any(row["notes"] == CASE_NOTES for row in review_rows) and any(row["notes"] == RUN_NOTES for row in review_rows))
        log.require("CSV export contains holdout exposure", len(exposure_rows) == 1 and exposure_rows[0]["exposureCount"] == "16")

        click(cdp, "#export-json")
        json_path = wait_download(downloads, ".json")
        export_payload = json.loads(json_path.read_text(encoding="utf-8"))
        log.require("browser JSON export contains reviews + exposure", len(export_payload["reviews"]) == 2 and len(export_payload["exposures"]) == 1)
        log.require("JSON export keeps scope/runId/caseHash", all(row.get("caseHash") and row.get("suiteRevision") for row in export_payload["reviews"]) and any(row.get("runId") == CURRENT_RUN for row in export_payload["reviews"]))
        log.require("JSON export notes preserved", any(row["notes"] == CASE_NOTES for row in export_payload["reviews"]) and any(row["notes"] == RUN_NOTES for row in export_payload["reviews"]))

        browser_export = None
        log.require("no browser JS errors", cdp.eval("window.__reviewErrors.length === 0"), cdp.eval("window.__reviewErrors"))

        after = {
            name: hashlib_sha256(ROOT / "eval" / "datasets" / "v1" / name)
            for name in ("suite.json", "cases.jsonl", "split.json", "audit.json", "evidence_index.json")
        }
        log.require("authoritative dataset unchanged", before == after)
        production_after = sorted(
            str(path.relative_to(production_store))
            for path in production_store.rglob("*.json")
        ) if production_store.exists() else []
        log.require("production review store untouched by browser test", production_before == production_after, {"before": production_before, "after": production_after})

        report = {
            "schema_version": "manual-review-browser-acceptance/v1",
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ok": log.ok,
            "durationSeconds": round(time.time() - started, 2),
            "baseUrl": f"http://127.0.0.1:{args.port}",
            "targetCase": TARGET_CASE,
            "runId": CURRENT_RUN,
            "screenshot": str(screenshot_path),
            "screenshotBytes": screenshot_path.stat().st_size if screenshot_path.exists() else 0,
            "checks": log.checks,
            "datasetHashesBefore": before,
            "datasetHashesAfter": after,
            "tempStore": str(store_root),
            "downloads": {"csv": str(csv_path), "json": str(json_path)},
            "userAgent": cdp.eval("navigator.userAgent"),
            "note": "Headless Chrome against a throw-away store; production eval/reviews was not written.",
        }
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"report: {report_path}")
        print(f"screenshot: {screenshot_path}")
        return 0 if log.ok else 1
    except Exception as error:
        log.add("acceptance aborted", False, f"{type(error).__name__}: {error}")
        debug = None
        if cdp is not None:
            try:
                debug = {
                    "snapshot": snapshot(cdp),
                    "errors": cdp.eval("window.__reviewErrors"),
                    "url": cdp.eval("location.href"),
                    "body": cdp.eval("document.body.innerText.slice(0, 2500)"),
                }
            except Exception as debug_error:
                debug = {"debugError": str(debug_error)}
        report = {
            "schema_version": "manual-review-browser-acceptance/v1",
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ok": False,
            "durationSeconds": round(time.time() - started, 2),
            "targetCase": TARGET_CASE,
            "checks": log.checks,
            "error": f"{type(error).__name__}: {error}",
            "debug": debug,
        }
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"report: {report_path}")
        return 1
    finally:
        if cdp is not None:
            cdp.close()
        if chrome is not None:
            try:
                chrome.terminate()
                chrome.wait(timeout=10)
            except Exception:
                try:
                    chrome.kill()
                except Exception:
                    pass
        if server is not None:
            try:
                server.terminate()
                server.wait(timeout=10)
            except Exception:
                try:
                    server.kill()
                except Exception:
                    pass
        if not args.keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)


def hashlib_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
