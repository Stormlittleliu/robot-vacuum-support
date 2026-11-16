# -*- coding: utf-8 -*-
"""资源目录：工具 / 块 / 工作流 / 场景的统一只读描述。

不构建场景、不调用模型；搜索是词法的。
"""
from modules.resources.catalog import ResourceCatalog, tokenize
from modules.resources.contracts import (
    ResourceDescriptor,
    ResourceKind,
    ResourceNotFoundError,
    ResourceRef,
    ResourceURIError,
    build_uri,
    parse_uri,
)

__all__ = [
    "ResourceCatalog",
    "ResourceDescriptor",
    "ResourceKind",
    "ResourceNotFoundError",
    "ResourceRef",
    "ResourceURIError",
    "build_uri",
    "parse_uri",
    "tokenize",
]
