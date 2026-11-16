# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.resources.providers`。

模块别名保证旧的内部 monkeypatch（``_workspace_tools`` / 私有常量）仍作用于
同一份实现。
"""
import sys

from modules.resources import providers as _impl

sys.modules[__name__] = _impl
