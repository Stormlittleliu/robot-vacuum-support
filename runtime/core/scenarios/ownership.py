# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.editor.ownership`。"""
import sys

from modules.editor import ownership as _impl

sys.modules[__name__] = _impl
