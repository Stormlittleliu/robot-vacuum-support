# -*- coding: utf-8 -*-
"""Formal robot-support eval v1 runner.

Commands (run from this project or any external cwd)::

    python eval/runner.py validate-data
    python eval/runner.py run --split development --mode state
    python eval/runner.py run --split development --mode live --limit 3
    python eval/runner.py summary

Boundaries:
* The formal v1 dataset under ``eval/datasets/v1`` is the only authority.
  ``eval/cases/v0/recharge-start.json`` and ``tests/recharge_cases.py`` stay as
  historical regression material, not copied/renamed into v1.
* ``state`` mode calls the existing pure ``apply_user_turn`` /
  ``apply_assistant_turn`` functions in a fresh session per case.
* ``live`` mode rebuilds a *new* artifact from source and starts a real instance
  on port 8940.  It never treats the old built artifact as the current product
  and never treats ``assistant_fixture`` as a model answer.
* ``case.evidence`` is visible to the scorer only; it is never merged into a
  model prompt.  The model must retrieve through the real RAG tools.
* Semantic claim/behaviour rubrics remain ``pending_human``; no regex and no
  self-judge is used as ground truth.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "runtime") not in sys.path:
    sys.path.insert(0, str(ROOT / "runtime"))

from eval.selection import (  # noqa: E402
    SelectionError,
    build_selection,
    load_manifest,
    select_cases,
    validate_manifest,
)
from eval.source_drift import build_source_drift_audit  # noqa: E402

from modules.evaluation.records import Score, new_run_id, now_iso  # noqa: E402
from eval.scorers import (  # noqa: E402
    SCORER_VERSION,
    aggregate_case_results,
    has_semantic_dimensions,
    overall_status,
    score_citations,
    score_state_subset,
    semantic_review_items,
)

DATA_DIR = ROOT / "eval" / "datasets" / "v1"
RESULTS_DIR = ROOT / "eval" / "results"
BUILT_DIR = ROOT.parent / "robot-vacuum-support-built"
DEFAULT_PORT = 8940
PYTHON = sys.executable
SCHEMA_VERSION = "robot-support-eval/v1"
SUITE_ID = "robot-support-v1"
STABLE_STATE_KEYS = {
    "case_status",
    "tried_actions",
    "not_tried_actions",
    "model",
    "case_count",
    "scope",
}
CASE_REQUIRED = {
    "id",
    "family_id",
    "category",
    "split",
    "origin",
    "review",
    "turns",
    "evidence",
    "setup",
    "scoring",
}
TURN_REQUIRED = {"role", "content", "expected"}
EXPECTED_REQUIRED = {
    "required_claims",
    "forbidden_claims",
    "state",
    "required_behaviors",
    "expected_sources",
}


class DataValidationError(RuntimeError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__("dataset validation failed: " + "; ".join(report.get("errors") or []))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


_TEXT_CACHE: dict[Path, str] = {}


def _extract_text(path: Path) -> str:
    path = path.resolve()
    if path in _TEXT_CACHE:
        return _TEXT_CACHE[path]
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts = ["\n".join((page.extract_text() or "") for page in reader.pages)]
        # Evidence was often checked with ``pdftotext -layout``.  Keep both
        # extraction views for existence checks; this is validation only and
        # never feeds a model prompt.
        try:
            tool = subprocess.run(
                ["pdftotext", "-enc", "UTF-8", "-layout", str(path), "-"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                timeout=60,
            )
            if tool.returncode == 0 and tool.stdout:
                parts.append(tool.stdout)
        except Exception:  # noqa: BLE001 - pypdf text is still usable
            pass
        text = "\n".join(parts)
    else:
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
    _TEXT_CACHE[path] = text
    return text


def _normalise_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _compact_space(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _find_hash_for_state(snapshot: Any) -> list[tuple[str, str]]:
    """Find declared hashes for the P1 state/runtime state source."""
    found: list[tuple[str, str]] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if isinstance(child, str) and re.fullmatch(r"[0-9a-fA-F]{64}", child):
                    lowered = child_path.lower().replace("\\", "/")
                    if (
                        ("p1" in lowered and "state" in lowered)
                        or "state_sha256" in lowered
                        or "state_hash" in lowered
                        or lowered.endswith("robot_vacuum_support/state.py")
                        or lowered.endswith("state.py")
                    ):
                        found.append((child_path, child.lower()))
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(snapshot)
    return found


def _split_ids(split: dict[str, Any], name: str) -> list[str]:
    direct = split.get(name)
    if isinstance(direct, list):
        return [str(item) for item in direct]
    if isinstance(direct, dict):
        for key in ("case_ids", "cases", "ids"):
            if isinstance(direct.get(key), list):
                return [str(item) for item in direct[key]]
    for container in ("splits", "case_ids", "caseIDs", "ids", "by_split"):
        value = split.get(container)
        if isinstance(value, dict):
            nested = value.get(name)
            if isinstance(nested, list):
                return [str(item) for item in nested]
            if isinstance(nested, dict):
                for key in ("case_ids", "cases", "ids"):
                    if isinstance(nested.get(key), list):
                        return [str(item) for item in nested[key]]
    return []


def _family_groups(split: dict[str, Any]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}

    def add_mapping(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for family, members in value.items():
            if isinstance(members, dict):
                members = (
                    members.get("case_ids")
                    or members.get("cases")
                    or members.get("ids")
                    or []
                )
            if isinstance(members, list):
                bucket = groups.setdefault(str(family), [])
                for member in members:
                    if str(member) not in bucket:
                        bucket.append(str(member))

    for key in ("families", "family_groups", "familyGroups"):
        add_mapping(split.get(key))
    for name in ("development", "holdout"):
        block = split.get(name)
        if isinstance(block, dict):
            add_mapping(block.get("families"))
    return groups


def _resolve_evidence_path(file_value: str) -> Path | None:
    raw = str(file_value or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    path = candidate if candidate.is_absolute() else ROOT / candidate
    path = path.resolve()
    try:
        path.relative_to(ROOT)
    except ValueError:
        return None
    return path


def validate_dataset(
    data_dir: Path | str = DATA_DIR,
    *,
    check_evidence: bool = True,
) -> dict[str, Any]:
    """Validate the shared v1 contract and return a machine-readable report."""
    base = Path(data_dir).expanduser().resolve()
    report: dict[str, Any] = {
        "kind": "robot-support-eval-validation",
        "schema_version": SCHEMA_VERSION,
        "data_dir": str(base),
        "checked_at": now_iso(),
        "errors": [],
        "warnings": [],
        "cases": 0,
        "counts": {},
        "freeze": {},
        "checks": {},
    }
    errors: list[str] = report["errors"]
    warnings: list[str] = report["warnings"]

    def error(message: str) -> None:
        errors.append(message)

    def warn(message: str) -> None:
        warnings.append(message)

    suite_path = base / "suite.json"
    cases_path = base / "cases.jsonl"
    split_path = base / "split.json"
    audit_path = base / "audit.json"
    for path in (suite_path, cases_path, split_path, audit_path):
        if not path.is_file():
            error(f"missing required file: {_rel(path)}")
    if errors:
        report["ok"] = False
        return report

    try:
        suite = _read_json(suite_path)
    except Exception as exc:  # noqa: BLE001
        error(f"suite.json is not valid JSON: {type(exc).__name__}: {exc}")
        report["ok"] = False
        return report
    if not isinstance(suite, dict):
        error("suite.json must be an object")
        report["ok"] = False
        return report
    report["suite"] = {
        "schema_version": suite.get("schema_version"),
        "suite_id": suite.get("suite_id"),
        "revision": suite.get("revision"),
        "review_status": suite.get("review_status"),
        "counts": suite.get("counts"),
    }
    if suite.get("schema_version") != SCHEMA_VERSION:
        error(f"suite.schema_version must be {SCHEMA_VERSION!r}")
    if suite.get("suite_id") != SUITE_ID:
        error(f"suite.suite_id must be {SUITE_ID!r}")
    if suite.get("cases_file") != "cases.jsonl":
        error("suite.cases_file must be 'cases.jsonl'")
    if suite.get("split_file") != "split.json":
        error("suite.split_file must be 'split.json'")
    if suite.get("review_status") != "pending_human":
        error("suite.review_status must be 'pending_human' (no fabricated human review)")
    if not isinstance(suite.get("source_snapshot"), dict) or not suite.get("source_snapshot"):
        error("suite.source_snapshot must be a non-empty object")
    suite_counts = suite.get("counts") if isinstance(suite.get("counts"), dict) else {}
    if not suite_counts:
        error("suite.counts must declare development/holdout counts")

    # P1 state snapshot: compare actual source hash with any declared hash.
    actual_state = ROOT / "runtime" / "robot_vacuum_support" / "state.py"
    actual_state_hash = _sha256_file(actual_state) if actual_state.is_file() else None
    declared_state = _find_hash_for_state(suite.get("source_snapshot"))
    matched = (
        all(value == actual_state_hash for _, value in declared_state)
        if declared_state
        else None
    )
    report["checks"]["p1_state"] = {
        "actual_sha256": actual_state_hash,
        "declared": declared_state,
        "matched": matched,
        "usable": bool(actual_state_hash) and matched is not False,
        "source": "declared_source_snapshot" if declared_state else "computed_at_run_start",
    }
    if actual_state_hash is None:
        error("runtime/robot_vacuum_support/state.py is missing")
    elif not declared_state:
        warn(
            "source_snapshot has no P1 state hash; the runner will compute and record "
            "the current state.py hash at run start"
        )
    elif not matched:
        error("source_snapshot P1 state hash does not match runtime/robot_vacuum_support/state.py; rebuild/refresh snapshot")

    # cases.jsonl
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for lineno, raw in enumerate(cases_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        text = raw.strip()
        if not text:
            continue
        try:
            case = json.loads(text)
        except ValueError as exc:
            error(f"cases.jsonl:{lineno} is not valid JSON: {exc}")
            continue
        if not isinstance(case, dict):
            error(f"cases.jsonl:{lineno} must be an object")
            continue
        case["_line"] = lineno
        cases.append(case)
    report["cases"] = len(cases)
    report["case_ids"] = [str(case.get("id") or "") for case in cases]
    if not cases:
        error("cases.jsonl contains no cases")

    evidence_checked = 0
    evidence_failed = 0
    requirement_drift: list[dict[str, Any]] = []
    knowledge_drift: list[dict[str, Any]] = []
    evidence_drift: list[dict[str, Any]] = []
    for case in cases:
        line = case.get("_line")
        prefix = f"case {case.get('id') or f'line-{line}'}"
        missing = CASE_REQUIRED - set(case)
        if missing:
            error(f"{prefix}: missing fields {sorted(missing)}")
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            error(f"{prefix}: id is required")
        elif case_id in seen_ids:
            error(f"{prefix}: duplicate id")
        seen_ids.add(case_id)
        if case.get("split") not in {"development", "holdout"}:
            error(f"{prefix}: split must be development|holdout")
        if not str(case.get("family_id") or "").strip():
            error(f"{prefix}: family_id is required")
        if not str(case.get("category") or "").strip():
            error(f"{prefix}: category is required")
        origin = case.get("origin")
        if not isinstance(origin, dict) or origin.get("kind") != "synthetic":
            error(f"{prefix}: origin.kind must be 'synthetic'")
        elif not isinstance(origin.get("seed_ids"), list):
            error(f"{prefix}: origin.seed_ids must be a list")
        review = case.get("review")
        if not isinstance(review, dict) or review.get("status") != "pending_human":
            error(f"{prefix}: review.status must be 'pending_human'")
        setup = case.get("setup")
        if not isinstance(setup, dict):
            error(f"{prefix}: setup must be an object")
        elif setup.get("kind") == "fault_injection":
            known_faults = {"timeout", "restart", "process_restart", "replay", "empty_retrieval", None}
            if setup.get("fault") not in known_faults:
                warn(f"{prefix}: unknown fault {setup.get('fault')!r}; will be unsupported")
            if not isinstance(setup.get("steps", []), list):
                error(f"{prefix}: setup.steps must be a list")
        scoring = case.get("scoring")
        if not isinstance(scoring, dict):
            error(f"{prefix}: scoring must be an object")
        else:
            for key in ("automatic", "human"):
                if not isinstance(scoring.get(key), list):
                    error(f"{prefix}: scoring.{key} must be a list")
            mode = scoring.get("expected_sources_mode")
            if mode not in {None, "any_of", "all"}:
                warn(f"{prefix}: unknown scoring.expected_sources_mode={mode!r}")
            state_score = scoring.get("state_score")
            if state_score not in {None, "included_current_p1", "not_applicable_current_p1"}:
                warn(f"{prefix}: unknown scoring.state_score={state_score!r}")
            # ``scoring.automatic`` also carries dataset-generation checks
            # (schema/hash/duplicate).  Unknown names are preserved and ignored
            # by the runtime scorer rather than treated as a pass.

        turns = case.get("turns")
        if not isinstance(turns, list) or not turns:
            error(f"{prefix}: turns must be a non-empty list")
            turns = []
        for turn_index, turn in enumerate(turns, 1):
            tprefix = f"{prefix} turn {turn_index}"
            if not isinstance(turn, dict):
                error(f"{tprefix}: turn must be an object")
                continue
            missing_turn = TURN_REQUIRED - set(turn)
            if missing_turn:
                error(f"{tprefix}: missing fields {sorted(missing_turn)}")
            if turn.get("role") not in {"user", "assistant_fixture"}:
                error(f"{tprefix}: role must be user|assistant_fixture")
            if not isinstance(turn.get("content"), str) or not turn.get("content").strip():
                error(f"{tprefix}: content must be a non-empty string")
            expected = turn.get("expected")
            if not isinstance(expected, dict):
                error(f"{tprefix}: expected must be an object")
                continue
            missing_expected = EXPECTED_REQUIRED - set(expected)
            if missing_expected:
                error(f"{tprefix}: expected missing fields {sorted(missing_expected)}")
            for key in ("required_claims", "forbidden_claims", "required_behaviors", "expected_sources"):
                if not isinstance(expected.get(key), list):
                    error(f"{tprefix}: expected.{key} must be a list")
            state = expected.get("state")
            if not isinstance(state, dict):
                error(f"{tprefix}: expected.state must be an object")
            else:
                for key, value in state.items():
                    if key not in STABLE_STATE_KEYS:
                        warn(f"{tprefix}: expected.state.{key} is not a stable v1 key; kept but not auto-scored")
                    if key == "case_status" and value not in {"active", "resolved"}:
                        error(f"{tprefix}: expected.state.case_status must be active|resolved")
                    if key in {"tried_actions", "not_tried_actions"} and not isinstance(value, list):
                        error(f"{tprefix}: expected.state.{key} must be a list")
                    if key == "case_count" and not isinstance(value, int):
                        error(f"{tprefix}: expected.state.case_count must be an integer")

        evidence = case.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            error(f"{prefix}: evidence must be a non-empty list")
            evidence = []
        evidence_ids: set[str] = set()
        for evidence_index, item in enumerate(evidence, 1):
            eprefix = f"{prefix} evidence {evidence_index}"
            if not isinstance(item, dict):
                error(f"{eprefix}: evidence must be an object")
                continue
            for key in ("id", "file", "quote", "sha256", "kind"):
                if not item.get(key):
                    error(f"{eprefix}: missing {key}")
            evidence_id = str(item.get("id") or "")
            if evidence_id in evidence_ids:
                error(f"{eprefix}: duplicate evidence id {evidence_id!r}")
            evidence_ids.add(evidence_id)
            kind = str(item.get("kind") or "")
            if kind not in {"knowledge", "requirement"}:
                error(f"{eprefix}: kind must be knowledge|requirement")
            path = _resolve_evidence_path(str(item.get("file") or ""))
            if path is None:
                error(f"{eprefix}: file must stay inside this project")
                continue
            if not path.is_file():
                error(f"{eprefix}: file does not exist: {_rel(path)}")
                continue
            declared = str(item.get("sha256") or "").lower()
            actual = _sha256_file(path)
            hash_match = declared == actual
            quote = str(item.get("quote") or "")
            quote_status = "not_checked"
            if check_evidence:
                evidence_checked += 1
                try:
                    source_text = _extract_text(path)
                except Exception as exc:  # noqa: BLE001
                    error(f"{eprefix}: cannot extract text from {_rel(path)}: {type(exc).__name__}: {exc}")
                    evidence_failed += 1
                    continue
                if _normalise_space(quote) in _normalise_space(source_text):
                    quote_status = "exact"
                elif _compact_space(quote) and _compact_space(quote) in _compact_space(source_text):
                    quote_status = "whitespace_normalised"
                    warn(f"{eprefix}: quote matches only after whitespace normalisation")
                else:
                    quote_status = "missing"
            if not hash_match:
                drift_row = {
                    "case_id": case.get("id"),
                    "evidence_id": item.get("id"),
                    "file": _rel(path),
                    "kind": kind,
                    "declared_sha256": declared,
                    "current_sha256": actual,
                    "quote_status": quote_status,
                }
                evidence_drift.append(drift_row)
                if kind == "knowledge":
                    knowledge_drift.append(drift_row)
                    error(f"{eprefix}: sha256 mismatch for knowledge source {_rel(path)}")
                elif kind == "requirement" and quote_status in {"exact", "whitespace_normalised"}:
                    requirement_drift.append(drift_row)
                    warn(
                        f"{eprefix}: requirement hash drift waived for {_rel(path)}; "
                        "the original quote is still present and requirement text is not RAG knowledge"
                    )
                else:
                    error(
                        f"{eprefix}: sha256 mismatch for {_rel(path)} and the original "
                        "requirement quote is not provably present"
                    )
            if quote_status == "missing":
                error(f"{eprefix}: quote not found verbatim in {_rel(path)}")
                evidence_failed += 1

    report["checks"]["evidence"] = {
        "quote_checks": evidence_checked,
        "failed": evidence_failed,
        "requirement_hash_drift_waived": len(requirement_drift),
        "knowledge_hash_drift": len(knowledge_drift),
        "evidence_drift": evidence_drift,
    }

    # Independent source-drift audit.  It never edits the frozen dataset or the
    # human review records.  A waiver is recorded only when requirement files
    # drifted, all original quotes are still present, and the current state /
    # question schema still exposes the reviewed stable keys.
    source_drift = build_source_drift_audit(dataset_dir=base)
    report["source_drift"] = source_drift
    report["waivers"] = [source_drift["waiver"]] if source_drift.get("waived") else []
    for row in source_drift.get("knowledge_drift") or []:
        message = f"knowledge source drift: {row.get('path')} declared={str(row.get('declared_sha256'))[:12]} current={str(row.get('current_sha256'))[:12]}"
        if not any(str(row.get("path")) in item for item in errors):
            error(message)
    if source_drift.get("quote_failures"):
        for row in source_drift["quote_failures"]:
            message = (
                f"source drift lost original quote: case {row.get('case_id')} "
                f"evidence {row.get('evidence_id')} in {row.get('file')}"
            )
            if not any(message.split(":")[0] in item for item in errors):
                error(message)
    if source_drift.get("requirement_drift") and not source_drift.get("waived"):
        error(
            "requirement source drift is not waivable: a quote is missing, a knowledge "
            "file changed, or the reviewed schema is not compatible"
        )

    # split.json
    try:
        split = _read_json(split_path)
    except Exception as exc:  # noqa: BLE001
        error(f"split.json is not valid JSON: {type(exc).__name__}: {exc}")
        split = {}
    if not isinstance(split, dict):
        error("split.json must be an object")
        split = {}
    dev_ids = _split_ids(split, "development")
    holdout_ids = _split_ids(split, "holdout")
    if not dev_ids:
        error("split.json has no development case IDs")
    if not holdout_ids:
        error("split.json has no holdout case IDs")
    split_dev = set(dev_ids)
    split_holdout = set(holdout_ids)
    overlap = sorted(split_dev & split_holdout)
    if overlap:
        error(f"development/holdout overlap: {overlap}")
    case_dev = {str(case.get("id")) for case in cases if case.get("split") == "development"}
    case_holdout = {str(case.get("id")) for case in cases if case.get("split") == "holdout"}
    if split_dev != case_dev:
        error(
            "split.json development IDs do not match case.split: "
            f"only_in_cases={sorted(case_dev - split_dev)} only_in_split={sorted(split_dev - case_dev)}"
        )
    if split_holdout != case_holdout:
        error(
            "split.json holdout IDs do not match case.split: "
            f"only_in_cases={sorted(case_holdout - split_holdout)} only_in_split={sorted(split_holdout - case_holdout)}"
        )
    actual_counts = {"development": len(case_dev), "holdout": len(case_holdout)}
    report["counts"] = actual_counts
    for name, expected_count in suite_counts.items():
        if name not in actual_counts:
            warn(f"suite.counts contains unknown split {name!r}")
            continue
        if int(expected_count) != actual_counts[name]:
            error(f"suite.counts.{name}={expected_count} but cases contain {actual_counts[name]}")
    if actual_counts != {"development": 32, "holdout": 16}:
        warn(f"contract example is 32/16; actual is {actual_counts}")
    families = _family_groups(split)
    report["checks"]["families"] = {
        "groups": len(families),
        "cases_in_groups": sum(len(items) for items in families.values()),
    }
    if not families:
        warn("split.json has no recognised family grouping")
    else:
        known = case_dev | case_holdout
        for family, members in families.items():
            unknown = sorted(set(members) - known)
            if unknown:
                error(f"split family {family!r} references unknown case IDs {unknown}")
            for case in cases:
                if case.get("id") in members and str(case.get("family_id")) != family:
                    error(f"case {case.get('id')} family_id does not match split group {family!r}")

    # audit.json
    try:
        audit = _read_json(audit_path)
    except Exception as exc:  # noqa: BLE001
        error(f"audit.json is not valid JSON: {type(exc).__name__}: {exc}")
        audit = {}
    if not isinstance(audit, dict):
        error("audit.json must be an object")
        audit = {}
    report["checks"]["audit_keys"] = sorted(str(key) for key in audit)
    if not any(
        any(token in str(key).lower() for token in ("old", "legacy", "recharge", "16", "audit"))
        for key in audit
    ):
        warn("audit.json shape unrecognised; old-16 evidence not proven by this validator")

    def verify_audit_hashes(value: Any, path: str = "") -> int:
        matches = 0
        if isinstance(value, dict):
            file_value = value.get("path") or value.get("file")
            hash_value = value.get("sha256") or value.get("hash")
            if isinstance(file_value, str) and isinstance(hash_value, str) and re.fullmatch(
                r"[0-9a-fA-F]{64}", hash_value
            ):
                target = _resolve_evidence_path(file_value)
                if target is not None and target.is_file():
                    actual = _sha256_file(target)
                    if actual != hash_value.lower():
                        error(f"audit {path or _rel(target)} sha256 mismatch")
                    else:
                        matches += 1
            for key, child in value.items():
                matches += verify_audit_hashes(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                matches += verify_audit_hashes(child, f"{path}[{index}]")
        return matches

    report["checks"]["audit_verified_hashes"] = verify_audit_hashes(audit)

    file_hashes = {
        "suite.json": _sha256_file(suite_path),
        "cases.jsonl": _sha256_file(cases_path),
        "split.json": _sha256_file(split_path),
        "audit.json": _sha256_file(audit_path),
    }
    freeze_seed = "\n".join(f"{name}:{digest}" for name, digest in file_hashes.items())
    report["freeze"] = {
        "dataset_hash": _sha256_bytes(freeze_seed.encode("utf-8")),
        "file_sha256": file_hashes,
        "freeze_note": "hash of suite.json+cases.jsonl+split.json+audit.json bytes; holdout not executed here",
    }
    report["ok"] = not errors
    return report


class SuiteData:
    def __init__(self, base: Path, suite: dict[str, Any], cases: list[dict[str, Any]], split: dict[str, Any], audit: dict[str, Any], validation: dict[str, Any]):
        self.base = base
        self.suite = suite
        self.cases = cases
        self.split = split
        self.audit = audit
        self.validation = validation
        self.dataset_hash = validation["freeze"]["dataset_hash"]
        self.file_hashes = validation["freeze"]["file_sha256"]
        self.by_id = {case["id"]: case for case in cases}


def load_dataset(data_dir: Path | str = DATA_DIR, *, check_evidence: bool = True) -> SuiteData:
    base = Path(data_dir).expanduser().resolve()
    report = validate_dataset(base, check_evidence=check_evidence)
    if not report.get("ok"):
        raise DataValidationError(report)
    suite = _read_json(base / "suite.json")
    split = _read_json(base / "split.json")
    audit = _read_json(base / "audit.json")
    cases: list[dict[str, Any]] = []
    for line in (base / "cases.jsonl").read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return SuiteData(base, suite, cases, split, audit, report)


def _select_cases(data: SuiteData, split: str, limit: int = 0, ids: list[str] | None = None) -> list[dict[str, Any]]:
    selected = [case for case in data.cases if case.get("split") == split]
    if ids:
        wanted = set(ids)
        selected = [case for case in selected if case.get("id") in wanted]
    if limit and limit > 0:
        selected = selected[:limit]
    return selected


def _case_input_hash(case: dict[str, Any]) -> str:
    payload = [
        {"role": turn.get("role"), "content": turn.get("content")}
        for turn in case.get("turns") or []
    ]
    return _sha256_bytes(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _case_expected_hash(case: dict[str, Any]) -> str:
    payload = {
        "id": case.get("id"),
        "turns": [
            {"role": turn.get("role"), "expected": turn.get("expected")}
            for turn in case.get("turns") or []
        ],
    }
    return _sha256_bytes(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _knowledge_expected_sources(case: dict[str, Any], turn: dict[str, Any]) -> list[str]:
    """Drop requirement-only files (e.g. README.md) from retrieval checks."""
    evidence_kind = {
        os.path.basename(str(item.get("file") or "").replace("\\", "/")).lower(): item.get("kind")
        for item in case.get("evidence") or []
        if isinstance(item, dict)
    }
    expected = (turn.get("expected") or {}).get("expected_sources") or []
    return [
        item
        for item in expected
        if evidence_kind.get(os.path.basename(str(item).replace("\\", "/")).lower()) != "requirement"
    ]


def _score_objects(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for check in checks:
        status = check.get("status")
        rows.append(
            Score(
                name=check.get("name") or "check",
                value=1.0 if status == "pass" else (0.0 if status == "fail" else 0.0),
                passed=status == "pass",
                reason=str(check.get("detail") or ""),
                metadata={
                    "status": status,
                    "expected": check.get("expected"),
                    "actual": check.get("actual"),
                    "dimension": check.get("dimension"),
                },
                data_type="BOOLEAN" if status in {"pass", "fail"} else "CATEGORICAL",
                source="EVAL",
            ).to_dict()
        )
    return rows


def _runtime_check(error: str | None = None) -> dict[str, Any]:
    return {
        "name": "runtime.no_error",
        "dimension": "automatic",
        "status": "fail" if error else "pass",
        "expected": "no runtime exception",
        "actual": error,
        "detail": error or "",
    }


def _semantic_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _number(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _turn_usage(usage: Any) -> dict[str, Any]:
    """Normalise the new turn-usage/v1 object and the legacy total-token shape."""
    if not isinstance(usage, dict) or not usage:
        return {
            "source": "missing",
            "model_requests": None,
            "model_calls": None,
            "ledger_events": None,
            "total_tokens": None,
            "usage_status": "unavailable",
            "by_layer": {},
            "route": None,
        }
    metrics = usage.get("turn_metrics")
    if isinstance(metrics, dict):
        by_layer: dict[str, dict[str, Any]] = {}
        for name, row in (metrics.get("by_layer") or {}).items():
            if not isinstance(row, dict):
                continue
            by_layer[str(name)] = {
                "requests": _number(row.get("requests")),
                "model_calls": _number(row.get("model_calls")),
                "input_tokens": _number(row.get("input_tokens")),
                "output_tokens": _number(row.get("output_tokens")),
                "total_tokens": _number(row.get("total_tokens")),
                "usage_status": row.get("usage_status"),
                "models": row.get("models") or [],
                "methods": row.get("methods") or [],
            }
        total = metrics.get("total") or {}
        calls = metrics.get("calls") or []
        return {
            "source": "turn_metrics",
            "model_requests": _number(metrics.get("requests")),
            "model_calls": _number(metrics.get("model_calls")),
            "ledger_events": len(calls) if isinstance(calls, list) else None,
            "total_tokens": _number(total.get("total_tokens") if isinstance(total, dict) else None),
            "input_tokens": _number(total.get("input_tokens") if isinstance(total, dict) else None),
            "output_tokens": _number(total.get("output_tokens") if isinstance(total, dict) else None),
            "usage_status": metrics.get("usage_status"),
            "route": metrics.get("route"),
            "route_reason": metrics.get("route_reason"),
            "by_layer": by_layer,
            "cost": metrics.get("cost"),
            "cost_status": metrics.get("cost_status"),
        }
    # Legacy shape: {"total_tokens": N, "token_limit": null}.  It proves token
    # usage but not a per-request/per-layer split, so requests stay null.
    return {
        "source": "legacy_total",
        "model_requests": None,
        "model_calls": None,
        "ledger_events": None,
        "total_tokens": _number(usage.get("total_tokens")),
        "usage_status": "reported" if _number(usage.get("total_tokens")) is not None else "partial",
        "by_layer": {},
        "route": None,
        "cost": None,
        "cost_status": "unavailable_no_authoritative_unit_price",
    }


def _aggregate_usage(turns: list[dict[str, Any]]) -> dict[str, Any]:
    per_turn: list[dict[str, Any]] = []
    by_layer: dict[str, dict[str, Any]] = {}
    requests_values: list[int] = []
    calls_values: list[int] = []
    ledger_values: list[int] = []
    token_values: list[int] = []
    unknown_requests = False
    unknown_calls = False
    statuses: dict[str, int] = {}
    for turn in turns:
        row = _turn_usage(turn.get("usage"))
        row["turn"] = turn.get("index")
        per_turn.append(row)
        status = str(row.get("usage_status") or "unavailable")
        statuses[status] = statuses.get(status, 0) + 1
        if row.get("model_requests") is None:
            unknown_requests = True
        else:
            requests_values.append(row["model_requests"])
        if row.get("model_calls") is None:
            unknown_calls = True
        else:
            calls_values.append(row["model_calls"])
        if row.get("ledger_events") is not None:
            ledger_values.append(row["ledger_events"])
        if row.get("total_tokens") is not None:
            token_values.append(row["total_tokens"])
        for name, layer in (row.get("by_layer") or {}).items():
            target = by_layer.setdefault(
                name,
                {"requests": 0, "model_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
            for key in ("requests", "model_calls", "input_tokens", "output_tokens", "total_tokens"):
                value = layer.get(key)
                if value is not None:
                    target[key] += int(value)
    if not per_turn:
        status = "not_called"
    elif any(item.get("usage_status") == "unavailable" for item in per_turn) or unknown_requests or unknown_calls:
        status = "partial" if token_values else "unavailable"
    elif statuses.get("not_called") == len(per_turn):
        status = "not_called"
    else:
        status = "reported"
    return {
        "model_requests": None if unknown_requests else sum(requests_values),
        "model_calls": None if unknown_calls else sum(calls_values),
        "ledger_events": sum(ledger_values) if ledger_values else 0,
        "total_tokens": sum(token_values) if token_values else None,
        "usage_status": status,
        "per_turn": per_turn,
        "by_layer": dict(sorted(by_layer.items())),
        "cost": None,
        "cost_currency": None,
        "cost_status": "unavailable_no_authoritative_unit_price",
        "note": (
            "model_requests is the real request counter; ledger_events is the number of "
            "aggregate usage rows. Missing values stay null and are never estimated."
        ),
    }


def _no_model_usage(reason: str) -> dict[str, Any]:
    return {
        "model_requests": 0,
        "model_calls": 0,
        "ledger_events": 0,
        "total_tokens": 0,
        "usage_status": "not_called",
        "reason": reason,
        "per_turn": [],
        "by_layer": {},
        "cost": None,
        "cost_currency": None,
        "cost_status": "unavailable_no_authoritative_unit_price",
    }


def _is_transient_error(error: str | None) -> bool:
    text = str(error or "").lower()
    return any(
        token in text
        for token in (
            "timeouterror",
            "timed out",
            "timeout",
            "urlerror",
            "socket.timeout",
            "connection",
            "remotedisconnected",
            "transport failed",
            "temporarily unavailable",
        )
    )


def _base_row(case: dict[str, Any], *, mode: str, bucket: str) -> dict[str, Any]:
    return {
        "case_id": case.get("id"),
        "family_id": case.get("family_id"),
        "category": case.get("category"),
        "split": case.get("split"),
        "mode": mode,
        "bucket": bucket,
        "origin": case.get("origin"),
        "review": case.get("review"),
        "setup": case.get("setup"),
        "turns": [],
        "checks": [],
        "semantic_review": [],
        "status": None,
        "attempted": False,
        "error": None,
        "skip_reason": None,
        "latency_ms": None,
        "usage": None,
        "trace_ids": [],
        "started_at": now_iso(),
        "ended_at": None,
        "case": case,
    }


def run_state_case(case: dict[str, Any], *, now_base: float | None = None) -> dict[str, Any]:
    from robot_vacuum_support import state as support

    row = _base_row(case, mode="state", bucket="fault" if (case.get("setup") or {}).get("kind") == "fault_injection" else "qa")
    if (case.get("setup") or {}).get("kind") == "fault_injection":
        row["attempted"] = any(turn.get("role") == "user" for turn in case.get("turns") or [])
        row["status"] = "unsupported"
        row["skip_reason"] = (
            "state mode has no real fault injection; replaying the pure state sequence "
            "is not restart/recovery evidence and must not be counted as pass/fail"
        )
        row["checks"] = [
            {
                "name": "fault.injection",
                "dimension": "automatic",
                "status": "unsupported",
                "expected": (case.get("setup") or {}).get("fault"),
                "actual": None,
                "detail": "pure state replay is not a fault injection run",
            }
        ]
        row["ended_at"] = now_iso()
        row["scores"] = _score_objects(row["checks"])
        row["semantic_counts"] = {}
        row["usage_summary"] = _no_model_usage("state mode has no real fault injection")
        row["llm_calls"] = 0
        return row
    state_score = (case.get("scoring") or {}).get("state_score")
    state_scored = state_score != "not_applicable_current_p1"
    row["state_score"] = state_score
    semantic_items = semantic_review_items(case.get("turns") or [], mode="state")
    row["semantic_review"] = semantic_items
    row["attempted"] = any(turn.get("role") == "user" for turn in case.get("turns") or [])
    if not state_scored:
        row["checks"].append(
            {
                "name": "state.contract",
                "dimension": "automatic",
                "status": "not_applicable",
                "expected": "state semantics",
                "actual": state_score,
                "detail": "dataset marks state_score=not_applicable_current_p1; QA/human only",
            }
        )
    base_time = float(now_base) if now_base is not None else time.time()
    state = support.empty_state(now=base_time)
    scope: str | None = None
    error: str | None = None
    started = time.perf_counter()
    try:
        for index, turn in enumerate(case.get("turns") or [], 1):
            role = turn.get("role")
            content = str(turn.get("content") or "")
            expected = turn.get("expected") or {}
            message_id = f"{case['id']}-t{index}-{role}"
            direct = None
            if role == "user":
                result = support.apply_user_turn(
                    state,
                    content,
                    message_id,
                    now=base_time + index,
                )
                state = result["state"]
                scope = result.get("scope")
                direct = result.get("direct_answer")
            elif role == "assistant_fixture":
                state = support.apply_assistant_turn(
                    state,
                    content,
                    message_id,
                    turn=int(state.get("turn") or index),
                    now=base_time + index,
                )
            else:
                raise ValueError(f"unsupported turn role: {role!r}")
            view = support.public_view(state)
            checks = (
                score_state_subset(expected.get("state"), view, scope=scope)
                if state_scored
                else []
            )
            row["checks"].extend(checks)
            row["turns"].append(
                {
                    "index": index,
                    "role": role,
                    "content": content,
                    "direct_answer": direct,
                    "scope": scope,
                    "support": view,
                    "state_checks": checks,
                }
            )
    except Exception as exc:  # noqa: BLE001 - failures are recorded, not dropped
        error = f"{type(exc).__name__}: {exc}"
    row["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    row["checks"].append(_runtime_check(error))
    row["error"] = error
    state_checks = [item for item in row["checks"] if item["name"].startswith("state.") and item["status"] in {"pass", "fail"}]
    if not error and not state_checks:
        row["status"] = "not_applicable"
        row["skip_reason"] = (
            f"state_score={state_score or 'unspecified'}; no included state expectation, "
            "so the pure run has no automatic target"
        )
    else:
        row["status"] = overall_status(row["checks"], semantic_items, error=error)
    row["ended_at"] = now_iso()
    row["scores"] = _score_objects(row["checks"])
    row["semantic_counts"] = _semantic_counts(semantic_items)
    row["usage_summary"] = _no_model_usage("pure state mode makes no model call")
    row["llm_calls"] = 0
    return row


def _events_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    answer = ""
    support = None
    retrieval = None
    trace_id = None
    usage = None
    errors: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("type")
        if kind == "answer":
            answer = event.get("answer") or answer
            support = event.get("support") or support
            retrieval = event.get("retrieval") or retrieval
            trace_id = event.get("trace_id") or trace_id
            usage = event.get("usage") or usage
        elif kind == "answer_delta":
            answer += event.get("delta") or ""
        elif kind == "interrupt":
            support = event.get("support") or support
            retrieval = event.get("retrieval") or retrieval
            trace_id = event.get("trace_id") or trace_id
            usage = event.get("usage") or usage
        elif kind == "error":
            errors.append(event)
            support = event.get("support") or support
            retrieval = event.get("retrieval") or retrieval
    return {
        "answer": answer,
        "support": support,
        "retrieval": retrieval,
        "trace_id": trace_id,
        "usage": usage,
        "errors": errors,
        "event_types": [event.get("type") for event in events],
    }


class LiveServer:
    def __init__(
        self,
        *,
        python: str,
        serve_py: Path,
        instance_path: Path,
        host: str,
        port: int,
        log_path: Path,
        cwd: Path,
    ):
        self.python = python
        self.serve_py = serve_py
        self.instance_path = instance_path
        self.host = host
        self.port = port
        self.log_path = log_path
        self.cwd = cwd
        self.process: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _creationflags(self) -> int:
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def _port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            return sock.connect_ex((self.host, self.port)) == 0

    def start(self, *, timeout: float = 120.0) -> dict[str, Any]:
        if self._port_in_use():
            raise RuntimeError(f"port {self.port} is already in use")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("AH_SPEC_ROOT", None)
        log = self.log_path.open("a", encoding="utf-8")
        self._log_handle = log
        self.process = subprocess.Popen(
            [self.python, str(self.serve_py), "--instance-config", str(self.instance_path)],
            cwd=str(self.cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=self._creationflags(),
            start_new_session=os.name != "nt",
        )
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"server exited early with code {self.process.returncode}; see {self.log_path}")
            try:
                last = _http_json(self.base_url + "/api/health", timeout=3)
                if last.get("ready"):
                    return last
            except Exception as exc:  # noqa: BLE001
                last = {"error": type(exc).__name__}
            time.sleep(1)
        raise RuntimeError(f"server not ready within {timeout:.0f}s: {last}")

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            pass
        elif process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=self._creationflags(),
                    timeout=20,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        handle = getattr(self, "_log_handle", None)
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_handle = None

    def restart(self) -> dict[str, Any]:
        self.stop()
        time.sleep(1)
        return self.start()


def _http_json(url: str, payload: dict[str, Any] | None = None, *, timeout: float = 60) -> Any:
    data = None
    headers = {}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict[str, Any], *, timeout: float = 60) -> Any:
    return _http_json(url, payload, timeout=timeout)


def _delete_session(base_url: str, session_id: str, *, timeout: float = 30) -> None:
    """Release the per-case session/workspace promptly; failure is non-fatal."""
    request = urllib.request.Request(
        base_url + f"/api/sessions/{session_id}",
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except Exception:  # noqa: BLE001 - cleanup must not mask the case result
        pass


def _send_message(base_url: str, session_id: str, prompt: str, *, timeout: float = 300) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        base_url + f"/api/sessions/{session_id}/messages",
        data=json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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
            if event.get("type") == "done":
                break
    return events


def _send_with_one_retry(
    base_url: str,
    session_id: str,
    prompt: str,
    *,
    timeout: float = 300,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Retry at most once on a client-side transport timeout; record the fact."""
    attempts = 0
    last_error: Exception | None = None
    while attempts < 2:
        attempts += 1
        try:
            events = _send_message(base_url, session_id, prompt, timeout=timeout)
            return events, {"attempts": attempts, "retried": attempts > 1}
        except (TimeoutError, urllib.error.URLError, socket.timeout) as exc:
            last_error = exc
            if attempts >= 2:
                break
            time.sleep(1)
    raise RuntimeError(f"transport failed after {attempts} attempt(s): {type(last_error).__name__}: {last_error}")


def _env_file_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_override = os.environ.get("ROBOT_VACUUM_INSTANCE_ENV") or os.environ.get("ROBOT_VACUUM_ENV_FILE")
    if env_override:
        candidates.append(Path(env_override))
    legacy_instance = ROOT / "outputs" / "p1-instance" / "instance.json"
    if legacy_instance.is_file():
        try:
            payload = _read_json(legacy_instance)
            value = payload.get("environment_file")
            if value:
                candidates.append(Path(str(value)))
        except Exception:  # noqa: BLE001
            pass
    candidates.append(ROOT.parent / "my_agent_harness" / ".env")
    return candidates


def _choose_env_file() -> Path | None:
    seen: set[str] = set()
    for candidate in _env_file_candidates():
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


def _model_name() -> str | None:
    rag = ROOT / "runtime" / "config" / "rag.yml"
    if not rag.is_file():
        return None
    import yaml

    try:
        payload = yaml.safe_load(rag.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return None
    provider = payload.get("chat_provider")
    model = payload.get("chat_model_name")
    return f"{provider}/{model}" if provider and model else (str(model) if model else None)


def _active_spec() -> tuple[str, str, str]:
    active = _read_json(ROOT / "robot_vacuum" / "active.json")
    version = str(active.get("version") or active.get("active") or "")
    path = ROOT / "robot_vacuum" / f"v{version}.json"
    if not path.is_file():
        candidates = sorted((ROOT / "robot_vacuum").glob("v*.json"))
        path = candidates[-1] if candidates else ROOT / "robot_vacuum" / "v1.json"
    return version, _rel(path), _sha256_file(path)


def _prompt_info() -> dict[str, Any]:
    version, path, digest = _active_spec()
    try:
        spec = _read_json(ROOT / path)
    except Exception:  # noqa: BLE001
        return {"spec_version": version, "spec_file": path, "spec_sha256": digest, "prompt_sha256": None}
    prompt = str(spec.get("system_prompt") or "")
    return {
        "spec_version": version,
        "spec_file": path,
        "spec_sha256": digest,
        "prompt_source": "spec.system_prompt",
        "prompt_sha256": _sha256_bytes(prompt.encode("utf-8")) if prompt else None,
        "prompt_chars": len(prompt),
    }


def _tree_fingerprint(path: Path) -> dict[str, Any]:
    """Deterministic file-tree fingerprint; excludes caches and generated bytes."""
    root = Path(path)
    if not root.is_dir():
        return {"exists": False, "files": 0, "sha256": None}
    digest = hashlib.sha256()
    count = 0
    for file in sorted(item for item in root.rglob("*") if item.is_file()):
        rel = file.relative_to(root).as_posix()
        if "__pycache__" in rel or rel.endswith((".pyc", ".pyo")):
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(file).encode("ascii"))
        digest.update(b"\n")
        count += 1
    return {"exists": True, "files": count, "sha256": digest.hexdigest()}


def _source_fingerprint() -> dict[str, Any]:
    """Fingerprint product source only; excludes generated outputs/results/caches."""
    digest = hashlib.sha256()
    count = 0
    candidates: list[Path] = [
        ROOT / "project.json",
        ROOT / "README.md",
        ROOT / "requirements.txt",
        ROOT / "runtime",
        ROOT / "product_ui",
        ROOT / "robot_vacuum",
    ]
    for candidate in candidates:
        files = [candidate] if candidate.is_file() else sorted(
            item for item in candidate.rglob("*") if item.is_file()
        ) if candidate.is_dir() else []
        for file in files:
            rel = file.relative_to(ROOT).as_posix()
            if "__pycache__" in rel or rel.endswith((".pyc", ".pyo")):
                continue
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(_sha256_file(file).encode("ascii"))
            digest.update(b"\n")
            count += 1
    return {"exists": True, "files": count, "sha256": digest.hexdigest(), "scope": "project.json+README+requirements+runtime+product_ui+robot_vacuum"}


def _build_live_artifact(run_dir: Path) -> dict[str, Any]:
    """Build a fresh artifact from this project's source; never reuse old IDs."""
    from modules.artifacts.project import build_project

    built = build_project(str(ROOT), str(BUILT_DIR), include_assets=True)
    bundle = Path(built["bundle"])
    manifest = built["manifest"]
    # Hidden, no terminal/browser window.
    check = subprocess.run(
        [PYTHON, str(bundle / "run.py"), "--check"],
        cwd=str(ROOT.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        timeout=180,
    )
    state_source = ROOT / "runtime" / "robot_vacuum_support" / "state.py"
    built_state_source = bundle / "runtime" / "robot_vacuum_support" / "state.py"
    state_hash = _sha256_file(state_source)
    built_state_hash = _sha256_file(built_state_source) if built_state_source.is_file() else None
    return {
        "artifact_id": manifest.get("artifact_id"),
        "artifact_root": str(bundle),
        "zip": built.get("zip"),
        "version": manifest.get("version"),
        "source": manifest.get("source"),
        "runtime_hash": (manifest.get("runtime") or {}).get("hash"),
        "content_hash": manifest.get("content_hash"),
        "built_from_source": str(ROOT),
        "built_at": now_iso(),
        "check": {
            "returncode": check.returncode,
            "stdout": (check.stdout or "")[-2000:],
            "stderr": (check.stderr or "")[-2000:],
        },
        "p1_state_sha256": state_hash,
        "built_p1_state_sha256": built_state_hash,
        "p1_state_matches_source": built_state_hash == state_hash,
        **_prompt_info(),
        "model": _model_name(),
        "fingerprints": {
            "source_project": _source_fingerprint(),
            "runtime_source": _tree_fingerprint(ROOT / "runtime"),
            "frontend_source": _tree_fingerprint(ROOT / "product_ui"),
            "spec_source": _tree_fingerprint(ROOT / "robot_vacuum"),
            "built_bundle": _tree_fingerprint(bundle),
        },
        "external_cwd": str(ROOT.parent),
        "external_cwd_check": check.returncode == 0,
    }


def _write_instance(
    *,
    run_dir: Path,
    artifact: dict[str, Any],
    env_file: Path,
    host: str,
    port: int,
) -> tuple[Path, Path]:
    storage = run_dir / "instance-storage"
    storage.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "instance_id": f"robot-vacuum-eval-{run_dir.name}",
        "artifact_id": artifact["artifact_id"],
        "artifact_root": artifact["artifact_root"],
        "name": "robot-vacuum eval v1",
        "scenario": "robot_vacuum",
        "version": int(artifact.get("version") or 0),
        "workflow": "agent",
        "host": host,
        "port": port,
        "storage_root": str(storage),
        "environment_file": str(env_file),
        "asset_bindings": {},
    }
    path = run_dir / "instance.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, storage


def run_live_case(
    case: dict[str, Any],
    *,
    base_url: str,
    server: LiveServer,
    now_base: float | None = None,
    timeout: float = 300,
) -> dict[str, Any]:
    setup = case.get("setup") or {}
    bucket = "fault" if setup.get("kind") == "fault_injection" else "qa"
    row = _base_row(case, mode="live", bucket=bucket)
    row["case_timeout_s"] = timeout
    turns = case.get("turns") or []
    semantic_items = semantic_review_items(turns, mode="live")
    row["semantic_review"] = semantic_items
    row["attempted"] = any(turn.get("role") == "user" for turn in turns)
    if not row["attempted"]:
        row["status"] = "not_applicable"
        row["skip_reason"] = "no user turn"
        row["usage_summary"] = _no_model_usage("no user turn")
        row["ended_at"] = now_iso()
        return row
    if any(turn.get("role") == "assistant_fixture" for turn in turns):
        # A fixture is a canned prior assistant turn.  Replaying it would not be
        # a real model output, so live mode does NOT inject it.  The model's own
        # prior answer becomes the live conversation context; this is recorded
        # as an explicit live variant, never as a fixture-as-model answer.
        row["fixture_policy"] = "assistant_fixture_retained_in_dataset_but_not_replayed_live"
        row["live_variant"] = "model_generated_prior_turn"
        for item in semantic_items:
            if item.get("source") == "assistant_fixture":
                item["status"] = "not_applicable"
                item["reason"] = "assistant_fixture was not replayed as a live model output"
    else:
        row["fixture_policy"] = "none"
        row["live_variant"] = "frozen_turns"

    declared_fault = setup.get("fault") if setup.get("kind") == "fault_injection" else None
    fault = "restart" if declared_fault in {"restart", "process_restart"} else declared_fault
    if setup.get("kind") == "fault_injection" and fault not in {"restart", "replay"}:
        row["status"] = "unsupported"
        row["skip_reason"] = (
            f"fault {declared_fault!r} has no real injection interface in this runtime; "
            "refusing to fake a timeout/empty retrieval or count it as pass"
        )
        row["usage_summary"] = _no_model_usage("fault has no real injection interface")
        row["ended_at"] = now_iso()
        return row

    started = time.perf_counter()
    error: str | None = None
    session_id: str | None = None
    transport: dict[str, Any] | None = None
    try:
        session = _post_json(base_url + "/api/sessions", {"workflow": "agent"})
        session_id = session.get("session_id")
        if not session_id:
            raise RuntimeError("session creation returned no session_id")
        row["session_id"] = session_id
        for index, turn in enumerate(turns, 1):
            if turn.get("role") != "user":
                continue
            content = str(turn.get("content") or "")
            expected = turn.get("expected") or {}
            turn_started = time.perf_counter()
            events, transport = _send_with_one_retry(base_url, session_id, content, timeout=timeout)
            turn_latency = round((time.perf_counter() - turn_started) * 1000, 3)
            summary = _events_summary(events)
            detail = None
            try:
                detail = _http_json(base_url + f"/api/sessions/{session_id}", timeout=30)
            except Exception:  # noqa: BLE001
                detail = None
            support = summary.get("support") or (detail or {}).get("support")
            retrieval = summary.get("retrieval") or (support or {}).get("last_retrieval") or {"status": "not_retrieved", "results": []}
            if summary.get("errors"):
                error_text = "; ".join(str(item.get("error") or "") for item in summary["errors"])
                error = error_text or "runtime error event"
            expected_sources = expected.get("expected_sources") or []
            knowledge_sources = _knowledge_expected_sources(case, turn)
            source_mode = (case.get("scoring") or {}).get("expected_sources_mode") or "any_of"
            checks = score_citations(
                summary.get("answer") or "",
                retrieval,
                expected_sources=knowledge_sources,
                require_citation=bool(knowledge_sources),
                expected_sources_mode=source_mode,
            )
            row.setdefault("expected_sources", {})[str(index)] = {
                "all": expected_sources,
                "knowledge_checked": knowledge_sources,
                "mode": source_mode,
            }
            state_score = (case.get("scoring") or {}).get("state_score")
            if support is not None and state_score != "not_applicable_current_p1":
                topic = ((support or {}).get("active_case") or {}).get("topic")
                observed_scope = "recharge" if topic == "recharge_failure" else ("other" if (support or {}).get("active_case") is not None else None)
                checks.extend(
                    score_state_subset(
                        expected.get("state"),
                        support,
                        scope=observed_scope,
                    )
                )
            elif support is not None:
                checks.append(
                    {
                        "name": "state.contract",
                        "dimension": "automatic",
                        "status": "not_applicable",
                        "expected": expected.get("state"),
                        "actual": state_score,
                        "detail": "dataset marks state_score=not_applicable_current_p1; QA/human only",
                    }
                )
            row["checks"].extend(checks)
            row["turns"].append(
                {
                    "index": index,
                    "role": "user",
                    "content": content,
                    "answer": summary.get("answer") or "",
                    "latency_ms": turn_latency,
                    "events": summary.get("event_types"),
                    "errors": summary.get("errors"),
                    "support": support,
                    "retrieval": retrieval,
                    "usage": summary.get("usage"),
                    "trace_id": summary.get("trace_id"),
                    "state_checks": checks,
                    "transport": transport,
                }
            )
            if summary.get("trace_id"):
                row["trace_ids"].append(summary["trace_id"])
            if summary.get("usage"):
                row["usage"] = summary["usage"]

        # Fault injection: restart or replay, using only real instance APIs.
        if fault == "restart" and session_id:
            try:
                health = server.restart()
                recovered = _http_json(base_url + f"/api/sessions/{session_id}", timeout=60)
                recovered_support = recovered.get("support")
                final_expected = (turns[-1].get("expected") or {}).get("state") if turns else None
                fault_checks = score_state_subset(final_expected, recovered_support, scope=None)
                fault_checks.append(
                    {
                        "name": "fault.restart_session_recovered",
                        "dimension": "automatic",
                        "status": "pass" if recovered_support is not None else "fail",
                        "expected": "session support readable after restart",
                        "actual": recovered_support is not None,
                        "detail": "real process restart with the same storage root",
                    }
                )
                row["checks"].extend(fault_checks)
                row["fault"] = {"kind": "restart", "health": health, "recovered_support": recovered_support}
            except Exception as exc:  # noqa: BLE001
                row["checks"].append(
                    {
                        "name": "fault.restart_session_recovered",
                        "dimension": "automatic",
                        "status": "fail",
                        "expected": "session support readable after restart",
                        "actual": f"{type(exc).__name__}: {exc}",
                        "detail": "restart failed",
                    }
                )
                error = f"restart_failed: {type(exc).__name__}: {exc}"
        elif fault == "replay" and session_id:
            user_turns = [turn for turn in turns if turn.get("role") == "user"]
            if user_turns:
                before = _http_json(base_url + f"/api/sessions/{session_id}", timeout=30).get("support")
                events, transport2 = _send_with_one_retry(
                    base_url, session_id, str(user_turns[-1].get("content") or ""), timeout=timeout
                )
                after = _http_json(base_url + f"/api/sessions/{session_id}", timeout=30).get("support")
                before_tried = ((before or {}).get("active_case") or {}).get("tried_actions") or []
                after_tried = ((after or {}).get("active_case") or {}).get("tried_actions") or []
                same = len(before_tried) == len(after_tried)
                row["checks"].append(
                    {
                        "name": "fault.replay_no_duplicate_tried",
                        "dimension": "automatic",
                        "status": "pass" if same else "fail",
                        "expected": "same tried_actions count after replayed user prompt",
                        "actual": {"before": len(before_tried), "after": len(after_tried)},
                        "detail": "real API replay; not a same-request-id idempotency claim",
                    }
                )
                row["fault"] = {
                    "kind": "replay",
                    "events": _events_summary(events).get("event_types"),
                    "before_tried": len(before_tried),
                    "after_tried": len(after_tried),
                    "transport": transport2,
                }
    except Exception as exc:  # noqa: BLE001 - model/transport failure stays in the sample
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if session_id:
            _delete_session(base_url, session_id)
    row["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    row["usage_summary"] = _aggregate_usage(row["turns"])
    row["llm_calls"] = row["usage_summary"].get("model_requests")
    row["checks"].append(_runtime_check(error))
    row["error"] = error
    row["ended_at"] = now_iso()
    if error:
        row["status"] = "error"
    else:
        row["status"] = overall_status(row["checks"], semantic_items)
    row["scores"] = _score_objects(row["checks"])
    row["semantic_counts"] = _semantic_counts(semantic_items)
    return row


def _write_review_template(path: Path, rows: list[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            for item in row.get("semantic_review") or []:
                record = {
                    "case_id": row.get("case_id"),
                    "family_id": row.get("family_id"),
                    "split": row.get("split"),
                    "mode": row.get("mode"),
                    "turn": item.get("turn"),
                    "dimension": item.get("dimension"),
                    "rubric": item.get("rubric"),
                    "input": item.get("input"),
                    "status": item.get("status"),
                    "reviewer": None,
                    "verdict": None,
                    "reason": None,
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
    return count


def _write_review_csv(path: Path, rows: list[dict[str, Any]]) -> int:
    fields = [
        "case_id",
        "family_id",
        "split",
        "mode",
        "turn",
        "dimension",
        "rubric",
        "status",
        "reviewer",
        "verdict",
        "reason",
    ]
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            for item in row.get("semantic_review") or []:
                writer.writerow(
                    {
                        "case_id": row.get("case_id"),
                        "family_id": row.get("family_id"),
                        "split": row.get("split"),
                        "mode": row.get("mode"),
                        "turn": item.get("turn"),
                        "dimension": item.get("dimension"),
                        "rubric": item.get("rubric"),
                        "status": item.get("status"),
                        "reviewer": "",
                        "verdict": "",
                        "reason": "",
                    }
                )
                count += 1
    return count


def _save_run(
    *,
    run_id: str,
    mode: str,
    split: str,
    data: SuiteData,
    selected: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    status: str,
    artifact: dict[str, Any] | None = None,
    live: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    selection: dict[str, Any] | None = None,
) -> Path:
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = aggregate_case_results(rows)
    with (run_dir / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    review_items = _write_review_template(run_dir / "review-template.jsonl", rows)
    _write_review_csv(run_dir / "review-template.csv", rows)
    selection_payload = {
        "selected": len(selected),
        "case_ids": [case.get("id") for case in selected],
        "qa_case_ids": [case.get("id") for case in selected if (case.get("setup") or {}).get("kind") != "fault_injection"],
        "fault_case_ids": [case.get("id") for case in selected if (case.get("setup") or {}).get("kind") == "fault_injection"],
        "limit": None,
        "frozen": False,
    }
    if selection:
        selection_payload.update(selection)
    run = {
        "schema_version": 1,
        "run_id": run_id,
        "kind": "robot-support-eval-v1-run",
        "mode": mode,
        "split": split,
        "scorer_version": SCORER_VERSION,
        "status": status,
        "started_at": rows[0].get("started_at") if rows else now_iso(),
        "finished_at": now_iso(),
        "dataset": {
            "suite_id": data.suite.get("suite_id"),
            "schema_version": data.suite.get("schema_version"),
            "revision": data.suite.get("revision"),
            "dataset_hash": data.dataset_hash,
            "file_sha256": data.file_hashes,
            "counts": data.validation.get("counts"),
            "source_snapshot": data.suite.get("source_snapshot"),
            "runner_source_snapshot": {
                "p1_state": data.validation.get("checks", {}).get("p1_state", {}),
                "recorded_at": now_iso(),
            },
            "source_drift": data.validation.get("source_drift"),
            "waivers": data.validation.get("waivers") or [],
            "review_status": data.suite.get("review_status"),
        },
        "selection": selection_payload,
        "artifact": artifact,
        "live": live,
        "summary": summary,
        "review": {
            "status": "pending_human",
            "items": review_items,
            "note": "Semantic rubrics are exported for a human reviewer; no human review has been performed.",
        },
        "notes": [
            "Formal v1 dataset is the only authority; v0/16 fixture stays historical regression.",
            "Selection is a question-quality filter from case_definition review, not an Agent score.",
            "Fault-injection cases are a separate bucket and never enter QA pass/fail denominators.",
            "No semantic claim is automatically passed; no LLM judge is enabled by default.",
            "Holdout was not executed in this task; only schema/evidence completeness and freeze hash.",
        ],
    }
    if extra:
        run.update(extra)
    (run_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return run_dir


def _state_schema_version() -> int | None:
    try:
        from robot_vacuum_support import state

        return int(getattr(state, "SCHEMA_VERSION", 0) or 0) or None
    except Exception:  # noqa: BLE001
        return None


def _resolve_selection(
    data: SuiteData,
    *,
    split: str,
    limit: int = 0,
    ids: list[str] | None = None,
    selection_manifest: str | Path | None = None,
    review_status: str = "all",
    bucket: str = "all",
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    """Resolve the frozen selection before any model call.

    ``selection_manifest`` is the fail-closed path: the frozen list and the
    current dataset/review bytes must still agree.  ``review_status=passed``
    rebuilds the same list from current review files and records it in the run.
    """
    manifest: dict[str, Any] | None = None
    if selection_manifest:
        manifest = load_manifest(selection_manifest)
        if split != "development":
            raise SelectionError("a frozen approved-development selection only supports --split development")
        report = validate_manifest(
            manifest,
            dataset_dir=data.base,
            review_dir=ROOT / "eval" / "reviews" / "reviews",
        )
        selected = select_cases(data.cases, manifest=manifest, bucket=bucket)
        payload = {
            "frozen": True,
            "source": "selection_manifest",
            "manifest": str(Path(selection_manifest).resolve()),
            "selection_sha256": manifest.get("selection_sha256"),
            "review_aggregate_sha256": (manifest.get("review_snapshot") or {}).get("aggregate_sha256"),
            "dataset_cases_sha256": (manifest.get("dataset") or {}).get("cases_sha256"),
            "included_case_ids": list(manifest.get("included_case_ids") or []),
            "qa_case_ids": list(manifest.get("qa_case_ids") or []),
            "fault_case_ids": list(manifest.get("fault_case_ids") or []),
            "counts": manifest.get("counts"),
            "bucket": bucket,
            "validation": report,
        }
    elif review_status == "passed":
        manifest = build_selection(dataset_dir=data.base, review_status="passed")
        report = validate_manifest(
            manifest,
            dataset_dir=data.base,
            review_dir=ROOT / "eval" / "reviews" / "reviews",
        )
        selected = select_cases(data.cases, manifest=manifest, bucket=bucket)
        payload = {
            "frozen": True,
            "source": "review_status_passed",
            "manifest": None,
            "selection_sha256": manifest.get("selection_sha256"),
            "review_aggregate_sha256": (manifest.get("review_snapshot") or {}).get("aggregate_sha256"),
            "dataset_cases_sha256": (manifest.get("dataset") or {}).get("cases_sha256"),
            "included_case_ids": list(manifest.get("included_case_ids") or []),
            "qa_case_ids": list(manifest.get("qa_case_ids") or []),
            "fault_case_ids": list(manifest.get("fault_case_ids") or []),
            "counts": manifest.get("counts"),
            "bucket": bucket,
            "validation": report,
        }
    elif review_status not in {"all", "none", ""}:
        raise SelectionError(f"unsupported --review-status {review_status!r}; use passed or all")
    else:
        selected = _select_cases(data, split, limit=0, ids=ids)
        payload = {"frozen": False, "source": f"split:{split}", "bucket": bucket}
    if ids:
        wanted = set(ids)
        selected = [case for case in selected if str(case.get("id")) in wanted]
    if limit and limit > 0:
        selected = selected[:limit]
    return selected, payload, manifest


def run_state(
    *,
    data_dir: Path,
    split: str,
    limit: int = 0,
    ids: list[str] | None = None,
    selection_manifest: str | Path | None = None,
    review_status: str = "all",
    bucket: str = "all",
) -> Path:
    data = load_dataset(data_dir)
    selected, selection_payload, _ = _resolve_selection(
        data,
        split=split,
        limit=limit,
        ids=ids,
        selection_manifest=selection_manifest,
        review_status=review_status,
        bucket=bucket,
    )
    run_id = new_run_id(f"v1-state-{split}")
    rows = [run_state_case(case) for case in selected]
    status = "completed" if rows and all(row.get("status") not in {"error"} for row in rows) else ("empty" if not rows else "completed_with_errors")
    return _save_run(
        run_id=run_id,
        mode="state",
        split=split,
        data=data,
        selected=selected,
        rows=rows,
        status=status,
        selection=selection_payload,
        extra={
            "state_runtime": {
                "module": "robot_vacuum_support.state",
                "functions": ["apply_user_turn", "apply_assistant_turn"],
                "schema_version": _state_schema_version(),
            }
        },
    )


def _skipped_row(case: dict[str, Any], reason: str, *, status: str = "skipped") -> dict[str, Any]:
    row = _base_row(
        case,
        mode="live",
        bucket="fault" if (case.get("setup") or {}).get("kind") == "fault_injection" else "qa",
    )
    row["status"] = status
    row["skip_reason"] = reason
    if status == "error":
        row["error"] = reason
    row["usage_summary"] = _no_model_usage(reason)
    row["llm_calls"] = 0
    row["ended_at"] = now_iso()
    return row


def _run_live_case_with_retry(
    case: dict[str, Any],
    *,
    server: LiveServer,
    timeout: float,
) -> dict[str, Any]:
    """One live case, with at most one transient retry; both attempts kept."""
    first = run_live_case(case, base_url=server.base_url, server=server, timeout=timeout)
    attempt_rows = [
        {
            "attempt": 1,
            "status": first.get("status"),
            "error": first.get("error"),
            "latency_ms": first.get("latency_ms"),
            "usage_summary": first.get("usage_summary"),
            "turn_count": len(first.get("turns") or []),
        }
    ]
    row = first
    if first.get("status") == "error" and _is_transient_error(first.get("error")):
        second = run_live_case(case, base_url=server.base_url, server=server, timeout=timeout)
        attempt_rows.append(
            {
                "attempt": 2,
                "status": second.get("status"),
                "error": second.get("error"),
                "latency_ms": second.get("latency_ms"),
                "usage_summary": second.get("usage_summary"),
                "turn_count": len(second.get("turns") or []),
            }
        )
        second["transient_retry"] = {
            "retried": True,
            "reason": first.get("error"),
            "attempts": attempt_rows,
        }
        row = second
    else:
        row["transient_retry"] = {"retried": False, "attempts": attempt_rows}
    return row


def _run_live_cases_parallel(
    cases: list[dict[str, Any]],
    *,
    server: LiveServer,
    workers: int,
    timeout: float,
    on_result: Any = None,
) -> list[dict[str, Any]]:
    """Bounded worker pool; one independent session/workspace per case."""
    if not cases:
        return []
    order = {str(case.get("id")): index for index, case in enumerate(cases)}
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = {
            executor.submit(_run_live_case_with_retry, case, server=server, timeout=timeout): case
            for case in cases
        }
        for future in as_completed(futures):
            case = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - never drop a sample
                results.append(_skipped_row(case, f"{type(exc).__name__}: {exc}", status="error"))
            if callable(on_result):
                on_result(list(results))
    results.sort(key=lambda row: order.get(str(row.get("case_id")), 10**9))
    return results


def run_live(
    *,
    data_dir: Path,
    split: str,
    limit: int = 0,
    ids: list[str] | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    selection_manifest: str | Path | None = None,
    review_status: str = "all",
    bucket: str = "all",
    workers: int = 1,
    case_timeout: float = 300,
) -> Path:
    data = load_dataset(data_dir)
    selected, selection_payload, _ = _resolve_selection(
        data,
        split=split,
        limit=limit,
        ids=ids,
        selection_manifest=selection_manifest,
        review_status=review_status,
        bucket=bucket,
    )
    run_id = new_run_id(f"v1-live-{split}")
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    env_file = _choose_env_file()
    p1_check = data.validation.get("checks", {}).get("p1_state", {})
    preflight: dict[str, Any] = {
        "port": port,
        "host": host,
        "env_file": str(env_file) if env_file else None,
        "env_values_read_or_logged": False,
        "p1_state": p1_check,
        "source_rebuild": False,
        "workers": max(1, int(workers)),
        "case_timeout_s": case_timeout,
        "bucket": bucket,
    }
    if not p1_check.get("usable"):
        rows = [
            _skipped_row(case, "P1 state source hash missing or mismatched; live refused")
            for case in selected
        ]
        return _save_run(
            run_id=run_id,
            mode="live",
            split=split,
            data=data,
            selected=selected,
            rows=rows,
            status="needs_configuration",
            live=preflight,
            selection=selection_payload,
            extra={"needs_configuration": ["runtime/robot_vacuum_support/state.py must match the declared snapshot"]},
        )
    if env_file is None:
        rows = [
            _skipped_row(case, "no instance environment_file with credentials; live mode did not mock success")
            for case in selected
        ]
        return _save_run(
            run_id=run_id,
            mode="live",
            split=split,
            data=data,
            selected=selected,
            rows=rows,
            status="needs_configuration",
            live=preflight,
            selection=selection_payload,
            extra={"needs_configuration": ["instance environment_file (model API key) not found"]},
        )

    artifact = _build_live_artifact(run_dir)
    preflight["source_rebuild"] = True
    preflight["artifact"] = artifact
    if not artifact.get("p1_state_matches_source"):
        rows = [
            _skipped_row(case, "built artifact P1 state.py hash does not match current source")
            for case in selected
        ]
        return _save_run(
            run_id=run_id,
            mode="live",
            split=split,
            data=data,
            selected=selected,
            rows=rows,
            status="needs_configuration",
            artifact=artifact,
            live=preflight,
            selection=selection_payload,
            extra={"needs_configuration": ["built artifact P1 state does not match current source"]},
        )
    instance_path, storage = _write_instance(
        run_dir=run_dir,
        artifact=artifact,
        env_file=env_file,
        host=host,
        port=port,
    )
    server = LiveServer(
        python=PYTHON,
        serve_py=Path(artifact["artifact_root"]) / "serve.py",
        instance_path=instance_path,
        host=host,
        port=port,
        log_path=run_dir / "server.log",
        cwd=ROOT.parent,
    )
    health: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    status = "completed"

    def _checkpoint(partial_rows: list[dict[str, Any]], *, phase: str) -> None:
        ordered = sorted(partial_rows, key=lambda row: str(row.get("case_id") or ""))
        status_counts: dict[str, int] = {}
        for item in ordered:
            key = str(item.get("status"))
            status_counts[key] = status_counts.get(key, 0) + 1
        (run_dir / "cases.partial.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ordered),
            encoding="utf-8",
        )
        (run_dir / "progress.json").write_text(
            json.dumps(
                {
                    "phase": phase,
                    "updated_at": now_iso(),
                    "selected": len(selected),
                    "completed": len(ordered),
                    "remaining": max(0, len(selected) - len(ordered)),
                    "statuses": dict(sorted(status_counts.items())),
                    "case_ids": [row.get("case_id") for row in ordered],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    _checkpoint(rows, phase="starting")
    try:
        health = server.start()
        preflight["health"] = {
            "status": health.get("status"),
            "ready": health.get("ready"),
            "missing": health.get("missing"),
        }
        if not health.get("ready"):
            status = "needs_configuration"
            rows = [
                _skipped_row(case, "instance health ready=false; see missing configuration")
                for case in selected
            ]
        else:
            qa_cases = [
                case for case in selected
                if (case.get("setup") or {}).get("kind") != "fault_injection"
            ]
            fault_cases = [
                case for case in selected
                if (case.get("setup") or {}).get("kind") == "fault_injection"
            ]
            if qa_cases:
                rows.extend(
                    _run_live_cases_parallel(
                        qa_cases,
                        server=server,
                        workers=max(1, int(workers)),
                        timeout=case_timeout,
                        on_result=lambda partial: _checkpoint(partial, phase="qa_live"),
                    )
                )
                _checkpoint(rows, phase="qa_live_done")
            # Restart/replay fault injection shares the server process, so it is
            # serial and runs only after the concurrent QA batch is finished.
            for case in fault_cases:
                rows.append(
                    _run_live_case_with_retry(case, server=server, timeout=case_timeout)
                )
                _checkpoint(rows, phase="fault_live")
            if any(row.get("status") == "error" for row in rows):
                status = "completed_with_errors"
    except Exception as exc:  # noqa: BLE001
        status = "error"
        preflight["fatal_error"] = f"{type(exc).__name__}: {exc}"
        rows = [
            _skipped_row(case, preflight["fatal_error"], status="error")
            for case in selected
        ]
    finally:
        server.stop()
        # Make it obvious that the temporary eval port was released.
        preflight["port_released"] = not server._port_in_use()
    return _save_run(
        run_id=run_id,
        mode="live",
        split=split,
        data=data,
        selected=selected,
        rows=rows,
        status=status,
        artifact=artifact,
        live={**preflight, "instance": str(instance_path), "storage_root": str(storage), "base_url": server.base_url},
        selection=selection_payload,
    )


def _regrade_case_row(
    old_row: dict[str, Any],
    new_case: dict[str, Any],
    *,
    source_run_id: str,
    source_dataset_hash: str,
) -> dict[str, Any]:
    """Re-score stored model outputs with the current scorer; no model call."""
    row = copy.deepcopy(old_row)
    old_case = old_row.get("case") or {}
    input_unchanged = _case_input_hash(old_case) == _case_input_hash(new_case)
    expected_unchanged = _case_expected_hash(old_case) == _case_expected_hash(new_case)
    row["case"] = new_case
    row["bucket"] = "fault" if (new_case.get("setup") or {}).get("kind") == "fault_injection" else "qa"
    row["regrade"] = {
        "source_run_id": source_run_id,
        "source_dataset_hash": source_dataset_hash,
        "source_case_input_sha256": _case_input_hash(old_case),
        "current_case_input_sha256": _case_input_hash(new_case),
        "source_expected_sha256": _case_expected_hash(old_case),
        "current_expected_sha256": _case_expected_hash(new_case),
        "inputs_unchanged": input_unchanged,
        "expected_unchanged": expected_unchanged,
        "model_outputs_reused": input_unchanged,
        "llm_calls": 0,
        "scorer_version": SCORER_VERSION,
    }
    row["raw_turns"] = copy.deepcopy(old_row.get("turns") or [])
    if not input_unchanged:
        row["checks"] = []
        row["semantic_review"] = []
        row["semantic_counts"] = {}
        row["scores"] = []
        row["status"] = "not_applicable"
        row["skip_reason"] = "case input changed after the model run; needs a new live run"
        return row

    semantic_items = semantic_review_items(new_case.get("turns") or [], mode="live")
    row["semantic_review"] = semantic_items
    checks: list[dict[str, Any]] = []
    old_turns = old_row.get("turns") or []
    new_turns = new_case.get("turns") or []
    for old_turn in old_turns:
        index = int(old_turn.get("index") or 0)
        if not (1 <= index <= len(new_turns)):
            continue
        new_turn = new_turns[index - 1]
        if new_turn.get("role") != "user":
            continue
        expected = new_turn.get("expected") or {}
        knowledge_sources = _knowledge_expected_sources(new_case, new_turn)
        source_mode = (new_case.get("scoring") or {}).get("expected_sources_mode") or "any_of"
        turn_checks = score_citations(
            old_turn.get("answer") or "",
            old_turn.get("retrieval") or {},
            expected_sources=knowledge_sources,
            require_citation=bool(knowledge_sources),
            expected_sources_mode=source_mode,
        )
        support = old_turn.get("support")
        state_score = (new_case.get("scoring") or {}).get("state_score")
        if support is not None and state_score != "not_applicable_current_p1":
            topic = ((support or {}).get("active_case") or {}).get("topic")
            observed_scope = (
                "recharge"
                if topic == "recharge_failure"
                else ("other" if (support or {}).get("active_case") is not None else None)
            )
            turn_checks.extend(
                score_state_subset(
                    expected.get("state"),
                    support,
                    scope=observed_scope,
                )
            )
        elif support is not None:
            turn_checks.append(
                {
                    "name": "state.contract",
                    "dimension": "automatic",
                    "status": "not_applicable",
                    "expected": expected.get("state"),
                    "actual": state_score,
                    "detail": "dataset marks state_score=not_applicable_current_p1; QA/human only",
                }
            )
        checks.extend(turn_checks)
        old_turn["state_checks"] = turn_checks
        old_turn["expected"] = expected
    row["checks"] = checks
    row["scores"] = _score_objects(checks)
    row["semantic_counts"] = _semantic_counts(semantic_items)
    error = old_row.get("error")
    if error:
        row["status"] = "error"
    else:
        row["status"] = overall_status(checks, semantic_items)
    return row


def regrade_run(source_run_dir: Path | str) -> Path:
    """Create a new run id from a completed live run's raw outputs.

    The source run directory is never modified.  The new run records both the
    old and current dataset/expected hashes, and reuses only stored answers,
    retrieval, support state, trace ids and usage.
    """
    source = summary_run(source_run_dir)
    source_dir = Path(source["run_dir"])
    source_run = source["run"]
    source_rows_path = source_dir / "cases.jsonl"
    if not source_rows_path.is_file():
        raise FileNotFoundError(f"source cases.jsonl not found in {source_dir}")
    data = load_dataset(DATA_DIR)
    old_rows = [
        json.loads(line)
        for line in source_rows_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = []
    selected = []
    for old_row in old_rows:
        case_id = str(old_row.get("case_id") or "")
        new_case = data.by_id.get(case_id)
        if new_case is None:
            row = copy.deepcopy(old_row)
            row["status"] = "not_applicable"
            row["skip_reason"] = "case ID no longer present in current dataset"
            row["semantic_review"] = []
            row["checks"] = []
            rows.append(row)
            continue
        selected.append(new_case)
        rows.append(
            _regrade_case_row(
                old_row,
                new_case,
                source_run_id=str(source_run.get("run_id") or source_dir.name),
                source_dataset_hash=str((source_run.get("dataset") or {}).get("dataset_hash") or ""),
            )
        )
    run_id = new_run_id(f"v1-live-regrade-{source_run.get('split') or 'development'}")
    status = "completed"
    if any(row.get("status") == "error" for row in rows):
        status = "completed_with_errors"
    artifact = source_run.get("artifact")
    live = dict(source_run.get("live") or {})
    live["regraded_from"] = str(source_dir)
    live["source_artifact_reused"] = True
    live["llm_calls"] = 0
    return _save_run(
        run_id=run_id,
        mode="live",
        split=str(source_run.get("split") or "development"),
        data=data,
        selected=selected,
        rows=rows,
        status=status,
        artifact=artifact,
        live=live,
        extra={
            "regrade": {
                "source_run_id": source_run.get("run_id"),
                "source_run_dir": str(source_dir),
                "source_dataset_hash": (source_run.get("dataset") or {}).get("dataset_hash"),
                "current_dataset_hash": data.dataset_hash,
                "source_scorer_version": source_run.get("scorer_version"),
                "current_scorer_version": SCORER_VERSION,
                "model_outputs_reused": True,
                "artifact_reused": True,
                "llm_calls": 0,
                "note": (
                    "Offline re-score of stored answers/traces; no product start, "
                    "no model call and no rebuild."
                ),
            }
        },
    )


def summary_run(run_dir: Path | str) -> dict[str, Any]:
    directory = Path(run_dir).expanduser()
    if not directory.is_absolute():
        directory = (ROOT / directory).resolve()
    run_path = directory / "run.json"
    summary_path = directory / "summary.json"
    if not run_path.is_file():
        raise FileNotFoundError(f"run.json not found in {directory}")
    run = _read_json(run_path)
    summary = _read_json(summary_path) if summary_path.is_file() else run.get("summary") or {}
    return {"run": run, "summary": summary, "run_dir": str(directory)}


def latest_run_dir() -> Path | None:
    if not RESULTS_DIR.is_dir():
        return None
    candidates = [
        item
        for item in RESULTS_DIR.iterdir()
        if item.is_dir() and (item / "run.json").is_file()
    ]
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="robot-support eval v1 runner")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-data", help="validate the formal v1 dataset and freeze hash")
    validate.add_argument("--data", default=str(DATA_DIR))
    validate.add_argument("--no-evidence", action="store_true", help="skip quote extraction checks")
    validate.add_argument("--json", action="store_true")

    select = sub.add_parser("select", help="freeze the approved-development selection from case_definition reviews")
    select.add_argument("--review-status", choices=["passed"], default="passed")
    select.add_argument("--data", default=str(DATA_DIR))
    select.add_argument("--reviews", default=str(ROOT / "eval" / "reviews" / "reviews"))
    select.add_argument("--output", default=str(ROOT / "outputs" / "approved-development-selection.json"))
    select.add_argument("--source-drift-output", default=str(ROOT / "outputs" / "approved-development-source-drift.json"))
    select.add_argument("--json", action="store_true")

    validate_selection = sub.add_parser("validate-selection", help="fail closed if a frozen selection drifted")
    validate_selection.add_argument("--manifest", required=True)
    validate_selection.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="run case IDs from the formal v1 dataset")
    run.add_argument("--split", choices=["development", "holdout"], default="development")
    run.add_argument("--mode", choices=["state", "live"], default="state")
    run.add_argument("--data", default=str(DATA_DIR))
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--ids", nargs="*", default=None)
    run.add_argument("--selection-manifest", default=None)
    run.add_argument("--review-status", choices=["all", "passed"], default="all")
    run.add_argument("--bucket", choices=["all", "qa", "fault"], default="all")
    run.add_argument("--workers", type=int, default=1, help="live QA case concurrency (bounded)")
    run.add_argument("--case-timeout", type=float, default=300, help="per-case HTTP/model timeout in seconds")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument("--json", action="store_true")

    regrade = sub.add_parser("regrade", help="offline re-score a completed live run (no model call)")
    regrade.add_argument("--run", required=True)
    regrade.add_argument("--json", action="store_true")

    summary = sub.add_parser("summary", help="print a saved run summary")
    summary.add_argument("--run", default="")
    summary.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "select":
        manifest = build_selection(
            dataset_dir=Path(args.data),
            review_dir=Path(args.reviews),
            review_status=args.review_status,
        )
        report = validate_manifest(manifest, dataset_dir=Path(args.data), review_dir=Path(args.reviews))
        drift = build_source_drift_audit(dataset_dir=Path(args.data))
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        drift_output = Path(args.source_drift_output)
        drift_output.parent.mkdir(parents=True, exist_ok=True)
        drift_output.write_text(json.dumps(drift, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        payload = {"manifest": str(output), "source_drift": str(drift_output), "validation": report, "counts": manifest.get("counts"), "selection_sha256": manifest.get("selection_sha256"), "source_drift_ok": drift.get("ok"), "waived": drift.get("waived")}
        if args.json:
            _print_json(payload)
        else:
            print(f"selection: {output}")
            print(f"selection_sha256: {manifest.get('selection_sha256')}")
            print(f"counts: {json.dumps(manifest.get('counts'), ensure_ascii=False)}")
            print(f"qa/fault: {len(manifest.get('qa_case_ids') or [])}/{len(manifest.get('fault_case_ids') or [])}")
            print(f"source_drift: {drift_output}  ok={drift.get('ok')} waived={drift.get('waived')}")
        return 0

    if args.command == "validate-selection":
        manifest = load_manifest(args.manifest)
        report = validate_manifest(manifest)
        _print_json(report) if args.json else print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1

    if args.command == "validate-data":
        report = validate_dataset(Path(args.data), check_evidence=not args.no_evidence)
        if args.json:
            _print_json(report)
        else:
            print(f"data_dir: {report['data_dir']}")
            print(f"ok: {report['ok']}")
            print(f"cases: {report['cases']}  counts: {json.dumps(report.get('counts'), ensure_ascii=False)}")
            print(f"dataset_hash: {report.get('freeze', {}).get('dataset_hash')}")
            if report.get("warnings"):
                print("warnings:")
                for item in report["warnings"]:
                    print(f"  - {item}")
            if report.get("errors"):
                print("errors:")
                for item in report["errors"]:
                    print(f"  - {item}")
        return 0 if report.get("ok") else 1

    if args.command == "run":
        try:
            if args.mode == "state":
                run_dir = run_state(
                    data_dir=Path(args.data),
                    split=args.split,
                    limit=args.limit,
                    ids=args.ids,
                    selection_manifest=args.selection_manifest,
                    review_status=args.review_status,
                    bucket=args.bucket,
                )
            else:
                run_dir = run_live(
                    data_dir=Path(args.data),
                    split=args.split,
                    limit=args.limit,
                    ids=args.ids,
                    host=args.host,
                    port=args.port,
                    selection_manifest=args.selection_manifest,
                    review_status=args.review_status,
                    bucket=args.bucket,
                    workers=args.workers,
                    case_timeout=args.case_timeout,
                )
        except (DataValidationError, SelectionError) as exc:
            if isinstance(exc, DataValidationError):
                _print_json(exc.report)
            else:
                _print_json({"ok": False, "error": str(exc), "report": exc.report})
            return 1
        payload = summary_run(run_dir)
        if args.json:
            _print_json(payload)
        else:
            print(f"run_dir: {run_dir}")
            print(f"mode: {payload['run'].get('mode')}  split: {payload['run'].get('split')}  status: {payload['run'].get('status')}")
            counts = payload["summary"].get("counts", {})
            print(f"counts: {json.dumps(counts, ensure_ascii=False)}")
            print(f"dataset_revision: {payload['run'].get('dataset', {}).get('revision')}")
            print(f"dataset_hash: {payload['run'].get('dataset', {}).get('dataset_hash')}")
            if payload["run"].get("needs_configuration"):
                print("needs_configuration: " + "; ".join(payload["run"]["needs_configuration"]))
        return 0 if payload["run"].get("status") not in {"error"} else 1

    if args.command == "regrade":
        try:
            run_dir = regrade_run(Path(args.run))
        except Exception as exc:  # noqa: BLE001
            print(f"regrade failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        payload = summary_run(run_dir)
        if args.json:
            _print_json(payload)
        else:
            print(f"regrade_run_dir: {run_dir}")
            print(f"status: {payload['run'].get('status')}")
            print(f"counts: {json.dumps(payload['summary'].get('counts'), ensure_ascii=False)}")
            print(f"regrade: {json.dumps(payload['run'].get('regrade'), ensure_ascii=False)}")
        return 0

    target = Path(args.run) if args.run else latest_run_dir()
    if target is None:
        print("no runs found under eval/results", file=sys.stderr)
        return 1
    payload = summary_run(target)
    if args.json:
        _print_json(payload)
    else:
        run_payload = payload["run"]
        counts = payload["summary"].get("counts", {})
        print(f"run_dir: {payload['run_dir']}")
        print(f"run_id: {run_payload.get('run_id')}")
        print(f"mode: {run_payload.get('mode')}  split: {run_payload.get('split')}  status: {run_payload.get('status')}")
        print(f"dataset_revision: {run_payload.get('dataset', {}).get('revision')}")
        print(f"dataset_hash: {run_payload.get('dataset', {}).get('dataset_hash')}")
        print(f"counts: {json.dumps(counts, ensure_ascii=False)}")
        print(f"latency: {json.dumps(payload['summary'].get('latency'), ensure_ascii=False)}")
        print(f"usage: {json.dumps(payload['summary'].get('usage'), ensure_ascii=False)}")
        if payload["summary"].get("fault_usage"):
            print(f"fault_usage: {json.dumps(payload['summary'].get('fault_usage'), ensure_ascii=False)}")
        print(f"tokens: {json.dumps(payload['summary'].get('tokens'), ensure_ascii=False)}")
        print(f"cost: {json.dumps(payload['summary'].get('cost'), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
