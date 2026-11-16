# -*- coding: utf-8 -*-
"""Check the two headless Chrome DOM dumps from the isolated 8955 UI.

The dumps were produced by Chrome --headless=new after the read-only API
acceptance. This script only parses local HTML; no browser or model is started.
"""
from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "action-state-answer-review-v1"
REPORT = ROOT / "outputs" / "action-state-answer-review-headless.json"


def text_of(path: Path) -> str:
    raw = path.read_text(encoding="utf-8-sig")
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))


def main() -> None:
    contact = text_of(OUT / "gui-headless-contact.html")
    dock = text_of(OUT / "gui-headless-dock.html")
    checks = {
        "contact_actual_answer_visible": "你刚才已经擦过充电触点" in contact,
        "contact_judge_run_visible": "jr-action-review-20261114-01" in contact,
        "contact_casehash_visible": "54dbaec5c9d9156a0aad542f00f2cb8189e67a0ff70fc8bb1e48a718dba12ea1" in contact,
        "contact_coverage_complete": "coverage present=是 · complete=是" in contact and '"expected": 11' in contact and '"covered": 11' in contact,
        "contact_machine_passed": "Case 总体判定（程序聚合） 通过" in contact,
        "contact_actual_not_expected": "运行回答（actual，不是 expected）" in contact,
        "dock_actual_answer_visible": "你已做过「更换新的充电座」" in dock,
        "dock_judge_run_visible": "jr-action-review-20261114-01" in dock,
        "dock_casehash_visible": "452ded9386d0f20673a799eb9ab9b509c8a088f63cb34feea405ec0353e30a07" in dock,
        "dock_coverage_complete": "coverage present=是 · complete=是" in dock and '"expected": 11' in dock and '"covered": 11' in dock,
        "dock_machine_uncertain": "Case 总体判定（程序聚合） 存疑" in dock,
        "dock_unresolved_visible": "case-evidence-support" in dock and "unresolved" in dock,
        "dock_actual_not_expected": "运行回答（actual，不是 expected）" in dock,
        "screenshots_exist": (OUT / "gui-headless-contact.png").exists() and (OUT / "gui-headless-dock.png").exists(),
    }
    report = {
        "schema_version": "action-state-answer-review-headless/v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "method": "Chrome --headless=new --dump-dom + --screenshot on the hidden 8955 server",
        "base_url": "http://127.0.0.1:8955",
        "dom_files": ["gui-headless-contact.html", "gui-headless-dock.html"],
        "screenshots": ["gui-headless-contact.png", "gui-headless-dock.png"],
        "checks": checks,
        "holdout_ui": {
            "isolated_holdout_count": 0,
            "dynamic_check": "outputs/action-state-answer-review-holdout-ui.json",
            "note": "The UI now hides the holdout button/gate/reveal at 0 and falls back #split=holdout to development; 16 keeps the original exposure gate.",
        },
        "note": "Headless DOM is read-only acceptance evidence; machine Judge output is not human review.",
    }
    assert all(checks.values()), checks
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(REPORT), "checks": checks}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
