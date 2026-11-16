# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.resources.catalog`。

用 sys.modules 别名保留私有名/模块级 monkeypatch 的旧语义；不要把实现复制回
core，否则会出现两份状态。
"""
import sys

from modules.resources import catalog as _impl

sys.modules[__name__] = _impl
