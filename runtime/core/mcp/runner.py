# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.mcp.runner`。"""
import sys

from modules.mcp import runner as _impl

sys.modules[__name__] = _impl
