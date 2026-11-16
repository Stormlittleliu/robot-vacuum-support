# -*- coding: utf-8 -*-
"""Frozen development selection from human case-definition reviews.

The selection is a *question-quality* filter, not an Agent-performance filter:

* only ``reviewScope=case_definition`` records are considered;
* only records whose ``caseHash`` still matches the current case definition
  (and whose dataset file hash still matches) are ``current``;
* ``passed`` records are included, ``failed`` / ``uncertain`` are listed as
  excluded but stay in the original dataset untouched;
* QA and fault injection are different denominators;
* review records for an old case hash are historical and never silently
  upgraded to a current review.

The manifest is deterministic.  ``selection_sha256`` is recomputed from the
current dataset bytes, the review snapshot bytes and the included case hashes,
so a run can bind to a frozen list and fail closed if either side drifts.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT / "eval" / "datasets" / "v1"
REVIEW_DIR = ROOT / "eval" / "reviews" / "reviews"
SELECTION_SCHEMA = "approved-development-selection/v1"
REVIEW_SCOPE = "case_definition"


class SelectionError(RuntimeError):
    def __init__(self, message: str, *, report: dict[str, Any] | None = None):
        self.report = report or {}
        super().__init__(message)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def case_hash(case: dict[str, Any]) -> str:
    """Same definition as the manual-review server: exclude mutable review."""
    payload = {key: value for key, value in case.items() if key != "review"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(raw.encode("utf-8"))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_cases(dataset_dir: Path) -> list[dict[str, Any]]:
    path = dataset_dir / "cases.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def _review_files(review_dir: Path) -> list[Path]:
    if not review_dir.is_dir():
        return []
    return sorted(path for path in review_dir.glob("*.json") if path.is_file())


def review_snapshot(review_dir: Path | str = REVIEW_DIR) -> dict[str, Any]:
    """Aggregate hash over sorted filename + raw bytes, as the review round did."""
    directory = Path(review_dir)
    files = _review_files(directory)
    digest = hashlib.sha256()
    latest: str | None = None
    records: list[dict[str, Any]] = []
    for path in files:
        raw = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\n")
        digest.update(raw)
        digest.update(b"\n")
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except Exception:  # noqa: BLE001 - a broken review is a data error below
            payload = {}
        records.append(payload)
        updated = str(payload.get("updatedAt") or "")
        if updated and (latest is None or updated > latest):
            latest = updated
    return {
        "directory": str(directory),
        "records": len(files),
        "aggregate_sha256": digest.hexdigest(),
        "latest_record_updated_at": latest,
        "files": [path.name for path in files],
        "records_payload": records,
    }


def _latest_review(reviews: list[dict[str, Any]]) -> dict[str, Any] | None:
    def key(item: dict[str, Any]) -> tuple[int, str, str]:
        try:
            revision = int(item.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        return (revision, str(item.get("updatedAt") or ""), str(item.get("reviewId") or ""))

    return max(reviews, key=key) if reviews else None


def _suite_revision(dataset_dir: Path) -> str:
    suite = _read_json(dataset_dir / "suite.json")
    return str(suite.get("revision") or "")


def _dataset_hashes(dataset_dir: Path) -> dict[str, str]:
    return {
        name: sha256_file(dataset_dir / name)
        for name in ("suite.json", "cases.jsonl", "split.json", "audit.json")
    }


def _dataset_freeze(file_hashes: dict[str, str]) -> str:
    seed = "\n".join(f"{name}:{digest}" for name, digest in file_hashes.items())
    return sha256_bytes(seed.encode("utf-8"))


def _canonical_selection_hash(
    *,
    dataset_file_hashes: dict[str, str],
    review_aggregate: str,
    included: list[dict[str, Any]],
    rule: dict[str, Any],
) -> str:
    payload = {
        "schema_version": SELECTION_SCHEMA,
        "dataset_file_sha256": dataset_file_hashes,
        "review_aggregate_sha256": review_aggregate,
        "rule": rule,
        "included": [
            {
                "case_id": item["case_id"],
                "case_hash": item["case_hash"],
                "review_id": item.get("review_id"),
                "review_revision": item.get("review_revision"),
                "status": item.get("review_status", item.get("status")),
                "bucket": item.get("bucket"),
            }
            for item in sorted(included, key=lambda row: row["case_id"])
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(raw.encode("utf-8"))


def build_selection(
    *,
    dataset_dir: Path | str = DATASET_DIR,
    review_dir: Path | str = REVIEW_DIR,
    review_status: str = "passed",
    now: str | None = None,
) -> dict[str, Any]:
    """Build the deterministic selection manifest from current bytes."""
    if review_status != "passed":
        raise SelectionError("only --review-status passed is a supported current selection")
    base = Path(dataset_dir).resolve()
    reviews_dir = Path(review_dir).resolve()
    cases = _read_cases(base)
    by_id = {str(case.get("id")): case for case in cases}
    current_suite_revision = _suite_revision(base)
    file_hashes = _dataset_hashes(base)
    current_cases_sha = file_hashes["cases.jsonl"]
    snap = review_snapshot(reviews_dir)
    records = snap.pop("records_payload")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if str(record.get("reviewScope") or "") != REVIEW_SCOPE:
            continue
        case_id = str(record.get("caseId") or "")
        if case_id:
            grouped.setdefault(case_id, []).append(record)

    rows: list[dict[str, Any]] = []
    for case_id in sorted(by_id):
        case = by_id[case_id]
        if case.get("split") != "development":
            continue
        bucket = "fault" if (case.get("setup") or {}).get("kind") == "fault_injection" else "qa"
        current_hash = case_hash(case)
        candidates = grouped.get(case_id, [])
        review = _latest_review(candidates)
        row: dict[str, Any] = {
            "case_id": case_id,
            "split": case.get("split"),
            "category": case.get("category"),
            "family_id": case.get("family_id"),
            "bucket": bucket,
            "current_case_hash": current_hash,
            "included": False,
            "review_status": None,
            "review_id": None,
            "review_revision": None,
            "review_updated_at": None,
            "case_hash": None,
            "hash_match": None,
            "dataset_cases_sha256": None,
            "dataset_match": None,
            "suite_revision": None,
            "current_suite_revision": current_suite_revision,
            "historical": False,
            "reason": "",
            "notes": "",
        }
        if review is None:
            row["reason"] = "no current case_definition review for this case"
            rows.append(row)
            continue
        row.update(
            {
                "review_status": review.get("status"),
                "review_id": review.get("reviewId"),
                "review_revision": review.get("revision"),
                "review_updated_at": review.get("updatedAt"),
                "case_hash": review.get("caseHash"),
                "hash_match": review.get("caseHash") == current_hash,
                "dataset_cases_sha256": review.get("datasetCasesSha256"),
                "dataset_match": review.get("datasetCasesSha256") == current_cases_sha,
                "suite_revision": review.get("suiteRevision"),
                "notes": str(review.get("notes") or ""),
            }
        )
        statuses = {str(item.get("status")) for item in candidates}
        same_revision_conflict = len({(item.get("revision"), item.get("status")) for item in candidates}) > 1 and len(statuses) > 1
        if not row["hash_match"] or not row["dataset_match"]:
            row["historical"] = True
            row["reason"] = (
                "review is historical: caseHash or datasetCasesSha256 no longer matches "
                "the current dataset; not counted as current"
            )
        elif row["suite_revision"] != current_suite_revision:
            row["historical"] = True
            row["reason"] = (
                f"review suiteRevision={row['suite_revision']!r} is not current "
                f"{current_suite_revision!r}; historical, not counted"
            )
        elif same_revision_conflict:
            row["reason"] = "conflicting current case_definition reviews; excluded until resolved"
        elif str(review.get("status")) == "passed":
            row["included"] = True
            row["reason"] = "current case_definition passed; question-quality filter passed"
        elif str(review.get("status")) == "failed":
            row["reason"] = "current case_definition failed; kept in dataset but excluded from approved run"
        elif str(review.get("status")) == "uncertain":
            row["reason"] = "current case_definition uncertain; kept in dataset but excluded from approved run"
        else:
            row["reason"] = f"unknown review status {review.get('status')!r}; excluded"
        rows.append(row)

    included = [row for row in rows if row["included"]]
    rule = {
        "review_scope": REVIEW_SCOPE,
        "review_status": review_status,
        "require_case_hash_match": True,
        "require_dataset_cases_sha256_match": True,
        "require_current_suite_revision": True,
        "split": "development",
        "selection_is_question_quality_filter": True,
        "not_agent_performance_filter": True,
        "fault_and_qa_separate": True,
        "excluded_reviews_keep_original_dataset": True,
        "dataset_mutation": "none",
    }
    selection_hash = _canonical_selection_hash(
        dataset_file_hashes=file_hashes,
        review_aggregate=snap["aggregate_sha256"],
        included=included,
        rule=rule,
    )
    excluded = [row for row in rows if not row["included"]]
    by_status: dict[str, int] = {}
    for row in rows:
        status = str(row.get("review_status") or "unreviewed")
        by_status[status] = by_status.get(status, 0) + 1
    manifest = {
        "schema_version": SELECTION_SCHEMA,
        "generated_at": now or datetime.now().astimezone().isoformat(timespec="seconds"),
        "frozen": True,
        "selection_is_question_quality_filter_not_agent_score": True,
        "selection_rule": rule,
        "dataset": {
            "directory": str(base),
            "suite_id": _read_json(base / "suite.json").get("suite_id"),
            "revision": current_suite_revision,
            "file_sha256": file_hashes,
            "dataset_hash": _dataset_freeze(file_hashes),
            "cases_sha256": current_cases_sha,
            "development_cases": sum(1 for case in cases if case.get("split") == "development"),
        },
        "review_snapshot": {
            "directory": snap["directory"],
            "review_scope": REVIEW_SCOPE,
            "records": snap["records"],
            "aggregate_sha256": snap["aggregate_sha256"],
            "latest_record_updated_at": snap["latest_record_updated_at"],
            "files": snap["files"],
            "dataset_cases_sha256": current_cases_sha,
            "suite_revision": current_suite_revision,
        },
        "counts": {
            "dataset_development": sum(1 for case in cases if case.get("split") == "development"),
            "reviewed_case_definition": len(rows),
            "included": len(included),
            "excluded": len(excluded),
            "qa_included": sum(1 for row in included if row["bucket"] == "qa"),
            "fault_included": sum(1 for row in included if row["bucket"] == "fault"),
            "excluded_failed": sum(1 for row in excluded if row.get("review_status") == "failed"),
            "excluded_uncertain": sum(1 for row in excluded if row.get("review_status") == "uncertain"),
            "excluded_stale": sum(1 for row in excluded if row.get("historical")),
            "by_review_status": dict(sorted(by_status.items())),
        },
        "included_case_ids": [row["case_id"] for row in included],
        "qa_case_ids": [row["case_id"] for row in included if row["bucket"] == "qa"],
        "fault_case_ids": [row["case_id"] for row in included if row["bucket"] == "fault"],
        "cases": rows,
        "excluded_cases": excluded,
        "selection_sha256": selection_hash,
        "notes": [
            "All 32 development cases remain in the original dataset; no review verdict or notes were edited.",
            "Included means the case definition/oracle/evidence passed the user's review; it is not an Agent answer score.",
            "failed/uncertain/stale reviews are listed with their original revision and hash and are excluded, not deleted.",
        ],
    }
    return manifest


def load_manifest(path: Path | str) -> dict[str, Any]:
    payload = _read_json(Path(path))
    if not isinstance(payload, dict):
        raise SelectionError("selection manifest must be a JSON object")
    if payload.get("schema_version") != SELECTION_SCHEMA:
        raise SelectionError(
            f"selection manifest schema_version must be {SELECTION_SCHEMA!r}, got {payload.get('schema_version')!r}"
        )
    return payload


def validate_manifest(
    manifest: dict[str, Any],
    *,
    dataset_dir: Path | str = DATASET_DIR,
    review_dir: Path | str = REVIEW_DIR,
) -> dict[str, Any]:
    """Fail closed if the frozen selection or its source bytes drifted."""
    base = Path(dataset_dir).resolve()
    cases = _read_cases(base)
    by_id = {str(case.get("id")): case for case in cases}
    file_hashes = _dataset_hashes(base)
    current_cases_sha = file_hashes["cases.jsonl"]
    errors: list[str] = []
    dataset = manifest.get("dataset") or {}
    if (dataset.get("file_sha256") or {}).get("cases.jsonl") != current_cases_sha:
        errors.append("selection manifest cases.jsonl hash does not match current dataset")
    review = manifest.get("review_snapshot") or {}
    current_review = review_snapshot(review_dir)
    if review.get("aggregate_sha256") != current_review["aggregate_sha256"]:
        errors.append(
            "selection manifest review aggregate hash does not match current review files; "
            "regenerate the frozen selection instead of silently changing the list"
        )
    included = [str(item) for item in manifest.get("included_case_ids") or []]
    if not included:
        errors.append("selection manifest has no included_case_ids")
    for case_id in included:
        case = by_id.get(case_id)
        if case is None:
            errors.append(f"included case {case_id!r} is not in the current dataset")
            continue
        if case.get("split") != "development":
            errors.append(f"included case {case_id!r} is not development")
        row = next((item for item in manifest.get("cases") or [] if item.get("case_id") == case_id), None)
        if row is None:
            errors.append(f"included case {case_id!r} has no manifest row")
            continue
        if row.get("case_hash") != case_hash(case):
            errors.append(f"included case {case_id!r} caseHash drifted since selection freeze")
    recomputed = _canonical_selection_hash(
        dataset_file_hashes=dataset.get("file_sha256") or {},
        review_aggregate=str(review.get("aggregate_sha256") or ""),
        included=[
            {
                "case_id": row.get("case_id"),
                "case_hash": row.get("case_hash"),
                "review_id": row.get("review_id"),
                "review_revision": row.get("review_revision"),
                "status": row.get("review_status"),
                "bucket": row.get("bucket"),
            }
            for row in (manifest.get("cases") or [])
            if row.get("case_id") in set(included)
        ],
        rule=manifest.get("selection_rule") or {},
    )
    if recomputed != manifest.get("selection_sha256"):
        errors.append("selection_sha256 does not recompute from the frozen manifest content")
    report = {
        "ok": not errors,
        "errors": errors,
        "included": len(included),
        "selection_sha256": manifest.get("selection_sha256"),
        "recomputed_selection_sha256": recomputed,
        "review_aggregate_sha256": current_review["aggregate_sha256"],
        "cases_sha256": current_cases_sha,
    }
    if errors:
        raise SelectionError("selection manifest validation failed: " + "; ".join(errors), report=report)
    return report


def select_cases(
    cases: Iterable[dict[str, Any]],
    *,
    manifest: dict[str, Any],
    bucket: str = "all",
) -> list[dict[str, Any]]:
    """Return dataset case objects in the frozen manifest order."""
    by_id = {str(case.get("id")): case for case in cases}
    keys: list[str]
    if bucket == "qa":
        keys = [str(item) for item in manifest.get("qa_case_ids") or []]
    elif bucket == "fault":
        keys = [str(item) for item in manifest.get("fault_case_ids") or []]
    elif bucket == "all":
        keys = [str(item) for item in manifest.get("included_case_ids") or []]
    else:
        raise SelectionError(f"unknown bucket {bucket!r}; expected all|qa|fault")
    selected = []
    for case_id in keys:
        case = by_id.get(case_id)
        if case is None:
            raise SelectionError(f"selected case {case_id!r} not present in dataset")
        selected.append(case)
    return selected


__all__ = [
    "DATASET_DIR",
    "REVIEW_DIR",
    "SELECTION_SCHEMA",
    "SelectionError",
    "build_selection",
    "case_hash",
    "load_manifest",
    "review_snapshot",
    "select_cases",
    "validate_manifest",
]
