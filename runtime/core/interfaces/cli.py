# -*- coding: utf-8 -*-
"""统一入口：workflow / trace / eval。

旧 BFCL、RAG、benchmark、agent_eval 命令已随旧评测栈移除；新评测走
``python -m modules.evaluation.run``。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _usage() -> None:
    print(
        "用法：\n"
        "  python test_entry.py eval [run 参数...]\n"
        "  python test_entry.py workflow --list\n"
        "  python test_entry.py workflow --run <name> --input \"...\"\n"
        "  python test_entry.py trace --last 10\n"
    )


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        _usage()
        return

    if argv[0] == "workflow":
        from core.interfaces.workflow_cli import main as workflow_main

        workflow_main(argv[1:])
        return

    if argv[0] == "trace":
        from core.observability.trace_report import main as trace_main

        trace_main(argv[1:])
        return

    if argv[0] == "eval":
        from modules.evaluation.run import main as eval_main

        eval_main(argv[1:])
        return

    _usage()


if __name__ == "__main__":
    main()
