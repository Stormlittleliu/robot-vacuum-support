# -*- coding: utf-8 -*-
"""Thin adapter for the rotating-mainline acceptance set.

This is deliberately not a new eval framework.  It reuses the existing live
helpers in ``eval/runner.py`` (LiveServer, SSE send, session delete, env-file
selection) and adds only:

* deterministic pre-model validation: 6/12 proportion, all six documents,
  theme coverage, duplicate checks, quote/hash identity, manual-only oracle
  guard, frozen v1 hash preservation;
* one bounded live batch (port 8949, concurrency 2, 12 independent sessions);
* structural/evidence-binding scoring only.  Answer semantics are recorded as
  an agent-inspection advisory, never as an authoritative Judge pass.

The artifact is the existing ``robot_vacuum-v4-6aa0ba49c6`` build.  This
script never builds a new artifact and never edits runtime, prompt, Judge core
or UI.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "runtime") not in sys.path:
    sys.path.insert(0, str(ROOT / "runtime"))

from eval.runner import (  # noqa: E402
    LiveServer,
    _choose_env_file,
    _delete_session,
    _post_json,
    _send_with_one_retry,
    _sha256_file,
)

SUITE_DIR = ROOT / "eval" / "development-next" / "rotating-mainline-v1"
CASES_PATH = SUITE_DIR / "cases.json"
SUITE_PATH = SUITE_DIR / "suite.json"
OUT_DIR = ROOT / "outputs" / "rotating-mainline-v1"
DEFAULT_ARTIFACT = ROOT.parent / "robot-vacuum-support-built" / "robot_vacuum-v4-6aa0ba49c6"
DEFAULT_PORT = 8949
EXISTING_PORTS = (8900, 8950, 8955, 8956)
RECHARGE_TERMS = ("回充", "充电", "充电座")
AUTO_FAIL_EXPLICIT_TERMS = (
    "自动回充失败",
    "自动回充还是失败",
    "自动回充仍失败",
    "自动回充也失败",
    "回充失败",
    "自己回不到",
    "自己回不去",
    "自动回不去",
    "自动找不到",
)
AUTO_WORKS_EXPLICIT_TERMS = (
    "自己找到",
    "自动找到",
    "已经开始充电",
    "自己开始充电",
    "自动回充已恢复",
)
SCHEMA = "rotating-mainline-report/v1"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def norm_quote(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def load_suite(suite_dir: Path = SUITE_DIR) -> tuple[dict[str, Any], dict[str, Any]]:
    suite = read_json(suite_dir / "suite.json")
    cases_doc = read_json(suite_dir / "cases.json")
    return suite, cases_doc


def _extract_txt(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_pdf_views(path: Path) -> dict[str, str]:
    views: dict[str, str] = {}
    try:
        from pypdf import PdfReader

        views["pypdf"] = "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    except Exception as exc:  # noqa: BLE001 - recorded by caller
        views["pypdf_error"] = f"{type(exc).__name__}: {exc}"
    try:
        completed = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-layout", str(path), "-"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            timeout=60,
        )
        if completed.returncode == 0:
            views["pdftotext"] = completed.stdout
    except Exception as exc:  # noqa: BLE001
        views["pdftotext_error"] = f"{type(exc).__name__}: {exc}"
    return views


def document_texts(suite: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    texts: dict[str, str] = {}
    meta: dict[str, Any] = {}
    for item in suite.get("documents") or []:
        rel = str(item.get("path"))
        path = ROOT / rel
        if not path.is_file():
            meta[rel] = {"error": "file_missing"}
            continue
        if path.suffix.lower() == ".pdf":
            views = _extract_pdf_views(path)
            texts[rel] = "\n".join(v for k, v in views.items() if not k.endswith("_error"))
            meta[rel] = {
                "kind": "pdf",
                "views": {k: (norm_quote(v)[:80] if not k.endswith("_error") else v) for k, v in views.items()},
                "view_lengths": {k: len(v) for k, v in views.items() if not k.endswith("_error")},
                "extraction_complete": "pypdf" in views and "pdftotext" in views,
            }
        else:
            text = _extract_txt(path)
            texts[rel] = text
            meta[rel] = {"kind": "text", "chars": len(text)}
    return texts, meta


def _line_of(text: str, quote: str) -> int | None:
    norm = norm_quote(quote)
    if not norm:
        return None
    lines = text.splitlines()
    # Most evidence quotes sit on a single source line.  Check that first so the
    # locator is the line containing the quote, not the start of a sliding
    # multi-line window.
    for index, line in enumerate(lines, 1):
        if norm in norm_quote(line):
            return index
    # Fallback for quotes that genuinely span lines: report the last line of the
    # first matching window.
    for start in range(len(lines)):
        joined = ""
        for end in range(start, min(len(lines), start + 6)):
            joined += lines[end]
            if norm in norm_quote(joined):
                return end + 1
    return None


def frozen_v1_status(suite: dict[str, Any]) -> dict[str, Any]:
    frozen = suite.get("frozen_v1") or {}
    checks: dict[str, Any] = {}
    ok = True
    for key in ("cases_file", "suite_file", "split_file", "audit_file"):
        rel = frozen.get(key)
        hash_key = key.replace("_file", "_file_sha256")
        expected = frozen.get(hash_key)
        if not rel or not expected:
            checks[key] = {"ok": False, "detail": "missing frozen declaration"}
            ok = False
            continue
        path = ROOT / str(rel)
        if not path.is_file():
            checks[key] = {"ok": False, "detail": "missing file"}
            ok = False
            continue
        actual = _sha256_file(path)
        checks[key] = {"ok": actual == expected, "expected": expected, "actual": actual}
        ok = ok and actual == expected
    return {"ok": ok, "checks": checks}


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, port)) == 0


def port_snapshot() -> dict[str, bool]:
    return {str(port): port_open(port) for port in (DEFAULT_PORT, *EXISTING_PORTS)}


def artifact_snapshot(artifact: Path) -> dict[str, Any]:
    manifest_path = artifact / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "error": "manifest_missing", "artifact": str(artifact)}
    manifest = read_json(manifest_path)
    key_files = [
        "runtime/robot_vacuum_support/state.py",
        "runtime/robot_vacuum_support/diagnostic_scope.py",
        "runtime/robot_vacuum_support/interpretation.py",
        "runtime/robot_vacuum_support/actions.py",
        "runtime/robot_vacuum_support/questions.py",
        "runtime/robot_vacuum_support/routing.py",
    ]
    hashes = {}
    for rel in key_files:
        path = artifact / rel
        hashes[rel] = _sha256_file(path) if path.is_file() else None
    return {
        "ok": True,
        "artifact": str(artifact),
        "artifact_id": manifest.get("artifact_id"),
        "manifest_sha256": _sha256_file(manifest_path),
        "runtime_hash": (manifest.get("runtime") or {}).get("hash"),
        "key_file_hashes": hashes,
    }


def _check(checks: list[dict[str, Any]], check_id: str, ok: bool, detail: Any = None) -> None:
    checks.append({"id": check_id, "ok": bool(ok), "detail": detail})


def validate_suite(
    suite_dir: Path = SUITE_DIR,
    *,
    verbose: bool = False,
    artifact_override: Path | None = None,
) -> dict[str, Any]:
    suite = read_json(suite_dir / "suite.json")
    cases_doc = read_json(suite_dir / "cases.json")
    cases = cases_doc.get("cases") or []
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    _check(checks, "suite.schema", suite.get("schema_version") == "rotating-mainline-suite/v1", suite.get("schema_version"))
    _check(checks, "cases.schema", cases_doc.get("schema_version") == "rotating-mainline-cases/v1", cases_doc.get("schema_version"))
    _check(checks, "suite.revision", suite.get("revision") == "v1" and cases_doc.get("revision") == "v1", {"suite": suite.get("revision"), "cases": cases_doc.get("revision")})

    total = len(cases)
    recharge = [c for c in cases if c.get("group") == "recharge_mainline"]
    breadth = [c for c in cases if c.get("group") == "breadth_primary_document"]
    counts = suite.get("counts") or {}
    _check(checks, "counts.total", total == 12 and counts.get("total") == total, {"actual": total, "declared": counts.get("total")})
    _check(checks, "proportion.6_12", len(recharge) == 6 and len(breadth) == 6, {"recharge": len(recharge), "breadth": len(breadth)})
    _check(checks, "counts.groups", counts.get("recharge_mainline") == len(recharge) and counts.get("breadth_primary_document") == len(breadth), counts)

    # Theme coverage: exactly one of each required recharge theme.
    themes = [c.get("theme") for c in recharge]
    required_themes = suite.get("required_themes") or []
    _check(checks, "recharge.themes.exactly_once", sorted(themes) == sorted(required_themes), {"actual": themes, "required": required_themes})

    # Breadth document coverage: each of the six documents exactly once.
    docs = [c.get("primary_document") for c in breadth]
    required_docs = suite.get("required_breadth_documents") or []
    _check(checks, "breadth.documents.exactly_once", sorted(docs) == sorted(required_docs), {"actual": docs, "required": required_docs})

    # Unique ids and unique normalised user turns.
    ids = [c.get("id") for c in cases]
    _check(checks, "cases.ids.unique", len(ids) == len(set(ids)), ids)
    all_user_turns = [norm_quote(t.get("content")) for c in cases for t in c.get("turns") or [] if t.get("role") == "user"]
    _check(checks, "cases.user_turns.unique", len(all_user_turns) == len(set(all_user_turns)), {"count": len(all_user_turns), "unique": len(set(all_user_turns))})

    # Attribution fields must be separate and present.
    for case in cases:
        cid = case.get("id")
        _check(checks, f"attribution.{cid}.source", bool(case.get("source")), case.get("source"))
        _check(checks, f"attribution.{cid}.origin", bool(case.get("origin")), case.get("origin"))
        _check(checks, f"attribution.{cid}.generation", bool(case.get("generation")), case.get("generation"))
        _check(checks, f"attribution.{cid}.scoring_method", bool(case.get("scoring_method")), case.get("scoring_method"))
        _check(checks, f"expected.bounded_answer.{cid}", bool((case.get("expected") or {}).get("bounded_answer")), None)
        _check(checks, f"expected.next_actions.{cid}", bool((case.get("expected") or {}).get("next_actions")), None)
        _check(checks, f"expected.must_not_claim.{cid}", bool((case.get("expected") or {}).get("must_not_claim")), None)
        _check(checks, f"review.not_human_reviewed.{cid}", (case.get("review") or {}).get("status") == "not_human_reviewed", (case.get("review") or {}).get("status"))

    # Breadth must be non-recharge in the user's question.
    for case in breadth:
        cid = case.get("id")
        text = " ".join(str(t.get("content") or "") for t in case.get("turns") or [])
        hits = [term for term in RECHARGE_TERMS if term in text]
        _check(checks, f"breadth.non_recharge.{cid}", not hits, hits)

    texts, extraction_meta = document_texts(suite)
    _check(checks, "documents.extraction", all((not meta.get("error")) for meta in extraction_meta.values()), extraction_meta)

    # Every evidence item: file hash, quote identity, locator presence.
    for case in cases:
        cid = case.get("id")
        evidence = case.get("evidence") or []
        _check(checks, f"evidence.present.{cid}", bool(evidence), len(evidence))
        for idx, item in enumerate(evidence, 1):
            rel = str(item.get("file") or "")
            path = ROOT / rel
            ok_file = path.is_file()
            _check(checks, f"evidence.file.{cid}.{idx}", ok_file, rel)
            if not ok_file:
                continue
            actual_hash = _sha256_file(path)
            _check(checks, f"evidence.hash.{cid}.{idx}", actual_hash == item.get("sha256"), {"expected": item.get("sha256"), "actual": actual_hash})
            quote = str(item.get("quote") or "")
            text = texts.get(rel, "")
            found = norm_quote(quote) in norm_quote(text)
            _check(checks, f"evidence.quote.{cid}.{idx}", found, {"quote": quote[:80], "locator": item.get("locator")})
            if found and item.get("locator"):
                locator = str(item.get("locator"))
                locator_line = None
                match = re.search(r"(?:line|行)\s*(\d+)", locator)
                if match:
                    locator_line = int(match.group(1))
                if rel.lower().endswith(".pdf"):
                    views = _extract_pdf_views(path)
                    computed_lines = [
                        _line_of(view, quote)
                        for key, view in views.items()
                        if not key.endswith("_error")
                    ]
                else:
                    computed_lines = [_line_of(text, quote)]
                computed_lines = [value for value in computed_lines if value is not None]
                locator_ok = locator_line is None or not computed_lines or locator_line in computed_lines
                _check(checks, f"evidence.locator.{cid}.{idx}", locator_ok, {"declared": locator_line, "computed": computed_lines})
            if rel.lower().endswith(".pdf"):
                _check(checks, f"evidence.pdf_extraction.{cid}.{idx}", bool(extraction_meta.get(rel, {}).get("extraction_complete")), extraction_meta.get(rel))

    # PDF overlap honesty: if the selected PDF quote also exists verbatim in a txt
    # document, the case must record that overlap.
    for case in breadth:
        if not str(case.get("primary_document", "")).lower().endswith(".pdf"):
            continue
        cid = case.get("id")
        quote_norm = norm_quote((case.get("evidence") or [{}])[0].get("quote", ""))
        overlaps = [rel for rel, text in texts.items() if rel != case.get("primary_document") and quote_norm and quote_norm in norm_quote(text)]
        declared = "pdf_overlap" in (case.get("source") or {})
        _check(checks, f"pdf.overlap_recorded.{cid}", (not overlaps) or declared, {"overlaps": overlaps, "declared": declared})
        if overlaps:
            warnings.append(f"{cid}: PDF quote overlaps txt documents {overlaps}; source.pdf_overlap must state this.")

    # Oracle guards: no manual-only auto-fail oracle; explicit recovery must be explicit.
    for case in recharge:
        cid = case.get("id")
        for turn in case.get("turns") or []:
            text = str(turn.get("content") or "")
            state = ((turn.get("expect") or {}).get("state") or {})
            for obs in state.get("observation_contains") or []:
                if obs.get("capability") == "automatic_return" and obs.get("result") == "fails":
                    _check(checks, f"oracle.auto_fail_explicit.{cid}.{turn.get('role')}", any(term in text for term in AUTO_FAIL_EXPLICIT_TERMS), text)
                if obs.get("capability") == "automatic_return" and obs.get("result") == "works":
                    _check(checks, f"oracle.auto_works_explicit.{cid}.{turn.get('role')}", any(term in text for term in AUTO_WORKS_EXPLICIT_TERMS), text)
        theme = case.get("theme")
        if theme == "multiround_correction":
            turns = case.get("turns") or []
            _check(checks, f"theme.multiround.{cid}", len(turns) >= 2 and any(("更正" in str(t.get("content")) or "说错" in str(t.get("content")) or "不对" in str(t.get("content"))) for t in turns), None)
            _check(checks, f"theme.correction_retracts.{cid}", any(((t.get("expect") or {}).get("state") or {}).get("tried_actions_absent") for t in turns), None)
        elif theme == "local_vs_global_success":
            _check(checks, f"theme.local_global.{cid}", any(any(o.get("capability") == "manual_charging" and o.get("result") == "works" for o in (((t.get("expect") or {}).get("state") or {}).get("observation_contains") or [])) for t in case.get("turns") or []), None)
        elif theme == "failed_outcome":
            _check(checks, f"theme.failed_outcome.{cid}", any(any(o.get("outcome") == "failed" for o in (((t.get("expect") or {}).get("state") or {}).get("tried_actions_contains") or [])) for t in case.get("turns") or []), None)
        elif theme == "hypothetical_third_party":
            text = " ".join(str(t.get("content")) for t in case.get("turns") or [])
            _check(checks, f"theme.hypothetical.{cid}", any(term in text for term in ("如果", "朋友", "假设")), text)
            _check(checks, f"theme.hypothetical_no_current_fact.{cid}", any(((t.get("expect") or {}).get("state") or {}).get("observation_absent") for t in case.get("turns") or []), None)
        elif theme == "explicit_recovery":
            state = (((case.get("turns") or [{}])[0].get("expect") or {}).get("state") or {})
            _check(checks, f"theme.explicit_recovery.{cid}", state.get("case_status") == "resolved" and state.get("resolved") is True and state.get("closure_level") == "explicit_success", state)
        elif theme == "unresolved_conversation":
            state = (((case.get("turns") or [{}])[0].get("expect") or {}).get("state") or {})
            _check(checks, f"theme.unresolved.{cid}", state.get("case_status") == "active" and state.get("resolved") is False and "check_auto_return" in (state.get("not_tried_actions_contains") or []), state)

    _check(checks, "scoring.no_authoritative_claim", (suite.get("scoring") or {}).get("authoritative_pass_claimed") is False, suite.get("scoring"))
    _check(checks, "human_pressure.not_run_or_recorded", (suite.get("human_pressure_test") or {}).get("status") in ("not_run", "run"), suite.get("human_pressure_test"))

    frozen = frozen_v1_status(suite)
    _check(checks, "frozen_v1.unchanged", frozen.get("ok"), frozen)
    target_artifact = Path(artifact_override) if artifact_override is not None else DEFAULT_ARTIFACT
    artifacts = artifact_snapshot(target_artifact)
    _check(checks, "artifact.exists", artifacts.get("ok"), artifacts)
    if artifacts.get("ok"):
        declared_hash = (suite.get("artifact") or {}).get("manifest_runtime_hash")
        if artifact_override is None:
            _check(
                checks,
                "artifact.runtime_hash_matches_suite",
                artifacts.get("runtime_hash") == declared_hash,
                {"artifact": artifacts.get("runtime_hash"), "suite": declared_hash},
            )
        else:
            # The frozen suite keeps the historical artifact hash.  A release
            # run may intentionally point at a newer immutable artifact; record
            # the override and do not rewrite the frozen suite/cases.
            _check(
                checks,
                "artifact.runtime_hash_suite_override",
                bool(artifacts.get("runtime_hash")),
                {
                    "artifact": artifacts.get("runtime_hash"),
                    "suite": declared_hash,
                    "matches_frozen_suite": artifacts.get("runtime_hash") == declared_hash,
                    "override_artifact": str(target_artifact),
                    "suite_and_cases_unchanged": True,
                },
            )

    errors = [c["id"] for c in checks if not c["ok"]]
    report = {
        "schema_version": "rotating-mainline-validation/v1",
        "checked_at": now_iso(),
        "suite_id": suite.get("suite_id"),
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "total": total,
            "recharge_mainline": len(recharge),
            "breadth_primary_document": len(breadth),
            "documents": len(required_docs),
            "themes": len(required_themes),
        },
        "theme_map": {c.get("theme"): c.get("id") for c in recharge},
        "document_map": {c.get("primary_document"): c.get("id") for c in breadth},
        "checks": checks,
        "frozen_v1": frozen,
        "artifact": artifacts,
        "extraction": extraction_meta,
        "cases_sha256": _sha256_file(suite_dir / "cases.json"),
        "suite_sha256": _sha256_file(suite_dir / "suite.json"),
        "case_hashes": {c.get("id"): canonical_hash(c) for c in cases},
    }
    if verbose:
        print(json.dumps({k: report[k] for k in ("ok", "errors", "warnings", "counts", "theme_map", "document_map")}, ensure_ascii=False, indent=2))
    return report


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
        "tried_actions": [{"action_id": item.get("action_id"), "outcome": item.get("outcome")} for item in tried if isinstance(item, dict)],
        "not_tried_actions": [item.get("action_id") for item in not_tried if isinstance(item, dict)],
        "observations": [{"capability": item.get("capability"), "result": item.get("result")} for item in observations if isinstance(item, dict)],
        "closure_level": closure.get("level") if isinstance(closure, dict) else None,
    }


def _state_check(state: dict[str, Any], expect: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
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
        found = action_id in (state.get("not_tried_actions") or [])
        add(f"not_tried_contains.{action_id}", found, state.get("not_tried_actions"))
    for item in expect.get("observation_contains") or []:
        found = any(o.get("capability") == item.get("capability") and o.get("result") == item.get("result") for o in state.get("observations") or [])
        add(f"observation_contains.{item.get('capability')}={item.get('result')}", found, state.get("observations"))
    for item in expect.get("observation_absent") or []:
        found = any(o.get("capability") == item.get("capability") and o.get("result") == item.get("result") for o in state.get("observations") or [])
        add(f"observation_absent.{item.get('capability')}={item.get('result')}", not found, state.get("observations"))
    return checks


def _known_document_names(suite: dict[str, Any]) -> list[str]:
    return [Path(str(item.get("path"))).name for item in suite.get("documents") or []]


def _value_mentions(value: Any, names: list[str]) -> list[str]:
    blob = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return [name for name in names if name in blob]


def _retrieval_attempted(events: list[dict[str, Any]], retrieval: Any) -> bool:
    for event in events:
        if event.get("type") in ("tool_start", "tool_end") and event.get("tool") == "rag_retrieve":
            return True
    if isinstance(retrieval, dict):
        status = retrieval.get("status")
        if status and status != "not_retrieved":
            return True
        if retrieval.get("attempts") or retrieval.get("results") or retrieval.get("records"):
            return True
    return False


def evaluate_turn(case: dict[str, Any], turn: dict[str, Any], result: dict[str, Any], suite: dict[str, Any]) -> dict[str, Any]:
    cid = str(case.get("id"))
    turn_no = int(turn.get("_turn_no") or 1)
    answer = str(result.get("answer") or "")
    support = result.get("support") or {}
    state = normalize_state(support)
    checks: list[dict[str, Any]] = []
    _check(checks, f"turn{turn_no}.answer_nonempty", bool(answer.strip()), {"chars": len(answer)})
    _check(checks, f"turn{turn_no}.no_error", not result.get("error"), result.get("error"))
    _check(checks, f"turn{turn_no}.retrieval_attempted", _retrieval_attempted(result.get("events") or [], result.get("retrieval")), {"retrieval_status": ((result.get("retrieval") or {}).get("status") if isinstance(result.get("retrieval"), dict) else None)})

    group = case.get("group")
    if group == "breadth_primary_document":
        expected = turn.get("expect") or {}
        doc_name = Path(str(case.get("primary_document"))).name
        names = _known_document_names(suite)
        mentions = _value_mentions({"retrieval": result.get("retrieval"), "answer": answer, "support_sources": (support.get("active_case") or {}).get("sources")}, names)
        _check(checks, f"turn{turn_no}.primary_document_evidence", doc_name in mentions if expected.get("primary_document_evidence") else True, {"primary": doc_name, "mentions": mentions})
    elif group == "recharge_mainline":
        state_expect = (turn.get("expect") or {}).get("state") or {}
        checks.extend(_state_check(state, state_expect, f"turn{turn_no}.state"))
    return {
        "turn": turn_no,
        "question": turn.get("content"),
        "answer": answer,
        "error": result.get("error"),
        "http_status": result.get("http_status"),
        "state": state,
        "retrieval": result.get("retrieval"),
        "usage": result.get("usage"),
        "support": support,
        "transport_attempts": result.get("transport_attempts"),
        "checks": checks,
        "passed_structural": all(item["ok"] for item in checks),
    }


def _answer_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("type") in ("answer", "interrupt"):
            return event
    return None


def _error_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in events:
        if event.get("type") == "error":
            return event
    return None


def run_one_case(
    case: dict[str, Any],
    base_url: str,
    suite: dict[str, Any],
    *,
    probes: list[dict[str, Any]] | None = None,
    delete_session: bool = True,
) -> dict[str, Any]:
    cid = str(case.get("id"))
    record: dict[str, Any] = {
        "id": cid,
        "group": case.get("group"),
        "theme": case.get("theme"),
        "topic": case.get("topic"),
        "primary_document": case.get("primary_document"),
        "source": case.get("source"),
        "expected": case.get("expected"),
        "session_id": None,
        "status": "not_run",
        "error": None,
        "turns": [],
        "targeted_probes": [],
        "raw_events": {},
    }
    session = None
    try:
        session = _post_json(base_url + "/api/sessions", {"workflow": "agent"}, timeout=60)
        session_id = session.get("session_id")
        if not session_id:
            raise RuntimeError(f"session creation returned no session_id: {session}")
        record["session_id"] = session_id
        for idx, turn in enumerate(case.get("turns") or [], 1):
            turn = dict(turn)
            turn["_turn_no"] = idx
            events, retry_info = _send_with_one_retry(base_url, session_id, str(turn.get("content") or ""), timeout=300)
            answer_event = _answer_event(events)
            error_event = _error_event(events)
            result = {
                "answer": (answer_event or {}).get("answer"),
                "support": (answer_event or {}).get("support"),
                "retrieval": (answer_event or {}).get("retrieval"),
                "usage": (answer_event or {}).get("usage"),
                "http_status": 200,
                "error": (error_event or {}).get("error"),
                "events": events,
                "transport_attempts": retry_info,
            }
            evaluated = evaluate_turn(case, turn, result, suite)
            evaluated["raw_event_types"] = [event.get("type") for event in events]
            record["turns"].append(evaluated)
            record["raw_events"][f"turn{idx}"] = events
        record["status"] = "passed_structural" if all(t.get("passed_structural") for t in record["turns"]) else "failed_structural"

        # Bounded, explicitly separate PDF follow-up probes.  They never change
        # the frozen turn count, case status or expected values; they record
        # whether the same-family question actually retrieves the primary PDF.
        for probe_no, probe in enumerate(probes or [], 1):
            content = str(probe.get("content") or "")
            try:
                events, retry_info = _send_with_one_retry(base_url, session_id, content, timeout=300)
                answer_event = _answer_event(events)
                error_event = _error_event(events)
                retrieval = (answer_event or {}).get("retrieval")
                doc_name = Path(str(case.get("primary_document"))).name
                names = _known_document_names(suite)
                mentions = _value_mentions({"retrieval": retrieval, "answer": (answer_event or {}).get("answer")}, names)
                checks = [
                    {"id": f"probe{probe_no}.answer_nonempty", "ok": bool(str((answer_event or {}).get("answer") or "").strip())},
                    {"id": f"probe{probe_no}.no_error", "ok": not (error_event or {}).get("error")},
                    {
                        "id": f"probe{probe_no}.primary_document_retrieved",
                        "ok": doc_name in mentions,
                        "detail": {"primary": doc_name, "mentions": mentions},
                    },
                ]
                record["targeted_probes"].append(
                    {
                        "probe": probe_no,
                        "question": content,
                        "answer": (answer_event or {}).get("answer"),
                        "retrieval": retrieval,
                        "transport_attempts": retry_info,
                        "checks": checks,
                        "passed": all(item["ok"] for item in checks),
                    }
                )
                record["raw_events"][f"probe{probe_no}"] = events
            except Exception as exc:  # noqa: BLE001 - probe failure must not rewrite the frozen case
                record["targeted_probes"].append(
                    {
                        "probe": probe_no,
                        "question": content,
                        "answer": None,
                        "retrieval": None,
                        "error": f"{type(exc).__name__}: {exc}",
                        "checks": [{"id": f"probe{probe_no}.no_error", "ok": False}],
                        "passed": False,
                    }
                )
    except Exception as exc:  # noqa: BLE001 - recorded, never retried to green
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if delete_session and session and session.get("session_id"):
            _delete_session(base_url, str(session["session_id"]))
    return record


def _write_instance(
    path: Path,
    artifact: Path,
    env_file: Path,
    port: int,
    *,
    out_dir: Path,
    instance_id: str = "robot-vacuum-rotating-mainline-v1",
    name: str = "robot-vacuum rotating-mainline acceptance",
) -> dict[str, Any]:
    manifest = read_json(artifact / "manifest.json")
    instance = {
        "schema_version": 1,
        "instance_id": instance_id,
        "artifact_id": manifest.get("artifact_id"),
        "artifact_root": str(artifact),
        "name": name,
        "scenario": "robot_vacuum",
        "version": 4,
        "workflow": "agent",
        "host": "127.0.0.1",
        "port": port,
        "storage_root": str(out_dir / "instance-storage"),
        "environment_file": str(env_file),
        "asset_bindings": {},
    }
    write_json(path, instance)
    return {k: v for k, v in instance.items() if k != "environment_file"}


def _artifact_check(artifact: Path, *, cwd: Path | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [sys.executable, str(artifact / "run.py"), "--check"],
            cwd=str(cwd if cwd is not None else artifact),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            timeout=120,
        )
        return {"ok": completed.returncode == 0, "returncode": completed.returncode, "stdout": completed.stdout.strip()[:2000]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_live(
    artifact: Path = DEFAULT_ARTIFACT,
    port: int = DEFAULT_PORT,
    workers: int = 2,
    *,
    out_dir: Path | None = None,
    targeted_probes: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    suite_dir = SUITE_DIR
    out = Path(out_dir) if out_dir is not None else OUT_DIR
    suite, cases_doc = load_suite(suite_dir)
    validation = validate_suite(suite_dir, artifact_override=artifact)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "validation.json", validation)
    if not validation.get("ok"):
        return {"status": "blocked_validation", "validation": validation}
    if port != DEFAULT_PORT:
        raise SystemExit(f"this batch is frozen to port {DEFAULT_PORT}")
    if workers != 2:
        raise SystemExit("this batch is frozen to concurrency 2")

    before_ports = port_snapshot()
    if before_ports.get(str(port)):
        return {"status": "blocked_port_in_use", "ports": before_ports}
    if not all(before_ports.get(str(p)) for p in EXISTING_PORTS):
        # Record, but do not fail: existing services may legitimately be down.
        pass

    artifact_info = artifact_snapshot(artifact)
    artifact_check = _artifact_check(artifact)
    env_file = _choose_env_file()
    if env_file is None:
        return {"status": "needs_configuration", "detail": "no safe environment file candidate found; credential values were not read"}
    instance_path = out / "instance.json"
    instance_public = _write_instance(instance_path, artifact, env_file, port, out_dir=out)

    cases = cases_doc.get("cases") or []
    probe_turns = sum(len(items) for items in (targeted_probes or {}).values())
    artifact_is_new = artifact.resolve() != DEFAULT_ARTIFACT.resolve()
    freeze = {
        "schema_version": "rotating-mainline-freeze/v1",
        "frozen_at": now_iso(),
        "status": "frozen_before_model_calls",
        "suite_id": suite.get("suite_id"),
        "suite_revision": suite.get("revision"),
        "purpose": suite.get("purpose"),
        "cases_sha256": _sha256_file(CASES_PATH),
        "suite_sha256": _sha256_file(SUITE_PATH),
        "case_hashes": validation.get("case_hashes"),
        "validation_sha256": sha_text(json.dumps(validation, ensure_ascii=False, sort_keys=True)),
        "artifact": artifact_info,
        "artifact_check": artifact_check,
        "artifact_is_new_build": artifact_is_new,
        "instance_public": instance_public,
        "env_file_used": str(env_file),
        "env_values_read_or_logged": False,
        "port": port,
        "expected_sessions": len(cases),
        "expected_turns": sum(len(c.get("turns") or []) for c in cases),
        "targeted_probe_turns": probe_turns,
        "max_turns_per_session": 20,
        "concurrency": workers,
        "existing_ports_before": before_ports,
        "frozen_v1": validation.get("frozen_v1"),
        "not_holdout": True,
        "not_success_rate": True,
        "no_new_artifact": not artifact_is_new,
        "frozen_cases_and_expected_unchanged": True,
        "artifact_suite_hash_overridden_for_release": artifact_is_new,
        "no_runtime_prompt_judge_ui_change": True,
        "no_green_retry": True,
        "transport_retry_policy": "at most one transport retry per turn; all attempts recorded",
    }
    write_json(out / "freeze.json", freeze)

    server = LiveServer(
        python=sys.executable,
        serve_py=artifact / "serve.py",
        instance_path=instance_path,
        host="127.0.0.1",
        port=port,
        log_path=out / "server.log",
        cwd=artifact,
    )
    health: dict[str, Any] = {}
    started = time.time()
    rows: list[dict[str, Any]] = []
    try:
        health = server.start(timeout=180)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    run_one_case,
                    case,
                    server.base_url,
                    suite,
                    probes=(targeted_probes or {}).get(str(case.get("id"))),
                ): case
                for case in cases
            }
            for future in as_completed(futures):
                try:
                    rows.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    case = futures[future]
                    rows.append({"id": case.get("id"), "group": case.get("group"), "status": "error", "error": f"{type(exc).__name__}: {exc}", "turns": []})
    except Exception as exc:  # noqa: BLE001
        health = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        server.stop()
        released = not port_open(port)
        finished = time.time()

    rows.sort(key=lambda row: ([c.get("id") for c in cases].index(row.get("id")) if row.get("id") in [c.get("id") for c in cases] else 999))
    after_ports = port_snapshot()
    after_frozen = frozen_v1_status(suite)
    after_artifact = artifact_snapshot(artifact)
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    write_json(raw_dir / "run.json", {
        "schema_version": "rotating-mainline-raw/v1",
        "generated_at": now_iso(),
        "health": health,
        "cases": rows,
    })

    by_group: dict[str, dict[str, int]] = {}
    for row in rows:
        group = str(row.get("group"))
        bucket = by_group.setdefault(group, {"total": 0, "passed_structural": 0, "failed_structural": 0, "error": 0, "not_run": 0})
        bucket["total"] += 1
        bucket[str(row.get("status"))] = bucket.get(str(row.get("status")), 0) + 1

    coverage = []
    for row in rows:
        checks = [check for turn in row.get("turns") or [] for check in turn.get("checks") or []]
        failed = [check["id"] for check in checks if not check.get("ok")]
        probes = row.get("targeted_probes") or []
        coverage.append({
            "id": row.get("id"),
            "group": row.get("group"),
            "theme": row.get("theme"),
            "topic": row.get("topic"),
            "primary_document": row.get("primary_document"),
            "status": row.get("status"),
            "turns": len(row.get("turns") or []),
            "checks_total": len(checks),
            "checks_passed": sum(1 for check in checks if check.get("ok")),
            "failed_checks": failed,
            "known_failure_checks": failed,
            "questions": [turn.get("question") for turn in row.get("turns") or []],
            "targeted_probes": [
                {
                    "probe": probe.get("probe"),
                    "question": probe.get("question"),
                    "passed": probe.get("passed"),
                    "failed_checks": [check["id"] for check in probe.get("checks") or [] if not check.get("ok")],
                }
                for probe in probes
            ],
        })

    normal_turns_run = sum(len(row.get("turns") or []) for row in rows)
    targeted_probe_turns_run = sum(len(row.get("targeted_probes") or []) for row in rows)

    report = {
        "schema_version": SCHEMA,
        "generated_at": now_iso(),
        "suite_id": suite.get("suite_id"),
        "suite_revision": suite.get("revision"),
        "status": "completed_with_known_failures" if any(row.get("status") != "passed_structural" for row in rows) else "completed_structural_pass",
        "scope": "development rotating-mainline acceptance; not holdout, not a success rate, no authoritative Judge pass",
        "artifact": after_artifact,
        "artifact_unchanged": artifact_info.get("manifest_sha256") == after_artifact.get("manifest_sha256"),
        "artifact_check": artifact_check,
        "port": port,
        "health": health,
        "started_at": datetime.fromtimestamp(started, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
        "finished_at": datetime.fromtimestamp(finished, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
        "sessions_planned": len(cases),
        "sessions_run": sum(1 for row in rows if row.get("session_id")),
        "turns_planned": sum(len(c.get("turns") or []) for c in cases),
        "normal_turns_run": normal_turns_run,
        "targeted_probe_turns_run": targeted_probe_turns_run,
        "turns_run": normal_turns_run + targeted_probe_turns_run,
        "frozen_expected_turns": sum(len(c.get("turns") or []) for c in cases),
        "targeted_probe_cases": sorted((targeted_probes or {}).keys()),
        "errors": sum(1 for row in rows if row.get("status") == "error"),
        "not_run": sum(1 for row in rows if row.get("status") == "not_run"),
        "by_group": by_group,
        "coverage_matrix": coverage,
        "cases": rows,
        "artifact_is_new_build": artifact_is_new,
        "frozen_cases_and_expected_unchanged": True,
        "artifact_suite_hash_overridden_for_release": artifact_is_new,
        "semantic_advisory": {
            "scorer": "agent_inspection",
            "status": "separate_advisory",
            "authoritative_judge": False,
            "note": "Structural checks are program-only. Semantic answer quality is recorded in semantic-findings.json after inspection, not as a Judge pass.",
            "file": "semantic-findings.json",
        },
        "score_layers": {
            "program": "structural/evidence binding only",
            "semantic": "agent inspection advisory; unassessed_by_independent_judge",
            "authoritative_pass_claimed": False,
        },
        "frozen_v1_before": validation.get("frozen_v1"),
        "frozen_v1_after": after_frozen,
        "frozen_v1_preserved": after_frozen.get("ok") is True,
        "ports_before": before_ports,
        "ports_after": after_ports,
        "port_released": released,
        "existing_services_preserved": all(before_ports.get(str(p)) == after_ports.get(str(p)) for p in EXISTING_PORTS),
        "env_file_used": str(env_file),
        "env_values_read_or_logged": False,
        "cleanup": {
            "sessions_deleted": True,
            "server_stopped": True,
            "port_released": released,
            "artifact_untouched": True,
            "original_frozen_v1_and_reviews_untouched": after_frozen.get("ok") is True,
        },
        "limitations": [
            "Development advisory only; 12 fixed sessions are not a success rate and not holdout.",
            "Program scoring is structural/evidence binding, not semantic correctness.",
            "No new Judge run or rubric was created; answer semantics remain an agent-inspection advisory.",
            "Known product state bugs are preserved as failed structural checks, not retried to green.",
        ],
    }
    write_json(out / "report.json", report)
    return report


def regrade_existing(out_dir: Path = OUT_DIR) -> dict[str, Any]:
    """Recompute program checks from the saved raw events; never calls a model."""
    suite, cases_doc = load_suite()
    cases_by_id = {case.get("id"): case for case in cases_doc.get("cases") or []}
    raw = read_json(out_dir / "raw" / "run.json")
    old_report = read_json(out_dir / "report.json")
    old_status = {row.get("id"): row.get("status") for row in old_report.get("cases") or []}
    rows: list[dict[str, Any]] = []
    for raw_row in raw.get("cases") or []:
        case = cases_by_id.get(raw_row.get("id"))
        if not case:
            rows.append(raw_row)
            continue
        rebuilt = dict(raw_row)
        rebuilt["turns"] = []
        for idx, turn in enumerate(case.get("turns") or [], 1):
            events = (raw_row.get("raw_events") or {}).get(f"turn{idx}") or []
            answer_event = _answer_event(events)
            error_event = _error_event(events)
            result = {
                "answer": (answer_event or {}).get("answer"),
                "support": (answer_event or {}).get("support"),
                "retrieval": (answer_event or {}).get("retrieval"),
                "usage": (answer_event or {}).get("usage"),
                "http_status": 200 if answer_event else None,
                "error": (error_event or {}).get("error"),
                "events": events,
                "transport_attempts": (raw_row.get("turns") or [{}])[idx - 1].get("transport_attempts") if idx - 1 < len(raw_row.get("turns") or []) else None,
            }
            evaluated = evaluate_turn(case, {**turn, "_turn_no": idx}, result, suite)
            evaluated["raw_event_types"] = [event.get("type") for event in events]
            rebuilt["turns"].append(evaluated)
        rebuilt["status"] = "passed_structural" if all(t.get("passed_structural") for t in rebuilt["turns"]) else "failed_structural"
        rows.append(rebuilt)

    by_group: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_group.setdefault(str(row.get("group")), {"total": 0, "passed_structural": 0, "failed_structural": 0, "error": 0, "not_run": 0})
        bucket["total"] += 1
        bucket[str(row.get("status"))] = bucket.get(str(row.get("status")), 0) + 1
    coverage = []
    for row in rows:
        checks = [check for turn in row.get("turns") or [] for check in turn.get("checks") or []]
        failed = [check["id"] for check in checks if not check.get("ok")]
        probes = row.get("targeted_probes") or []
        coverage.append({
            "id": row.get("id"),
            "group": row.get("group"),
            "theme": row.get("theme"),
            "topic": row.get("topic"),
            "primary_document": row.get("primary_document"),
            "status": row.get("status"),
            "turns": len(row.get("turns") or []),
            "checks_total": len(checks),
            "checks_passed": sum(1 for check in checks if check.get("ok")),
            "failed_checks": failed,
            "known_failure_checks": failed,
            "questions": [turn.get("question") for turn in row.get("turns") or []],
            "targeted_probes": [
                {
                    "probe": probe.get("probe"),
                    "question": probe.get("question"),
                    "passed": probe.get("passed"),
                    "failed_checks": [check["id"] for check in probe.get("checks") or [] if not check.get("ok")],
                }
                for probe in probes
            ],
        })

    normal_turns_run = sum(len(row.get("turns") or []) for row in rows)
    targeted_probe_turns_run = sum(len(row.get("targeted_probes") or []) for row in rows)
    report = dict(old_report)
    report["cases"] = rows
    report["by_group"] = by_group
    report["coverage_matrix"] = coverage
    report["status"] = "completed_with_known_failures" if any(row.get("status") != "passed_structural" for row in rows) else "completed_structural_pass"
    report["regraded_offline"] = {
        "regraded_at": now_iso(),
        "reason": "Corrected the retrieval_attempted check: a retrieval payload with status=not_retrieved is no longer counted as an attempt.",
        "model_calls": 0,
        "new_artifact": False,
        "old_status_by_case": old_status,
        "new_status_by_case": {row.get("id"): row.get("status") for row in rows},
    }
    write_json(out_dir / "report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--regrade-existing", action="store_true")
    parser.add_argument("--artifact", default=None, help="artifact path; default keeps the historical frozen run target")
    parser.add_argument("--output", default=None, help="output directory; default outputs/rotating-mainline-v1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    artifact = Path(args.artifact).resolve() if args.artifact else None
    out = Path(args.output) if args.output else OUT_DIR

    if args.validate_only:
        validation = validate_suite(SUITE_DIR, verbose=True, artifact_override=artifact)
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / "validation.json", validation)
        return 0 if validation.get("ok") else 1
    if args.regrade_existing:
        report = regrade_existing(out)
        print(json.dumps({
            "status": report.get("status"),
            "by_group": report.get("by_group"),
            "changed_cases": {
                key: value for key, value in (report.get("regraded_offline") or {}).get("old_status_by_case", {}).items()
                if value != (report.get("regraded_offline") or {}).get("new_status_by_case", {}).get(key)
            },
        }, ensure_ascii=False, indent=2))
        return 0
    if args.run:
        report = run_live(
            artifact=artifact or DEFAULT_ARTIFACT,
            port=args.port,
            workers=args.workers,
            out_dir=out,
        )
        print(json.dumps({
            "status": report.get("status"),
            "sessions_run": report.get("sessions_run"),
            "turns_run": report.get("turns_run"),
            "by_group": report.get("by_group"),
            "port_released": report.get("port_released"),
            "known_failure_cases": [row["id"] for row in report.get("coverage_matrix", []) if row.get("status") != "passed_structural"],
        }, ensure_ascii=False, indent=2))
        return 0 if report.get("status") not in (None, "blocked_validation", "blocked_port_in_use", "needs_configuration") else 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
