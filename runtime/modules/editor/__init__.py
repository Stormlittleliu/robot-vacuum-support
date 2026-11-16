# -*- coding: utf-8 -*-
"""编辑器：spec 语义操作、CAS、归属、发布。

不依赖 Streamlit；写入仍只有 ``save_spec`` 这一条出口。
"""
from modules.editor.entry import ChatEntry, load_entry, publish, unpublish
from modules.editor.ownership import (
    OWNER_CODE,
    OWNER_SPEC,
    OwnershipError,
    ensure_spec_editable,
    load_state,
    status,
)
from modules.editor.spec_edit import (
    apply_ops,
    diff_spec,
    draft_from_spec,
    load_draft,
    load_draft_versioned,
    merge_canvas,
    save_draft,
)
from modules.scenario_format.spec import SpecConflict, save_spec

__all__ = [
    "ChatEntry",
    "OWNER_CODE",
    "OWNER_SPEC",
    "OwnershipError",
    "SpecConflict",
    "apply_ops",
    "diff_spec",
    "draft_from_spec",
    "ensure_spec_editable",
    "load_draft",
    "load_draft_versioned",
    "load_entry",
    "load_state",
    "merge_canvas",
    "publish",
    "save_draft",
    "save_spec",
    "status",
    "unpublish",
]
