# -*- coding: utf-8 -*-
"""Small development runner for the recharge state (not a formal benchmark).

Usage (source cwd, no main-repo PYTHONPATH)::

    python eval/run_recharge_dev.py
    python eval/run_recharge_dev.py --out eval/runs/p1-dev.json

It runs:
* the 16 hand-written deterministic fixtures in ``tests/recharge_cases.py``;
* the independent 16-case starter suite in ``eval/cases/v0/recharge-start.json``
  when that file exists, applying only the deterministic state contract to its
  user turns (no model is called and no claim is made about answer quality).

It is intentionally not a pytest replacement and does not implement Inspect AI.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "tests"))

from recharge_cases import CASES  # noqa: E402
from robot_vacuum_support import state as support  # noqa: E402


def play(turns):
    state = support.empty_state()
    direct = []
    scope = None
    number = 0
    for role, text in turns:
        number += 1
        if role == "user":
            result = support.apply_user_turn(state, text, f"m-{number}", now=100 + number)
            state = result["state"]
            scope = result["scope"]
            if result.get("direct_answer"):
                direct.append(result["direct_answer"])
        else:
            state = support.apply_assistant_turn(
                state, text, f"a-{number}", turn=state.get("turn"), now=100 + number
            )
    return state, direct, scope


def run_fixture_suite():
    rows = []
    failures = []
    for case in CASES:
        state, direct, scope = play(case["turns"])
        view = support.public_view(state)
        active = view["active_case"]
        expected = case["expect"]
        errors = []
        if "scope" in expected and scope != expected["scope"]:
            errors.append(f"scope={scope!r} expected={expected['scope']!r}")
        if "has_case" in expected and (active is not None) != expected["has_case"]:
            errors.append("case presence mismatch")
        if active is not None:
            if "tried" in expected:
                got = [(x["action_id"], x["outcome"]) for x in active["tried_actions"]]
                if got != expected["tried"]:
                    errors.append(f"tried={got!r} expected={expected['tried']!r}")
            if "not_tried" in expected:
                got = [x["action_id"] for x in active["not_tried_actions"]]
                if got != expected["not_tried"]:
                    errors.append(f"not_tried={got!r} expected={expected['not_tried']!r}")
            if "next_action" in expected:
                got = (active.get("next_step") or {}).get("action_id")
                if got != expected["next_action"]:
                    errors.append(f"next={got!r} expected={expected['next_action']!r}")
            if "next_kind" in expected:
                got = (active.get("next_step") or {}).get("kind")
                if got != expected["next_kind"]:
                    errors.append(f"next_kind={got!r} expected={expected['next_kind']!r}")
            if "status" in expected and active["status"] != expected["status"]:
                errors.append(f"status={active['status']!r} expected={expected['status']!r}")
            if "model" in expected and active.get("model") != expected["model"]:
                errors.append(f"model={active.get('model')!r} expected={expected['model']!r}")
        if "case_count" in expected and len(view["cases"]) != expected["case_count"]:
            errors.append(f"cases={len(view['cases'])} expected={expected['case_count']}")
        if "direct_contains" in expected:
            if not any(expected["direct_contains"] in item for item in direct):
                errors.append("direct answer mismatch")
        if errors:
            failures.append({"id": case["id"], "errors": errors})
        rows.append(
            {
                "id": case["id"],
                "title": case["title"],
                "ok": not errors,
                "errors": errors,
                "scope": scope,
                "active_case": active,
                "case_count": len(view["cases"]),
                "direct_answers": direct,
            }
        )
    return {"suite": "fixtures-16", "total": len(rows), "passed": len(rows) - len(failures), "failures": failures, "rows": rows}


def _apply_independent_case(case):
    state = support.empty_state()
    directs = []
    scopes = []
    for index, turn in enumerate(case.get("turns") or [], 1):
        text = str(turn.get("user") or "")
        result = support.apply_user_turn(state, text, f"{case['id']}-u{index}", now=100 + index)
        state = result["state"]
        scopes.append(result["scope"])
        if result.get("direct_answer"):
            directs.append(result["direct_answer"])
        # The independent suite has no assistant turns; a deterministic
        # primary suggestion is still needed for later "还是不行" linking.
        if (support.public_view(state).get("active_case") or {}).get("next_step"):
            state = support.apply_assistant_turn(
                state,
                "按状态给出的下一步尝试。",
                f"{case['id']}-a{index}",
                turn=state.get("turn"),
                now=100.5 + index,
            )
    return state, directs, scopes


def run_independent_suite(path: Path):
    if not path.is_file():
        return {"suite": "recharge-start", "available": False, "total": 0, "rows": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    rows = []
    for case in cases:
        state, directs, scopes = _apply_independent_case(case)
        view = support.public_view(state)
        active = view["active_case"]
        tags = set(case.get("tags") or [])
        checks = {}
        if "negation" in tags:
            checks["no_failed_attempt_from_negation"] = not (active and active.get("tried_actions"))
        if "closure" in tags or "resolution" in tags:
            checks["resolved"] = bool(active and active.get("status") == "resolved")
        if "topic-switch" in tags:
            checks["recharge_steps_unchanged"] = bool(active is None or not active.get("tried_actions"))
        if "unknown-model" in tags or "no-basis" in tags:
            checks["no_guessed_model"] = bool(active is None or not active.get("model"))
        if "duplicate" in tags:
            checks["one_case"] = len(view["cases"]) <= 1
        if "session" in tags:
            checks["recall_is_direct"] = any("记录" in item for item in directs)
        rows.append(
            {
                "id": case.get("id"),
                "title": case.get("title"),
                "tags": sorted(tags),
                "checks": checks,
                "ok": all(checks.values()) if checks else None,
                "active_case": active,
                "case_count": len(view["cases"]),
                "scopes": scopes,
                "direct_answers": directs,
            }
        )
    return {
        "suite": payload.get("suite_id") or "recharge-start",
        "available": True,
        "source": str(path.relative_to(ROOT)).replace("\\", "/"),
        "split": payload.get("split"),
        "total": len(rows),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    args = parser.parse_args()
    result = {
        "schema_version": 1,
        "kind": "development-regression",
        "note": "16 starter cases only; not the 40-60 formal eval, not a success-rate claim.",
        "fixtures": run_fixture_suite(),
        "independent_suite": run_independent_suite(ROOT / "eval/cases/v0/recharge-start.json"),
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        target = Path(args.out)
        if not target.is_absolute():
            target = ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {target}")
    else:
        print(text)
    return 0 if not result["fixtures"]["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
