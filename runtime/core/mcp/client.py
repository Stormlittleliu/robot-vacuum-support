# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.mcp.client`。

保留 ``core.mcp.client`` 路径，场景注册表与旧调用方无需改 import。
"""
import sys

from modules.mcp import client as _impl

sys.modules[__name__] = _impl
