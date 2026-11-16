# -*- coding: utf-8 -*-
"""Local manual-review server for the robot-vacuum v1 eval dataset.

The server is deliberately small: it only reads the known dataset/results
directories and writes review records to a separate store (``eval/reviews`` by
default).  It never calls the product runtime, never executes a holdout case,
and never treats a human verdict as an automatic eval score.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

try:
    from eval.judge_adapter import (
        SCHEMA_VERSION as JUDGE_SCHEMA_VERSION,
        JudgeContractError,
        JudgeRepo,
        text_corpus,
        verify_evidence_quotes,
    )
except ModuleNotFoundError:  # ``python eval/review_server.py`` puts eval/ on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from eval.judge_adapter import (
        SCHEMA_VERSION as JUDGE_SCHEMA_VERSION,
        JudgeContractError,
        JudgeRepo,
        text_corpus,
        verify_evidence_quotes,
    )

APP_NAME = "robot-vacuum-manual-review"
APP_VERSION = "manual-review/v2"
STORE_SCHEMA = "manual-review-store/v1"
RECORD_SCHEMA = "manual-review-record/v1"
EXPORT_SCHEMA = "manual-review-export/v1"

SCOPES = ("case_definition", "run_answer")
SCOPE_LABELS = {"case_definition": "案例定义", "run_answer": "运行回答"}
STATUSES = ("passed", "failed", "uncertain")
STATUS_LABELS = {"passed": "通过", "failed": "不通过", "uncertain": "存疑"}
DEFAULT_REVIEWER = "local-user"
MAX_NOTES = 20000
MAX_REVIEWER = 100
HOLDOUT_NOTE = "用户通过“查看留出集”按钮主动曝光；只查看题目/预期，不自动运行留出集。"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def case_hash(case: dict[str, Any]) -> str:
    """Hash the case definition, excluding the mutable human-review marker."""
    payload = {key: value for key, value in case.items() if key != "review"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(raw.encode("utf-8"))


def record_key(
    case_id: str,
    scope: str,
    run_id: str | None,
    definition_hash: str,
    suite_revision: str,
    run_cases_sha: str = "",
) -> str:
    raw = "|".join(
        [case_id, scope, run_id or "", definition_hash, suite_revision, run_cases_sha]
    )
    return sha256_bytes(raw.encode("utf-8"))[:40]


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class DatasetRepo:
    """Read-only cache for the authoritative v1 dataset."""

    def __init__(self, dataset_dir: Path):
        self.dataset_dir = dataset_dir.resolve()
        self._lock = threading.Lock()
        self._key: tuple[Any, ...] | None = None
        self._data: dict[str, Any] | None = None

    def _current_key(self) -> tuple[Any, ...]:
        names = ("suite.json", "cases.jsonl", "split.json", "audit.json", "evidence_index.json")
        result = []
        for name in names:
            path = self.dataset_dir / name
            if not path.exists():
                result.append((name, None, None))
            else:
                stat = path.stat()
                result.append((name, stat.st_mtime_ns, stat.st_size))
        return tuple(result)

    def load(self) -> dict[str, Any]:
        key = self._current_key()
        with self._lock:
            if self._data is not None and key == self._key:
                return self._data
            suite_path = self.dataset_dir / "suite.json"
            cases_path = self.dataset_dir / "cases.jsonl"
            split_path = self.dataset_dir / "split.json"
            if not cases_path.exists():
                raise RuntimeError(f"cases.jsonl not found under {self.dataset_dir}")
            suite_bytes = suite_path.read_bytes() if suite_path.exists() else b"{}"
            cases_bytes = cases_path.read_bytes()
            split_bytes = split_path.read_bytes() if split_path.exists() else b"{}"
            suite = json.loads(suite_bytes.decode("utf-8") or "{}")
            split = json.loads(split_bytes.decode("utf-8") or "{}")
            cases: list[dict[str, Any]] = []
            for line in cases_bytes.decode("utf-8").splitlines():
                if line.strip():
                    cases.append(json.loads(line))
            by_id = {case["id"]: case for case in cases}
            hashes = {
                "suite.json": sha256_bytes(suite_bytes),
                "cases.jsonl": sha256_bytes(cases_bytes),
                "split.json": sha256_bytes(split_bytes),
            }
            for optional in ("audit.json", "evidence_index.json"):
                optional_path = self.dataset_dir / optional
                hashes[optional] = (
                    sha256_bytes(optional_path.read_bytes()) if optional_path.exists() else None
                )
            revision = suite.get("revision") or suite.get("review_status") or "unknown"
            self._data = {
                "dir": self.dataset_dir,
                "suite": suite,
                "split": split,
                "cases": cases,
                "by_id": by_id,
                "case_hashes": {case["id"]: case_hash(case) for case in cases},
                "hashes": hashes,
                "suite_revision": str(revision),
                "suite_id": suite.get("suite_id") or "robot-support-v1",
            }
            self._key = key
            return self._data

    def case(self, case_id: str) -> dict[str, Any] | None:
        return self.load()["by_id"].get(case_id)


class RunRepo:
    """Read-only index of development run outputs (never runs anything)."""

    def __init__(self, results_dir: Path):
        self.results_dir = results_dir.resolve()
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}

    def refresh(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            current_ids: set[str] = set()
            if self.results_dir.exists():
                for run_dir in sorted(self.results_dir.iterdir()):
                    if not run_dir.is_dir():
                        continue
                    run_json_path = run_dir / "run.json"
                    cases_path = run_dir / "cases.jsonl"
                    if not run_json_path.exists() or not cases_path.exists():
                        continue
                    try:
                        stat_key = (
                            run_json_path.stat().st_mtime_ns,
                            cases_path.stat().st_mtime_ns,
                        )
                    except OSError:
                        continue
                    run_json = load_json(run_json_path, {}) or {}
                    run_id = str(run_json.get("run_id") or run_dir.name)
                    current_ids.add(run_id)
                    cached = self._runs.get(run_id)
                    if cached and cached.get("_stat") == stat_key:
                        continue
                    if run_json.get("split") != "development":
                        continue
                    rows: dict[str, dict[str, Any]] = {}
                    try:
                        lines = cases_path.read_text(encoding="utf-8").splitlines()
                    except OSError:
                        continue
                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        case_id = row.get("case_id") or (row.get("case") or {}).get("id")
                        if case_id:
                            rows[case_id] = row
                    dataset = run_json.get("dataset") or {}
                    file_hashes = dataset.get("file_sha256") or {}
                    self._runs[run_id] = {
                        "runId": run_id,
                        "dir": run_dir,
                        "mode": run_json.get("mode"),
                        "kind": run_json.get("kind"),
                        "status": run_json.get("status"),
                        "split": run_json.get("split"),
                        "startedAt": run_json.get("started_at"),
                        "finishedAt": run_json.get("finished_at"),
                        "datasetHash": dataset.get("dataset_hash"),
                        "casesSha256": file_hashes.get("cases.jsonl"),
                        "suiteSha256": file_hashes.get("suite.json"),
                        "regrade": bool(run_json.get("regrade")) or "regrade" in run_id,
                        "rows": rows,
                        "_stat": stat_key,
                    }
            for stale in set(self._runs) - current_ids:
                del self._runs[stale]
            return dict(self._runs)

    def runs_for_case(self, case_id: str, current_cases_sha: str) -> list[dict[str, Any]]:
        runs = self.refresh()
        result = []
        for run in runs.values():
            row = run["rows"].get(case_id)
            if row is None or not self._row_has_answer(row):
                continue
            result.append(self.public_run(run, current_cases_sha, has_result=True))
        result.sort(key=lambda item: (item.get("startedAt") or "", item["runId"]), reverse=True)
        return result

    def preferred_run(self, case_id: str, current_cases_sha: str) -> dict[str, Any] | None:
        runs = self.runs_for_case(case_id, current_cases_sha)
        if not runs:
            return None
        current_live = [r for r in runs if r["mode"] == "live" and not r["historical"]]
        if current_live:
            return current_live[0]
        current = [r for r in runs if not r["historical"]]
        if current:
            return current[0]
        live = [r for r in runs if r["mode"] == "live"]
        return live[0] if live else runs[0]

    @staticmethod
    def public_run(run: dict[str, Any], current_cases_sha: str, *, has_result: bool) -> dict[str, Any]:
        return {
            "runId": run["runId"],
            "mode": run.get("mode"),
            "status": run.get("status"),
            "startedAt": run.get("startedAt"),
            "finishedAt": run.get("finishedAt"),
            "casesSha256": run.get("casesSha256"),
            "datasetHash": run.get("datasetHash"),
            "historical": bool(run.get("casesSha256") and run["casesSha256"] != current_cases_sha),
            "regrade": bool(run.get("regrade")),
            "hasResult": has_result,
        }

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self.refresh().get(run_id)

    def result_for(self, case_id: str, run_id: str, current_cases_sha: str) -> dict[str, Any] | None:
        run = self.get(run_id)
        if run is None:
            return None
        row = run["rows"].get(case_id)
        if row is None:
            return None
        public = self.public_run(run, current_cases_sha, has_result=True)
        return self._summarize_result(case_id, row, public)

    @staticmethod
    def _row_has_answer(row: dict[str, Any]) -> bool:
        return any(RunRepo._answer_text(turn) for turn in (row.get("turns") or []))

    @staticmethod
    def _answer_text(turn: dict[str, Any]) -> str | None:
        for key in ("answer", "direct_answer"):
            value = turn.get(key)
            if value is None:
                continue
            text = str(value)
            if text.strip() and text.strip().lower() not in ("none", "null"):
                return text
        return None

    @staticmethod
    def _summarize_result(case_id: str, row: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        turns_out = []
        trace_ids: list[str] = []
        all_events: list[str] = []
        all_errors: list[str] = []
        tool_names: set[str] = set()
        for index, turn in enumerate(row.get("turns") or []):
            answer = RunRepo._answer_text(turn)
            retrieval = turn.get("retrieval") or {}
            sources = retrieval.get("sources") or []
            citations = retrieval.get("citations") or []
            events = turn.get("events") or []
            event_names: list[str] = []
            for event in events:
                name = event.get("type") or event.get("name") if isinstance(event, dict) else str(event)
                if name:
                    event_names.append(str(name))
                    all_events.append(str(name))
            errors = turn.get("errors") or []
            if isinstance(errors, str):
                errors = [errors]
            all_errors.extend(str(item) for item in errors if item)
            support = turn.get("support") or {}
            for record in support.get("records") or []:
                if isinstance(record, dict) and record.get("tool"):
                    tool_names.add(str(record["tool"]))
            trace_id = turn.get("trace_id")
            if trace_id:
                trace_ids.append(str(trace_id))
            turns_out.append(
                {
                    "index": turn.get("index", index + 1),
                    "role": turn.get("role"),
                    "answer": answer,
                    "hasAnswer": answer is not None,
                    "latencyMs": turn.get("latency_ms"),
                    "traceId": trace_id,
                    "eventCount": len(events),
                    "eventTypes": sorted(set(event_names)),
                    "errors": errors,
                    "retrievalStatus": retrieval.get("status"),
                    "retrievalAttempts": retrieval.get("attempts"),
                    "sourceCount": len(retrieval.get("results") or []),
                    "sources": sources,
                    "citations": citations,
                    "usage": turn.get("usage"),
                }
            )
        row_trace_ids = [str(item) for item in (row.get("trace_ids") or [])]
        for item in row_trace_ids:
            if item not in trace_ids:
                trace_ids.append(item)
        unique_events = sorted(set(all_events))
        checks = [
            {"name": check.get("name"), "status": check.get("status")}
            for check in (row.get("checks") or [])
            if isinstance(check, dict)
        ]
        answers = [turn for turn in turns_out if turn["hasAnswer"]]
        message = None
        if not answers:
            message = "该运行没有回答文本（状态检查运行）。不把 expected 当作 actual。"
        return {
            "available": True,
            "caseId": case_id,
            "run": run,
            "rowStatus": row.get("status"),
            "rowMode": row.get("mode") or run.get("mode"),
            "attempted": row.get("attempted"),
            "error": row.get("error"),
            "skipReason": row.get("skip_reason"),
            "startedAt": row.get("started_at"),
            "endedAt": row.get("ended_at"),
            "latencyMs": row.get("latency_ms"),
            "sessionId": row.get("session_id"),
            "turns": turns_out,
            "hasAnyAnswer": bool(answers),
            "message": message,
            "trace": {
                "traceIds": trace_ids,
                "eventCount": len(all_events),
                "eventTypes": unique_events,
                "tools": sorted(tool_names),
                "errors": all_errors,
                "retrievalTurns": sum(1 for turn in turns_out if turn.get("retrievalStatus")),
            },
            "checks": checks,
            "semanticCounts": row.get("semantic_counts") or {},
            "note": "运行结果只读展示；人工审核不写回 run，也不作为自动得分。",
        }


class ReviewStore:
    """Revisioned, hashed review records plus holdout exposure events."""

    def __init__(self, store_dir: Path, default_reviewer: str = DEFAULT_REVIEWER):
        self.root = store_dir.resolve()
        self.reviews_dir = self.root / "reviews"
        self.exposures_path = self.root / "exposures.json"
        self.meta_path = self.root / "store.json"
        self.default_reviewer = default_reviewer
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.reviews_dir.mkdir(parents=True, exist_ok=True)
        if not self.meta_path.exists():
            atomic_write_json(
                self.meta_path,
                {
                    "schema_version": STORE_SCHEMA,
                    "created_at": now_iso(),
                    "note": "人工审核记录独立于数据集与 run；不写入自动评分。",
                },
            )

    def _records(self) -> list[dict[str, Any]]:
        records = []
        for path in self.reviews_dir.glob("*.json"):
            record = load_json(path)
            if isinstance(record, dict) and record.get("reviewId"):
                records.append(record)
        return records

    def current(
        self,
        case_id: str,
        scope: str,
        run_id: str | None,
        definition_hash: str,
        suite_revision: str,
        run_cases_sha: str = "",
    ) -> dict[str, Any] | None:
        key = record_key(case_id, scope, run_id, definition_hash, suite_revision, run_cases_sha)
        return load_json(self.reviews_dir / f"{key}.json")

    def historical(
        self,
        case_id: str,
        scope: str,
        run_id: str | None,
        current_key: str,
    ) -> list[dict[str, Any]]:
        result = []
        for record in self._records():
            if (
                record.get("caseId") == case_id
                and record.get("reviewScope") == scope
                and (record.get("runId") or None) == (run_id or None)
                and record.get("reviewId") != current_key
            ):
                result.append(record)
        result.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
        return result

    def save(self, record: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        key = record["reviewId"]
        path = self.reviews_dir / f"{key}.json"
        with self._lock:
            existing = load_json(path)
            current_revision = int(existing.get("revision", 0)) if existing else 0
            if expected_revision != current_revision:
                return {
                    "conflict": True,
                    "currentRevision": current_revision,
                    "current": existing,
                }
            revision = current_revision + 1
            created_at = (
                existing.get("createdAt") if existing else record.get("createdAt")
            ) or now_iso()
            record = dict(record)
            record.update(
                {
                    "schemaVersion": RECORD_SCHEMA,
                    "revision": revision,
                    "createdAt": created_at,
                    "updatedAt": now_iso(),
                }
            )
            atomic_write_json(path, record)
            return {"conflict": False, "record": record}

    def save_initial(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.save(record, 0)

    def exposures(self) -> list[dict[str, Any]]:
        payload = load_json(self.exposures_path, {"schema_version": "manual-review-exposures/v1", "events": []})
        events = payload.get("events") if isinstance(payload, dict) else None
        return list(events or [])

    def expose_holdout(self, reviewer: str, request: Request, suite_revision: str, case_count: int) -> dict[str, Any]:
        with self._lock:
            events = self.exposures()
            if events:
                return {"first": False, "event": events[0]}
            event = {
                "eventId": "holdout-exposure-1",
                "at": now_iso(),
                "reviewer": reviewer,
                "suiteRevision": suite_revision,
                "caseCount": case_count,
                "split": "holdout",
                "note": HOLDOUT_NOTE,
                "userAgent": (request.headers.get("user-agent") or "")[:300],
                "remote": request.client.host if request.client else None,
            }
            atomic_write_json(
                self.exposures_path,
                {
                    "schema_version": "manual-review-exposures/v1",
                    "events": [event],
                    "note": "首次显式查看留出集的事件；不包含留出题内容，不代表运行。",
                },
            )
            return {"first": True, "event": event}

    def counts(self) -> dict[str, Any]:
        records = self._records()
        counts = {
            "reviewCount": len(records),
            "exposureCount": len(self.exposures()),
            "byStatus": {status: 0 for status in STATUSES},
            "byScope": {scope: 0 for scope in SCOPES},
        }
        for record in records:
            status = record.get("status")
            if status in counts["byStatus"]:
                counts["byStatus"][status] += 1
            scope = record.get("reviewScope")
            if scope in counts["byScope"]:
                counts["byScope"][scope] += 1
        return counts

    def export_payload(self, dataset: dict[str, Any], runs: RunRepo) -> dict[str, Any]:
        records = sorted(self._records(), key=lambda item: item.get("updatedAt") or "")
        enriched = []
        for record in records:
            item = dict(record)
            run = runs.get(record.get("runId") or "") if record.get("runId") else None
            item["runMode"] = run.get("mode") if run else None
            item["runHistorical"] = bool(
                run
                and run.get("casesSha256")
                and run.get("casesSha256") != dataset["hashes"]["cases.jsonl"]
            )
            enriched.append(item)
        counts = self.counts()
        return {
            "schemaVersion": EXPORT_SCHEMA,
            "exportedAt": now_iso(),
            "suite": {
                "suiteId": dataset["suite_id"],
                "revision": dataset["suite_revision"],
                "schemaVersion": (dataset["suite"] or {}).get("schema_version"),
                "counts": (dataset["suite"] or {}).get("counts"),
            },
            "dataset": {
                "casesSha256": dataset["hashes"]["cases.jsonl"],
                "splitSha256": dataset["hashes"]["split.json"],
                "suiteSha256": dataset["hashes"]["suite.json"],
            },
            "store": {
                "schemaVersion": STORE_SCHEMA,
                "reviewCount": counts["reviewCount"],
                "exposureCount": counts["exposureCount"],
            },
            "counts": counts,
            "reviews": enriched,
            "exposures": self.exposures(),
            "note": "人工审核标注；不自动计入 Agent 得分，不改变原始 dataset/split/hash。",
        }


def csv_export_bytes(payload: dict[str, Any]) -> bytes:
    buffer = io.StringIO(newline="")
    fieldnames = [
        "record_type",
        "caseId",
        "title",
        "split",
        "reviewScope",
        "runId",
        "runMode",
        "runHistorical",
        "status",
        "statusLabel",
        "notes",
        "reviewer",
        "caseHash",
        "suiteId",
        "suiteRevision",
        "revision",
        "createdAt",
        "updatedAt",
        "exposureAt",
        "exposureCount",
        "exposureNote",
        "runDatasetCasesSha256",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for record in payload.get("reviews") or []:
        run_snapshot = record.get("runSnapshot") or {}
        writer.writerow(
            {
                "record_type": "review",
                "caseId": record.get("caseId"),
                "title": record.get("title"),
                "split": record.get("split"),
                "reviewScope": record.get("reviewScope"),
                "runId": record.get("runId") or "",
                "runMode": record.get("runMode") or "",
                "runHistorical": record.get("runHistorical", False),
                "status": record.get("status"),
                "statusLabel": STATUS_LABELS.get(record.get("status"), ""),
                "notes": record.get("notes") or "",
                "reviewer": record.get("reviewer"),
                "caseHash": record.get("caseHash"),
                "suiteId": record.get("suiteId"),
                "suiteRevision": record.get("suiteRevision"),
                "revision": record.get("revision"),
                "createdAt": record.get("createdAt"),
                "updatedAt": record.get("updatedAt"),
                "runDatasetCasesSha256": run_snapshot.get("casesSha256") or "",
            }
        )
    for event in payload.get("exposures") or []:
        writer.writerow(
            {
                "record_type": "holdout_exposure",
                "reviewer": event.get("reviewer"),
                "suiteRevision": event.get("suiteRevision"),
                "exposureAt": event.get("at"),
                "exposureCount": event.get("caseCount"),
                "exposureNote": event.get("note"),
            }
        )
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def _host_ok(host_header: str) -> bool:
    host = (host_header or "").strip().lower()
    if not host:
        return False
    hostname = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
    hostname = hostname.strip("[]")
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _origin_matches(origin: str | None, host: str, scheme: str) -> bool:
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return parsed.netloc.lower() == host.lower()


class ReviewIn(BaseModel):
    caseId: str
    caseHash: str
    suiteRevision: str
    reviewScope: str
    runId: str | None = None
    status: str
    notes: str = ""
    reviewer: str | None = None
    expectedRevision: int = Field(ge=0)


class ExposureIn(BaseModel):
    reviewer: str | None = None


def create_app(
    *,
    project_root: Path | str | None = None,
    dataset_dir: Path | str | None = None,
    results_dir: Path | str | None = None,
    store_dir: Path | str | None = None,
    judge_results_dir: Path | str | None = None,
    default_reviewer: str = DEFAULT_REVIEWER,
    server_state_file: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 8950,
) -> FastAPI:
    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parents[1]
    dataset_path = Path(dataset_dir).resolve() if dataset_dir else root / "eval" / "datasets" / "v1"
    results_path = Path(results_dir).resolve() if results_dir else root / "eval" / "results"
    store_path = Path(store_dir).resolve() if store_dir else root / "eval" / "reviews"
    judge_results_path = (
        Path(judge_results_dir).resolve()
        if judge_results_dir
        else root / "eval" / "judge_results"
    )
    ui_dir = (root / "eval" / "review_ui").resolve()
    dataset_repo = DatasetRepo(dataset_path)
    run_repo = RunRepo(results_path)
    store = ReviewStore(store_path, default_reviewer=default_reviewer)
    judge_repo = JudgeRepo(judge_results_path)
    state_file = Path(server_state_file).resolve() if server_state_file else None
    started_at = now_iso()

    def dataset() -> dict[str, Any]:
        return dataset_repo.load()

    def state_payload(status: str) -> dict[str, Any]:
        return {
            "status": status,
            "app": APP_NAME,
            "version": APP_VERSION,
            "pid": os.getpid(),
            "host": host,
            "port": port,
            "baseUrl": f"http://{host}:{port}",
            "startedAt": started_at,
            "updatedAt": now_iso(),
            "store": str(store_path),
            "dataset": str(dataset_path),
            "logNote": "后台启动使用 pythonw，窗口不可见；health 为权威存活检查。",
        }

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if state_file:
            atomic_write_json(state_file, state_payload("running"))
        try:
            yield
        finally:
            if state_file:
                payload = state_payload("stopped")
                payload["stoppedAt"] = now_iso()
                atomic_write_json(state_file, payload)

    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.dataset_repo = dataset_repo
    app.state.run_repo = run_repo
    app.state.store = store
    app.state.judge_repo = judge_repo

    @app.middleware("http")
    async def security(request: Request, call_next):
        if not _host_ok(request.headers.get("host", "")):
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": "host_not_allowed", "message": "只允许 127.0.0.1/localhost 访问。"},
            )
        sec_fetch = (request.headers.get("sec-fetch-site") or "").lower()
        if sec_fetch and sec_fetch not in ("same-origin", "same-site", "none"):
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": "cross_origin", "message": "拒绝跨站提交。"},
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        return response

    def require_mutation(request: Request) -> None:
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        if origin and not _origin_matches(origin, host, request.url.scheme):
            raise HTTPException(
                status_code=403,
                detail={"ok": False, "error": "origin_not_allowed", "message": "只接受同源页面提交。"},
            )
        referer = request.headers.get("referer")
        if referer and not _origin_matches(urlsplit(referer).scheme + "://" + urlsplit(referer).netloc, host, request.url.scheme):
            raise HTTPException(
                status_code=403,
                detail={"ok": False, "error": "referer_not_allowed", "message": "只接受同源页面提交。"},
            )
        if (request.headers.get("x-requested-with") or "") != "manual-review":
            raise HTTPException(
                status_code=403,
                detail={"ok": False, "error": "missing_client_header", "message": "缺少本地审核页面请求头。"},
            )
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" not in content_type:
            raise HTTPException(
                status_code=415,
                detail={"ok": False, "error": "json_required", "message": "提交必须使用 application/json。"},
            )

    def holdout_exposed() -> bool:
        return bool(store.exposures())

    def ensure_holdout_allowed(split: str, include_holdout: bool) -> None:
        if split == "holdout" and (not include_holdout or not holdout_exposed()):
            raise HTTPException(
                status_code=403,
                detail={
                    "ok": False,
                    "error": "holdout_hidden",
                    "message": "留出集默认隐藏；请先点击“查看留出集”并记录曝光。",
                },
            )

    def run_snapshot_for(case_id: str, run_id: str | None) -> dict[str, Any] | None:
        if not run_id:
            return None
        ds = dataset()
        result = run_repo.result_for(case_id, run_id, ds["hashes"]["cases.jsonl"])
        if result is None:
            return None
        run = result["run"]
        return {
            "runId": run_id,
            "mode": run.get("mode"),
            "status": run.get("status"),
            "startedAt": run.get("startedAt"),
            "casesSha256": run.get("casesSha256"),
            "datasetHash": run.get("datasetHash"),
            "historical": run.get("historical"),
        }

    def build_record(case: dict[str, Any], body: ReviewIn) -> dict[str, Any]:
        ds = dataset()
        definition_hash = ds["case_hashes"][case["id"]]
        run_snapshot = run_snapshot_for(case["id"], body.runId) if body.reviewScope == "run_answer" else None
        key = record_key(
            case["id"],
            body.reviewScope,
            body.runId,
            definition_hash,
            ds["suite_revision"],
            (run_snapshot or {}).get("casesSha256") or "",
        )
        reviewer = (body.reviewer or store.default_reviewer).strip()[:MAX_REVIEWER]
        if not reviewer:
            reviewer = store.default_reviewer
        return {
            "reviewId": key,
            "schemaVersion": RECORD_SCHEMA,
            "caseId": case["id"],
            "title": case.get("title") or "",
            "category": case.get("category"),
            "split": case.get("split"),
            "reviewScope": body.reviewScope,
            "scopeLabel": SCOPE_LABELS[body.reviewScope],
            "runId": body.runId if body.reviewScope == "run_answer" else None,
            "runSnapshot": run_snapshot,
            "status": body.status,
            "statusLabel": STATUS_LABELS[body.status],
            "notes": body.notes,
            "reviewer": reviewer,
            "caseHash": definition_hash,
            "suiteId": ds["suite_id"],
            "suiteRevision": ds["suite_revision"],
            "datasetCasesSha256": ds["hashes"]["cases.jsonl"],
            "createdAt": now_iso(),
        }

    def judge_overview() -> dict[str, Any]:
        """Read-only index summary; missing/empty root is a valid empty state."""
        try:
            runs = judge_repo.list_runs()
        except (OSError, ValueError) as error:
            return {
                "root": str(judge_results_path),
                "exists": judge_repo.exists(),
                "runCount": 0,
                "resultCount": 0,
                "malformedCount": 0,
                "runs": [],
                "readOnly": True,
                "noModel": True,
                "error": str(error),
            }
        return {
            "root": str(judge_results_path),
            "exists": judge_repo.exists(),
            "runCount": len(runs),
            "resultCount": sum(run["caseCount"] for run in runs),
            "malformedCount": sum(run["malformedCount"] for run in runs),
            "runs": runs,
            "readOnly": True,
            "noModel": True,
            "schemaVersion": JUDGE_SCHEMA_VERSION,
        }

    def judge_for_case(case: dict[str, Any], run_id: str | None) -> dict[str, Any]:
        ds = dataset()
        resolution = judge_repo.resolve_for_case(
            case["id"],
            run_id,
            ds["case_hashes"][case["id"]],
        )
        # The judge artifact is external/untrusted.  Verify its quoted evidence
        # against the read-only dataset case + the exact selected run row, so a
        # fabricated quote is shown as untraceable instead of "原文证据".
        corpus_parts = [
            json.dumps(case, ensure_ascii=False, sort_keys=True),
            text_corpus(case),
        ]
        if run_id:
            run = run_repo.get(run_id)
            if run is not None:
                row = run["rows"].get(case["id"])
                if row is not None:
                    corpus_parts.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
                    corpus_parts.append(text_corpus(row))
        corpus = "\n".join(corpus_parts)
        buckets = [resolution.get("selected")] + list(resolution.get("results") or []) + list(
            resolution.get("historical") or []
        )
        for item in buckets:
            if isinstance(item, dict):
                item["evidenceTrust"] = verify_evidence_quotes(item, corpus)
        selected = resolution.get("selected")
        resolution["evidenceTrust"] = (
            selected.get("evidenceTrust") if isinstance(selected, dict) else None
        )
        resolution["boundaries"] = {
            "readOnlyJudgeResults": True,
            "noModelCall": True,
            "doesNotWriteHumanReview": True,
            "doesNotOverwriteNotes": True,
            "doesNotRunHoldout": True,
            "onlyWhitelistedRoot": True,
            "evidenceQuotesLocallyVerified": True,
            "judgeArtifactUntrusted": True,
        }
        resolution["judgeRoot"] = str(judge_results_path)
        return resolution

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        ds = dataset()
        counts = store.counts()
        judge = judge_overview()
        return {
            "ok": True,
            "app": APP_NAME,
            "version": APP_VERSION,
            "service": "robot-vacuum-support/manual-review",
            "pid": os.getpid(),
            "host": host,
            "port": port,
            "store": str(store_path),
            "datasetRevision": ds["suite_revision"],
            "reviewCount": counts["reviewCount"],
            "exposureCount": counts["exposureCount"],
            "holdoutExposed": holdout_exposed(),
            "judgeRunCount": judge["runCount"],
            "judgeResultCount": judge["resultCount"],
            "judgeSchemaVersion": JUDGE_SCHEMA_VERSION,
            "readOnlyDataset": True,
            "canRunHoldout": False,
            "canRunJudge": False,
        }

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        ds = dataset()
        exposures = store.exposures()
        return {
            "ok": True,
            "app": APP_NAME,
            "version": APP_VERSION,
            "reviewerDefault": store.default_reviewer,
            "suite": {
                "suiteId": ds["suite_id"],
                "revision": ds["suite_revision"],
                "schemaVersion": (ds["suite"] or {}).get("schema_version"),
                "counts": (ds["suite"] or {}).get("counts"),
                "casesSha256": ds["hashes"]["cases.jsonl"],
                "splitSha256": ds["hashes"]["split.json"],
                "suiteSha256": ds["hashes"]["suite.json"],
                "reviewStatus": (ds["suite"] or {}).get("review_status"),
                "caseCount": len(ds["cases"]),
            },
            "scopes": [{"value": scope, "label": SCOPE_LABELS[scope]} for scope in SCOPES],
            "statuses": [{"value": status, "label": STATUS_LABELS[status]} for status in STATUSES],
            "holdout": {
                "hiddenByDefault": True,
                "exposed": bool(exposures),
                "firstExposure": exposures[0] if exposures else None,
                "note": HOLDOUT_NOTE,
            },
            "store": {"label": "eval/reviews", "counts": store.counts()},
            "judge": {
                "schemaVersion": JUDGE_SCHEMA_VERSION,
                "rootLabel": "eval/judge_results",
                "root": str(judge_results_path),
                "readOnly": True,
                "noModel": True,
                "doesNotWriteHumanReview": True,
                "overview": judge_overview(),
                "note": "DSH Judge 结果只读展示；不触发模型、不写人工状态/备注、不改 dataset/run。",
            },
            "boundaries": {
                "noAgentRun": True,
                "noHoldoutRun": True,
                "humanScoresAreNotAutomaticScores": True,
                "judgeReadOnly": True,
                "judgeDoesNotWriteHumanState": True,
                "judgeNeverTreatsErrorAsPass": True,
            },
        }

    def current_review_for(case: dict[str, Any], scope: str, run_id: str | None) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
        ds = dataset()
        definition_hash = ds["case_hashes"][case["id"]]
        run_snapshot = run_snapshot_for(case["id"], run_id) if scope == "run_answer" else None
        key = record_key(
            case["id"],
            scope,
            run_id,
            definition_hash,
            ds["suite_revision"],
            (run_snapshot or {}).get("casesSha256") or "",
        )
        current = store.current(
            case["id"],
            scope,
            run_id,
            definition_hash,
            ds["suite_revision"],
            (run_snapshot or {}).get("casesSha256") or "",
        )
        historical = store.historical(case["id"], scope, run_id, key)
        return key, current, historical

    @app.get("/api/cases")
    def cases(
        request: Request,
        split: str = "development",
        filter: str = "all",
        scope: str = "case_definition",
    ) -> dict[str, Any]:
        if split not in ("development", "holdout"):
            raise HTTPException(status_code=400, detail={"error": "bad_split", "message": "split 只能是 development/holdout。"})
        if scope not in SCOPES:
            raise HTTPException(status_code=400, detail={"error": "bad_scope", "message": "未知审核对象。"})
        if filter not in ("all", "unreviewed", *STATUSES):
            raise HTTPException(status_code=400, detail={"error": "bad_filter", "message": "未知筛选状态。"})
        include_holdout = request.query_params.get("include_holdout") == "1"
        ensure_holdout_allowed(split, include_holdout)
        ds = dataset()
        split_ids = (ds["split"].get(split) or {}).get("case_ids") or []
        if not split_ids:
            split_ids = [case["id"] for case in ds["cases"] if case.get("split") == split]
        current_sha = ds["hashes"]["cases.jsonl"]
        rows = []
        progress = {
            "case_definition": {"total": 0, "reviewed": 0, "unreviewed": 0, **{s: 0 for s in STATUSES}},
            "run_answer": {"total": 0, "eligible": 0, "reviewed": 0, "unreviewed": 0, **{s: 0 for s in STATUSES}},
        }
        records = store._records()
        for case_id in split_ids:
            case = ds["by_id"].get(case_id)
            if not case:
                continue
            definition_hash = ds["case_hashes"][case_id]
            definition_record = store.current(
                case_id, "case_definition", None, definition_hash, ds["suite_revision"], ""
            )
            runs = run_repo.runs_for_case(case_id, current_sha)
            preferred = run_repo.preferred_run(case_id, current_sha)
            run_record = None
            if preferred:
                run_record = store.current(
                    case_id,
                    "run_answer",
                    preferred["runId"],
                    definition_hash,
                    ds["suite_revision"],
                    preferred.get("casesSha256") or "",
                )
            reviewed_runs = [
                record
                for record in records
                if record.get("caseId") == case_id and record.get("reviewScope") == "run_answer"
            ]
            definition_status = (definition_record or {}).get("status") or "unreviewed"
            run_status = (run_record or {}).get("status") or "unreviewed"
            progress["case_definition"]["total"] += 1
            if runs:
                progress["run_answer"]["total"] += 1
                progress["run_answer"]["eligible"] += 1
            for metric, status in (("case_definition", definition_status), ("run_answer", run_status)):
                if metric == "run_answer" and not runs:
                    continue
                if status == "unreviewed":
                    progress[metric]["unreviewed"] += 1
                else:
                    progress[metric]["reviewed"] += 1
                    if status in STATUSES:
                        progress[metric][status] += 1
            selected_status = definition_status if scope == "case_definition" else run_status
            if filter == "unreviewed" and selected_status != "unreviewed":
                continue
            if filter in STATUSES and selected_status != filter:
                continue
            rows.append(
                {
                    "caseId": case_id,
                    "title": case.get("title") or "",
                    "category": case.get("category"),
                    "split": case.get("split"),
                    "familyId": case.get("family_id"),
                    "turnCount": len(case.get("turns") or []),
                    "caseHash": definition_hash,
                    "review": {
                        "case_definition": {
                            "status": definition_status,
                            "revision": (definition_record or {}).get("revision"),
                            "updatedAt": (definition_record or {}).get("updatedAt"),
                        },
                        "run_answer": {
                            "status": run_status,
                            "revision": (run_record or {}).get("revision"),
                            "updatedAt": (run_record or {}).get("updatedAt"),
                            "runId": preferred["runId"] if preferred else None,
                            "reviewedRunCount": len(reviewed_runs),
                        },
                    },
                    "hasRuns": bool(runs),
                    "defaultRunId": preferred["runId"] if preferred else None,
                }
            )
        return {
            "ok": True,
            "split": split,
            "scope": scope,
            "filter": filter,
            "includeHoldout": include_holdout,
            "suiteRevision": ds["suite_revision"],
            "rows": rows,
            "totalBeforeFilter": len(split_ids),
            "filteredTotal": len(rows),
            "progress": progress,
            "holdout": {"exposed": holdout_exposed()},
        }

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: str, request: Request) -> dict[str, Any]:
        ds = dataset()
        case = ds["by_id"].get(case_id)
        if not case:
            raise HTTPException(status_code=404, detail={"ok": False, "error": "case_not_found", "message": "未知 case。"})
        if case.get("split") == "holdout":
            ensure_holdout_allowed("holdout", request.query_params.get("include_holdout") == "1")
        current_sha = ds["hashes"]["cases.jsonl"]
        runs = run_repo.runs_for_case(case_id, current_sha)
        preferred = run_repo.preferred_run(case_id, current_sha)
        query_run = request.query_params.get("runId")
        selected_run_id = query_run if query_run and any(r["runId"] == query_run for r in runs) else (preferred["runId"] if preferred else None)
        return {
            "ok": True,
            "case": case,
            "caseHash": ds["case_hashes"][case_id],
            "suiteRevision": ds["suite_revision"],
            "suiteId": ds["suite_id"],
            "runs": runs,
            "defaultRunId": preferred["runId"] if preferred else None,
            "selectedRunId": selected_run_id,
            "runCount": len(runs),
            "noRunMessage": None if runs else "暂无运行结果",
        }

    @app.get("/api/cases/{case_id}/run")
    def case_run(case_id: str, request: Request, runId: str = "") -> dict[str, Any]:
        ds = dataset()
        case = ds["by_id"].get(case_id)
        if not case:
            raise HTTPException(status_code=404, detail={"ok": False, "error": "case_not_found", "message": "未知 case。"})
        if case.get("split") == "holdout":
            ensure_holdout_allowed("holdout", request.query_params.get("include_holdout") == "1")
        if not runId:
            raise HTTPException(status_code=400, detail={"ok": False, "error": "run_required", "message": "缺少 runId。"})
        result = run_repo.result_for(case_id, runId, ds["hashes"]["cases.jsonl"])
        if result is None:
            return {
                "ok": True,
                "available": False,
                "caseId": case_id,
                "runId": runId,
                "message": "暂无运行结果",
                "note": "只展示已有 development 运行；不会为此触发模型或 holdout。",
            }
        return {"ok": True, **result}

    @app.get("/api/judge/runs")
    def judge_runs() -> dict[str, Any]:
        """List whitelisted judge runs (metadata only). Never runs a judge."""
        overview = judge_overview()
        return {"ok": True, "schemaVersion": JUDGE_SCHEMA_VERSION, **overview}

    @app.get("/api/cases/{case_id}/judge")
    def case_judge(case_id: str, request: Request, runId: str = "") -> dict[str, Any]:
        """Read-only judge advice for the selected case + run.

        Selection binds to the current case definition hash and the requested
        run id.  Old/mismatched results are returned under ``historical`` and
        never become ``selected``.  An empty result is an explicit pending
        state, not a pass.
        """
        ds = dataset()
        case = ds["by_id"].get(case_id)
        if not case:
            raise HTTPException(status_code=404, detail={"ok": False, "error": "case_not_found", "message": "未知 case。"})
        if case.get("split") == "holdout":
            ensure_holdout_allowed("holdout", request.query_params.get("include_holdout") == "1")
        run_id = runId.strip() or None
        if run_id and run_repo.get(run_id) is None:
            return {
                "ok": True,
                "available": False,
                "caseId": case_id,
                "runId": run_id,
                "currentCaseHash": ds["case_hashes"][case_id],
                "selected": None,
                "results": [],
                "historical": [],
                "errors": [],
                "message": "该 run 不在只读 run 索引中；暂无 Judge 建议。",
                "runKnown": False,
                "boundaries": {
                    "readOnlyJudgeResults": True,
                    "noModelCall": True,
                    "doesNotWriteHumanReview": True,
                },
            }
        payload = judge_for_case(case, run_id)
        payload["runKnown"] = run_id is None or run_repo.get(run_id) is not None
        return payload

    @app.get("/api/judge/results/{judge_run_id}/{case_id}")
    def judge_result(judge_run_id: str, case_id: str, request: Request) -> dict[str, Any]:
        """Read one whitelisted artifact by ids (no caller-supplied path)."""
        ds = dataset()
        case = ds["by_id"].get(case_id)
        if not case:
            raise HTTPException(status_code=404, detail={"ok": False, "error": "case_not_found", "message": "未知 case。"})
        if case.get("split") == "holdout":
            ensure_holdout_allowed("holdout", request.query_params.get("include_holdout") == "1")
        try:
            result = judge_repo.result_for(judge_run_id, case_id)
        except JudgeContractError as error:
            status = 404 if error.code == "unreadable_result" else 400
            raise HTTPException(
                status_code=status,
                detail={
                    "ok": False,
                    "error": error.code,
                    "message": error.message,
                    "path": error.path,
                },
            ) from error
        result["historical"] = result["caseHash"] != ds["case_hashes"][case_id]
        return {
            "ok": True,
            "result": result,
            "boundaries": {"readOnly": True, "noModel": True, "onlyWhitelistedRoot": True},
        }

    @app.get("/api/reviews")
    def reviews(case_id: str, request: Request, scope: str = "case_definition", runId: str = "") -> dict[str, Any]:
        if scope not in SCOPES:
            raise HTTPException(status_code=400, detail={"ok": False, "error": "bad_scope", "message": "未知审核对象。"})
        ds = dataset()
        case = ds["by_id"].get(case_id)
        if not case:
            raise HTTPException(status_code=404, detail={"ok": False, "error": "case_not_found", "message": "未知 case。"})
        if case.get("split") == "holdout":
            ensure_holdout_allowed("holdout", request.query_params.get("include_holdout") == "1")
        run_id = runId or None
        if scope == "case_definition":
            run_id = None
        if scope == "run_answer" and not run_id:
            return {"ok": True, "caseId": case_id, "scope": scope, "runId": None, "current": None, "historical": []}
        if scope == "run_answer":
            result = run_repo.result_for(case_id, run_id, ds["hashes"]["cases.jsonl"])
            if result is None or not result.get("hasAnyAnswer"):
                return {"ok": True, "caseId": case_id, "scope": scope, "runId": run_id, "current": None, "historical": [], "noResult": True}
        key, current, historical = current_review_for(case, scope, run_id)
        return {
            "ok": True,
            "caseId": case_id,
            "scope": scope,
            "runId": run_id,
            "reviewId": key,
            "current": current,
            "historical": historical,
            "currentCaseHash": ds["case_hashes"][case_id],
            "suiteRevision": ds["suite_revision"],
        }

    @app.post("/api/reviews")
    def save_review(body: ReviewIn, request: Request) -> Any:
        require_mutation(request)
        if body.reviewScope not in SCOPES:
            raise HTTPException(status_code=400, detail={"ok": False, "error": "bad_scope", "message": "未知审核对象。"})
        if body.status not in STATUSES:
            raise HTTPException(status_code=400, detail={"ok": False, "error": "bad_status", "message": "状态只能是 passed/failed/uncertain。"})
        if len(body.notes) > MAX_NOTES:
            raise HTTPException(status_code=400, detail={"ok": False, "error": "notes_too_long", "message": "备注过长。"})
        ds = dataset()
        case = ds["by_id"].get(body.caseId)
        if not case:
            raise HTTPException(status_code=404, detail={"ok": False, "error": "case_not_found", "message": "未知 case。"})
        if case.get("split") == "holdout" and not holdout_exposed():
            raise HTTPException(status_code=403, detail={"ok": False, "error": "holdout_hidden", "message": "留出集需先显式查看并记录曝光。"})
        current_hash = ds["case_hashes"][case["id"]]
        if body.caseHash != current_hash or body.suiteRevision != ds["suite_revision"]:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error": "dataset_changed",
                    "message": "题目或数据版本已变化；旧审核保留为历史快照。",
                    "currentCaseHash": current_hash,
                    "currentSuiteRevision": ds["suite_revision"],
                },
            )
        run_snapshot = None
        if body.reviewScope == "case_definition":
            if body.runId:
                raise HTTPException(status_code=400, detail={"ok": False, "error": "run_not_allowed", "message": "案例定义审核不接受 runId。"})
        else:
            if not body.runId:
                raise HTTPException(status_code=400, detail={"ok": False, "error": "run_required", "message": "运行回答审核必须选择 run。"})
            run_snapshot = run_snapshot_for(case["id"], body.runId)
            if run_snapshot is None:
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "error": "run_snapshot_unavailable",
                        "message": "该 run 没有此 case 的回答快照，不能保存运行回答审核。",
                        "runId": body.runId,
                    },
                )
            run_result = run_repo.result_for(case["id"], body.runId, ds["hashes"]["cases.jsonl"])
            if run_result is None or not run_result.get("hasAnyAnswer"):
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "error": "no_run_answer",
                        "message": "该 run 没有回答文本（例如状态检查运行）；不能把 expected 当作 actual 来保存运行回答审核。",
                        "runId": body.runId,
                    },
                )
        record = build_record(case, body)
        result = store.save(record, body.expectedRevision)
        if result.get("conflict"):
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error": "revision_conflict",
                    "message": "审核已被其他页面更新；你的本地输入未丢失。",
                    "currentRevision": result["currentRevision"],
                    "current": result.get("current"),
                    "expectedRevision": body.expectedRevision,
                },
            )
        return {"ok": True, "record": result["record"], "savedAt": result["record"]["updatedAt"]}

    @app.post("/api/holdout/expose")
    def expose_holdout(body: ExposureIn, request: Request) -> dict[str, Any]:
        require_mutation(request)
        ds = dataset()
        reviewer = (body.reviewer or store.default_reviewer).strip()[:MAX_REVIEWER] or store.default_reviewer
        count = ((ds["split"].get("holdout") or {}).get("case_ids") or [])
        result = store.expose_holdout(reviewer, request, ds["suite_revision"], len(count))
        return {"ok": True, **result, "note": HOLDOUT_NOTE}

    @app.get("/api/export")
    def export(format: str = "json") -> Response:
        if format not in ("json", "csv"):
            raise HTTPException(status_code=400, detail={"ok": False, "error": "bad_format", "message": "format 只能是 json/csv。"})
        ds = dataset()
        payload = store.export_payload(ds, run_repo)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if format == "csv":
            return Response(
                content=csv_export_bytes(payload),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="manual-review-{stamp}.csv"'},
            )
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="manual-review-{stamp}.json"'},
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        file = ui_dir / "index.html"
        if not file.exists():
            raise HTTPException(status_code=500, detail={"error": "ui_missing", "message": "审核页面文件缺失。"})
        # The UI is edited in place; never let a browser keep a stale app.js
        # after the review server is restarted.
        return HTMLResponse(file.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @app.get("/static/{name}")
    def static_file(name: str) -> Response:
        allowed = {
            "app.js": "application/javascript; charset=utf-8",
            "styles.css": "text/css; charset=utf-8",
        }
        if name not in allowed:
            raise HTTPException(status_code=404, detail={"ok": False, "error": "not_found"})
        file = ui_dir / name
        if not file.exists():
            raise HTTPException(status_code=404, detail={"ok": False, "error": "not_found"})
        return Response(
            content=file.read_text(encoding="utf-8"),
            media_type=allowed[name],
            headers={"Cache-Control": "no-store"},
        )

    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="robot-vacuum manual review server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8950)
    parser.add_argument("--store", default=None, help="review store directory (default eval/reviews)")
    parser.add_argument("--dataset", default=None, help="dataset directory (default eval/datasets/v1)")
    parser.add_argument("--results", default=None, help="results directory (default eval/results)")
    parser.add_argument(
        "--judge-results",
        default=None,
        help="read-only DSH judge results root (default eval/judge_results)",
    )
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("review server only binds loopback")
    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = log_path.open("a", encoding="utf-8", buffering=1)
        sys.stdout = stream
        sys.stderr = stream
    app = create_app(
        store_dir=args.store,
        dataset_dir=args.dataset,
        results_dir=args.results,
        judge_results_dir=args.judge_results,
        default_reviewer=args.reviewer,
        server_state_file=args.state_file,
        host=args.host,
        port=args.port,
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
