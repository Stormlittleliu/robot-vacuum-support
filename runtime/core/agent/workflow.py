# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.runtime.workflow`。"""
import sys

from modules.runtime import workflow as _impl

sys.modules[__name__] = _impl
