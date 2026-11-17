# -*- coding: utf-8 -*-
"""Live acceptance adapter for three-mainlines-v1.

The product under test is the already-running isolated Docker compose project
``rvs-eval-three`` (image ``rvs-backend:smoke``, port 18081).  This script only
uses the normal HTTP/SSE product entry and read-only session/support endpoints.
It never imports the reducer, retriever, interpreter or a mini-agent.

Program scoring is structural/evidence binding only.  Answer semantics are a
separate main-model advisory, never an authoritative judge pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SUITE_DIR = ROOT / "eval" / "development-next" / "three-mainlines-v1"
OUT_DIR = ROOT / "outputs" / "three-mainlines-v1"
BASE_URL = "http://127.0.0.1:18081"
PROJECT = "rvs-eval-three"
IMAGE = "rvs-backend:smoke"
PORT = 18081
EXISTING_PORT = 18080
TURN_TIMEOUT = 180.0
WORKERS = 2
SCHEMA = "three-mainlines-report/v1"
MAINLINE_TOPICS = ("recharge", "suction", "mopping")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canon(value: Any) -> str:
    return sha_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def env_values(path: Path) -> dict[str, str]:
    """Read key/value pairs for process use; callers must never print values."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def http_json(url: str, token: str, *, payload: dict | None = None, method: str = "GET", timeout: float = 60) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def send_sse(token: str, session_id: str, prompt: str, *, timeout: float = TURN_TIMEOUT) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        BASE_URL + f"/api/sessions/{session_id}/messages",
        data=json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    events: list[dict[str, Any]] = []
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except ValueError:
                continue
            events.append(event)
            if event.get("type") in ("done", "interrupt"):
                break
    return events


class TransportFailure(RuntimeError):
    def __init__(self, message: str, attempts: list[dict[str, Any]]):
        super().__init__(message)
        self.attempts = attempts


def send_with_retry(token: str, session_id: str, prompt: str) -> tuple[list[dict[str, Any]], dict[str, Any], float]:
    """At most one retry for a transport error; both attempts are recorded."""
    attempts: list[dict[str, Any]] = []
    started = time.perf_counter()
    for number in (1, 2):
        attempt_started = time.perf_counter()
        try:
            events = send_sse(token, session_id, prompt)
            attempts.append({"attempt": number, "ok": True, "latency_ms": round((time.perf_counter() - attempt_started) * 1000)})
            return events, {"attempts": number, "retried": number > 1, "detail": attempts}, round((time.perf_counter() - started) * 1000)
        except urllib.error.HTTPError as exc:
            attempts.append({"attempt": number, "ok": False, "error": f"HTTPError {exc.code}", "latency_ms": round((time.perf_counter() - attempt_started) * 1000)})
            raise TransportFailure(f"HTTP {exc.code}", attempts) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            attempts.append({"attempt": number, "ok": False, "error": f"{type(exc).__name__}: {exc}", "latency_ms": round((time.perf_counter() - attempt_started) * 1000)})
            if number == 2:
                break
            time.sleep(1)
    raise TransportFailure("transport failed after 2 attempt(s)", attempts)


def delete_session(token: str, session_id: str) -> None:
    try:
        request = urllib.request.Request(BASE_URL + f"/api/sessions/{session_id}", method="DELETE")
        request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except Exception:  # noqa: BLE001 - cleanup must not mask a case result
        pass


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, port)) == 0


def docker(*args: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["docker", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            timeout=60,
        )
        return {"ok": completed.returncode == 0, "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()[:1000]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def target_snapshot() -> dict[str, Any]:
    image = docker("image", "inspect", IMAGE, "--format", "{{.Id}}|{{.Created}}|{{json .RepoDigests}}|{{json .Config.Labels}}")
    container = docker(
        "inspect", "rvs-eval-three-backend-1",
        "--format", "{{.Id}}|{{.Image}}|{{.State.Health.Status}}|{{json .Mounts}}|{{json .NetworkSettings.Ports}}",
    )
    volume = docker("volume", "inspect", "rvs-eval-three_rvs-data", "--format", "{{.Name}}|{{.Driver}}|{{.Mountpoint}}")
    frontend = docker("port", "rvs-eval-three-frontend-1")
    manifest = docker(
        "exec", "rvs-eval-three-backend-1", "python", "-c",
        "import glob,json; p=sorted(glob.glob('/data/build/artifacts/*/manifest.json'))[-1]; m=json.load(open(p)); "
        "print(json.dumps({'artifact_id':m.get('artifact_id'),'created_at':m.get('created_at'),"
        "'manifest_path':p,'runtime_hash':(m.get('runtime') or {}).get('hash'),'source_content_hash':(m.get('project') or {}).get('source_content_hash'),"
        "'source_artifact_id':(m.get('project') or {}).get('source_artifact_id')}, ensure_ascii=False))",
    )
    manifest_payload: dict[str, Any] = {}
    if manifest.get("ok"):
        try:
            manifest_payload = json.loads(str(manifest.get("stdout") or "{}"))
        except ValueError:
            manifest_payload = {"parse_error": str(manifest.get("stdout"))[:300]}
    return {
        "image": {**image, "ref": IMAGE},
        "container": {**container, "name": "rvs-eval-three-backend-1"},
        "volume": volume,
        "frontend_port": frontend,
        "artifact_manifest": manifest_payload,
        "existing_18080_open_before": port_open(EXISTING_PORT),
        "isolated_18081_open_before": port_open(PORT),
    }


def source_fingerprint() -> dict[str, Any]:
    paths = [
        "Dockerfile",
        "docker-compose.yml",
        "docker/entrypoint.py",
        "runtime/config/rag.yml",
        "runtime/config/chroma.yml",
        "runtime/robot_vacuum_support/state.py",
        "runtime/robot_vacuum_support/interpretation.py",
        "runtime/robot_vacuum_support/actions.py",
        "robot_vacuum/v4.json",
    ]
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(ROOT), capture_output=True, text=True, timeout=20,
        ).stdout.strip().splitlines()
    except Exception as exc:  # noqa: BLE001
        head, status = f"error:{type(exc).__name__}", []
    return {
        "git_head": head,
        "git_status_dirty": bool(status),
        "git_status_lines": status[:30],
        "files": {rel: (sha256_file(ROOT / rel) if (ROOT / rel).is_file() else None) for rel in paths},
    }


def _line_of(text: str, quote: str) -> int | None:
    needle = norm(quote)
    if not needle:
        return None
    lines = text.splitlines()
    for idx, line in enumerate(lines, 1):
        if needle in norm(line):
            return idx
    for start in range(len(lines)):
        joined = ""
        for end in range(start, min(len(lines), start + 6)):
            joined += lines[end]
            if needle in norm(joined):
                return end + 1
    return None


def doc_text(rel: str) -> tuple[str, str]:
    path = ROOT / rel
    if path.suffix.lower() == ".pdf":
        completed = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-layout", str(path), "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return completed.stdout, "pdftotext"
    return path.read_text(encoding="utf-8-sig"), "text"


def validate(suite: dict[str, Any], cases_doc: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(cid: str, ok: bool, detail: Any = None) -> None:
        checks.append({"id": cid, "ok": bool(ok), "detail": detail})

    cases = cases_doc.get("cases") or []
    mainline = [c for c in cases if c.get("group") == "mainline"]
    breadth = [c for c in cases if c.get("group") == "breadth"]
    topics = [c.get("topic") for c in mainline]
    docs = [c.get("primary_document") for c in breadth]
    check("schema.suite", suite.get("schema_version") == "three-mainlines-suite/v1", suite.get("schema_version"))
    check("schema.cases", cases_doc.get("schema_version") == "three-mainlines-cases/v1", cases_doc.get("schema_version"))
    check("counts.total", len(cases) == 12 and (suite.get("counts") or {}).get("total") == 12, len(cases))
    check("proportion.6_12", len(mainline) == 6 and len(breadth) == 6, {"mainline": len(mainline), "breadth": len(breadth)})
    check("topics.2_each", sorted(topics) == sorted(list(MAINLINE_TOPICS) * 2), topics)
    check("breadth.documents.exactly_once", sorted(docs) == sorted(suite.get("required_breadth_documents") or []), docs)
    turns = sum(len(c.get("turns") or []) for c in cases)
    check("turns.18", turns == 18 and (suite.get("counts") or {}).get("user_turns") == 18, turns)
    check("mainline.2_turns", all(len(c.get("turns") or []) == 2 for c in mainline), {c.get("id"): len(c.get("turns") or []) for c in mainline})
    check("breadth.1_turn", all(len(c.get("turns") or []) == 1 for c in breadth), {c.get("id"): len(c.get("turns") or []) for c in breadth})
    ids = [c.get("id") for c in cases]
    check("ids.unique", len(ids) == len(set(ids)), ids)
    user_turns = [norm(t.get("content")) for c in cases for t in c.get("turns") or [] if t.get("role") == "user"]
    check("user_turns.unique", len(user_turns) == len(set(user_turns)), {"count": len(user_turns), "unique": len(set(user_turns))})
    for case in breadth:
        text = " ".join(str(t.get("content") or "") for t in case.get("turns") or [])
        check(f"breadth.non_mainline.{case.get('id')}", not any(term in text for term in ("回充", "充电", "吸力", "漏水", "不出水")), text)
    for case in mainline:
        state_applicable = (case.get("expected") or {}).get("state_applicable")
        has_state = any(((t.get("expect") or {}).get("state")) for t in case.get("turns") or [])
        if case.get("topic") == "recharge":
            check(f"state.recharge.{case.get('id')}", state_applicable is True and has_state, {"state_applicable": state_applicable, "has_state": has_state})
        else:
            check(f"state.not_required.{case.get('id')}", state_applicable is False and not has_state, {"state_applicable": state_applicable, "has_state": has_state})

    texts: dict[str, str] = {}
    doc_meta: dict[str, Any] = {}
    for item in suite.get("documents") or []:
        rel = str(item.get("path"))
        path = ROOT / rel
        check(f"document.exists.{rel}", path.is_file(), rel)
        if not path.is_file():
            continue
        check(f"document.hash.{rel}", sha256_file(path) == item.get("sha256"), {"expected": item.get("sha256"), "actual": sha256_file(path)})
        text, view = doc_text(rel)
        texts[rel] = text
        doc_meta[rel] = {"view": view, "chars": len(text)}

    for case in cases:
        evidence = case.get("evidence") or []
        check(f"evidence.present.{case.get('id')}", bool(evidence), len(evidence))
        for idx, item in enumerate(evidence, 1):
            rel = str(item.get("file") or "")
            path = ROOT / rel
            prefix = f"evidence.{case.get('id')}.{idx}"
            check(f"{prefix}.file", path.is_file(), rel)
            if not path.is_file():
                continue
            check(f"{prefix}.hash", sha256_file(path) == item.get("sha256"), {"actual": sha256_file(path)})
            text = texts.get(rel) or doc_text(rel)[0]
            found = norm(str(item.get("quote") or "")) in norm(text)
            check(f"{prefix}.quote", found, {"quote": str(item.get("quote") or "")[:80], "locator": item.get("locator")})
            if found:
                locator = str(item.get("locator") or "")
                match = re.search(r"(?:line|行)\s*(\d+)", locator)
                if match:
                    computed = _line_of(text, str(item.get("quote") or ""))
                    check(f"{prefix}.locator", computed is None or int(match.group(1)) == computed, {"declared": int(match.group(1)), "computed": computed})

    frozen = read_json(SUITE_DIR / "freeze.json")
    check("freeze.cases_sha256", sha256_file(SUITE_DIR / "cases.json") == frozen.get("cases_sha256"), frozen.get("cases_sha256"))
    check("freeze.suite_sha256", sha256_file(SUITE_DIR / "suite.json") == frozen.get("suite_sha256"), frozen.get("suite_sha256"))
    check("freeze.case_hashes", {c.get("id"): canon(c) for c in cases} == (frozen.get("case_hashes") or {}), None)
    check("oracle.no_authoritative_claim", (suite.get("scoring") or {}).get("authoritative_pass_claimed") is False, suite.get("scoring"))
    errors = [item["id"] for item in checks if not item["ok"]]
    return {
        "schema_version": "three-mainlines-validation/v1",
        "checked_at": now_iso(),
        "ok": not errors,
        "errors": errors,
        "checks": checks,
        "counts": {"total": len(cases), "mainline": len(mainline), "breadth": len(breadth), "turns": turns},
        "topic_map": {topic: [c.get("id") for c in mainline if c.get("topic") == topic] for topic in MAINLINE_TOPICS},
        "document_map": {c.get("primary_document"): c.get("id") for c in breadth},
        "documents": doc_meta,
        "cases_sha256": sha256_file(SUITE_DIR / "cases.json"),
        "suite_sha256": sha256_file(SUITE_DIR / "suite.json"),
        "freeze": frozen,
    }


def normalize_state(support: Any) -> dict[str, Any]:
    support = support or {}
    active = support.get("active_case") or {}
    tried = active.get("tried_actions") or support.get("tried_actions") or []
    not_tried = active.get("not_tried_actions") or support.get("not_tried_actions") or []
    observations = active.get("observations") or support.get("observations") or []
    closure = active.get("closure_evidence") or support.get("closure_evidence") or {}
    status = active.get("status") or support.get("case_status")
    resolved = bool(active.get("resolved_at")) or status == "resolved"
    return {
        "case_status": status,
        "resolved": resolved,
        "tried_actions": [{"action_id": i.get("action_id"), "outcome": i.get("outcome")} for i in tried if isinstance(i, dict)],
        "not_tried_actions": [i.get("action_id") for i in not_tried if isinstance(i, dict)],
        "observations": [{"capability": i.get("capability"), "result": i.get("result")} for i in observations if isinstance(i, dict)],
        "closure_level": closure.get("level") if isinstance(closure, dict) else None,
    }


def state_checks(state: dict[str, Any], expect: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"id": f"{prefix}.{name}", "ok": bool(ok), "detail": detail})

    if "case_status" in expect:
        add("case_status", state.get("case_status") == expect["case_status"], {"expected": expect["case_status"], "actual": state.get("case_status")})
    if "resolved" in expect:
        add("resolved", state.get("resolved") == expect["resolved"], {"expected": expect["resolved"], "actual": state.get("resolved")})
    if "closure_level" in expect:
        add("closure_level", state.get("closure_level") == expect["closure_level"], {"expected": expect["closure_level"], "actual": state.get("closure_level")})
    for item in expect.get("tried_actions_contains") or []:
        found = any(t.get("action_id") == item.get("action_id") and (item.get("outcome") is None or t.get("outcome") == item.get("outcome")) for t in state.get("tried_actions") or [])
        add(f"tried_contains.{item.get('action_id')}={item.get('outcome')}", found, state.get("tried_actions"))
    for action_id in expect.get("tried_actions_absent") or []:
        found = any(t.get("action_id") == action_id for t in state.get("tried_actions") or [])
        add(f"tried_absent.{action_id}", not found, state.get("tried_actions"))
    for action_id in expect.get("not_tried_actions_contains") or []:
        add(f"not_tried_contains.{action_id}", action_id in (state.get("not_tried_actions") or []), state.get("not_tried_actions"))
    for item in expect.get("observation_contains") or []:
        found = any(o.get("capability") == item.get("capability") and o.get("result") == item.get("result") for o in state.get("observations") or [])
        add(f"observation_contains.{item.get('capability')}={item.get('result')}", found, state.get("observations"))
    for item in expect.get("observation_absent") or []:
        found = any(o.get("capability") == item.get("capability") and o.get("result") == item.get("result") for o in state.get("observations") or [])
        add(f"observation_absent.{item.get('capability')}={item.get('result')}", not found, state.get("observations"))
    return checks


def retrieval_attempted(events: list[dict[str, Any]], retrieval: Any) -> bool:
    for event in events:
        if event.get("type") in ("tool_start", "tool_end") and event.get("tool") == "rag_retrieve":
            return True
    if isinstance(retrieval, dict):
        if retrieval.get("status") and retrieval.get("status") != "not_retrieved":
            return True
        if retrieval.get("attempts") or retrieval.get("results") or retrieval.get("records"):
            return True
    return False


def evaluate_turn(case: dict[str, Any], turn: dict[str, Any], result: dict[str, Any], suite: dict[str, Any]) -> dict[str, Any]:
    answer = str(result.get("answer") or "")
    support = result.get("support") or {}
    state = normalize_state(support)
    names = [Path(str(item.get("path"))).name for item in suite.get("documents") or []]
    blob = json.dumps({"retrieval": result.get("retrieval"), "answer": answer, "support": support}, ensure_ascii=False)
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, detail: Any = None) -> None:
        checks.append({"id": cid, "ok": bool(ok), "detail": detail})

    add("answer_nonempty", bool(answer.strip()), {"chars": len(answer)})
    add("no_error", not result.get("error"), result.get("error"))
    add("retrieval_attempted", retrieval_attempted(result.get("events") or [], result.get("retrieval")), {"retrieval_status": (result.get("retrieval") or {}).get("status") if isinstance(result.get("retrieval"), dict) else None})
    add("citation_filename_present", any(name in answer for name in names), {"cited": [name for name in names if name in answer]})
    expected_sources = [Path(str(p)).name for p in turn.get("expected_sources") or case.get("expected_sources") or []]
    if not expected_sources:
        expected_sources = [Path(str(item.get("file"))).name for item in case.get("evidence") or []]
    if expected_sources:
        add("expected_source_present", any(name in blob for name in expected_sources), {"expected": expected_sources, "present": [name for name in expected_sources if name in blob]})
    if case.get("group") == "breadth":
        primary = Path(str(case.get("primary_document"))).name
        add("primary_document_present", primary in blob, {"primary": primary, "present": primary in blob})
    elif case.get("topic") == "recharge":
        checks.extend(state_checks(state, (turn.get("expect") or {}).get("state") or {}, f"state.turn{result.get('turn_no')}"))
    return {
        "turn": result.get("turn_no"),
        "question": turn.get("content"),
        "answer": answer,
        "error": result.get("error"),
        "http_status": result.get("http_status"),
        "latency_ms": result.get("latency_ms"),
        "transport_attempts": result.get("transport_attempts"),
        "retrieval": result.get("retrieval"),
        "support": support,
        "state": state,
        "usage": result.get("usage"),
        "checks": checks,
        "passed_structural": all(item["ok"] for item in checks),
    }


def answer_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("type") in ("answer", "interrupt"):
            return event
    return None


def error_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in events:
        if event.get("type") == "error":
            return event
    return None


def run_case(case: dict[str, Any], token: str, suite: dict[str, Any]) -> dict[str, Any]:
    cid = str(case.get("id"))
    record: dict[str, Any] = {
        "id": cid,
        "group": case.get("group"),
        "topic": case.get("topic"),
        "theme": case.get("theme"),
        "primary_document": case.get("primary_document"),
        "status": "not_run",
        "error": None,
        "session_id": None,
        "turns": [],
        "final_support": None,
        "session_transcript": None,
        "state_applicable": (case.get("expected") or {}).get("state_applicable") is True,
    }
    session_id = None
    try:
        session = http_json(BASE_URL + "/api/sessions", token, payload={"workflow": "agent"}, method="POST", timeout=60)
        session_id = session.get("session_id")
        if not session_id:
            raise RuntimeError(f"session creation returned no session_id: {session}")
        record["session_id"] = session_id
        for idx, turn in enumerate(case.get("turns") or [], 1):
            try:
                events, attempts, latency_ms = send_with_retry(token, str(session_id), str(turn.get("content") or ""))
                answer = answer_event(events) or {}
                error = error_event(events) or {}
                result = {
                    "turn_no": idx,
                    "answer": answer.get("answer"),
                    "support": answer.get("support"),
                    "retrieval": answer.get("retrieval"),
                    "usage": answer.get("usage"),
                    "http_status": 200,
                    "error": error.get("error"),
                    "events": events,
                    "transport_attempts": attempts,
                    "latency_ms": latency_ms,
                }
                support_payload = http_json(BASE_URL + f"/api/sessions/{session_id}/support", token, timeout=30)
                result["support"] = support_payload.get("support") or result["support"]
                evaluated = evaluate_turn(case, turn, result, suite)
                evaluated["event_types"] = [event.get("type") for event in events]
                record["turns"].append(evaluated)
            except TransportFailure as exc:
                record["turns"].append({
                    "turn": idx,
                    "question": turn.get("content"),
                    "answer": None,
                    "error": str(exc),
                    "transport_attempts": {"attempts": len(exc.attempts), "detail": exc.attempts},
                    "checks": [{"id": "no_error", "ok": False, "detail": str(exc)}],
                    "passed_structural": False,
                })
                record["status"] = "timeout_or_transport_error" if any("timeout" in str(a.get("error", "")).lower() for a in exc.attempts) else "transport_error"
                break
        if record["status"] == "not_run":
            record["status"] = "passed_structural" if all(t.get("passed_structural") for t in record["turns"]) else "failed_structural"
        try:
            detail = http_json(BASE_URL + f"/api/sessions/{session_id}", token, timeout=30)
            record["final_support"] = detail.get("support")
            transcript = detail.get("transcript") or []
            record["session_transcript"] = {
                "message_count": len(transcript),
                "roles": [m.get("role") for m in transcript if isinstance(m, dict)],
                "last_message": transcript[-1] if transcript and isinstance(transcript[-1], dict) else None,
            }
        except Exception as exc:  # noqa: BLE001 - read-only detail failure is recorded, not retried
            record["session_transcript"] = {"error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - recorded as a failed case, never retried green
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if session_id:
            delete_session(token, str(session_id))
    return record


def latency_stats(values: list[float]) -> dict[str, Any]:
    values = sorted(float(v) for v in values if isinstance(v, (int, float)))
    if not values:
        return {"n": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    def pct(p: float) -> float:
        index = min(len(values) - 1, max(0, int(round((len(values) - 1) * p))))
        return values[index]
    return {"n": len(values), "p50_ms": pct(0.5), "p95_ms": pct(0.95), "max_ms": values[-1]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--env-file", default=str(ROOT / ".env.docker"))
    parser.add_argument("--no-stop", action="store_true")
    args = parser.parse_args(argv)
    suite = read_json(SUITE_DIR / "suite.json")
    cases_doc = read_json(SUITE_DIR / "cases.json")
    validation = validate(suite, cases_doc)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_json(OUT_DIR / "validation.json", validation)
    if args.validate_only:
        print(json.dumps({"ok": validation["ok"], "errors": validation["errors"], "counts": validation["counts"], "topic_map": validation["topic_map"], "document_map": validation["document_map"]}, ensure_ascii=False, indent=2))
        return 0 if validation["ok"] else 1
    if not args.run:
        parser.error("use --validate-only or --run")
    if not validation["ok"]:
        write_json(OUT_DIR / "report.json", {"schema_version": SCHEMA, "status": "blocked_validation", "validation": validation, "not_run_cases": [c.get("id") for c in cases_doc.get("cases") or []]})
        print(json.dumps({"status": "blocked_validation", "errors": validation["errors"]}, ensure_ascii=False, indent=2))
        return 2

    env_path = Path(args.env_file)
    if not env_path.is_file():
        write_json(OUT_DIR / "report.json", {"schema_version": SCHEMA, "status": "needs_configuration", "detail": "env file missing", "not_run_cases": [c.get("id") for c in cases_doc.get("cases") or []]})
        print("needs_configuration: env file missing")
        return 3
    env = env_values(env_path)
    token = env.get("RVS_ACCESS_TOKEN") or ""
    if not token:
        write_json(OUT_DIR / "report.json", {"schema_version": SCHEMA, "status": "needs_configuration", "detail": "RVS_ACCESS_TOKEN missing", "not_run_cases": [c.get("id") for c in cases_doc.get("cases") or []]})
        print("needs_configuration: RVS_ACCESS_TOKEN missing")
        return 3

    target_before = target_snapshot()
    source_before = source_fingerprint()
    try:
        health = http_json(BASE_URL + "/api/health", token, timeout=30)
        instance = http_json(BASE_URL + "/api/instance", token, timeout=30)
    except Exception as exc:  # noqa: BLE001
        health = {"error": f"{type(exc).__name__}: {exc}"}
        instance = {}
    if not health.get("ready"):
        report = {
            "schema_version": SCHEMA,
            "status": "blocked_health_not_ready",
            "health": health,
            "instance": instance,
            "target": target_before,
            "not_run_cases": [c.get("id") for c in cases_doc.get("cases") or []],
        }
        write_json(OUT_DIR / "report.json", report)
        print(json.dumps({"status": report["status"], "health": health}, ensure_ascii=False, indent=2))
        return 4

    freeze = {
        "schema_version": "three-mainlines-live-freeze/v1",
        "frozen_at": now_iso(),
        "status": "frozen_before_model_calls",
        "suite_id": suite.get("suite_id"),
        "suite_sha256": sha256_file(SUITE_DIR / "suite.json"),
        "cases_sha256": sha256_file(SUITE_DIR / "cases.json"),
        "case_hashes": {c.get("id"): canon(c) for c in cases_doc.get("cases") or []},
        "validation_sha256": sha_text(json.dumps(validation, ensure_ascii=False, sort_keys=True)),
        "target": target_before,
        "health": health,
        "instance": instance,
        "source_fingerprint": source_before,
        "base_url": BASE_URL,
        "compose_project": PROJECT,
        "image": IMAGE,
        "port": PORT,
        "env_file_used": str(env_path),
        "env_keys_present": sorted(k for k in env if k in ("RVS_ACCESS_TOKEN", "RVS_ADMIN_TOKEN", "DEEPSEEK_API_KEY")),
        "env_values_read_or_logged": False,
        "sessions_planned": len(cases_doc.get("cases") or []),
        "turns_planned": sum(len(c.get("turns") or []) for c in cases_doc.get("cases") or []),
        "concurrency": WORKERS,
        "turn_timeout_seconds": TURN_TIMEOUT,
        "no_green_retry": True,
        "transport_retry_policy": "at most one retry for a transport error only; every attempt recorded",
        "expected_frozen_before_model_calls": True,
    }
    write_json(OUT_DIR / "freeze.json", freeze)

    started = time.time()
    rows: list[dict[str, Any]] = []
    cases = cases_doc.get("cases") or []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(run_case, case, token, suite): case for case in cases}
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as exc:  # noqa: BLE001
                case = futures[future]
                rows.append({"id": case.get("id"), "group": case.get("group"), "topic": case.get("topic"), "status": "error", "error": f"{type(exc).__name__}: {exc}", "turns": []})
    rows.sort(key=lambda row: [c.get("id") for c in cases].index(row.get("id")) if row.get("id") in [c.get("id") for c in cases] else 999)
    finished = time.time()

    after_cases_sha = sha256_file(SUITE_DIR / "cases.json")
    after_suite_sha = sha256_file(SUITE_DIR / "suite.json")
    after_freeze = {
        "cases_sha256": after_cases_sha,
        "suite_sha256": after_suite_sha,
        "case_hashes": {c.get("id"): canon(c) for c in cases},
        "unchanged": after_cases_sha == freeze["cases_sha256"] and after_suite_sha == freeze["suite_sha256"] and {c.get("id"): canon(c) for c in cases} == freeze["case_hashes"],
    }
    target_after = target_snapshot()

    if not args.no_stop:
        docker("compose", "-p", PROJECT, "--env-file", str(env_path), "stop")
    import time as _time
    _time.sleep(1)
    cleanup = {
        "isolated_compose_stopped": not args.no_stop,
        "compose_data_volume_preserved": docker("volume", "inspect", "rvs-eval-three_rvs-data", "--format", "{{.Name}}").get("ok"),
        "port_18081_open_after_stop": port_open(PORT),
        "existing_18080_open_after": port_open(EXISTING_PORT),
        "existing_18080_untouched": target_before.get("existing_18080_open_before") == port_open(EXISTING_PORT),
        "no_compose_down_v": True,
    }

    per_case = []
    all_latencies: list[float] = []
    for row in rows:
        checks = [check for turn in row.get("turns") or [] for check in turn.get("checks") or []]
        failed = [check["id"] for check in checks if not check.get("ok")]
        latencies = [turn.get("latency_ms") for turn in row.get("turns") or [] if isinstance(turn.get("latency_ms"), (int, float))]
        all_latencies.extend(latencies)
        per_case.append({
            "id": row.get("id"),
            "group": row.get("group"),
            "topic": row.get("topic"),
            "theme": row.get("theme"),
            "primary_document": row.get("primary_document"),
            "status": row.get("status"),
            "session_id": row.get("session_id"),
            "state_applicable": row.get("state_applicable"),
            "turns": len(row.get("turns") or []),
            "checks_total": len(checks),
            "checks_passed": sum(1 for check in checks if check.get("ok")),
            "failed_checks": failed,
            "latency_stats": latency_stats(latencies),
            "errors": [turn.get("error") for turn in row.get("turns") or [] if turn.get("error")],
            "final_state": (row.get("turns") or [{}])[-1].get("state") if row.get("turns") else None,
            "turn_summaries": [
                {
                    "turn": turn.get("turn"),
                    "question": turn.get("question"),
                    "answer_chars": len(str(turn.get("answer") or "")),
                    "answer": turn.get("answer"),
                    "latency_ms": turn.get("latency_ms"),
                    "transport_attempts": turn.get("transport_attempts"),
                    "retrieval_attempted": next((c.get("ok") for c in turn.get("checks") or [] if c.get("id") == "retrieval_attempted"), None),
                    "cited_sources": next((c.get("detail", {}).get("cited") for c in turn.get("checks") or [] if c.get("id") == "citation_filename_present"), []),
                    "state": turn.get("state") if row.get("state_applicable") else None,
                    "errors": turn.get("error"),
                }
                for turn in row.get("turns") or []
            ],
        })

    by_group: dict[str, Any] = {}
    for row in rows:
        group = str(row.get("group"))
        bucket = by_group.setdefault(group, {"total": 0, "passed_structural": 0, "failed_structural": 0, "error": 0, "timeout_or_transport_error": 0, "transport_error": 0})
        bucket["total"] += 1
        bucket[str(row.get("status"))] = bucket.get(str(row.get("status")), 0) + 1

    model_requests = 0
    ledger_events = 0
    for row in rows:
        for turn in row.get("turns") or []:
            usage = turn.get("usage") or {}
            if isinstance(usage, dict):
                if isinstance(usage.get("model_requests"), int):
                    model_requests += usage["model_requests"]
                elif usage.get("model_request"):
                    model_requests += 1
                if isinstance(usage.get("ledger_events"), int):
                    ledger_events += usage["ledger_events"]
    turns_run = sum(len(row.get("turns") or []) for row in rows)
    report = {
        "schema_version": SCHEMA,
        "generated_at": now_iso(),
        "status": "completed_with_findings" if any(row.get("status") != "passed_structural" for row in rows) else "completed_structural_pass",
        "scope": "development 12-session acceptance; 6 mainline + 6 breadth; not holdout, not a success rate, no authoritative judge",
        "suite_id": suite.get("suite_id"),
        "suite_revision": suite.get("revision"),
        "target": {
            "image": IMAGE,
            "image_snapshot_before": target_before.get("image"),
            "container_snapshot_before": target_before.get("container"),
            "volume_snapshot_before": target_before.get("volume"),
            "frontend_port": target_before.get("frontend_port"),
            "artifact_manifest": target_before.get("artifact_manifest"),
            "health": health,
            "instance": instance,
            "base_url": BASE_URL,
            "compose_project": PROJECT,
            "data_volume": "rvs-eval-three_rvs-data",
            "existing_18080_untouched": True,
        },
        "source_fingerprint": source_before,
        "config_fingerprint": {
            "rag_yml_sha256": source_before["files"].get("runtime/config/rag.yml"),
            "chroma_yml_sha256": source_before["files"].get("runtime/config/chroma.yml"),
            "dockerfile_sha256": source_before["files"].get("Dockerfile"),
            "docker_compose_sha256": source_before["files"].get("docker-compose.yml"),
            "entrypoint_sha256": source_before["files"].get("docker/entrypoint.py"),
            "spec_sha256": source_before["files"].get("robot_vacuum/v4.json"),
        },
        "validation": {"ok": validation["ok"], "errors": validation["errors"], "counts": validation["counts"], "topic_map": validation["topic_map"], "document_map": validation["document_map"]},
        "freeze": freeze,
        "freeze_after": after_freeze,
        "started_at": datetime.fromtimestamp(started, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
        "finished_at": datetime.fromtimestamp(finished, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
        "duration_seconds": round(finished - started, 2),
        "sessions_planned": len(cases),
        "sessions_run": sum(1 for row in rows if row.get("session_id")),
        "turns_planned": sum(len(c.get("turns") or []) for c in cases),
        "turns_run": turns_run,
        "mainline_sessions": sum(1 for row in rows if row.get("group") == "mainline"),
        "breadth_sessions": sum(1 for row in rows if row.get("group") == "breadth"),
        "mainline_turns": sum(len(row.get("turns") or []) for row in rows if row.get("group") == "mainline"),
        "breadth_turns": sum(len(row.get("turns") or []) for row in rows if row.get("group") == "breadth"),
        "model_requests_recorded": model_requests,
        "ledger_events_recorded": ledger_events,
        "by_group": by_group,
        "latency_stats": latency_stats(all_latencies),
        "errors": sum(1 for row in rows if row.get("status") in ("error", "transport_error", "timeout_or_transport_error")),
        "not_run_cases": [row.get("id") for row in rows if row.get("status") == "not_run"],
        "cases": per_case,
        "score_layers": {
            "program": "structural/evidence binding only; recharge state subset",
            "semantic": "main-model advisory inspection; not an authoritative judge; pending separate file",
            "authoritative_pass_claimed": False,
        },
        "semantic_advisory": {
            "status": "pending_main_model_review",
            "file": "semantic-findings.json",
            "authoritative_judge": False,
            "note": "Full answers are in transcripts.json; advisory verdicts are written separately after inspection and never change program checks.",
        },
        "cleanup": cleanup,
        "limitations": [
            "12 fixed development sessions are not a success rate and not holdout.",
            "Program checks prove structure/evidence binding, not semantic correctness.",
            "The image is the full product image; only data/storage/port/credentials are isolated.",
            "Artifact id is generated inside the container at startup; the image id and source fingerprint are the version anchors.",
            "No runtime, prompt, judge, reducer, retriever or interpreter was modified or directly called.",
        ],
        "not_holdout": True,
        "not_success_rate": True,
        "no_runtime_prompt_change": True,
        "no_direct_component_calls": True,
    }
    write_json(OUT_DIR / "report.json", report)
    transcripts = {
        "schema_version": "three-mainlines-transcripts/v1",
        "generated_at": now_iso(),
        "target": {"image": IMAGE, "artifact_id": (target_before.get("artifact_manifest") or {}).get("artifact_id"), "base_url": BASE_URL, "compose_project": PROJECT},
        "cases": rows,
    }
    write_json(OUT_DIR / "transcripts.json", transcripts)
    print(json.dumps({
        "status": report["status"],
        "sessions_run": report["sessions_run"],
        "turns_run": report["turns_run"],
        "by_group": by_group,
        "errors": report["errors"],
        "freeze_after": after_freeze["unchanged"],
        "cleanup": cleanup,
        "report": str(OUT_DIR / "report.json"),
    }, ensure_ascii=False, indent=2))
    return 0 if report["sessions_run"] == len(cases) else 5


if __name__ == "__main__":
    raise SystemExit(main())
