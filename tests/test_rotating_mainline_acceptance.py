# -*- coding: utf-8 -*-
"""Targeted regression tests for the rotating-mainline acceptance set.

Only the checks needed to catch ratio, source, duplicate and oracle regressions
are covered.  This is not a re-run of the whole eval suite.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_rotating_mainline as rm  # noqa: E402


def _load():
    return rm.load_suite(rm.SUITE_DIR)


def _copy_suite(tmp_path: Path):
    target = tmp_path / "rotating-mainline-v1"
    shutil.copytree(rm.SUITE_DIR, target)
    return target


def test_ratio_is_six_recharge_and_six_breadth():
    _, cases_doc = _load()
    cases = cases_doc["cases"]
    assert len(cases) == 12
    assert sum(1 for c in cases if c["group"] == "recharge_mainline") == 6
    assert sum(1 for c in cases if c["group"] == "breadth_primary_document") == 6
    assert {c["theme"] for c in cases if c["group"] == "recharge_mainline"} == {
        "multiround_correction",
        "local_vs_global_success",
        "failed_outcome",
        "hypothetical_third_party",
        "explicit_recovery",
        "unresolved_conversation",
    }


def test_all_six_documents_have_exact_quote_and_hash():
    suite, cases_doc = _load()
    texts, _ = rm.document_texts(suite)
    breadth = [c for c in cases_doc["cases"] if c["group"] == "breadth_primary_document"]
    assert {c["primary_document"] for c in breadth} == set(suite["required_breadth_documents"])
    for case in breadth:
        evidence = case["evidence"][0]
        path = ROOT / evidence["file"]
        assert path.is_file()
        assert rm._sha256_file(path) == evidence["sha256"]
        assert rm.norm_quote(evidence["quote"]) in rm.norm_quote(texts[evidence["file"]])


def test_pdf_quote_exists_in_both_extraction_views():
    suite, _ = _load()
    pdf = "assets/knowledge/扫地机器人100问.pdf"
    quote = None
    for case in _load()[1]["cases"]:
        if case["primary_document"] == pdf:
            quote = case["evidence"][0]["quote"]
            break
    assert quote
    views = rm._extract_pdf_views(ROOT / pdf)
    assert rm.norm_quote(quote) in rm.norm_quote(views["pypdf"])
    assert rm.norm_quote(quote) in rm.norm_quote(views["pdftotext"])


def test_duplicate_user_turn_is_rejected(tmp_path):
    target = _copy_suite(tmp_path)
    cases_path = target / "cases.json"
    cases_doc = json.loads(cases_path.read_text(encoding="utf-8"))
    cases_doc["cases"][1]["turns"][0]["content"] = cases_doc["cases"][0]["turns"][0]["content"]
    cases_path.write_text(json.dumps(cases_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = rm.validate_suite(target)
    assert result["ok"] is False
    assert "cases.user_turns.unique" in result["errors"]


def test_manual_only_auto_fail_oracle_is_rejected(tmp_path):
    target = _copy_suite(tmp_path)
    cases_path = target / "cases.json"
    cases_doc = json.loads(cases_path.read_text(encoding="utf-8"))
    for case in cases_doc["cases"]:
        if case["id"] == "rm-recharge-unresolved-conversation":
            case["turns"][0]["expect"]["state"]["observation_contains"].append(
                {"capability": "automatic_return", "result": "fails"}
            )
    cases_path.write_text(json.dumps(cases_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = rm.validate_suite(target)
    assert result["ok"] is False
    assert any("oracle.auto_fail_explicit" in error for error in result["errors"])


def test_missing_breadth_document_breaks_coverage(tmp_path):
    target = _copy_suite(tmp_path)
    cases_path = target / "cases.json"
    cases_doc = json.loads(cases_path.read_text(encoding="utf-8"))
    removed = next(c for c in cases_doc["cases"] if c["group"] == "breadth_primary_document")
    cases_doc["cases"] = [c for c in cases_doc["cases"] if c["id"] != removed["id"]]
    cases_path.write_text(json.dumps(cases_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = rm.validate_suite(target)
    assert result["ok"] is False
    assert "proportion.6_12" in result["errors"]
    assert "breadth.documents.exactly_once" in result["errors"]


def test_frozen_v1_hashes_are_preserved():
    suite, _ = _load()
    status = rm.frozen_v1_status(suite)
    assert status["ok"] is True
    for key, value in status["checks"].items():
        assert value["ok"] is True, key


def test_attribution_fields_are_separate():
    _, cases_doc = _load()
    for case in cases_doc["cases"]:
        assert isinstance(case["source"], dict) and case["source"]
        assert isinstance(case["origin"], dict) and case["origin"]
        assert isinstance(case["generation"], dict) and case["generation"]
        assert isinstance(case["scoring_method"], dict) and case["scoring_method"]
        assert case["source"] != case["generation"]
        assert case["origin"] != case["scoring_method"]
