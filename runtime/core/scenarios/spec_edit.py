# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.editor.spec_edit`。"""
import sys

from modules.editor import spec_edit as _impl

sys.modules[__name__] = _impl
