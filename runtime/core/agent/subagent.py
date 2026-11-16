# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.runtime.subagent`。"""
import sys

from modules.runtime import subagent as _impl

sys.modules[__name__] = _impl
