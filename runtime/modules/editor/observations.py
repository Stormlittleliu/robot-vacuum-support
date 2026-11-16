# -*- coding: utf-8 -*-
"""Observer contract v1 的最小实现。

只做数据形状 / 存储 / 读取；不执行、不渲染、不接 UI。
契约见 docs/design/observer-contract.md。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.paths import PROJECT_ROOT

Status = Literal["observed", "declared", "unobserved", "stale", "error"]
SourceType = Literal["node", "run", "resource", "artifact"]


class Source(BaseModel):
    type: SourceType
    ref: str


class ObserverSpec(BaseModel):
    """只存在 visual layer；不进入 WorkflowSpec。"""

    id: str
    kind: str
    title: str = ""
    source: Source
    mode: Literal["latest", "snapshot", "window", "diff"] = "latest"


class Observation(BaseModel):
    schema_version: Literal[1] = 1
    observation_id: str = Field(default_factory=lambda: f"obs_{uuid.uuid4().hex}")
    run_id: str
    scenario: str
    workflow: str
    kind: str
    source: Source
    status: Status
    captured_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


def observations_root(root: str | Path | None = None) -> Path:
    return (Path(root) if root is not None else PROJECT_ROOT / "outputs") / "runs"


def observation_path(run_id: str, observation_id: str, root: str | Path | None = None) -> Path:
    return observations_root(root) / run_id / "observations" / f"{observation_id}.json"


def save_observation(observation: Observation, root: str | Path | None = None) -> Path:
    path = observation_path(observation.run_id, observation.observation_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(observation.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(path)
    return path


def load_observations(
    run_id: str,
    *,
    source: Source | None = None,
    root: str | Path | None = None,
) -> list[Observation]:
    directory = observations_root(root) / run_id / "observations"
    if not directory.is_dir():
        return []
    out: list[Observation] = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(Observation.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    if source is not None:
        out = [
            item
            for item in out
            if item.source.type == source.type and item.source.ref == source.ref
        ]
    return out
