# -*- coding: utf-8 -*-
"""Bounded product closed-loop smoke (recharge mainline, 2026-11-15).

One combined command:

* optionally builds a new immutable artifact with the project's own
  ``build_project`` into the external built directory and runs ``run.py --check``
  from an external cwd;
* starts one hidden instance on isolated 8949 with isolated storage and the
  existing safe environment-file injection (credential values are never read);
* runs at most 6 sessions / 10 turns selected from the frozen rotating-mainline
  cases (not a full 12-case rerun);
* verifies confirmed-failure regressions, source/hash evidence, GET/POST handoff
  read-only behavior, restart/continue/isolation and the resolved-session
  lifecycle;
* drives the actual product UI in hidden Chrome headless for copy, UTF-8
  download, selected-session and stale-summary behavior.

This is a development smoke, not a holdout, not a success rate and not an
authoritative semantic Judge run.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "runtime"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from eval.runner import (  # noqa: E402
    LiveServer,
    _choose_env_file,
    _http_json,
    _post_json,
    _send_with_one_retry,
    _sha256_file,
)

OUT = ROOT / "outputs" / "product-closed-loop-v1"
SUITE_DIR = ROOT / "eval" / "development-next" / "rotating-mainline-v1"
CASES_PATH = SUITE_DIR / "cases.json"
BUILT_ROOT = ROOT.parent / "robot-vacuum-support-built"
DEFAULT_PORT = 8949
EXISTING_PORTS = (8900, 8950, 8955, 8956)
SELECTED_CASES = (
    "rm-recharge-failed-outcome",
    "rm-recharge-multiround-correction",
    "rm-recharge-explicit-recovery",
    "rm-recharge-hypothetical-third-party",
    "rm-breadth-selection-bedroom-noise",
)
MAX_SESSIONS = 6
MAX_TURNS = 10
REPORT_SCHEMA = "product-closed-loop-report/v1"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def norm_quote(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------
def build_new_artifact() -> Path:
    from modules.artifacts.project import build_project

    result = build_project(ROOT, BUILT_ROOT, include_assets=True)
    payload = {
        key: result.get(key)
        for key in ("bundle", "zip", "manifest", "verification", "needs_configuration", "assets")
    }
    path = OUT / f"build-{result['manifest']['artifact_id']}.json"
    write_json(path, payload)
    return Path(result["bundle"])


def artifact_check(artifact: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [sys.executable, str(artifact / "run.py"), "--check"],
            cwd=str(artifact),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            timeout=180,
        )
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip()[:2000],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def artifact_evidence(artifact: Path) -> dict[str, Any]:
    manifest = read_json(artifact / "manifest.json")
    key_files = [
        "runtime/robot_vacuum_support/state.py",
        "runtime/robot_vacuum_support/diagnostic_scope.py",
        "runtime/robot_vacuum_support/actions.py",
        "runtime/modules/instances/runtime/server.py",
        "product_ui/index.html",
    ]
    hashes = {}
    for relative in key_files:
        path = artifact / relative
        hashes[relative] = _sha256_file(path) if path.is_file() else None
    matches = sorted(OUT.glob(f"build-{manifest['artifact_id']}.json"))
    build_info = matches[-1] if matches else OUT / "build.json"
    if not build_info.is_file():
        build_info = OUT / "build.json"
    return {
        "path": str(artifact),
        "artifact_id": manifest.get("artifact_id"),
        "manifest_sha256": _sha256_file(artifact / "manifest.json"),
        "content_hash": manifest.get("content_hash"),
        "runtime_hash": (manifest.get("runtime") or {}).get("hash"),
        "key_file_hashes": hashes,
        "build_record": str(build_info) if build_info.is_file() else None,
    }


# ---------------------------------------------------------------------------
# Evidence / state checks
# ---------------------------------------------------------------------------
def source_evidence(cases: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for case_id, case in cases.items():
        rows = []
        for item in case.get("evidence") or []:
            path = ROOT / str(item.get("file"))
            exists = path.is_file()
            file_hash = _sha256_file(path) if exists else None
            text = read_text_file(path) if exists and path.suffix.lower() != ".pdf" else ""
            quote = str(item.get("quote") or "")
            rows.append(
                {
                    "file": str(item.get("file")),
                    "locator": item.get("locator"),
                    "declared_sha256": item.get("sha256"),
                    "actual_sha256": file_hash,
                    "sha256_match": bool(exists and file_hash == item.get("sha256")),
                    "quote_sha256": sha_text(quote),
                    "quote_present": bool(exists and quote and norm_quote(quote) in norm_quote(text)),
                }
            )
        result[case_id] = rows
    return result


def state_snapshot(support_public: dict[str, Any] | None) -> dict[str, Any]:
    active = (support_public or {}).get("active_case") or {}
    return {
        "case_status": active.get("status"),
        "resolved": bool(active.get("status") == "resolved"),
        "closure_level": (active.get("closure_evidence") or {}).get("level"),
        "tried_actions": [
            {"action_id": item.get("action_id"), "outcome": item.get("outcome")}
            for item in active.get("tried_actions") or []
        ],
        "not_tried_actions": [
            item.get("action_id") for item in active.get("not_tried_actions") or []
        ],
        "observations": [
            {"capability": item.get("capability"), "result": item.get("result")}
            for item in active.get("observations") or []
        ],
    }


def state_checks(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    if "case_status" in expected:
        checks["case_status"] = actual.get("case_status") == expected["case_status"]
    if "resolved" in expected:
        checks["resolved"] = actual.get("resolved") is bool(expected["resolved"])
    tried_rows = [
        (str(item.get("action_id")), str(item.get("outcome")))
        for item in actual.get("tried_actions") or []
    ]
    not_tried = {str(item) for item in actual.get("not_tried_actions") or []}
    observations = {
        (str(item.get("capability")), str(item.get("result")))
        for item in actual.get("observations") or []
    }
    for index, expected_row in enumerate(expected.get("tried_actions_contains") or []):
        key = f"tried_contains[{index}]"
        checks[key] = (
            str(expected_row.get("action_id")),
            str(expected_row.get("outcome")),
        ) in tried_rows
    for index, action_id in enumerate(expected.get("tried_actions_absent") or []):
        checks[f"tried_absent[{index}]"] = str(action_id) not in {row[0] for row in tried_rows}
    for index, action_id in enumerate(expected.get("not_tried_actions_contains") or []):
        checks[f"not_tried_contains[{index}]"] = str(action_id) in not_tried
    for index, expected_row in enumerate(expected.get("observation_contains") or []):
        checks[f"observation_contains[{index}]"] = (
            str(expected_row.get("capability")),
            str(expected_row.get("result")),
        ) in observations
    for index, expected_row in enumerate(expected.get("observation_absent") or []):
        checks[f"observation_absent[{index}]"] = (
            str(expected_row.get("capability")),
            str(expected_row.get("result")),
        ) not in observations
    if "closure_level" in expected:
        checks["closure_level"] = actual.get("closure_level") == expected["closure_level"]
    return checks


def answer_and_retrieval(events: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    for event in reversed(events):
        if event.get("type") == "answer":
            return (
                str(event.get("answer") or ""),
                event.get("support"),
                event.get("retrieval"),
            )
        if event.get("type") == "error":
            raise RuntimeError(str(event.get("error") or "turn error"))
    raise RuntimeError("turn ended without an answer/error event")


def retrieval_attempted(retrieval: dict[str, Any] | None) -> bool:
    if not isinstance(retrieval, dict):
        return False
    if int(retrieval.get("attempts") or 0) > 0:
        return True
    if retrieval.get("records"):
        return True
    return str(retrieval.get("status") or "") in {"ok", "error"}


def primary_document_bound(retrieval: dict[str, Any] | None, primary_document: str) -> bool:
    if not isinstance(retrieval, dict):
        return False
    name = Path(primary_document).name
    for item in retrieval.get("results") or []:
        if isinstance(item, dict) and Path(str(item.get("source") or "")).name == name:
            return True
    return False


# ---------------------------------------------------------------------------
# Handoff summary checks
# ---------------------------------------------------------------------------
def get_handoff(base_url: str, session_id: str, method: str = "GET") -> dict[str, Any]:
    url = base_url + f"/api/sessions/{session_id}/handoff-summary"
    if method == "POST":
        return _post_json(url, {})
    return _http_json(url)


def state_file_hash(session_id: str) -> str:
    path = OUT / "instance-storage" / "sessions" / session_id / "support_state.json"
    return _sha256_file(path) if path.is_file() else ""


def summary_checks(
    case: dict[str, Any],
    payload: dict[str, Any],
    retrieval: dict[str, Any] | None,
    expected_case_id: str | None,
) -> dict[str, bool]:
    summary = str(payload.get("summary") or "")
    checks: dict[str, bool] = {
        "nonempty": bool(summary.strip()),
        "local_only": "未自动发送" in summary,
        "case_id_matches": payload.get("case_id") == expected_case_id,
    }
    if case.get("group") == "recharge_mainline":
        checks["mode_structured"] = payload.get("mode") == "structured_case"
        checks["has_current_problem"] = "当前问题" in summary
        checks["has_tried_section"] = "用户已执行的动作与结果" in summary
        checks["has_suggestions_section"] = "助手建议" in summary
        checks["has_sources_section"] = "来源（仅真实绑定记录）" in summary
        checks["has_message_refs"] = "用户第" in summary and "m-" in summary
        status = str((retrieval or {}).get("status") or "")
        if status != "ok":
            checks["no_unbound_sources"] = "不把未绑定文件名写成来源" in summary
    else:
        checks["mode_transcript"] = payload.get("mode") == "transcript_only"
        checks["has_question"] = "当前问题（用户原话）" in summary
        checks["has_answer"] = "最近回答" in summary
        checks["no_fault_claim"] = "没有形成回充故障案例" in summary
        checks["uncertainty"] = "不推断" in summary
    return checks


# ---------------------------------------------------------------------------
# Headless Chrome / CDP
# ---------------------------------------------------------------------------
class CDP:
    def __init__(self, ws_url: str):
        import websocket

        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.counter = 0

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.counter += 1
        call_id = self.counter
        self.ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == call_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method}: {message['error']}")
                return message.get("result") or {}

    def eval(self, expression: str, *, await_promise: bool = True) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            raise RuntimeError(str(details.get("text") or details))
        return (result.get("result") or {}).get("value")


def wait_js(cdp: CDP, expression: str, *, timeout: float = 20.0) -> Any:
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        try:
            last = cdp.eval(expression)
            if last:
                return last
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    raise RuntimeError(f"JS condition not met: {expression}; last={last!r}")


def chrome_binary() -> Path | None:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def start_chrome(chrome: Path, base_url: str, debug_port: int, profile: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={profile}",
            "--window-size=1280,900",
            base_url,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def chrome_target(debug_port: int, base_url: str) -> str:
    deadline = time.time() + 30
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{debug_port}/json/list", timeout=3
            ) as response:
                targets = json.load(response)
            for target in targets:
                if target.get("type") == "page" and str(target.get("url") or "").startswith(base_url):
                    return str(target["webSocketDebuggerUrl"])
            last = targets
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.3)
    raise RuntimeError(f"no Chrome page target for {base_url}: {last}")


def run_gui(base_url: str, recharge_sid: str, qa_sid: str, recharge_case_id: str) -> dict[str, Any]:
    chrome = chrome_binary()
    if chrome is None:
        return {"status": "not_run", "error": "headless Chrome/Edge not found"}
    gui_dir = OUT / "gui"
    gui_dir.mkdir(parents=True, exist_ok=True)
    for old in gui_dir.glob("*.txt"):
        old.unlink()
    profile = Path(tempfile.mkdtemp(prefix="rvs-chrome-"))
    debug_port = 9333
    if port_open("127.0.0.1", debug_port):
        raise RuntimeError(f"Chrome debug port {debug_port} is in use")
    process = start_chrome(chrome, base_url, debug_port, profile)
    cdp: CDP | None = None
    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {}
    try:
        cdp = CDP(chrome_target(debug_port, base_url))
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        wait_js(cdp, "document.readyState === 'complete'")
        wait_js(cdp, "document.querySelectorAll('#sessions li').length >= 5")

        cdp.eval(f"window.openSession({json.dumps(recharge_sid)})")
        wait_js(cdp, f"document.getElementById('support-case').textContent === {json.dumps(recharge_case_id)}")
        checks["selected_session_correct"] = cdp.eval(
            f"document.getElementById('support-case').textContent === {json.dumps(recharge_case_id)}"
        )
        checks["handoff_bar_visible"] = bool(
            cdp.eval("!!document.getElementById('handoff') && document.getElementById('handoff').offsetParent !== null")
        )

        cdp.eval("document.getElementById('handoff').click(); true")
        wait_js(
            cdp,
            "!document.getElementById('handoff-text').hidden && "
            "document.getElementById('handoff-text').textContent.length > 20 && "
            "document.getElementById('handoff-status').textContent.indexOf('已整理') >= 0",
            timeout=30,
        )
        summary_text = str(cdp.eval("document.getElementById('handoff-text').textContent") or "")
        checks["summary_visible"] = bool(summary_text.strip())
        checks["summary_has_current_problem"] = "当前问题" in summary_text
        checks["summary_has_tried_section"] = "用户已执行的动作与结果" in summary_text
        checks["summary_has_session"] = recharge_sid in summary_text
        evidence["summary_sha256"] = sha_text(summary_text)
        evidence["summary_excerpt"] = summary_text[:600]

        # Actual copy path.  Override clipboard and also listen to execCommand.
        cdp.eval(
            "window.__copied = null; window.__copiedEvent = null; "
            "document.addEventListener('copy', () => { window.__copiedEvent = document.getSelection().toString(); }); "
            "try { Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{writeText: async (t) => { window.__copied = t; }}}); } "
            "catch (e) { try { Object.defineProperty(Object.getPrototypeOf(navigator), 'clipboard', "
            "{configurable:true, get(){ return {writeText: async (t) => { window.__copied = t; }}; }}); } catch (e2) {} } true"
        )
        cdp.eval("document.getElementById('handoff-copy').click(); true")
        wait_js(
            cdp,
            "document.getElementById('handoff-status').textContent.indexOf('已复制') >= 0",
            timeout=10,
        )
        copied = cdp.eval("window.__copied || window.__copiedEvent || ''")
        checks["copy_called"] = bool(copied)
        checks["copy_matches_summary"] = bool(copied) and copied == summary_text
        evidence["copied_chars"] = len(copied or "")

        # Actual download path through a real browser download.
        download_dir = gui_dir / "download"
        shutil.rmtree(download_dir, ignore_errors=True)
        download_dir.mkdir(parents=True, exist_ok=True)
        try:
            cdp.call(
                "Browser.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": str(download_dir)},
            )
        except Exception:  # noqa: BLE001
            cdp.call(
                "Page.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": str(download_dir)},
            )
        cdp.eval("document.getElementById('handoff-download').click(); true")
        downloaded = None
        deadline = time.time() + 20
        while time.time() < deadline:
            files = [item for item in download_dir.glob("*.txt") if item.is_file()]
            if files:
                downloaded = files[0]
                break
            time.sleep(0.2)
        if downloaded is None:
            checks["download_file_saved"] = False
        else:
            content = downloaded.read_text(encoding="utf-8")
            checks["download_file_saved"] = True
            checks["download_utf8_matches_summary"] = content == summary_text
            checks["download_safe_filename"] = bool(
                re.fullmatch(r"handoff-[A-Za-z0-9_-]+\.txt", downloaded.name)
            )
            evidence["download_file"] = str(downloaded)
            evidence["download_chars"] = len(content)
            evidence["download_sha256"] = sha_text(content)
            shutil.copyfile(downloaded, gui_dir / "downloaded-summary.txt")

        screenshot = cdp.call("Page.captureScreenshot", {"format": "png"})
        (gui_dir / "product-ui.png").write_bytes(base64.b64decode(screenshot["data"]))

        # Error display + retry: point at a missing session without losing the
        # real selected session.
        cdp.eval(f"window.__savedSession = state.current; state.current = 's-missing-closed-loop'; true")
        cdp.eval("document.getElementById('handoff').click(); true")
        wait_js(
            cdp,
            "document.getElementById('handoff-status').textContent.indexOf('整理失败') >= 0",
            timeout=15,
        )
        checks["error_shown"] = "整理失败" in str(
            cdp.eval("document.getElementById('handoff-status').textContent")
        )
        checks["error_retry_enabled"] = not bool(cdp.eval("document.getElementById('handoff').disabled"))
        cdp.eval("state.current = window.__savedSession; true")
        cdp.eval(f"window.openSession({json.dumps(recharge_sid)})")
        wait_js(cdp, f"document.getElementById('support-case').textContent === {json.dumps(recharge_case_id)}")

        # Stale summary after session switch.
        cdp.eval("document.getElementById('handoff').click(); true")
        wait_js(
            cdp,
            "!document.getElementById('handoff-text').hidden && "
            "document.getElementById('handoff-status').textContent.indexOf('已整理') >= 0",
            timeout=30,
        )
        cdp.eval(f"window.openSession({json.dumps(qa_sid)})")
        wait_js(cdp, "document.getElementById('support').classList.contains('show') === false")
        checks["stale_cleared_on_switch"] = bool(
            cdp.eval("document.getElementById('handoff-text').hidden")
        )
        checks["copy_hidden_on_switch"] = bool(
            cdp.eval("document.getElementById('handoff-copy').hidden")
        )

        # Non-recharge Q&A still gets an actual summary (no recharge-only UI
        # assumption), then a send clears it before any stale text can remain.
        cdp.eval("document.getElementById('handoff').click(); true")
        wait_js(
            cdp,
            "!document.getElementById('handoff-text').hidden && "
            "document.getElementById('handoff-status').textContent.indexOf('已整理') >= 0",
            timeout=30,
        )
        qa_summary = str(cdp.eval("document.getElementById('handoff-text').textContent") or "")
        checks["qa_summary_visible"] = "当前问题（用户原话）" in qa_summary
        checks["qa_summary_no_fault_claim"] = "没有形成回充故障案例" in qa_summary
        evidence["qa_summary_excerpt"] = qa_summary[:500]
        cdp.eval("window.__sendPromise = window.sendMessage('谢谢', 'run', {prompt:'谢谢'}); true")
        checks["stale_cleared_on_send"] = bool(
            cdp.eval("document.getElementById('handoff-text').hidden")
        )
        wait_js(cdp, "state.busy === false", timeout=60)

        return {
            "status": "completed" if all(checks.values()) else "completed_with_failed_checks",
            "checks": checks,
            "evidence": evidence,
            "screenshot": str(gui_dir / "product-ui.png"),
            "download_dir": str(download_dir),
        }
    finally:
        if cdp is not None:
            cdp.close()
        if process.poll() is None:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=20,
                )
            else:
                process.terminate()
        try:
            process.wait(timeout=15)
        except Exception:  # noqa: BLE001
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, default=None)
    parser.add_argument("--build", action="store_true", help="build a new immutable artifact first")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if args.port != DEFAULT_PORT:
        raise SystemExit(f"this bounded smoke is frozen to port {DEFAULT_PORT}")
    if port_open("127.0.0.1", args.port):
        raise SystemExit(f"port {args.port} is already in use")

    existing_before = {str(port): port_open("127.0.0.1", port) for port in EXISTING_PORTS}
    if args.build:
        artifact = build_new_artifact()
    elif args.artifact:
        artifact = args.artifact.expanduser().resolve()
    else:
        artifact = build_new_artifact()
    if not (artifact / "run.py").is_file():
        raise SystemExit(f"artifact is missing run.py: {artifact}")

    cases_doc = read_json(CASES_PATH)
    by_id = {case["id"]: case for case in cases_doc.get("cases") or []}
    selected = [by_id[case_id] for case_id in SELECTED_CASES]
    total_turns = sum(len(case.get("turns") or []) for case in selected)
    if len(selected) > MAX_SESSIONS or total_turns > MAX_TURNS:
        raise SystemExit("smoke selection exceeds 6 sessions / 10 turns")

    evidence_by_case = source_evidence(by_id)
    checks = artifact_check(artifact)
    env_file = _choose_env_file()
    if env_file is None:
        write_json(
            OUT / "report.json",
            {
                "schema_version": REPORT_SCHEMA,
                "status": "needs_configuration",
                "detail": "no safe environment file candidate; credential values were not read",
                "artifact": artifact_evidence(artifact),
            },
        )
        return 2

    storage = OUT / "instance-storage"
    if storage.exists():
        raise SystemExit(f"isolated storage already exists; refusing to overwrite prior evidence: {storage}")
    instance = {
        "schema_version": 1,
        "instance_id": "robot-vacuum-product-closed-loop-v1",
        "artifact_id": (read_json(artifact / "manifest.json")).get("artifact_id"),
        "artifact_root": str(artifact),
        "name": "robot-vacuum product closed loop",
        "scenario": "robot_vacuum",
        "version": 4,
        "workflow": "agent",
        "host": "127.0.0.1",
        "port": args.port,
        "storage_root": str(storage),
        "environment_file": str(env_file),
        "asset_bindings": {},
    }
    instance_path = OUT / "instance.json"
    write_json(instance_path, instance)
    instance_public = {key: value for key, value in instance.items() if key != "environment_file"}

    freeze = {
        "schema_version": "product-closed-loop-freeze/v1",
        "frozen_at": now_iso(),
        "scope": "bounded recharge mainline product closed loop",
        "not_holdout": True,
        "not_success_rate": True,
        "selected_cases": list(SELECTED_CASES),
        "sessions": len(selected),
        "turns": total_turns,
        "max_sessions": MAX_SESSIONS,
        "max_turns": MAX_TURNS,
        "artifact": artifact_evidence(artifact),
        "artifact_check": checks,
        "source_evidence": evidence_by_case,
        "instance_public": instance_public,
        "environment_file_used": env_file.name,
        "environment_values_read_or_logged": False,
        "existing_ports_before": existing_before,
        "port": args.port,
        "no_external_ticket": True,
        "known_pending": "PDF retrieval miss remains out of scope; no RAG change in this slice.",
    }
    write_json(OUT / "freeze.json", freeze)

    server = LiveServer(
        python=sys.executable,
        serve_py=artifact / "serve.py",
        instance_path=instance_path,
        host="127.0.0.1",
        port=args.port,
        log_path=OUT / "server.log",
        cwd=artifact,
    )
    records: list[dict[str, Any]] = []
    summaries_before: dict[str, Any] = {}
    support_before: dict[str, Any] = {}
    health: dict[str, Any] = {}
    started = time.time()
    try:
        health = server.start(timeout=180)
        for case in selected:
            session = _post_json(server.base_url + "/api/sessions", {"workflow": "agent"})
            session_id = session["session_id"]
            record: dict[str, Any] = {
                "id": case["id"],
                "group": case.get("group"),
                "theme": case.get("theme"),
                "primary_document": case.get("primary_document"),
                "session_id": session_id,
                "turns": [],
                "source_evidence": evidence_by_case.get(case["id"]) or [],
            }
            turn_checks: list[bool] = []
            for index, turn in enumerate(case.get("turns") or [], start=1):
                events, retry = _send_with_one_retry(
                    server.base_url, session_id, str(turn.get("content") or ""), timeout=300
                )
                answer, support, retrieval = answer_and_retrieval(events)
                actual = state_snapshot(support)
                expect = (turn.get("expect") or {}).get("state") or {}
                turn_check_map = state_checks(expect, actual)
                if (turn.get("expect") or {}).get("answer_nonempty"):
                    turn_check_map["answer_nonempty"] = bool(answer.strip())
                if (turn.get("expect") or {}).get("retrieval_attempted"):
                    turn_check_map["retrieval_attempted"] = retrieval_attempted(retrieval)
                if (turn.get("expect") or {}).get("primary_document_evidence"):
                    turn_check_map["primary_document_evidence"] = primary_document_bound(
                        retrieval, str(case.get("primary_document") or "")
                    )
                failed = [key for key, ok in turn_check_map.items() if not ok]
                turn_checks.extend(turn_check_map.values())
                record["turns"].append(
                    {
                        "turn": index,
                        "question": str(turn.get("content") or ""),
                        "answer_chars": len(answer),
                        "answer_excerpt": answer[:500],
                        "retry": retry,
                        "event_types": [event.get("type") for event in events],
                        "retrieval": {
                            "status": (retrieval or {}).get("status"),
                            "attempts": (retrieval or {}).get("attempts"),
                            "sources": [
                                item.get("source")
                                for item in (retrieval or {}).get("results") or []
                                if isinstance(item, dict)
                            ],
                        },
                        "state": actual,
                        "checks": turn_check_map,
                        "failed_checks": failed,
                    }
                )
            record["checks_total"] = len(turn_checks)
            record["checks_passed"] = sum(1 for ok in turn_checks if ok)
            record["failed_checks"] = [
                check
                for turn in record["turns"]
                for check in turn.get("failed_checks") or []
            ]
            record["status"] = "passed_structural" if not record["failed_checks"] else "failed_structural"
            # Read-only summary before restart.
            before_hash = state_file_hash(session_id)
            get_payload = get_handoff(server.base_url, session_id, "GET")
            post_payload = get_handoff(server.base_url, session_id, "POST")
            after_hash = state_file_hash(session_id)
            support_get = _http_json(server.base_url + f"/api/sessions/{session_id}/support")
            support_active = (support_get.get("support") or {}).get("active_case") or {}
            expected_case_id = support_active.get("case_id")
            record["_actual_case_id"] = expected_case_id
            summary_check = summary_checks(case, get_payload, retrieval, expected_case_id)
            summary_check["get_post_same"] = get_payload.get("summary") == post_payload.get("summary")
            summary_check["state_read_only"] = before_hash == after_hash
            record["summary"] = {
                "mode": get_payload.get("mode"),
                "case_id": get_payload.get("case_id"),
                "sha256": sha_text(str(get_payload.get("summary") or "")),
                "excerpt": str(get_payload.get("summary") or "")[:800],
                "checks": summary_check,
            }
            record["support_sha256"] = sha_text(canonical(support_get.get("support")))
            records.append(record)
            summaries_before[session_id] = {
                "summary": str(get_payload.get("summary") or ""),
                "sha256": sha_text(str(get_payload.get("summary") or "")),
                "mode": get_payload.get("mode"),
                "case_id": get_payload.get("case_id"),
            }
            support_before[session_id] = record["support_sha256"]

        initial_sessions = _http_json(server.base_url + "/api/sessions").get("sessions") or []
        initial_ids = [item.get("session_id") for item in initial_sessions]
        isolation_checks = {
            "all_sessions_present": set(initial_ids) == set(summaries_before),
            "unique_session_ids": len(set(initial_ids)) == len(initial_ids),
            "unique_summaries": len({item["sha256"] for item in summaries_before.values()})
            == len(summaries_before),
            "state_files_separate": len({state_file_hash(sid) for sid in summaries_before}) == len(summaries_before),
        }

        # Restart with the same isolated storage.
        server.stop()
        restart_ready = not port_open("127.0.0.1", args.port)
        health_after = server.start(timeout=180)
        restart_sessions = _http_json(server.base_url + "/api/sessions").get("sessions") or []
        restart_ids = [item.get("session_id") for item in restart_sessions]
        restart_checks = {
            "port_released_before_restart": restart_ready,
            "session_list_preserved": set(restart_ids) == set(initial_ids),
            "health_ready": bool(health_after.get("ready")),
        }
        restart_details = {}
        for session_id in initial_ids:
            support_now = _http_json(server.base_url + f"/api/sessions/{session_id}/support")
            support_hash = sha_text(canonical(support_now.get("support")))
            handoff_now = get_handoff(server.base_url, session_id, "GET")
            summary_now = str(handoff_now.get("summary") or "")
            restart_checks[f"support_stable:{session_id}"] = support_hash == support_before.get(session_id)
            restart_checks[f"summary_stable:{session_id}"] = summary_now == summaries_before.get(session_id, {}).get("summary")
            restart_details[session_id] = {
                "support_sha256": support_hash,
                "summary_sha256": sha_text(summary_now),
                "mode": handoff_now.get("mode"),
            }

        # Continue after restart on the non-recharge session; "谢谢" is routed to
        # a deterministic protocol ack, so this does not need another model call.
        qa_sid = next(
            record["session_id"]
            for record in records
            if record["group"] != "recharge_mainline"
        )
        qa_events, qa_retry = _send_with_one_retry(
            server.base_url, qa_sid, "谢谢", timeout=120
        )
        qa_answer, qa_support, _qa_retrieval = answer_and_retrieval(qa_events)
        continue_checks = {
            "continue_after_restart_answer": bool(qa_answer.strip()),
            "continue_keeps_no_case": (qa_support or {}).get("active_case") is None,
            "continue_transport_retry_not_needed": not qa_retry.get("retried"),
        }

        # Resolved session remains viewable, can be ended/reopened through the
        # existing API, and a new session can still be created.
        resolved_sid = next(
            record["session_id"]
            for record in records
            if record["id"] == "rm-recharge-explicit-recovery"
        )
        delete = urllib.request.Request(
            server.base_url + f"/api/sessions/{resolved_sid}", method="DELETE"
        )
        with urllib.request.urlopen(delete, timeout=30) as response:
            delete_payload = json.load(response)
        session_after_end = _http_json(server.base_url + f"/api/sessions/{resolved_sid}")
        handoff_after_end = get_handoff(server.base_url, resolved_sid, "GET")
        reopen = _post_json(server.base_url + f"/api/sessions/{resolved_sid}/reopen", {})
        session_after_reopen = _http_json(server.base_url + f"/api/sessions/{resolved_sid}")
        new_session = _post_json(server.base_url + "/api/sessions", {"workflow": "agent"})
        new_support = _http_json(server.base_url + f"/api/sessions/{new_session['session_id']}/support")
        new_handoff = get_handoff(server.base_url, new_session["session_id"], "GET")
        lifecycle_checks = {
            "end_ok": delete_payload.get("status") == "ended",
            "ended_session_viewable": session_after_end.get("session_id") == resolved_sid,
            "ended_status": (session_after_end.get("session") or {}).get("status") == "ended"
            or session_after_end.get("status") == "ended",
            "ended_handoff_readable": bool(handoff_after_end.get("summary")),
            "reopen_ok": reopen.get("status") == "active",
            "reopened_viewable": session_after_reopen.get("session_id") == resolved_sid,
            "new_session_created": bool(new_session.get("session_id")),
            "new_session_isolated": (new_support.get("support") or {}).get("active_case") is None,
            "new_session_summary_transcript_only": new_handoff.get("mode") == "transcript_only",
            "resolved_session_viewable": (session_after_end.get("support") or {}).get("active_case", {}).get("status")
            == "resolved",
        }

        gui = run_gui(
            server.base_url,
            records[0]["session_id"],
            qa_sid,
            str(next(record for record in records if record["id"] == "rm-recharge-failed-outcome")["_actual_case_id"]),
        )
    finally:
        server.stop()
        released = not port_open("127.0.0.1", args.port)

    existing_after = {str(port): port_open("127.0.0.1", port) for port in EXISTING_PORTS}
    all_checks = {
        "artifact_check_ok": bool(checks.get("ok")),
        "port_released": released,
        "existing_services_untouched": existing_before == existing_after,
        "source_sha_and_quote": all(
            row.get("sha256_match") and row.get("quote_present")
            for case_id in SELECTED_CASES
            for row in evidence_by_case.get(case_id) or []
        ),
    }
    for record in records:
        all_checks[f"case:{record['id']}"] = record["status"] == "passed_structural"
        all_checks[f"case_summary:{record['id']}"] = all(record["summary"]["checks"].values())
    for name, value in isolation_checks.items():
        all_checks[f"isolation:{name}"] = bool(value)
    for name, value in restart_checks.items():
        all_checks[f"restart:{name}"] = bool(value)
    for name, value in continue_checks.items():
        all_checks[f"continue:{name}"] = bool(value)
    for name, value in lifecycle_checks.items():
        all_checks[f"lifecycle:{name}"] = bool(value)
    for name, value in (gui.get("checks") or {}).items():
        all_checks[f"gui:{name}"] = bool(value)

    failures = [name for name, ok in all_checks.items() if not ok]
    status = "completed" if not failures else "completed_with_failed_checks"
    report = {
        "schema_version": REPORT_SCHEMA,
        "generated_at": now_iso(),
        "status": status,
        "scope": "bounded recharge-mainline product closed loop; development smoke, not holdout, not a success rate",
        "not_holdout": True,
        "not_success_rate": True,
        "external_ticket_created": False,
        "started_at": started,
        "finished_at": time.time(),
        "artifact": artifact_evidence(artifact),
        "artifact_check": checks,
        "instance_public": instance_public,
        "health_before": health,
        "health_after_restart": health_after,
        "existing_ports_before": existing_before,
        "existing_ports_after": existing_after,
        "sessions_run": len(records),
        "turns_run": sum(len(record["turns"]) for record in records),
        "max_sessions": MAX_SESSIONS,
        "max_turns": MAX_TURNS,
        "cases": records,
        "isolation": isolation_checks,
        "restart": {"checks": restart_checks, "details": restart_details},
        "continue": continue_checks,
        "lifecycle": lifecycle_checks,
        "gui": gui,
        "checks": all_checks,
        "failures": failures,
        "known_limits": [
            "This is a 5-session/7-turn initial smoke plus restart/continue/GUI turns, not the full 12-case set and not a success rate.",
            "Answer semantics are not re-judged by an authoritative Judge; structural state checks and read-only summaries are the scored layer.",
            "PDF retrieval coverage remains a known pending RAG issue and was not changed in this slice.",
            "The UI headless run uses a test-time clipboard override; it does not verify OS clipboard content outside the browser context.",
        ],
    }
    write_json(OUT / "report.json", report)
    print(
        json.dumps(
            {
                "status": status,
                "artifact": str(artifact),
                "sessions": report["sessions_run"],
                "turns": report["turns_run"],
                "failures": failures,
                "report": str(OUT / "report.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
