# -*- coding: utf-8 -*-
"""Regression tests for the local manual-review server/UI data contract.

All tests use a temporary review store.  They must never write review records
into the production ``eval/reviews`` store or modify ``eval/datasets/v1``.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.review_server import create_app  # noqa: E402

BASE_URL = "http://127.0.0.1:8950"
HEADERS = {
    "Origin": BASE_URL,
    "X-Requested-With": "manual-review",
    "Content-Type": "application/json",
}
DEV_CASE = "rsv1-dev-boundary-unknown-model-price-01"
NO_ANSWER_CASE = "rsv1-dev-direct-suction-decline-02"
CURRENT_LIVE_RUN = "v1-live-regrade-development-20261113-033753-69b438"
OLD_LIVE_RUN = "v1-live-development-20261113-032228-2f644a"
STATE_RUN = "v1-state-development-20261113-033740-df0c5b"
# Keep these tests deterministic against a small temp run root.  Production
# eval/results may gain newer runs at any time; tests must not depend on which
# run happens to be preferred today, and must never write production runs.
RUN_FIXTURES = (CURRENT_LIVE_RUN, OLD_LIVE_RUN, STATE_RUN)
DATASET_FILES = ("suite.json", "cases.jsonl", "split.json", "audit.json", "evidence_index.json")


def file_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256((ROOT / "eval" / "datasets" / "v1" / name).read_bytes()).hexdigest()
        for name in DATASET_FILES
    }


@pytest.fixture
def client(tmp_path):
    store = tmp_path / "reviews"
    results = tmp_path / "results"
    results.mkdir()
    for run_id in RUN_FIXTURES:
        source = ROOT / "eval" / "results" / run_id
        assert source.is_dir(), f"missing historical run fixture: {run_id}"
        shutil.copytree(source, results / run_id)
    app = create_app(store_dir=store, results_dir=results)
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client, store


def get_case_hash(client: TestClient, case_id: str = DEV_CASE) -> str:
    response = client.get(f"/api/cases/{case_id}")
    assert response.status_code == 200, response.text
    return response.json()["caseHash"]


def save(client: TestClient, **overrides):
    payload = {
        "caseId": DEV_CASE,
        "caseHash": get_case_hash(client, overrides.get("caseId", DEV_CASE)),
        "suiteRevision": "v1.1",
        "reviewScope": "case_definition",
        "runId": None,
        "status": "passed",
        "notes": "",
        "expectedRevision": 0,
    }
    payload.update(overrides)
    return client.post("/api/reviews", json=payload, headers=HEADERS)


def test_health_and_start_are_unreviewed(client):
    c, store = client
    health = c.get("/api/health").json()
    assert health["ok"] and health["reviewCount"] == 0 and health["exposureCount"] == 0
    assert health["canRunHoldout"] is False
    rows = c.get("/api/cases?split=development&scope=case_definition").json()["rows"]
    assert len(rows) == 32
    assert all(row["review"]["case_definition"]["status"] == "unreviewed" for row in rows)
    assert all(row["review"]["run_answer"]["status"] == "unreviewed" for row in rows)
    assert not list((store / "reviews").glob("*.json"))


def test_save_reload_three_statuses_notes_and_revisions(client):
    c, _ = client
    notes = '第一行：通过但有保留\n第二行含 "引号", 逗号, 分号; 以及\n换行。'
    revisions = []
    for index, status in enumerate(("passed", "failed", "uncertain")):
        response = save(c, status=status, notes=notes, expectedRevision=index)
        assert response.status_code == 200, response.text
        record = response.json()["record"]
        revisions.append(record["revision"])
        assert record["reviewer"] == "local-user"
        assert record["reviewScope"] == "case_definition"
        assert record["runId"] is None
        assert record["notes"] == notes
        loaded = c.get(
            "/api/reviews",
            params={"case_id": DEV_CASE, "scope": "case_definition"},
        ).json()
        assert loaded["current"]["status"] == status
        assert loaded["current"]["notes"] == notes
        assert loaded["current"]["revision"] == index + 1
        assert loaded["current"]["caseHash"] == get_case_hash(c)
        assert loaded["current"]["suiteRevision"] == "v1.1"
        assert loaded["current"]["updatedAt"]
    assert revisions == [1, 2, 3]
    health = c.get("/api/health").json()
    assert health["reviewCount"] == 1


def test_case_and_run_reviews_are_separate(client):
    c, _ = client
    case_response = save(c, status="passed", notes="案例定义通过", expectedRevision=0)
    assert case_response.status_code == 200
    run_response = save(
        c,
        status="uncertain",
        notes="回答存疑",
        reviewScope="run_answer",
        runId=CURRENT_LIVE_RUN,
        expectedRevision=0,
    )
    assert run_response.status_code == 200, run_response.text
    case_record = case_response.json()["record"]
    run_record = run_response.json()["record"]
    assert case_record["reviewId"] != run_record["reviewId"]
    assert run_record["reviewScope"] == "run_answer"
    assert run_record["runId"] == CURRENT_LIVE_RUN
    assert run_record["runSnapshot"]["casesSha256"]

    definition = c.get("/api/reviews", params={"case_id": DEV_CASE, "scope": "case_definition"}).json()
    run = c.get(
        "/api/reviews",
        params={"case_id": DEV_CASE, "scope": "run_answer", "runId": CURRENT_LIVE_RUN},
    ).json()
    assert definition["current"]["status"] == "passed"
    assert run["current"]["status"] == "uncertain"
    list_rows = c.get("/api/cases?split=development&scope=run_answer").json()["rows"]
    row = next(item for item in list_rows if item["caseId"] == DEV_CASE)
    assert row["review"]["case_definition"]["status"] == "passed"
    assert row["review"]["run_answer"]["status"] == "uncertain"


def test_concurrency_conflict_returns_409_and_keeps_current(client):
    c, _ = client
    first = save(c, status="passed", notes="first", expectedRevision=0)
    assert first.status_code == 200
    stale = save(c, status="failed", notes="stale local edit", expectedRevision=0)
    assert stale.status_code == 409
    body = stale.json()
    assert body["error"] == "revision_conflict"
    assert body["currentRevision"] == 1
    assert body["current"]["status"] == "passed"
    current = c.get("/api/reviews", params={"case_id": DEV_CASE, "scope": "case_definition"}).json()["current"]
    assert current["revision"] == 1
    assert current["notes"] == "first"
    second = save(c, status="uncertain", notes="merged", expectedRevision=1)
    assert second.status_code == 200
    assert second.json()["record"]["revision"] == 2


def test_holdout_hidden_exposure_and_export(client):
    c, _ = client
    holdout = c.get("/api/cases?split=holdout")
    assert holdout.status_code == 403
    assert holdout.json()["detail"]["error"] == "holdout_hidden"
    case_id = "rsv1-hold-direct-charge-heat-01"
    holdout_case = c.get(f"/api/cases/{case_id}")
    assert holdout_case.status_code == 403
    blocked_save = c.post(
        "/api/reviews",
        json={
            "caseId": case_id,
            "caseHash": "0" * 64,
            "suiteRevision": "v1.1",
            "reviewScope": "case_definition",
            "runId": None,
            "status": "passed",
            "notes": "",
            "expectedRevision": 0,
        },
        headers=HEADERS,
    )
    assert blocked_save.status_code == 403

    first = c.post("/api/holdout/expose", json={"reviewer": "tester"}, headers=HEADERS)
    assert first.status_code == 200 and first.json()["first"] is True
    second = c.post("/api/holdout/expose", json={"reviewer": "tester"}, headers=HEADERS)
    assert second.status_code == 200 and second.json()["first"] is False
    exposed = c.get("/api/cases?split=holdout&include_holdout=1")
    assert exposed.status_code == 200
    assert len(exposed.json()["rows"]) == 16

    detail = c.get(f"/api/cases/{case_id}?include_holdout=1").json()
    assert detail["runs"] == []
    assert detail["noRunMessage"] == "暂无运行结果"
    run = c.get(f"/api/cases/{case_id}/run?runId={CURRENT_LIVE_RUN}&include_holdout=1")
    assert run.status_code == 200
    assert run.json()["available"] is False
    assert run.json()["message"] == "暂无运行结果"

    export = c.get("/api/export?format=json").json()
    assert export["store"]["exposureCount"] == 1
    assert len(export["exposures"]) == 1
    assert export["exposures"][0]["note"]
    csv_text = c.get("/api/export?format=csv").content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    exposure_rows = [row for row in rows if row["record_type"] == "holdout_exposure"]
    assert len(exposure_rows) == 1
    assert exposure_rows[0]["exposureAt"]
    assert exposure_rows[0]["exposureCount"] == "16"


def test_export_csv_bom_quotes_and_newlines(client):
    c, _ = client
    notes = 'multiline "quoted", comma\nsecond line\r\nthird line'
    response = save(c, status="uncertain", notes=notes, expectedRevision=0)
    assert response.status_code == 200
    raw = c.get("/api/export?format=csv").content
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    review_rows = [row for row in rows if row["record_type"] == "review"]
    assert len(review_rows) == 1
    row = review_rows[0]
    assert row["notes"] == notes
    assert row["status"] == "uncertain"
    assert row["statusLabel"] == "存疑"
    assert row["caseHash"] == get_case_hash(c)
    assert row["suiteRevision"] == "v1.1"
    assert row["reviewScope"] == "case_definition"
    assert row["revision"] == "1"
    assert row["updatedAt"]
    assert row["reviewer"] == "local-user"
    payload = c.get("/api/export?format=json").json()
    assert payload["reviews"][0]["notes"] == notes
    assert payload["dataset"]["casesSha256"] == hashlib.sha256(
        (ROOT / "eval" / "datasets" / "v1" / "cases.jsonl").read_bytes()
    ).hexdigest()


def test_runs_are_development_only_historical_marked_and_no_fake_actual(client):
    c, _ = client
    current = c.get(f"/api/cases/{DEV_CASE}/run?runId={CURRENT_LIVE_RUN}").json()
    assert current["available"] is True
    assert current["run"]["historical"] is False
    assert current["hasAnyAnswer"] is True
    assert "不作为自动得分" in current["note"]
    assert current["turns"][0]["answer"]

    old = c.get(f"/api/cases/{DEV_CASE}/run?runId={OLD_LIVE_RUN}").json()
    assert old["available"] is True
    assert old["run"]["historical"] is True

    state = c.get(f"/api/cases/{NO_ANSWER_CASE}/run?runId={STATE_RUN}").json()
    assert state["available"] is True
    assert state["hasAnyAnswer"] is False
    assert "暂无运行结果" in state["message"] or "没有回答文本" in state["message"]
    assert all(turn["answer"] is None for turn in state["turns"])
    state_detail = c.get(f"/api/cases/{NO_ANSWER_CASE}").json()
    assert state_detail["runs"] == []
    assert state_detail["noRunMessage"] == "暂无运行结果"
    cannot_review_state = save(
        c,
        caseId=NO_ANSWER_CASE,
        status="failed",
        reviewScope="run_answer",
        runId=STATE_RUN,
        expectedRevision=0,
    )
    assert cannot_review_state.status_code == 409
    assert cannot_review_state.json()["error"] == "no_run_answer"

    detail = c.get(f"/api/cases/{DEV_CASE}").json()
    assert detail["runs"]
    development_ids = {
        row["caseId"] for row in c.get("/api/cases?split=development&scope=case_definition").json()["rows"]
    }
    assert all(row_id.startswith("rsv1-dev-") for row_id in development_ids)


def test_dataset_hash_binding_keeps_old_review_as_history(tmp_path):
    store = tmp_path / "reviews"
    dataset = tmp_path / "v1"
    dataset.mkdir()
    for name in DATASET_FILES:
        shutil.copy2(ROOT / "eval" / "datasets" / "v1" / name, dataset / name)

    app1 = create_app(dataset_dir=dataset, store_dir=store, results_dir=ROOT / "eval" / "results")
    with TestClient(app1, base_url=BASE_URL) as c1:
        old_hash = get_case_hash(c1)
        response = save(c1, status="passed", notes="old review", expectedRevision=0)
        assert response.status_code == 200

    cases_path = dataset / "cases.jsonl"
    lines = cases_path.read_text(encoding="utf-8").splitlines()
    changed = False
    for index, line in enumerate(lines):
        case = json.loads(line)
        if case["id"] == DEV_CASE:
            case["title"] = case.get("title", "") + "（已改题）"
            lines[index] = json.dumps(case, ensure_ascii=False)
            changed = True
            break
    assert changed
    cases_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    app2 = create_app(dataset_dir=dataset, store_dir=store, results_dir=ROOT / "eval" / "results")
    with TestClient(app2, base_url=BASE_URL) as c2:
        new_hash = get_case_hash(c2)
        assert new_hash != old_hash
        loaded = c2.get("/api/reviews", params={"case_id": DEV_CASE, "scope": "case_definition"}).json()
        assert loaded["current"] is None
        assert len(loaded["historical"]) == 1
        assert loaded["historical"][0]["caseHash"] == old_hash
        stale = c2.post(
            "/api/reviews",
            json={
                "caseId": DEV_CASE,
                "caseHash": old_hash,
                "suiteRevision": "v1.1",
                "reviewScope": "case_definition",
                "runId": None,
                "status": "failed",
                "notes": "should not overwrite",
                "expectedRevision": 1,
            },
            headers=HEADERS,
        )
        assert stale.status_code == 409
        assert stale.json()["error"] == "dataset_changed"


def test_same_origin_and_client_header_are_required(client):
    c, _ = client
    payload = {
        "caseId": DEV_CASE,
        "caseHash": get_case_hash(c),
        "suiteRevision": "v1.1",
        "reviewScope": "case_definition",
        "runId": None,
        "status": "passed",
        "notes": "",
        "expectedRevision": 0,
    }
    no_header = c.post("/api/reviews", json=payload, headers={"Content-Type": "application/json"})
    assert no_header.status_code == 403
    evil = c.post(
        "/api/reviews",
        json=payload,
        headers={
            "Origin": "http://evil.example",
            "X-Requested-With": "manual-review",
            "Content-Type": "application/json",
        },
    )
    assert evil.status_code == 403
    assert c.get("/static/../README.md").status_code == 404
    assert c.get("/static/not-a-file").status_code == 404


def test_dataset_is_never_modified_by_review_flow(client):
    c, _ = client
    before = file_hashes()
    response = save(c, status="passed", notes="no dataset write", expectedRevision=0)
    assert response.status_code == 200
    c.get("/api/export?format=csv")
    c.get("/api/export?format=json")
    after = file_hashes()
    assert before == after


def test_unknown_case_and_bad_parameters(client):
    c, _ = client
    assert c.get("/api/cases/not-real").status_code == 404
    assert c.get("/api/cases?split=bad").status_code == 400
    assert c.get("/api/cases?scope=bad").status_code == 400
    assert c.get("/api/export?format=xml").status_code == 400

