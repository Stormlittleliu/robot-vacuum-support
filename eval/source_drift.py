# -*- coding: utf-8 -*-
"""Independent source-drift audit for the frozen development dataset.

The dataset and human review records are immutable in this task.  When a
source file changes after generation (for example README.md gains a new
documentation section), this module answers three separate questions:

1. Did a **knowledge** source (``assets/knowledge/*``) drift?  That is a real
   evidence change and must not be waived.
2. Did a **requirement** source (README policy text) drift?  If every original
   quote is still present verbatim (or only whitespace-normalised), the drift is
   non-semantic for the reviewed cases and may be recorded as a scoped waiver.
3. Is the current runtime schema compatible with the reviewed v1.1 case
   contract?  Only a proven semantic-compatible schema may be recorded as a
   waiver scope.

Nothing here edits ``eval/datasets/v1``, review records, or old hashes.  The
report is a separate audit artifact.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from eval.selection import case_hash, sha256_file

ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT / "eval" / "datasets" / "v1"
STABLE_STATE_KEYS = {
    "case_status",
    "tried_actions",
    "not_tried_actions",
    "model",
    "case_count",
    "scope",
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _normalise_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _compact_space(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception:  # noqa: BLE001 - audit records extraction failure
            return ""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _quote_status(quote: str, text: str) -> str:
    if _normalise_space(quote) and _normalise_space(quote) in _normalise_space(text):
        return "exact_or_whitespace_normalised"
    if _compact_space(quote) and _compact_space(quote) in _compact_space(text):
        return "whitespace_only_after_compaction"
    return "missing"


def _state_schema() -> dict[str, Any]:
    import sys

    runtime = ROOT / "runtime"
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    try:
        from robot_vacuum_support import questions, state

        public_keys = sorted(state.public_view(state.empty_state(now=0)).keys())
        return {
            "state_schema_version": getattr(state, "SCHEMA_VERSION", None),
            "legacy_state_versions": list(getattr(state, "LEGACY_SCHEMA_VERSIONS", []) or []),
            "public_view_keys": public_keys,
            "stable_state_keys": sorted(STABLE_STATE_KEYS),
            "questions_schema_version": getattr(questions, "QUESTION_SCHEMA_VERSION", None),
            "state_module_sha256": sha256_file(runtime / "robot_vacuum_support" / "state.py"),
            "questions_module_sha256": sha256_file(runtime / "robot_vacuum_support" / "questions.py"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_source_drift_audit(
    *,
    dataset_dir: Path | str = DATASET_DIR,
    generated_at: str | None = None,
) -> dict[str, Any]:
    from datetime import datetime

    base = Path(dataset_dir).resolve()
    suite = _read_json(base / "suite.json")
    cases = [
        json.loads(line)
        for line in (base / "cases.jsonl").read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    snapshot = suite.get("source_snapshot") or {}
    documents = [doc for doc in snapshot.get("documents") or [] if isinstance(doc, dict)]

    files: list[dict[str, Any]] = []
    knowledge_drift: list[dict[str, Any]] = []
    requirement_drift: list[dict[str, Any]] = []
    changed_paths: set[str] = set()
    for doc in documents:
        rel = str(doc.get("path") or "").replace("\\", "/")
        path = (ROOT / rel).resolve()
        try:
            path.relative_to(ROOT)
        except ValueError:
            path = base / rel
        kind = str(doc.get("kind") or "unknown")
        declared = str(doc.get("sha256") or "").lower()
        current = sha256_file(path) if path.is_file() else None
        changed = current is not None and declared != current
        row = {
            "path": rel,
            "kind": kind,
            "declared_sha256": declared,
            "current_sha256": current,
            "exists": path.is_file(),
            "changed": changed,
            "declared_bytes": doc.get("bytes"),
            "current_bytes": path.stat().st_size if path.is_file() else None,
        }
        files.append(row)
        if changed:
            changed_paths.add(rel)
            (knowledge_drift if kind == "knowledge" else requirement_drift).append(row)

    # Every evidence row whose source file changed is checked separately.
    evidence_rows: list[dict[str, Any]] = []
    quote_failures: list[dict[str, Any]] = []
    for rel in sorted(changed_paths):
        path = (ROOT / rel).resolve()
        try:
            text = _extract_text(path)
        except Exception as exc:  # noqa: BLE001
            text = ""
            quote_status_error = f"{type(exc).__name__}: {exc}"
        else:
            quote_status_error = None
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for case in cases:
            for item in case.get("evidence") or []:
                file_value = str(item.get("file") or "").replace("\\", "/")
                if file_value != rel:
                    continue
                quote = str(item.get("quote") or "")
                status = _quote_status(quote, text) if text else "missing"
                key = (quote, str(item.get("id") or ""))
                row = seen.get(key)
                if row is None:
                    row = {
                        "file": rel,
                        "kind": item.get("kind"),
                        "evidence_id": item.get("id"),
                        "declared_sha256": str(item.get("sha256") or "").lower(),
                        "quote": quote,
                        "quote_status": status,
                        "affected_cases": [],
                    }
                    seen[key] = row
                row["affected_cases"].append(case.get("id"))
                if status == "missing":
                    quote_failures.append(
                        {
                            "case_id": case.get("id"),
                            "evidence_id": item.get("id"),
                            "file": rel,
                            "quote": quote,
                        }
                    )
        for row in seen.values():
            row["affected_cases"] = sorted(set(row["affected_cases"]))
            evidence_rows.append(row)

    # Schema compatibility for the reviewed case contract.
    schema = _state_schema()
    expected_state_keys: set[str] = set()
    for case in cases:
        if case.get("split") != "development":
            continue
        for turn in case.get("turns") or []:
            expected_state_keys.update(((turn.get("expected") or {}).get("state") or {}).keys())
    unknown_state_keys = sorted(expected_state_keys - STABLE_STATE_KEYS)
    current_review_hashes_match = True
    review_hash_errors: list[str] = []
    # Review hash agreement is also checked by eval/selection.py; record it here
    # because a semantic waiver is only valid while the case definitions match.
    review_dir = ROOT / "eval" / "reviews" / "reviews"
    by_id = {str(case.get("id")): case for case in cases}
    for path in sorted(review_dir.glob("*.json")) if review_dir.is_dir() else []:
        record = _read_json(path)
        if record.get("reviewScope") != "case_definition":
            continue
        case = by_id.get(str(record.get("caseId")))
        if case is None:
            continue
        if case_hash(case) != record.get("caseHash"):
            current_review_hashes_match = False
            review_hash_errors.append(str(record.get("caseId")))
    schema_ok = bool(schema.get("public_view_keys")) and not unknown_state_keys and current_review_hashes_match
    schema["expected_state_keys"] = sorted(expected_state_keys)
    schema["unknown_expected_state_keys"] = unknown_state_keys
    schema["review_case_hashes_match"] = current_review_hashes_match
    schema["review_hash_errors"] = review_hash_errors
    schema["compatible"] = schema_ok

    # A waiver is only allowed for requirement-only drift with all quotes
    # present and a semantically compatible schema.  Knowledge drift always
    # blocks; a missing requirement quote also blocks.
    waived = bool(requirement_drift) and not knowledge_drift and not quote_failures and schema_ok
    ok = not knowledge_drift and not quote_failures and (not requirement_drift or waived)
    report = {
        "schema_version": "approved-development-source-drift/v1",
        "generated_at": generated_at
        or __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataset_dir": str(base),
        "dataset_revision": suite.get("revision"),
        "dataset_declared_source_snapshot": {
            "generated_at": snapshot.get("generated_at"),
            "generator_version": snapshot.get("generator_version"),
            "cases_sha256": snapshot.get("cases_sha256"),
            "holdout_cases_sha256": snapshot.get("holdout_cases_sha256"),
        },
        "files": files,
        "knowledge_drift": knowledge_drift,
        "requirement_drift": requirement_drift,
        "evidence_quote_checks": evidence_rows,
        "quote_failures": quote_failures,
        "schema_compatibility": schema,
        "ok": ok,
        "waived": waived,
        "waiver": (
            {
                "scope": "requirement-only source hash drift",
                "files": [row["path"] for row in requirement_drift],
                "conditions": [
                    "no knowledge source changed",
                    "every original requirement quote still present (verbatim or whitespace-normalised)",
                    "reviewed case definitions/caseHashes unchanged",
                    "current state/question schema exposes the reviewed stable keys",
                ],
                "not_waived": [
                    "knowledge file hash drift",
                    "missing original quote",
                    "changed case definition or caseHash",
                    "unknown stable state key",
                ],
                "effect": "pure development evaluation may proceed; dataset and review hashes stay unchanged",
            }
            if waived
            else None
        ),
        "notes": [
            "This audit never rewrites the declared hashes in suite.json or case evidence.",
            "Requirement evidence (README policy text) is not used as RAG knowledge; knowledge drift is never waived.",
            "A waiver is scoped to this source snapshot and must be regenerated if any quoted source changes again.",
        ],
    }
    return report


__all__ = ["build_source_drift_audit"]
