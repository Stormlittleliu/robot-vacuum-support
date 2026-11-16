# -*- coding: utf-8 -*-
"""项目路径的唯一入口：代码、数据、运行时、spec、知识库。

约定：

- ``AH_DATA_ROOT``：所有项目数据的根，默认 ``<repo>/data``；
- ``AH_RUNTIME_ROOT``：可写运行时数据，默认 ``<data>/runtime``；
- ``AH_SPEC_ROOT``：场景 spec 根，默认 ``<data>/specs``；
- ``AH_OUTPUT_ROOT``：可写产物根（logs / traces），默认 ``<repo>/outputs``。

``outputs`` 与源码目录分离：artifact 运行时必须把 ``AH_OUTPUT_ROOT`` 显式指到
instance / session 的 storage 树，不能往只读产物里写 pycache / outputs / logs /
traces / checkpoints。
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


DATA_ROOT = _env_path("AH_DATA_ROOT", PROJECT_ROOT / "data")
RUNTIME_ROOT = _env_path("AH_RUNTIME_ROOT", DATA_ROOT / "runtime")
SPEC_ROOT = _env_path("AH_SPEC_ROOT", DATA_ROOT / "specs")
KNOWLEDGE_ROOT = DATA_ROOT / "knowledge"
MODEL_ROOT = _env_path("AH_MODEL_ROOT", DATA_ROOT / "models")
OUTPUT_ROOT = _env_path("AH_OUTPUT_ROOT", PROJECT_ROOT / "outputs")


def repo_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def output_path(*parts: str) -> Path:
    """可写产物路径：显式 ``AH_OUTPUT_ROOT`` 优先，默认仓库 ``outputs/``。"""
    return OUTPUT_ROOT.joinpath(*parts)


def resolve_data_path(path: str | Path) -> Path:
    """把 ``data/...`` 相对路径解析到 DATA_ROOT；绝对路径原样返回。"""
    value = Path(path)
    if value.is_absolute():
        return value
    normalized = value.as_posix()
    if normalized == "data":
        return DATA_ROOT
    if normalized.startswith("data/runtime/"):
        return RUNTIME_ROOT / normalized[len("data/runtime/"):]
    if normalized == "data/models":
        return MODEL_ROOT
    if normalized.startswith("data/models/"):
        return MODEL_ROOT / normalized[len("data/models/"):]
    if normalized.startswith("data/specs/"):
        return SPEC_ROOT / normalized[len("data/specs/"):]
    if normalized.startswith("data/"):
        return DATA_ROOT / normalized[5:]
    return PROJECT_ROOT / value


def data_path(*parts: str) -> Path:
    return DATA_ROOT.joinpath(*parts)


def runtime_path(*parts: str) -> Path:
    return RUNTIME_ROOT.joinpath(*parts)


def spec_path(name: str = "") -> Path:
    return SPEC_ROOT / name if name else SPEC_ROOT
