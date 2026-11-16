# -*- coding: utf-8 -*-
"""Tiny helpers for hand-authored family/variant seeds.

The generator does not invent semantics.  Claims/behaviours below were written
by the executor from the evidence premises; the script only expands them into
the shared JSON contract and validates it.
"""
from __future__ import annotations


def T(
    role: str,
    content: str,
    claims=None,
    forbidden=None,
    state=None,
    behaviors=None,
    sources=None,
    session=None,
) -> dict:
    if role == "assistant_fixture":
        expected = {
            "required_claims": [],
            "forbidden_claims": [],
            "state": {},
            "required_behaviors": ["fixture_context_only"],
            "expected_sources": [],
        }
    else:
        expected = {
            "required_claims": list(claims or []),
            "forbidden_claims": list(forbidden or []),
            "state": dict(state or {}),
            "required_behaviors": list(behaviors or []),
            "expected_sources": list(sources or []),
        }
    turn = {"role": role, "content": content, "expected": expected}
    if session:
        turn["session"] = session
    return turn


def V(cid: str, title: str, turns, seed_ids=None, setup=None, evidence_ids=None) -> dict:
    return {
        "id": cid,
        "title": title,
        "seed_ids": list(seed_ids or []),
        "setup": dict(setup or {}),
        "turns": turns,
        "evidence_ids": list(evidence_ids or []),
    }


def F(
    family_id: str,
    category: str,
    split: str,
    title: str,
    oracle: str,
    evidence_ids,
    variants,
    human_focus=None,
) -> dict:
    return {
        "family_id": family_id,
        "category": category,
        "split": split,
        "title": title,
        "oracle": oracle,
        "variant_budget": 2,
        "evidence_ids": list(evidence_ids),
        "variants": list(variants),
        "human_focus": list(human_focus or []),
    }
