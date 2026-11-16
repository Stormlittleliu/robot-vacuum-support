# -*- coding: utf-8 -*-
"""``python -m modules.artifacts``：artifact 校验 / fork 源工程 / headless build。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from modules.artifacts.project import ProjectError, build_project, fork_artifact


def _print_payload(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            print(f"{key}: {value}")


def _fork(args: argparse.Namespace) -> int:
    result = fork_artifact(args.artifact_root, args.project_dir)
    if args.json:
        _print_payload(result, True)
    else:
        print(f"Forked artifact {result['source_artifact_id']} -> {result['project']}")
        print("This is an editable source project. Rebuild with:")
        print(
            "  python -m modules.artifacts build-project "
            f'"{result["project"]}" --out <output_dir>'
        )
    return 0


def _build_project(args: argparse.Namespace) -> int:
    result = build_project(
        args.project_dir, args.out, include_assets=args.include_assets
    )
    if args.json:
        _print_payload(result, True)
    else:
        print(f"Built artifact {result['manifest']['artifact_id']} at {result['bundle']}")
        print(f"Zip: {result['zip']}")
        print(
            "Static verification: "
            f"{'ok' if result['verification'].get('ok') else 'failed'} "
            "(checksums/manifest only)."
        )
        print(
            "This is a build artifact. Runtime readiness requires instance validation: "
            "serve.py --instance-config <instances/id/instance.json>."
        )
        print(
            "这是构建产物；运行就绪仍需实例验证（serve.py --instance-config ...），"
            "静态校验只代表字节完整。"
        )
        needs = result.get("needs_configuration") or []
        if needs:
            print("needs_configuration:")
            for item in needs:
                print(f"  - {item}")
    return 0


def _build(args: argparse.Namespace) -> int:
    # headless 入口：调用现有 builder.build_artifact，不改函数本身。
    from modules.artifacts.builder import build_artifact

    result = build_artifact(args.scenario, root=args.out)
    if args.json:
        _print_payload(
            {
                "bundle": result["bundle"],
                "zip": result["zip"],
                "manifest": result["manifest"],
                "check_scope": "checksums_only",
            },
            True,
        )
    else:
        print(f"Built artifact {result['manifest']['artifact_id']} at {result['bundle']}")
        print(f"Zip: {result['zip']}")
        print(
            "Static verification is checksums/manifest only; runtime readiness "
            "requires instance validation (serve.py --instance-config ...)."
        )
        print(
            "这是构建产物；静态校验只代表字节完整，运行就绪仍需实例验证。"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m modules.artifacts",
        description=(
            "Artifact utilities: fork a complete v2 artifact into an editable source "
            "project, rebuild from that project, or run the existing headless builder."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fork = subparsers.add_parser(
        "fork", help="copy a complete v2 artifact into a new editable source project"
    )
    fork.add_argument("artifact_root")
    fork.add_argument("project_dir")
    fork.add_argument("--json", action="store_true")
    fork.set_defaults(handler=_fork)

    build_source = subparsers.add_parser(
        "build-project",
        help="rebuild a new v2 artifact from a source project's own files",
    )
    build_source.add_argument("project_dir")
    build_source.add_argument("--out", required=True)
    build_source.add_argument(
        "--include-assets",
        action="store_true",
        help="copy only explicitly declared read-only project assets",
    )
    build_source.add_argument("--json", action="store_true")
    build_source.set_defaults(handler=_build_project)

    build = subparsers.add_parser(
        "build",
        help="headless: call the existing builder.build_artifact for a host scenario",
    )
    build.add_argument("scenario")
    build.add_argument("--out", required=True)
    build.add_argument("--json", action="store_true")
    build.set_defaults(handler=_build)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ProjectError, FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
