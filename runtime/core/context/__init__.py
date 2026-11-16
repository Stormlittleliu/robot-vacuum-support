# -*- coding: utf-8 -*-
"""兼容 shim：感受野实现已迁移到 :mod:`modules.workspace`。

旧 import ``core.context`` 仍可用；新代码请直接 import ``modules.workspace``。
"""
from modules.workspace import *  # noqa: F401,F403
