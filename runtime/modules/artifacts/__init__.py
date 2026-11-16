# -*- coding: utf-8 -*-
"""构建产物（artifact）契约：manifest 加载、校验与签名。

本包只处理只读校验与构建逻辑，不启动实例、不调用模型。
"""
from modules.artifacts.manifest import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    ManifestError,
    compute_content_hash,
    hash_tree,
    load_manifest,
    verify_artifact,
)
from modules.artifacts.project import (
    PROJECT_FILE,
    PROJECT_SCHEMA_VERSION,
    ProjectError,
    build_project,
    fork_artifact,
)
from modules.artifacts.service_entry import write_service_entry

__all__ = [
    "LEGACY_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "ManifestError",
    "PROJECT_FILE",
    "PROJECT_SCHEMA_VERSION",
    "ProjectError",
    "build_project",
    "compute_content_hash",
    "fork_artifact",
    "hash_tree",
    "load_manifest",
    "verify_artifact",
    "write_service_entry",
]
