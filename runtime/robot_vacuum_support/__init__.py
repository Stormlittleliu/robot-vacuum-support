# -*- coding: utf-8 -*-
"""Project-owned business policy for the robot-vacuum support product.

This package is copied into the artifact together with ``runtime`` but is not
part of the generic harness contracts.  The generic runtime only passes an
opaque ``support_context`` string into the prompt and returns retrieval
evidence; all recharge-failure rules live here.
"""
from .interpretation import (  # noqa: F401
    INTERPRETATION_SCHEMA_VERSION,
    LlmTurnInterpreter,
    fallback_interpretation,
    interpret_turn,
    normalize_interpretation,
)
from .state import (  # noqa: F401
    SCHEMA_VERSION,
    empty_state,
    load_state,
    apply_user_turn,
    apply_assistant_turn,
    public_view,
    prompt_context,
    decision_snapshot,
    classify_slot_answer,
    protocol_ack_answer,
    mark_protocol_ack,
    handoff_summary,
    query_tried_answer,
    is_recharge_related,
)
from .questions import (  # noqa: F401
    QUESTION_SCHEMA_VERSION,
    QUESTION_STATUSES,
    audit_assistant_answer,
    current_question,
    question_summary,
)

__all__ = [
    "SCHEMA_VERSION",
    "INTERPRETATION_SCHEMA_VERSION",
    "QUESTION_SCHEMA_VERSION",
    "QUESTION_STATUSES",
    "LlmTurnInterpreter",
    "fallback_interpretation",
    "interpret_turn",
    "normalize_interpretation",
    "empty_state",
    "load_state",
    "apply_user_turn",
    "apply_assistant_turn",
    "public_view",
    "prompt_context",
    "decision_snapshot",
    "classify_slot_answer",
    "protocol_ack_answer",
    "mark_protocol_ack",
    "audit_assistant_answer",
    "current_question",
    "question_summary",
    "handoff_summary",
    "query_tried_answer",
    "is_recharge_related",
]
