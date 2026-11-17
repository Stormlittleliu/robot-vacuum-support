# -*- coding: utf-8 -*-
"""Read-only trace/worker-log collector for the stopped isolated project.

Reads the named Docker volume without starting the product and without calling
any model/tool.  It only summarizes trace events already written by the product.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "three-mainlines-v1" / "trace-summary.json"
VOLUME = "rvs-eval-three_rvs-data"

INNER = r'''
import glob, json, os
result = {}
for session_dir in sorted(glob.glob('/data/runtime/sessions/*')):
    sid = os.path.basename(session_dir)
    models = []
    runs = []
    tools = []
    for path in glob.glob(session_dir + '/outputs/traces/*.jsonl'):
        with open(path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                name = event.get('event')
                if name == 'model_error':
                    models.append({'error': event.get('error'), 'model': event.get('model'), 'latency_ms': event.get('latency_ms')})
                elif name == 'model_start':
                    models.append({'started': True, 'model': event.get('model')})
                elif name == 'run_end':
                    runs.append({k: event.get(k) for k in ('status', 'error', 'model_calls', 'tool_calls', 'prompt_tokens', 'completion_tokens', 'total_tokens', 'duration_ms')})
                elif name in ('tool_start', 'tool_end'):
                    tools.append({'event': name, 'tool': event.get('tool'), 'error': event.get('error')})
    worker_path = os.path.join(session_dir, 'worker.log')
    worker = ''
    if os.path.isfile(worker_path):
        worker = open(worker_path, encoding='utf-8', errors='replace').read()
    result[sid] = {
        'model_events': models,
        'run_end': runs,
        'tool_events': tools,
        'worker_has_model_path_double_nesting': 'bge-small-zh-v1.5/bge-small-zh-v1.5' in worker,
        'worker_has_pdf_coverage_failure': '修复失败' in worker,
        'worker_has_file_not_found': 'FileNotFoundError' in worker,
    }
print(json.dumps(result, ensure_ascii=False))
'''


def main() -> int:
    completed = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{VOLUME}:/data:ro", "--entrypoint", "python", "rvs-backend:smoke", "-c", INNER],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if completed.returncode != 0:
        print(completed.stderr[-2000:], file=sys.stderr)
        return completed.returncode
    payload = json.loads(completed.stdout)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    errors = sum(1 for item in payload.values() for event in item["model_events"] if event.get("error"))
    print(json.dumps({"sessions": len(payload), "model_errors": errors, "out": str(OUT)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
