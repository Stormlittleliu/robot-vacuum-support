# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.evaluation.tasksets`。"""
import sys

from modules.evaluation import tasksets as _impl

sys.modules[__name__] = _impl
