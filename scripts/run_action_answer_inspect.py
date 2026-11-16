# -*- coding: utf-8 -*-
"""Run the existing Inspect thin adapter once against the new review slice.

The adapter's judge scorer reads only completed Judge files; its structural
scorer remains separate. The judge scorer's answer text is the concatenated
real assistant answers from results/, never the state-solver completion.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUT = ROOT / "outputs" / "action-state-answer-review-v1"
RUN_ID = "asc-live-development-20261114-cdf5aac89d"
JUDGE_RUN_ID = "jr-action-review-20261114-01"
REPORT = ROOT / "outputs" / "action-state-answer-review-inspect.json"
LOG_DIR = OUT / "inspect-logs"


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    from eval.inspect_tasks import run_inspect_state_smoke

    cases = read_jsonl(OUT / "dataset" / "cases.jsonl")
    rows = read_jsonl(OUT / "results" / RUN_ID / "cases.jsonl")
    run_answers = {
        row["case_id"]: "\n\n".join(
            f"T{turn['index']}: {turn['answer']}" for turn in row["turns"]
        )
        for row in rows
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logs = run_inspect_state_smoke(
        cases,
        log_dir=LOG_DIR,
        judge_results_root=OUT / "judge_results",
        judge_run_id=JUDGE_RUN_ID,
        run_id=RUN_ID,
        run_answers=run_answers,
    )
    samples = []
    for log in logs:
        for sample in getattr(log, "samples", None) or []:
            scores = {}
            for name, score in (sample.scores or {}).items():
                if hasattr(score, "model_dump"):
                    payload = score.model_dump()
                elif hasattr(score, "dict"):
                    payload = score.dict()
                else:
                    payload = {"value": getattr(score, "value", None)}
                scores[name] = payload
            samples.append({
                "id": str(sample.id),
                "input": str(sample.input),
                "target": str(sample.target),
                "metadata": sample.metadata,
                "scores": scores,
            })
    report = {
        "schema_version": "action-state-answer-review-inspect/v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runner": "eval.inspect_tasks.run_inspect_state_smoke",
        "framework": "inspect_ai.eval",
        "model_placeholder": "mockllm/model",
        "no_model_call": True,
        "run_id": RUN_ID,
        "judge_run_id": JUDGE_RUN_ID,
        "judge_results_root": str(OUT / "judge_results"),
        "log_dir": str(LOG_DIR),
        "samples": samples,
        "note": "Judge scorer answer text is the real concatenated assistant answers; the structural scorer is separate and its state completion is not used as the judged answer.",
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(REPORT),
        "sample_count": len(samples),
        "scores": {row["id"]: {name: value.get("value") for name, value in row["scores"].items()} for row in samples},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
