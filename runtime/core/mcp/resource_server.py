# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.mcp.resource_server`；``python -m`` 仍可用。"""
import sys

from modules.mcp import resource_server as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    _impl.mcp.run(transport="stdio")
