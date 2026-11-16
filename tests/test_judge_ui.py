# -*- coding: utf-8 -*-
"""One-shot headless GUI acceptance for the read-only Judge panel.

Uses a temporary store + temporary synthetic ``dsh-judge/v1`` results, a hidden
server on 127.0.0.1:8952, and headless Chrome.  The server and browser are
always terminated.  This is contract/UI verification only: the synthetic result
is not a real DSH 6-case run.

Run with::

    python -m pytest tests/test_judge_ui.py -q
"""
from __future__ import annotations

import base64
import json
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PORT = 8952
CASE_A = "rsv1-dev-boundary-unknown-model-price-01"
CASE_B = "rsv1-dev-boundary-unknown-model-price-02"


def free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def wait_json(url: str, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:  # noqa: BLE001
            last = error
            time.sleep(0.2)
    raise RuntimeError(f"HTTP not ready: {url}: {last}")


def payload(case_id: str, case_hash: str, run_id: str, *, status: str = "completed",
            verdict: str = "passed", judge_run_id: str = "judge-ui-synthetic") -> dict:
    return {
        "schema_version": "dsh-judge/v1",
        "case_id": case_id,
        "run_id": run_id,
        "case_hash": case_hash,
        "input_hash": "synthetic-ui-input-hash",
        "judge_run_id": judge_run_id,
        "status": status,
        "verdict": verdict,
        "roles": [
            {
                "role": "semantic_support",
                "verdict": verdict,
                "findings": [
                    {
                        "kind": "agent" if status == "completed" else "evaluation",
                        "severity": "medium",
                        "summary": "合成 Judge finding：UI 需要显示这条证据",
                        "evidence_refs": [
                            {"id": "fault-troubleshooting#1", "quote": "先清理充电触点，再尝试回充。"}
                        ],
                        "suggested_layer": "validation",
                    }
                ],
                "reason": "合成契约测试，不是真实 DSH 结果",
            }
        ],
        "arbitration": {"required": False, "reason": "合成角色一致"},
        "aggregation": {
            "source": "deterministic_issue_aggregation",
            "case_verdict": verdict,
            "coverage": {
                "complete": True,
                "missing_issue_ids": [],
                "unbound_issue_ids": [],
                "uncertain_issue_ids": [],
                "unresolved_issue_ids": [],
                "issues_total": 1,
                "issues_covered": 1,
            },
            "counts": {"issues_total": 1, "issues_judged": 1},
            "disputed_issue_ids": [],
            "unresolved_issue_ids": [],
            "issues": [],
        },
        "limitations": ["合成 UI 契约测试；真实 DSH 6 case 由另一任务运行"],
        "provenance": {
            "dsh_version": "synthetic-ui",
            "model": "none",
            "prompt_version": "judge-prompt/v2",
            "workflow_version": "dsh-judge-workflow/v2",
        },
        "created_at": "2026-11-14T12:00:00+08:00",
        "finished_at": "2026-11-14T12:00:00+08:00",
    }


@pytest.mark.skipif(not free(PORT), reason=f"port {PORT} is already in use")
def test_headless_judge_panel_placement_content_and_empty_state(tmp_path):
    pytest.importorskip("websocket")
    try:
        from eval.review_browser_acceptance import (  # noqa: PLC0415
            CDP,
            chrome_path,
            new_tab,
            start_chrome,
        )
    except Exception as error:  # pragma: no cover
        pytest.skip(f"browser harness unavailable: {error}")
    try:
        chrome_path()
    except Exception as error:  # pragma: no cover
        pytest.skip(f"Chrome/Edge not available: {error}")

    temp = tmp_path / "judge-ui"
    judge_root = temp / "judge_results"
    judge_root.mkdir(parents=True)
    store = temp / "reviews"
    log = (temp / "server.log").open("ab")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = [
        sys.executable,
        str(ROOT / "eval" / "review_server.py"),
        "--host", "127.0.0.1",
        "--port", str(PORT),
        "--store", str(store),
        "--judge-results", str(judge_root),
        "--log-file", str(temp / "server.log"),
        "--state-file", str(temp / "server_state.json"),
    ]
    server = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=log, creationflags=flags)
    chrome = None
    cdp = None
    checks: list[dict] = []
    try:
        health = wait_json(f"http://127.0.0.1:{PORT}/api/health")
        assert health["pid"] == server.pid, (health, server.pid)
        detail = wait_json(f"http://127.0.0.1:{PORT}/api/cases/{CASE_A}")
        run_id = detail["selectedRunId"] or detail["defaultRunId"]
        assert run_id
        result_path = judge_root / "judge-ui-synthetic" / f"{CASE_A}.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_text(
            json.dumps(payload(CASE_A, detail["caseHash"], run_id), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # A second synthetic error result for the other run proves the panel
        # never turns error into a pass.
        error_run = next(run["runId"] for run in detail["runs"] if run["runId"] != run_id)
        (judge_root / "judge-ui-error").mkdir(parents=True)
        (judge_root / "judge-ui-error" / f"{CASE_A}.json").write_text(
            json.dumps(
                payload(CASE_A, detail["caseHash"], error_run, status="error", verdict="passed",
                        judge_run_id="judge-ui-error"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        debug_port = int(probe.getsockname()[1])
        probe.close()
        chrome = start_chrome(temp / "chrome-profile", debug_port, temp / "chrome.log")
        wait_json(f"http://127.0.0.1:{debug_port}/json/version")
        url = f"http://127.0.0.1:{PORT}/#case={CASE_A}&scope=run_answer&run={run_id}"
        cdp = CDP(new_tab(debug_port, f"http://127.0.0.1:{PORT}/"))
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.wait_js("window.__reviewReady === true", timeout=40, label="review ready")
        # /json/new truncates the fragment, so navigate explicitly with the hash.
        cdp.navigate(url)
        cdp.wait_js("window.__reviewReady === true", timeout=40, label="review ready after deep link")
        cdp.wait_js(
            "window.__reviewSnapshot().selectedCaseId === " + json.dumps(CASE_A),
            timeout=25,
            label="deep linked case A",
        )
        cdp.wait_js(
            "window.__reviewSnapshot().judgeAvailable === true",
            timeout=25,
            label="judge available",
        )
        snapshot = cdp.eval("window.__reviewSnapshot()")
        assert snapshot["judgeVerdict"] == "passed", snapshot
        assert snapshot["judgeStatus"] == "completed", snapshot
        assert snapshot["judgeReviewTrust"] == "trusted", snapshot
        assert snapshot["judgeCoverage"]["complete"] is True, snapshot
        assert snapshot["judgePanelAfterRun"] is True, snapshot
        order = cdp.eval(
            "Array.from(document.querySelector('#main-column').children).map(e => e.id)"
        )
        assert order[0] == "run-panel" and order[1] == "judge-panel", order
        checks.append({"name": "actual_answer_first_and_judge_after_it", "ok": True, "order": order})
        checks.append({"name": "judge_panel_visible_current_run", "ok": True, "runId": run_id})

        panel_text = cdp.eval("document.querySelector('#judge-panel').innerText")
        for expected in ("Judge 独立建议", "总体判定", "通过", "semantic_support",
                         "先清理充电触点", "分歧仲裁", "版本与来源绑定",
                         "judge-ui-synthetic", "synthetic-ui",
                         "覆盖与完整性", "coverage 完整", "issues_total"):
            assert expected in panel_text, (expected, panel_text)
        assert "不写人工通过/不通过" in panel_text
        checks.append({"name": "judge_content_roles_evidence_arbitration_versions", "ok": True})

        # Error result must show an explicit not-pass banner.
        cdp.eval(
            "(() => { const el = document.querySelector('#run-select');"
            f"el.value = {json.dumps(error_run)};"
            "el.dispatchEvent(new Event('change', {bubbles: true})); return true; })()"
        )
        cdp.wait_js(
            "window.__reviewSnapshot().judgeStatus === 'error'",
            timeout=20,
            label="error judge result",
        )
        error_text = cdp.eval("document.querySelector('#judge-panel').innerText")
        assert "不足以判定" in error_text
        assert "不能当成通过" in error_text
        assert cdp.eval("!!document.querySelector('[data-judge-status=\"error\"]')") is True
        checks.append({"name": "error_status_not_shown_as_pass", "ok": True})

        # Back to the completed result, then switch to a case with no runs and
        # assert the explicit empty state (no fake pass).
        cdp.eval(
            "(() => { const el = document.querySelector('#run-select');"
            f"el.value = {json.dumps(run_id)};"
            "el.dispatchEvent(new Event('change', {bubbles: true})); return true; })()"
        )
        cdp.wait_js("window.__reviewSnapshot().judgeAvailable === true", timeout=20, label="back to pass")
        # Capture the first screen before scrolling: the actual-answer panel must
        # be what the reviewer sees first, with the Judge panel below it.
        cdp.eval("window.scrollTo(0, 0); true")
        time.sleep(0.3)
        top_shot = cdp.call("Page.captureScreenshot", {"format": "png"})
        (ROOT / "outputs" / "judge-ui-top.png").write_bytes(base64.b64decode(top_shot["data"]))
        checks.append({"name": "actual_answer_first_top_screenshot", "ok": True, "file": "outputs/judge-ui-top.png"})
        cdp.eval("document.querySelector('#judge-panel').scrollIntoView({block: 'start'}); true")
        time.sleep(0.3)
        screenshot = cdp.call("Page.captureScreenshot", {"format": "png"})
        (ROOT / "outputs" / "judge-ui.png").write_bytes(base64.b64decode(screenshot["data"]))
        checks.append({"name": "judge_panel_screenshot", "ok": True, "file": "outputs/judge-ui.png"})
        cdp.eval(f"window.location.hash = '#case={CASE_B}&scope=case_definition'; true")
        cdp.wait_js(
            "window.__reviewSnapshot().selectedCaseId === " + json.dumps(CASE_B),
            timeout=20,
            label="case B selected",
        )
        cdp.wait_js(
            "window.__reviewSnapshot().judgeAvailable === false",
            timeout=20,
            label="case B judge empty",
        )
        empty_text = cdp.eval("document.querySelector('[data-judge-empty=\"no-result\"]')?.innerText || ''")
        assert "暂无 Judge 建议" in empty_text, empty_text
        checks.append({"name": "clear_empty_state", "ok": True})

        assert cdp.eval("window.__reviewErrors.length") == 0
        assert not list((store / "reviews").glob("*.json")), "Judge panel must not save a human review"
        checks.append({"name": "no_js_error_no_human_write", "ok": True})
        (ROOT / "outputs" / "judge-ui-acceptance.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "simulatedContractVerification": True,
                    "note": "临时合成 dsh-judge/v1 结果；不是真实 DSH 6 case 运行，不代表模型质量。",
                    "port": PORT,
                    "serverPid": server.pid,
                    "caseId": CASE_A,
                    "runId": run_id,
                    "checks": checks,
                    "capturedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "screenshot": "outputs/judge-ui.png",
                    "topScreenshot": "outputs/judge-ui-top.png",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    finally:
        if cdp is not None:
            cdp.close()
        if chrome is not None:
            try:
                chrome.terminate()
                chrome.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    chrome.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            server.terminate()
            server.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                server.kill()
            except Exception:  # noqa: BLE001
                pass
        log.close()

@pytest.mark.skipif(not free(8953), reason="port 8953 is already in use")
def test_stale_judge_response_cannot_overwrite_newer_case(tmp_path):
    """Regression: a slow judge response for case B must not paint case A."""
    import base64  # noqa: F401
    pytest.importorskip("websocket")
    try:
        from eval.review_browser_acceptance import CDP, chrome_path, new_tab, start_chrome  # noqa: PLC0415
    except Exception as error:  # pragma: no cover
        pytest.skip(f"browser harness unavailable: {error}")
    try:
        chrome_path()
    except Exception as error:  # pragma: no cover
        pytest.skip(f"Chrome/Edge not available: {error}")

    port = 8953
    base = f"http://127.0.0.1:{port}"
    temp = tmp_path / "judge-race"
    judge_root = temp / "judge_results"
    judge_root.mkdir(parents=True)
    store = temp / "reviews"
    log = (temp / "server.log").open("ab")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = [
        sys.executable, str(ROOT / "eval" / "review_server.py"),
        "--host", "127.0.0.1", "--port", str(port), "--store", str(store),
        "--judge-results", str(judge_root), "--log-file", str(temp / "server.log"),
        "--state-file", str(temp / "server_state.json"),
    ]
    server = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=log, creationflags=flags)
    chrome = None
    cdp = None
    try:
        wait_json(f"{base}/api/health")
        detail_a = wait_json(f"{base}/api/cases/{CASE_A}")
        detail_b = wait_json(f"{base}/api/cases/{CASE_B}")
        run_a = detail_a["selectedRunId"] or detail_a["defaultRunId"]
        run_b = detail_b["selectedRunId"] or detail_b["defaultRunId"]
        assert run_a and run_b
        for case_id, run_id, judge_run_id, verdict, summary in (
            (CASE_A, run_a, "judge-race-a", "passed", "A_UNIQUE_PASS_SUMMARY"),
            (CASE_B, run_b, "judge-race-b", "failed", "B_UNIQUE_FAIL_SUMMARY"),
        ):
            path = judge_root / judge_run_id / f"{case_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            detail = detail_a if case_id == CASE_A else detail_b
            payload_data = payload(case_id, detail["caseHash"], run_id,
                                   verdict=verdict, judge_run_id=judge_run_id)
            payload_data["roles"][0]["findings"][0]["summary"] = summary
            path.write_text(json.dumps(payload_data, ensure_ascii=False), encoding="utf-8")

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        debug_port = int(probe.getsockname()[1])
        probe.close()
        chrome = start_chrome(temp / "chrome-profile", debug_port, temp / "chrome.log")
        wait_json(f"http://127.0.0.1:{debug_port}/json/version")
        cdp = CDP(new_tab(debug_port, f"{base}/"))
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.wait_js("window.__reviewReady === true", timeout=40, label="review ready")
        cdp.navigate(f"{base}/#case={CASE_A}&scope=run_answer&run={run_a}")
        cdp.wait_js("window.__reviewReady === true", timeout=40, label="review ready A")
        cdp.wait_js(
            "window.__reviewSnapshot().selectedCaseId === " + json.dumps(CASE_A)
            + " && window.__reviewSnapshot().judgeVerdict === 'passed'",
            timeout=30, label="case A pass judge",
        )
        cdp.eval(
            "(() => {"
            "window.__judgeRequests = [];"
            "const orig = window.fetch.bind(window);"
            "window.fetch = async (url, opts) => {"
            "  const u = String(url);"
            "  if (u.includes('/judge')) window.__judgeRequests.push(u);"
            f"  if (u.includes('/judge') && u.includes({json.dumps(CASE_B)}))"
            "    await new Promise((resolve) => setTimeout(resolve, 1800));"
            "  return orig(url, opts);"
            "}; return true; })()"
        )
        cdp.eval(f"window.location.hash = '#case={CASE_B}&scope=run_answer&run={run_b}'; true")
        cdp.wait_js(
            "window.__judgeRequests.some((u) => u.includes(" + json.dumps(CASE_B) + "))",
            timeout=25, label="slow B judge request started",
        )
        # Switch back to A while B's judge response is still deliberately slow.
        cdp.eval(f"window.location.hash = '#case={CASE_A}&scope=run_answer&run={run_a}'; true")
        cdp.wait_js(
            "window.__reviewSnapshot().selectedCaseId === " + json.dumps(CASE_A)
            + " && window.__reviewSnapshot().judgeVerdict === 'passed'",
            timeout=30, label="back to case A",
        )
        time.sleep(2.6)  # let the stale B judge response arrive and be dropped
        snapshot = cdp.eval("window.__reviewSnapshot()")
        assert snapshot["selectedCaseId"] == CASE_A, snapshot
        assert snapshot["judgeVerdict"] == "passed", snapshot
        assert snapshot["judgeSelectedRunId"] == run_a, snapshot
        panel_text = cdp.eval("document.querySelector('#judge-panel').innerText")
        assert "A_UNIQUE_PASS_SUMMARY" in panel_text, panel_text
        assert "B_UNIQUE_FAIL_SUMMARY" not in panel_text, panel_text
        assert cdp.eval("window.__reviewErrors.length") == 0
        (ROOT / "outputs" / "judge-race-acceptance.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "caseA": CASE_A, "caseB": CASE_B,
                    "selectedCaseId": snapshot["selectedCaseId"],
                    "judgeSelectedRunId": snapshot["judgeSelectedRunId"],
                    "staleResponseDropped": True,
                    "note": "临时合成 judge 结果；验证 loadSeq 旧响应隔离，不是真实 DSH 结果。",
                    "capturedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
                ensure_ascii=False, indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    finally:
        if cdp is not None:
            cdp.close()
        if chrome is not None:
            try:
                chrome.terminate(); chrome.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try: chrome.kill()
                except Exception: pass
        try:
            server.terminate(); server.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try: server.kill()
            except Exception: pass
        log.close()
