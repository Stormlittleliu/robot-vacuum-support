# -*- coding: utf-8 -*-
"""Read-only trace/worker-log collector for the stopped isolated project.

Reads a named Docker volume without starting the product and without calling
any model/tool.  It only summarizes events already written by the product.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "outputs" / "three-mainlines-v1" / "trace-summary.json"
DEFAULT_VOLUME = "rvs-eval-three_rvs-data"

INNER = r'''
import glob, json, os
result = {}
for session_dir in sorted(glob.glob('/data/runtime/sessions/*')):
    sid = os.path.basename(session_dir)
    models = []
    runs = []
    tools = []
    node_errors = []
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
                elif name == 'node_error':
                    node_errors.append({'node': event.get('node'), 'code': event.get('code'), 'error': event.get('error')})
    worker_path = os.path.join(session_dir, 'worker.log')
    worker = ''
    if os.path.isfile(worker_path):
        worker = open(worker_path, encoding='utf-8', errors='replace').read()
    meta = {}
    meta_path = os.path.join(session_dir, 'session.json')
    if os.path.isfile(meta_path):
        try:
            meta = json.load(open(meta_path, encoding='utf-8'))
        except ValueError:
            meta = {}
    errors = []
    transcript_path = os.path.join(session_dir, 'transcript.jsonl')
    if os.path.isfile(transcript_path):
        for line in open(transcript_path, encoding='utf-8', errors='replace'):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get('role')) == 'error':
                errors.append(str(row.get('content') or '')[:300])
    support_summary = {}
    support_path = os.path.join(session_dir, 'support_state.json')
    if os.path.isfile(support_path):
        try:
            state = json.load(open(support_path, encoding='utf-8'))
            active = state.get('active_case') or {}
            support_summary = {
                'active_case_id': state.get('active_case_id'),
                'case_status': active.get('status'),
                'resolved_at': active.get('resolved_at'),
                'last_interpretation_method': ((state.get('last_interpretation') or {}).get('trace') or {}).get('method'),
                'last_interpretation_fallback': ((state.get('last_interpretation') or {}).get('trace') or {}).get('fallback_reason'),
                'tried_actions': [i.get('action_id') for i in active.get('tried_actions') or [] if isinstance(i, dict)],
                'not_tried_actions': [i.get('action_id') for i in active.get('not_tried_actions') or [] if isinstance(i, dict)],
                'fact_audit_kinds': [(i.get('kind'), i.get('action_id')) for i in active.get('fact_audit') or [] if isinstance(i, dict)],
            }
        except ValueError:
            support_summary = {}
    spans = []
    for path in glob.glob(session_dir + '/outputs/latency/spans_*.jsonl'):
        for line in open(path, encoding='utf-8', errors='replace'):
            try:
                spans.append(json.loads(line))
            except ValueError:
                continue
    embedding_spans = [r for r in spans if r.get('stage') == 'embedding.model_load']
    retrieval_spans = [r for r in spans if str(r.get('stage') or '').startswith('retrieval.')]
    result[sid] = {
        'model_events': models,
        'run_end': runs,
        'tool_events': tools,
        'node_errors': node_errors,
        'session_last_status': meta.get('last_status'),
        'session_status': meta.get('status'),
        'error_messages': errors,
        'support': support_summary,
        'embedding_load': [
            {'status': r.get('status'), 'duration_ms': r.get('duration_ms'), 'attrs': r.get('attrs')}
            for r in embedding_spans
        ],
        'retrieval_spans': [
            {'stage': r.get('stage'), 'status': r.get('status'), 'duration_ms': r.get('duration_ms')}
            for r in retrieval_spans
        ],
        'worker_has_model_path_double_nesting': 'bge-small-zh-v1.5/bge-small-zh-v1.5' in worker,
        'worker_has_pdf_coverage_failure': '修复失败' in worker,
        'worker_has_file_not_found': 'FileNotFoundError' in worker,
    }
print(json.dumps(result, ensure_ascii=False))
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--volume", default=DEFAULT_VOLUME)
    parser.add_argument("--image", default="rvs-backend:smoke")
    parser.add_argument("--session-ids-file", default="", help="Optional report.json; keep only its case session ids.")
    args = parser.parse_args(argv)
    out = Path(args.out_dir).resolve()
    if out.suffix.lower() != ".json":
        out = out / "trace-summary.json"
    completed = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{args.volume}:/data:ro", "--entrypoint", "python", args.image, "-c", INNER],
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
    if args.session_ids_file:
        report = json.loads(Path(args.session_ids_file).read_text(encoding="utf-8"))
        wanted = {
            str(case.get("session_id"))
            for case in report.get("cases") or []
            if case.get("session_id")
        }
        payload = {key: value for key, value in payload.items() if key in wanted}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    errors = sum(1 for item in payload.values() for event in item["model_events"] if event.get("error"))
    print(json.dumps({"sessions": len(payload), "model_errors": errors, "out": str(out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
