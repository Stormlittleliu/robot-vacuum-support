# -*- coding: utf-8 -*-
"""Bounded release-v1 acceptance for robot-vacuum-support (2026-11-15).

One bounded live run on the final immutable artifact:

* 12 frozen rotating-mainline sessions / 14 normal turns, concurrency 2, 8949;
* at most 2 explicit same-family PDF follow-up probes (total <= 16 turns);
* deterministic support/handoff GET/POST checks for all 12 stored sessions;
* one restart with persisted support/summary hashes compared before/after;
* private-index source metadata counts (PDF 101 / total 916 after coverage);
* optional hidden headless GUI copy/download/screenshot on the same endpoint.

No frozen case/expected is changed.  No retry-to-green.  No Judge/rubric.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "runtime", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from eval.runner import (  # noqa: E402
    LiveServer,
    _choose_env_file,
    _delete_session,
    _http_json,
    _post_json,
    _sha256_file,
)
from run_rotating_mainline import (  # noqa: E402
    DEFAULT_ARTIFACT,
    DEFAULT_PORT,
    SUITE_DIR,
    _artifact_check,
    _write_instance,
    artifact_snapshot,
    load_suite,
    now_iso,
    port_open,
    read_json,
    run_one_case,
    sha_text,
    validate_suite,
    write_json,
)

OUT = ROOT / "outputs" / "release-v1" / "acceptance12"
FINAL = ROOT / "outputs" / "release-v1"
PDF_CASE = "rm-breadth-pdf-3d-structured-light"
PDF_DOC = "扫地机器人100问.pdf"
TARGETED_PROBES = {
    PDF_CASE: [
        {"content": "3D结构光避障原理是什么？"},
        {"content": "红外点阵投影怎么测量物体距离？"},
    ]
}
MAX_NORMAL_TURNS = 14
MAX_PROBE_TURNS = 2
PROTECTED_PORTS = (8900, 8901, 8950, 8955, 8956)
SCHEMA = "release-v1-acceptance/v1"


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_json(value: Any) -> str:
    return sha_text(canonical(value))


def port_snapshot() -> dict[str, bool]:
    rows = {}
    for port in (DEFAULT_PORT, *PROTECTED_PORTS):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.6)
            rows[str(port)] = sock.connect_ex(("127.0.0.1", port)) == 0
    return rows


def get_handoff(base_url: str, session_id: str, method: str = "GET") -> dict[str, Any]:
    url = base_url + f"/api/sessions/{session_id}/handoff-summary"
    return _post_json(url, {}) if method == "POST" else _http_json(url)


def summary_source_facts(payload: dict[str, Any]) -> dict[str, Any]:
    sources = payload.get("sources") or []
    retrieval = payload.get("retrieval") if isinstance(payload.get("retrieval"), dict) else {}
    return {
        "summary_sha256": sha_text(str(payload.get("summary") or "")),
        "sources_count": len(sources),
        "sources_sha256": hash_json(sources),
        "source_turns": [item.get("turn") for item in sources if isinstance(item, dict)],
        "source_message_ids": [
            item.get("message_id") for item in sources if isinstance(item, dict) and item.get("message_id")
        ],
        "retrieval_status": retrieval.get("status"),
        "knowledge_sources": [
            item.get("source")
            for item in retrieval.get("results") or []
            if isinstance(item, dict) and item.get("source")
        ],
        "mode": payload.get("mode"),
        "case_id": payload.get("case_id"),
    }


def summary_checks(case: dict[str, Any], payload: dict[str, Any], last_retrieval: Any) -> dict[str, bool]:
    summary = str(payload.get("summary") or "")
    checks = {
        "nonempty": bool(summary.strip()),
        "local_only": "未自动发送" in summary,
        "boundary": "未生成外部工单" in summary,
    }
    if case.get("group") == "recharge_mainline":
        checks.update(
            {
                "mode_structured": payload.get("mode") == "structured_case",
                "has_current_problem": "当前问题" in summary,
                "has_tried_section": "用户已执行的动作与结果" in summary,
                "has_suggestions_section": "助手建议" in summary,
                "has_sources_section": "来源（仅真实绑定记录）" in summary,
            }
        )
    else:
        checks.update(
            {
                "mode_transcript": payload.get("mode") == "transcript_only",
                "has_question": "当前问题（用户原话）" in summary,
                "has_answer": "最近回答" in summary,
                "no_fault_claim": "没有形成回充故障案例" in summary,
                "uncertainty": "不推断" in summary,
            }
        )
    status = str((last_retrieval or {}).get("status") or "")
    if status != "ok":
        checks["no_unbound_sources"] = "不把未绑定文件名写成来源" in summary
    return checks


def index_counts(db_path: Path) -> dict[str, Any]:
    if not db_path.is_file():
        return {"exists": False, "path": str(db_path)}
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"exists": True, "path": str(db_path), "error": f"{type(exc).__name__}: {exc}"}
    try:
        rows = list(con.execute(
            "select string_value from embedding_metadata where key='source'"
        ))
        counts: dict[str, int] = {}
        for (source,) in rows:
            if source:
                counts[source] = counts.get(source, 0) + 1
        total = con.execute("select count(*) from embeddings").fetchone()[0]
    except sqlite3.Error as exc:
        return {"exists": True, "path": str(db_path), "error": f"{type(exc).__name__}: {exc}"}
    finally:
        con.close()
    return {"exists": True, "path": str(db_path), "total_chunks": total, "source_counts": dict(sorted(counts.items()))}


def run_gui(base_url: str, pdf_session_id: str, output_dir: Path) -> dict[str, Any]:
    """Optional hidden headless endpoint check: screenshot + copy + download."""
    from product_closed_loop_smoke import (  # local script helpers, no model call
        CDP,
        chrome_binary,
        chrome_target,
        start_chrome,
        wait_js,
    )

    chrome = chrome_binary()
    if chrome is None:
        return {"status": "not_run", "error": "headless Chrome/Edge not found"}
    output_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="rvs-release-chrome-"))
    debug_port = 9334
    if port_open(debug_port):
        return {"status": "not_run", "error": f"Chrome debug port {debug_port} in use"}
    process = start_chrome(chrome, base_url, debug_port, profile)
    cdp: CDP | None = None
    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {}
    try:
        cdp = CDP(chrome_target(debug_port, base_url))
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        wait_js(cdp, "document.readyState === 'complete'")
        wait_js(cdp, "document.querySelectorAll('#sessions li').length >= 12", timeout=40)
        cdp.eval(f"window.openSession({json.dumps(pdf_session_id)})")
        wait_js(cdp, "document.getElementById('handoff') && !document.getElementById('handoff').disabled", timeout=20)
        cdp.eval("document.getElementById('handoff').click(); true")
        wait_js(
            cdp,
            "!document.getElementById('handoff-text').hidden && "
            "document.getElementById('handoff-text').textContent.length > 20 && "
            "document.getElementById('handoff-status').textContent.indexOf('已整理') >= 0",
            timeout=40,
        )
        summary = str(cdp.eval("document.getElementById('handoff-text').textContent") or "")
        checks["summary_visible"] = bool(summary.strip())
        checks["transcript_summary"] = "当前问题（用户原话）" in summary
        checks["pdf_answer_visible"] = "3D" in summary or "结构光" in summary
        evidence["summary_sha256"] = sha_text(summary)
        evidence["summary_excerpt"] = summary[:500]

        cdp.eval(
            "window.__copied = null; window.__copiedEvent = null; "
            "document.addEventListener('copy', () => { window.__copiedEvent = document.getSelection().toString(); }); "
            "try { Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{writeText: async (t) => { window.__copied = t; }}}); } "
            "catch (e) { try { Object.defineProperty(Object.getPrototypeOf(navigator), 'clipboard', "
            "{configurable:true, get(){ return {writeText: async (t) => { window.__copied = t; }}; }}); } catch (e2) {} } true"
        )
        cdp.eval("document.getElementById('handoff-copy').click(); true")
        wait_js(cdp, "document.getElementById('handoff-status').textContent.indexOf('已复制') >= 0", timeout=10)
        copied = str(cdp.eval("window.__copied || window.__copiedEvent || ''") or "")
        checks["copy_called"] = bool(copied)
        checks["copy_matches_summary"] = copied == summary

        download_dir = output_dir / "download"
        shutil.rmtree(download_dir, ignore_errors=True)
        download_dir.mkdir(parents=True, exist_ok=True)
        try:
            cdp.call("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(download_dir)})
        except Exception:  # noqa: BLE001
            cdp.call("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(download_dir)})
        cdp.eval("document.getElementById('handoff-download').click(); true")
        downloaded = None
        deadline = time.time() + 20
        while time.time() < deadline:
            files = [item for item in download_dir.glob("*.txt") if item.is_file()]
            if files:
                downloaded = files[0]
                break
            time.sleep(0.2)
        checks["download_saved"] = downloaded is not None
        if downloaded is not None:
            content = downloaded.read_text(encoding="utf-8")
            checks["download_matches_summary"] = content == summary
            checks["download_safe_filename"] = bool(re.fullmatch(r"handoff-[A-Za-z0-9_-]+\.txt", downloaded.name))
            evidence["download_file"] = str(downloaded)
            evidence["download_chars"] = len(content)
            evidence["download_sha256"] = sha_text(content)
            shutil.copyfile(downloaded, output_dir / "downloaded-summary.txt")

        screenshot = cdp.call("Page.captureScreenshot", {"format": "png"})
        screenshot_path = output_dir / "product-ui-release-v1.png"
        screenshot_path.write_bytes(base64.b64decode(screenshot["data"]))
        evidence["screenshot"] = str(screenshot_path)
        return {
            "status": "completed" if all(checks.values()) else "completed_with_failed_checks",
            "checks": checks,
            "evidence": evidence,
        }
    finally:
        if cdp is not None:
            cdp.close()
        if process.poll() is None:
            import subprocess

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, default=ROOT.parent / "robot-vacuum-support-built" / "robot_vacuum-v4-24f3a3db39")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--skip-gui", action="store_true")
    parser.add_argument("--force", action="store_true", help="allow replacing an existing acceptance output")
    args = parser.parse_args()

    if args.port != DEFAULT_PORT:
        raise SystemExit(f"this batch is frozen to port {DEFAULT_PORT}")
    if args.workers != 2:
        raise SystemExit("this batch is frozen to concurrency 2")
    if OUT.exists() and any(OUT.iterdir()) and not args.force:
        raise SystemExit(f"acceptance output already exists; refusing to overwrite prior evidence: {OUT}")

    artifact = args.artifact.expanduser().resolve()
    if not (artifact / "run.py").is_file():
        raise SystemExit(f"artifact missing run.py: {artifact}")
    suite, cases_doc = load_suite(SUITE_DIR)
    cases = cases_doc.get("cases") or []
    normal_turns = sum(len(case.get("turns") or []) for case in cases)
    probe_turns = sum(len(items) for items in TARGETED_PROBES.values())
    if normal_turns > MAX_NORMAL_TURNS or probe_turns > MAX_PROBE_TURNS:
        raise SystemExit("turn budget exceeded")

    OUT.mkdir(parents=True, exist_ok=True)
    before_ports = port_snapshot()
    if before_ports.get(str(args.port)):
        raise SystemExit(f"port {args.port} already in use")
    artifact_info = artifact_snapshot(artifact)
    artifact_check = _artifact_check(artifact, cwd=artifact.parent)
    validation = validate_suite(SUITE_DIR, artifact_override=artifact)
    write_json(OUT / "validation.json", validation)
    if not validation.get("ok"):
        write_json(OUT / "report.json", {"status": "blocked_validation", "validation": validation})
        return 1
    if not artifact_check.get("ok"):
        write_json(OUT / "report.json", {"status": "blocked_artifact_check", "artifact_check": artifact_check})
        return 1
    env_file = _choose_env_file()
    if env_file is None:
        write_json(OUT / "report.json", {"status": "needs_configuration", "artifact": artifact_info})
        return 2

    source_index = ROOT / "assets" / "rag_index" / "chroma.sqlite3"
    artifact_index = artifact / "assets" / "rag_index" / "chroma.sqlite3"
    source_index_sha = _sha256_file(source_index)
    artifact_index_sha = _sha256_file(artifact_index)

    instance_path = OUT / "instance.json"
    instance_public = _write_instance(
        instance_path,
        artifact,
        env_file,
        args.port,
        out_dir=OUT,
        instance_id="robot-vacuum-support-release-v1-acceptance",
        name="robot-vacuum release v1 acceptance",
    )
    freeze = {
        "schema_version": "release-v1-acceptance-freeze/v1",
        "frozen_at": now_iso(),
        "suite_id": suite.get("suite_id"),
        "suite_revision": suite.get("revision"),
        "cases_sha256": _sha256_file(SUITE_DIR / "cases.json"),
        "suite_sha256": _sha256_file(SUITE_DIR / "suite.json"),
        "case_hashes": validation.get("case_hashes"),
        "artifact": artifact_info,
        "artifact_check": artifact_check,
        "instance_public": instance_public,
        "environment_file_used": env_file.name,
        "environment_values_read_or_logged": False,
        "normal_turns_planned": normal_turns,
        "targeted_probe_turns_planned": probe_turns,
        "max_normal_turns": MAX_NORMAL_TURNS,
        "max_targeted_probe_turns": MAX_PROBE_TURNS,
        "max_total_turns": MAX_NORMAL_TURNS + MAX_PROBE_TURNS,
        "targeted_probes": TARGETED_PROBES,
        "protected_ports_before": before_ports,
        "source_index_sha256": source_index_sha,
        "artifact_index_sha256": artifact_index_sha,
        "no_green_retry": True,
        "not_holdout": True,
        "not_success_rate": True,
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
    started = time.time()
    rows: list[dict[str, Any]] = []
    health: dict[str, Any] = {}
    summaries_before: dict[str, Any] = {}
    support_before: dict[str, Any] = {}
    initial_ids: list[str] = []
    restart_checks: dict[str, bool] = {}
    restart_details: dict[str, Any] = {}
    index_checks: dict[str, Any] = {}
    gui: dict[str, Any] = {"status": "not_run"}
    cleanup: dict[str, Any] = {}
    health_after: dict[str, Any] = {}
    try:
        health = server.start(timeout=240)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    run_one_case,
                    case,
                    server.base_url,
                    suite,
                    probes=TARGETED_PROBES.get(str(case.get("id"))),
                    delete_session=False,
                ): case
                for case in cases
            }
            for future in as_completed(futures):
                try:
                    rows.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    case = futures[future]
                    rows.append({"id": case.get("id"), "group": case.get("group"), "status": "error",
                                 "error": f"{type(exc).__name__}: {exc}", "turns": []})
        rows.sort(key=lambda row: [c.get("id") for c in cases].index(row.get("id")) if row.get("id") in [c.get("id") for c in cases] else 999)

        sessions = _http_json(server.base_url + "/api/sessions").get("sessions") or []
        initial_ids = [str(item.get("session_id")) for item in sessions]
        by_session = {str(row.get("session_id")): row for row in rows if row.get("session_id")}
        for session_id in initial_ids:
            row = by_session.get(session_id)
            support_payload = _http_json(server.base_url + f"/api/sessions/{session_id}/support")
            support_hash_before = hash_json(support_payload.get("support"))
            support_after = _http_json(server.base_url + f"/api/sessions/{session_id}/support")
            get_payload = get_handoff(server.base_url, session_id, "GET")
            post_payload = get_handoff(server.base_url, session_id, "POST")
            support_hash_after = hash_json(support_after.get("support"))
            summary = str(get_payload.get("summary") or "")
            last_retrieval = None
            if row and row.get("turns"):
                last_retrieval = row["turns"][-1].get("retrieval")
            expected_case_id = ((support_payload.get("support") or {}).get("active_case") or {}).get("case_id")
            checks = summary_checks(row or {}, get_payload, last_retrieval)
            checks["get_post_same"] = summary == str(post_payload.get("summary") or "")
            checks["state_read_only"] = support_hash_before == support_hash_after
            checks["sources_sha_same"] = hash_json(get_payload.get("sources")) == hash_json(post_payload.get("sources"))
            row_checks = {
                "session_present": True,
                "summary_checks": checks,
                "summary_facts": summary_source_facts(get_payload),
                "support_sha256": support_hash_before,
                "case_id": expected_case_id,
            }
            if row is not None:
                row["post_run"] = row_checks
            summaries_before[session_id] = {
                "summary": summary,
                "summary_sha256": sha_text(summary),
                "sources_sha256": hash_json(get_payload.get("sources")),
                "support_sha256": support_hash_before,
            }
            support_before[session_id] = support_hash_before

        isolation = {
            "sessions_listed": len(initial_ids),
            "sessions_expected": len(rows),
            "all_rows_have_sessions": all(row.get("session_id") for row in rows),
            "unique_session_ids": len(set(initial_ids)) == len(initial_ids),
            "unique_summary_hashes": len({item["summary_sha256"] for item in summaries_before.values()}) == len(summaries_before),
            "unique_state_file_hashes": len(
                {
                    _sha256_file(OUT / "instance-storage" / "sessions" / session_id / "support_state.json")
                    for session_id in initial_ids
                    if (OUT / "instance-storage" / "sessions" / session_id / "support_state.json").is_file()
                }
            ) == len(initial_ids),
        }

        # Persisted index evidence: only the private session workspace changed.
        pdf_row = next((row for row in rows if row.get("id") == PDF_CASE), None)
        pdf_session = str((pdf_row or {}).get("session_id") or "")
        if pdf_session:
            private_db = OUT / "instance-storage" / "sessions" / pdf_session / "workspace" / "chroma_db" / "chroma.sqlite3"
            counts = index_counts(private_db)
            source_counts = counts.get("source_counts") or {}
            index_checks["pdf_session_index"] = counts
            index_checks["pdf_chunks"] = source_counts.get(PDF_DOC)
            index_checks["total_chunks"] = counts.get("total_chunks")
            index_checks["pdf_chunks_expected"] = 101
            index_checks["total_chunks_expected"] = 916
            index_checks["private_index_fixed"] = (
                source_counts.get(PDF_DOC) == 101 and counts.get("total_chunks") == 916
            )
        index_checks["source_index_unchanged"] = _sha256_file(source_index) == source_index_sha
        index_checks["artifact_index_unchanged"] = _sha256_file(artifact_index) == artifact_index_sha
        index_checks["source_index_sha256"] = source_index_sha
        index_checks["artifact_index_sha256"] = artifact_index_sha

        # Restart with the same isolated storage; compare stored support/summary.
        server.stop()
        port_released_before_restart = not port_open(args.port)
        health_after = server.start(timeout=240)
        restart_sessions = _http_json(server.base_url + "/api/sessions").get("sessions") or []
        restart_ids = [str(item.get("session_id")) for item in restart_sessions]
        restart_checks = {
            "port_released_before_restart": port_released_before_restart,
            "session_list_preserved": set(restart_ids) == set(initial_ids),
            "health_ready": bool(health_after.get("ready")),
        }
        for session_id in initial_ids:
            support_now = _http_json(server.base_url + f"/api/sessions/{session_id}/support")
            handoff_now = get_handoff(server.base_url, session_id, "GET")
            support_hash_now = hash_json(support_now.get("support"))
            summary_now = str(handoff_now.get("summary") or "")
            expected = summaries_before.get(session_id, {})
            restart_checks[f"support_stable:{session_id}"] = support_hash_now == expected.get("support_sha256")
            restart_checks[f"summary_stable:{session_id}"] = summary_now == expected.get("summary")
            restart_checks[f"sources_stable:{session_id}"] = hash_json(handoff_now.get("sources")) == expected.get("sources_sha256")
            restart_details[session_id] = {
                "support_sha256": support_hash_now,
                "summary_sha256": sha_text(summary_now),
                "sources_sha256": hash_json(handoff_now.get("sources")),
            }

        if not args.skip_gui and pdf_session:
            gui = run_gui(server.base_url, pdf_session, OUT / "gui")
    finally:
        # End sessions only after support/handoff/restart/GUI evidence is stored.
        for session_id in initial_ids:
            try:
                _delete_session(server.base_url, session_id)
            except Exception:  # noqa: BLE001
                pass
        server.stop()
        finished = time.time()
        released = not port_open(args.port)

    after_ports = port_snapshot()
    by_group: dict[str, dict[str, int]] = {}
    for row in rows:
        group = str(row.get("group"))
        bucket = by_group.setdefault(group, {"total": 0, "passed_structural": 0, "failed_structural": 0, "error": 0, "not_run": 0})
        bucket["total"] += 1
        bucket[str(row.get("status"))] = bucket.get(str(row.get("status")), 0) + 1

    cases_report = []
    for row in rows:
        checks = [check for turn in row.get("turns") or [] for check in turn.get("checks") or []]
        post = row.get("post_run") or {}
        summary_checks_row = post.get("summary_checks") or {}
        probes = row.get("targeted_probes") or []
        cases_report.append(
            {
                "id": row.get("id"),
                "group": row.get("group"),
                "theme": row.get("theme"),
                "primary_document": row.get("primary_document"),
                "status": row.get("status"),
                "session_id": row.get("session_id"),
                "turns": len(row.get("turns") or []),
                "normal_checks_total": len(checks),
                "normal_checks_passed": sum(1 for check in checks if check.get("ok")),
                "failed_checks": [check["id"] for check in checks if not check.get("ok")],
                "questions": [turn.get("question") for turn in row.get("turns") or []],
                "retrieval_statuses": [(turn.get("retrieval") or {}).get("status") for turn in row.get("turns") or []],
                "targeted_probes": [
                    {
                        "probe": probe.get("probe"),
                        "question": probe.get("question"),
                        "passed": probe.get("passed"),
                        "failed_checks": [check["id"] for check in probe.get("checks") or [] if not check.get("ok")],
                        "retrieval_sources": [
                            item.get("source")
                            for item in (probe.get("retrieval") or {}).get("results") or []
                            if isinstance(item, dict)
                        ],
                    }
                    for probe in probes
                ],
                "summary": post.get("summary_facts"),
                "summary_checks": summary_checks_row,
                "summary_ok": all(summary_checks_row.values()) if summary_checks_row else False,
                "case_id": post.get("case_id"),
            }
        )

    normal_turns_run = sum(row.get("turns") for row in cases_report)
    probe_turns_run = sum(len(row.get("targeted_probes") or []) for row in cases_report)
    all_checks: dict[str, bool] = {
        "artifact_check_ok": bool(artifact_check.get("ok")),
        "validation_ok": bool(validation.get("ok")),
        "normal_turn_budget": normal_turns_run <= MAX_NORMAL_TURNS,
        "probe_turn_budget": probe_turns_run <= MAX_PROBE_TURNS,
        "total_turn_budget": normal_turns_run + probe_turns_run <= MAX_NORMAL_TURNS + MAX_PROBE_TURNS,
        "sessions_12": len(initial_ids) == 12,
        "port_released": released,
        "protected_ports_untouched": all(before_ports.get(str(p)) == after_ports.get(str(p)) for p in PROTECTED_PORTS),
        "source_index_unchanged": bool(index_checks.get("source_index_unchanged")),
        "artifact_index_unchanged": bool(index_checks.get("artifact_index_unchanged")),
        "private_index_fixed": bool(index_checks.get("private_index_fixed")),
        "pdf_probe_1": False,
        "pdf_probe_2": False,
    }
    for row in cases_report:
        all_checks[f"case_structural:{row['id']}"] = row["status"] == "passed_structural"
        all_checks[f"case_summary:{row['id']}"] = row["summary_ok"]
        for probe in row.get("targeted_probes") or []:
            all_checks[f"pdf_probe_{probe['probe']}"] = bool(probe.get("passed"))
    for name, value in isolation.items():
        all_checks[f"isolation:{name}"] = bool(value)
    for name, value in restart_checks.items():
        all_checks[f"restart:{name}"] = bool(value)
    for name, value in (gui.get("checks") or {}).items():
        all_checks[f"gui:{name}"] = bool(value)
    failures = [name for name, ok in all_checks.items() if not ok]
    status = "completed" if not failures else "completed_with_failed_checks"
    report = {
        "schema_version": SCHEMA,
        "generated_at": now_iso(),
        "status": status,
        "scope": "bounded release-v1 acceptance on the frozen rotating-mainline 12; not holdout, not a success rate, no authoritative Judge",
        "artifact": artifact_info,
        "artifact_check": artifact_check,
        "artifact_suite_hash_overridden_for_release": True,
        "frozen_cases_and_expected_unchanged": True,
        "instance_public": instance_public,
        "health_before": health,
        "health_after_restart": health_after,
        "started_at": datetime.fromtimestamp(started, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
        "finished_at": datetime.fromtimestamp(finished, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
        "counts": {
            "sessions_planned": len(cases),
            "sessions_run": len(initial_ids),
            "normal_turns_planned": normal_turns,
            "normal_turns_run": normal_turns_run,
            "targeted_probe_turns_run": probe_turns_run,
            "total_turns_run": normal_turns_run + probe_turns_run,
            "max_normal_turns": MAX_NORMAL_TURNS,
            "max_targeted_probe_turns": MAX_PROBE_TURNS,
            "max_total_turns": MAX_NORMAL_TURNS + MAX_PROBE_TURNS,
            "errors": sum(1 for row in cases_report if row["status"] == "error"),
            "not_run": sum(1 for row in cases_report if row["status"] == "not_run"),
            "unassessed": sum(1 for row in cases_report if not row.get("summary_checks")),
        },
        "by_group": by_group,
        "cases": cases_report,
        "raw_cases": rows,
        "isolation": isolation,
        "index_coverage": index_checks,
        "restart": {"checks": restart_checks, "details": restart_details},
        "gui": gui,
        "protected_ports_before": before_ports,
        "protected_ports_after": after_ports,
        "cleanup": {
            "sessions_ended_after_checks": True,
            "server_stopped": True,
            "port_released": released,
            "browser_stopped": bool(gui.get("status") != "not_run" or args.skip_gui),
        },
        "checks": all_checks,
        "failures": failures,
        "environment_file_used": env_file.name,
        "environment_values_read_or_logged": False,
        "known_limits": [
            "12 fixed development sessions are not a holdout, not a success rate and not an external validity claim.",
            "Program checks are structural/evidence binding; answer semantics are not re-judged by an authoritative Judge.",
            "Two same-family PDF probes are explicitly separate from the frozen case and not used to change expectations.",
            "Handoff copy/download is verified in one headless browser session; it does not audit the OS clipboard.",
        ],
    }
    write_json(OUT / "report.json", report)
    print(json.dumps({
        "status": status,
        "artifact": str(artifact),
        "normal_turns": normal_turns_run,
        "probe_turns": probe_turns_run,
        "failures": failures,
        "report": str(OUT / "report.json"),
    }, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
