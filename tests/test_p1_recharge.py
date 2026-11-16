# -*- coding: utf-8 -*-
"""P1-03/P1-04 development tests (run only this file, not the whole repo)."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain_core.documents import Document  # noqa: E402

from core.tools.rag import evidence as rag_evidence  # noqa: E402
from core.tools.rag.service import RagSummarizeService  # noqa: E402
from core.tools.rag.tools import build_rag_tools  # noqa: E402
from robot_vacuum_support import state as support_state  # noqa: E402
from robot_vacuum_support.evidence import review  # noqa: E402
from recharge_cases import CASES  # noqa: E402


def play(turns):
    state = support_state.empty_state()
    direct = None
    scope = None
    message_no = 0
    for role, text in turns:
        if role == "user":
            message_no += 1
            result = support_state.apply_user_turn(
                state, text, f"m-{message_no}", now=100 + message_no
            )
            state = result["state"]
            scope = result["scope"]
            direct = result.get("direct_answer")
        else:
            message_no += 1
            state = support_state.apply_assistant_turn(
                state, text, f"a-{message_no}", turn=state.get("turn"), now=100 + message_no
            )
    return state, direct, scope


class StateContractTests(unittest.TestCase):
    def test_16_starter_cases(self):
        self.assertEqual(len(CASES), 16)
        for case in CASES:
            with self.subTest(case=case["id"]):
                state, direct, scope = play(case["turns"])
                view = support_state.public_view(state)
                active = view["active_case"]
                expected = case["expect"]
                if "scope" in expected:
                    self.assertEqual(scope, expected["scope"], case["id"])
                if "has_case" in expected:
                    self.assertEqual(active is not None, expected["has_case"], case["id"])
                if active is None:
                    continue
                if "model" in expected:
                    self.assertEqual(active["model"], expected["model"], case["id"])
                if "model_source_turn" in expected:
                    self.assertEqual(active["model_source_turn"], expected["model_source_turn"], case["id"])
                if "tried" in expected:
                    got = [(item["action_id"], item["outcome"]) for item in active["tried_actions"]]
                    self.assertEqual(got, expected["tried"], case["id"])
                if "not_tried" in expected:
                    got = [item["action_id"] for item in active["not_tried_actions"]]
                    self.assertEqual(got, expected["not_tried"], case["id"])
                if "next_action" in expected:
                    next_action = (active.get("next_step") or {}).get("action_id")
                    self.assertEqual(next_action, expected["next_action"], case["id"])
                if "next_kind" in expected:
                    self.assertEqual((active.get("next_step") or {}).get("kind"), expected["next_kind"], case["id"])
                if "status" in expected:
                    self.assertEqual(active["status"], expected["status"], case["id"])
                if "case_count" in expected:
                    self.assertEqual(len(view["cases"]), expected["case_count"], case["id"])
                if "direct_contains" in expected:
                    self.assertIn(expected["direct_contains"], direct or "", case["id"])

    def test_negation_and_arbitrary_cannot(self):
        state = support_state.empty_state()
        state = support_state.apply_user_turn(state, "回充失败", "m1", now=1)["state"]
        state = support_state.apply_user_turn(state, "没清理触点，还是不行", "m2", now=2)["state"]
        active = support_state.public_view(state)["active_case"]
        self.assertEqual(active["tried_actions"], [])
        self.assertIn("clean_contacts", [item["action_id"] for item in active["not_tried_actions"]])
        state = support_state.apply_user_turn(state, "不能开机", "m3", now=3)["state"]
        active = support_state.public_view(state)["active_case"]
        self.assertEqual(active["tried_actions"], [])

    def test_clear_dock_is_not_clean_contacts(self):
        state = support_state.apply_user_turn(
            {},
            "回充失败，清理过充电座周围的障碍物了，还是找不到充电座",
            message_id="probe",
            now=1,
        )["state"]
        active = support_state.public_view(state)["active_case"]
        self.assertEqual(
            [(x["action_id"], x["outcome"]) for x in active["tried_actions"]],
            [("clear_dock_obstacles", "failed")],
        )
        self.assertNotIn("clean_contacts", [x["action_id"] for x in active["tried_actions"]])

    def test_negated_and_hypothetical_success_do_not_close(self):
        for text in (
            "回充失败，还没有恢复正常",
            "回充失败，还不能正常回充",
            "回充失败，如果恢复正常了怎么办",
        ):
            with self.subTest(text=text):
                active = support_state.public_view(
                    support_state.apply_user_turn({}, text, message_id="probe", now=1)["state"]
                )["active_case"]
                self.assertEqual(active["status"], "active")
                self.assertIsNotNone(active["next_step"])
        for text in ("回充失败，现在可以正常回充了", "回充失败，充上电了", "回充失败，已恢复正常"):
            with self.subTest(text=text):
                active = support_state.public_view(
                    support_state.apply_user_turn({}, text, message_id="probe", now=1)["state"]
                )["active_case"]
                self.assertEqual(active["status"], "resolved")

    def test_mixed_reports_use_last_explicit_clause(self):
        resolved = support_state.public_view(
            support_state.apply_user_turn(
                {}, "充不上电，现在可以正常回充了", message_id="probe", now=1
            )["state"]
        )["active_case"]
        self.assertEqual(resolved["status"], "resolved")
        reopened = support_state.public_view(
            support_state.apply_user_turn(
                {}, "回充失败，充上了，但又不充了", message_id="probe", now=1
            )["state"]
        )["active_case"]
        self.assertEqual(reopened["status"], "active")

    def test_same_sentence_multi_action_and_negation_scope(self):
        state = support_state.apply_user_turn(
            {},
            "回充失败，没清理充电座周围障碍物，但擦了触点和回充传感器，还是不行",
            message_id="probe",
            now=1,
        )["state"]
        active = support_state.public_view(state)["active_case"]
        tried = [(x["action_id"], x["outcome"]) for x in active["tried_actions"]]
        self.assertNotIn("clear_dock_obstacles", [x[0] for x in tried])
        self.assertIn(("clean_contacts", "failed"), tried)
        self.assertIn(("clean_sensors", "failed"), tried)
        self.assertIn("clear_dock_obstacles", [x["action_id"] for x in active["not_tried_actions"]])

    def test_factory_reset_is_not_restart_and_not_auto_suggested(self):
        state = support_state.apply_user_turn(
            {}, "回充失败，恢复出厂设置了还是不行", message_id="probe", now=1
        )["state"]
        active = support_state.public_view(state)["active_case"]
        self.assertEqual([x["action_id"] for x in active["tried_actions"]], ["factory_reset"])
        self.assertNotEqual((active.get("next_step") or {}).get("action_id"), "factory_reset")

    def test_power_symptom_prioritizes_supply_check(self):
        state = support_state.empty_state()
        state = support_state.apply_user_turn(
            state, "回充失败，充电座指示灯不亮", "m1", now=1
        )["state"]
        active = support_state.public_view(state)["active_case"]
        self.assertEqual(active["next_step"]["action_id"], "check_power")

    def test_duplicate_reports_do_not_duplicate_tried(self):
        state = support_state.empty_state()
        state = support_state.apply_user_turn(state, "充不上电", "m1", now=1)["state"]
        state = support_state.apply_user_turn(state, "擦了触点，还是不行", "m2", now=2)["state"]
        state = support_state.apply_user_turn(state, "擦了触点，还是不行", "m3", now=3)["state"]
        active = support_state.public_view(state)["active_case"]
        self.assertEqual(len(active["tried_actions"]), 1)
        # Same source message replay is a no-op.
        snapshot = json.dumps(state, ensure_ascii=False, sort_keys=True)
        replay = support_state.apply_user_turn(state, "擦了触点，还是不行", "m3", now=4)
        self.assertFalse(replay["changed"])
        self.assertEqual(json.dumps(replay["state"], ensure_ascii=False, sort_keys=True), snapshot)

    def test_assistant_never_becomes_user_fact(self):
        state = support_state.empty_state()
        state = support_state.apply_user_turn(state, "充不上电", "m1", now=1)["state"]
        state = support_state.apply_assistant_turn(state, "你已经清理了触点，也检查了电源。", "a1", turn=1, now=2)
        active = support_state.public_view(state)["active_case"]
        self.assertEqual(active["tried_actions"], [])
        self.assertEqual([item["action_id"] for item in active["suggested_actions"]], ["clear_dock_obstacles"])

    def test_unique_followup_links_but_ambiguous_asks(self):
        state = support_state.empty_state()
        state = support_state.apply_user_turn(state, "充不上电", "m1", now=1)["state"]
        state = support_state.apply_assistant_turn(state, "先清理充电座周围障碍物，再检查电源。", "a1", turn=1, now=2)
        # Only the state's single primary suggestion is a clear antecedent.
        result = support_state.apply_user_turn(state, "还是不行", "m2", now=3)
        active = support_state.public_view(result["state"])["active_case"]
        self.assertEqual(result["direct_answer"], None)
        self.assertEqual([(x["action_id"], x["outcome"]) for x in active["tried_actions"]], [("clear_dock_obstacles", "failed")])

        # Two unresolved suggestions with no explicit step: ask instead of
        # marking both (or the wrong one) failed.
        state2 = support_state.empty_state()
        state2 = support_state.apply_user_turn(state2, "充不上电", "n1", now=1)["state"]
        case = state2["cases"][0]
        case["suggested_actions"] = [
            {"action_id": "clean_contacts", "label": "清洁充电触点", "primary": True, "status": "suggested", "source_turn": 1},
            {"action_id": "check_power", "label": "检查充电座电源", "primary": False, "status": "suggested", "source_turn": 1},
        ]
        result2 = support_state.apply_user_turn(state2, "还是不行", "n2", now=2)
        active2 = support_state.public_view(result2["state"])["active_case"]
        self.assertIn("哪一步", result2["direct_answer"] or "")
        self.assertEqual(active2["tried_actions"], [])

    def test_resolved_new_fault_keeps_history(self):
        state = support_state.empty_state()
        state = support_state.apply_user_turn(state, "充不上电", "m1", now=1)["state"]
        state = support_state.apply_user_turn(state, "充上了", "m2", now=2)["state"]
        state = support_state.apply_user_turn(state, "又充不上电了", "m3", now=3)["state"]
        view = support_state.public_view(state)
        self.assertEqual(len(view["cases"]), 2)
        self.assertEqual(view["cases"][0]["status"], "resolved")
        self.assertEqual(view["active_case"]["status"], "active")

    def test_prompt_context_has_sources_and_does_not_repeat_failed_step(self):
        state = support_state.empty_state()
        state = support_state.apply_user_turn(state, "充不上电", "m1", now=1)["state"]
        state = support_state.apply_user_turn(state, "擦了触点，还是不行", "m2", now=2)["state"]
        state = support_state.apply_assistant_turn(state, "下一步检查电源。", "a2", turn=2, now=3)
        context = support_state.prompt_context(state)
        self.assertIn("消息 m2", context)
        self.assertIn("清洁充电触点：失败", context)
        self.assertIn("清理充电座周围障碍物", context)
        self.assertNotIn("重置系统提示", context)

    def test_handoff_summary_is_local_and_has_provenance(self):
        state = support_state.empty_state()
        state = support_state.apply_user_turn(state, "我家的是石头G20，充不上电", "m1", now=1)["state"]
        summary = support_state.handoff_summary(state, "s-1")["summary"]
        self.assertIn("石头G20", summary)
        self.assertIn("未自动发送", summary)
        self.assertIn("用户第1轮", summary)


class FakeConfig:
    instance_id = "inst-test"
    artifact_id = "art-test"
    scenario = "robot_vacuum"

    def __init__(self, root: Path):
        self.sessions_root = root / "sessions"
        self.storage_root = root


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = FakeConfig(Path(self.tmp.name))
        from modules.instances.runtime.store import SessionStore

        self.store = SessionStore(self.config)
        self.session = self.store.create(workflow="agent")["session_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_session_lazy_state_and_restart(self):
        self.assertIsNone(self.store.load_support_state(self.session))
        from robot_vacuum_support import state as support

        state = support.load_state(None)
        result = support.apply_user_turn(state, "回充失败", "m1", now=1)
        self.store.save_support_state(self.session, result["state"])
        self.assertTrue((self.store.session_root(self.session) / "support_state.json").is_file())
        from modules.instances.runtime.store import SessionStore

        reopened = SessionStore(self.config)
        loaded = support.load_state(reopened.load_support_state(self.session))
        self.assertEqual(support.public_view(loaded)["active_case"]["status"], "active")

    def test_session_isolation_and_corrupt_file(self):
        other = self.store.create()["session_id"]
        from robot_vacuum_support import state as support

        state = support.apply_user_turn(support.empty_state(), "充不上电", "m1", now=1)["state"]
        self.store.save_support_state(self.session, state)
        self.assertIsNone(self.store.load_support_state(other))
        path = self.store.session_root(other) / "support_state.json"
        path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(self.store.load_support_state(other))
        self.assertEqual(support.public_view(support.load_state(None))["active_case"], None)

    def test_concurrent_saves_are_valid_json(self):
        from robot_vacuum_support import state as support

        base = support.apply_user_turn(support.empty_state(), "充不上电", "m1", now=1)["state"]
        errors = []

        def write(index):
            try:
                self.store.save_support_state(self.session, {**base, "turn": index})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        json.loads((self.store.session_root(self.session) / "support_state.json").read_text(encoding="utf-8"))

    def test_bracketed_ipv6_no_proxy_is_removed_before_worker(self):
        from modules.instances.runtime.worker import _safe_no_proxy

        value = "localhost,127.0.0.1,::1,[::1],*.local"
        cleaned = _safe_no_proxy(value)
        self.assertNotIn("[::1]", cleaned)
        self.assertIn("::1", cleaned)
        self.assertIn("localhost", cleaned)

    def test_duplicate_busy_uses_existing_worker_lock(self):
        from fastapi import HTTPException
        from modules.instances.runtime.server import reserve_session

        class FakeSession:
            def __init__(self):
                self.busy = threading.Lock()

        fake = FakeSession()

        class FakeManager:
            def get(self, session_id):
                return fake

            def peek(self, session_id):
                return fake

        self.store.require_active(self.session)
        self.assertTrue(fake.busy.acquire(blocking=False))
        try:
            with self.assertRaises(HTTPException) as caught:
                reserve_session(FakeManager(), self.store, self.session)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(caught.exception.detail["code"], "session_busy")
        finally:
            fake.busy.release()
        self.assertIs(reserve_session(FakeManager(), self.store, self.session), fake)
        fake.busy.release()


class FakeRetriever:
    def __init__(self, docs=None, error=None):
        self.docs = docs or []
        self.error = error

    def invoke(self, query):
        if self.error is not None:
            raise self.error
        return list(self.docs)


class RagEvidenceTests(unittest.TestCase):
    def _tools(self, retriever):
        service = RagSummarizeService(retriever=retriever)
        return build_rag_tools(service=service)

    def test_real_snippets_and_citation_mapping(self):
        docs = [
            Document(
                page_content="回充失败时，先清洁机器人底部充电触点和充电座金属片。",
                metadata={"source": "C:/kb/故障排除.txt", "chunk_id": "c1"},
            )
        ]
        _summarize, retrieve = self._tools(FakeRetriever(docs))
        token = rag_evidence.begin_collection()
        try:
            retrieve.invoke({"query": "回充失败", "k": 4})
        finally:
            payload = rag_evidence.end_collection(token)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["results"][0]["source"], "故障排除.txt")
        self.assertIn("充电触点", payload["results"][0]["snippet"])
        decision = review("先清洁触点 [故障排除.txt]", payload, recharge_scope=True)
        self.assertEqual(decision["citation_status"], "matched_current_evidence")
        self.assertEqual(decision["mapped_citations"], ["故障排除.txt"])

    def test_unmapped_citation_is_not_claimed_correct(self):
        docs = [Document(page_content="回充失败先清洁触点。", metadata={"source": "故障排除.txt"})]
        _summarize, retrieve = self._tools(FakeRetriever(docs))
        token = rag_evidence.begin_collection()
        try:
            retrieve.invoke({"query": "回充失败"})
        finally:
            payload = rag_evidence.end_collection(token)
        decision = review("答案 [故障排除.txt] [编造.txt]", payload, recharge_scope=True)
        self.assertEqual(decision["citation_status"], "partial")
        self.assertEqual(decision["unmapped_citations"], ["编造.txt"])
        self.assertIn("不能宣称引用正确", decision["notice"])

    def test_plain_filename_citation_maps_to_evidence(self):
        payload = {
            "status": "ok",
            "queries": ["回充"],
            "results": [{"source": "故障排除.txt", "snippet": "回充失败先清洁触点。", "rank": 1}],
        }
        decision = review("参考来源：故障排除.txt", payload, recharge_scope=True)
        self.assertEqual(decision["citation_status"], "matched_current_evidence")
        self.assertNotIn("故障排除.txt", decision["unmapped_citations"])

    def test_empty_result_is_explicit(self):
        _summarize, retrieve = self._tools(FakeRetriever([]))
        token = rag_evidence.begin_collection()
        try:
            retrieve.invoke({"query": "Galaxy X99 价格"})
        finally:
            payload = rag_evidence.end_collection(token)
        self.assertEqual(payload["status"], "empty")
        decision = review("我不知道", payload, recharge_scope=False)
        self.assertEqual(decision["citation_status"], "no_evidence")
        self.assertIn("没有返回可用资料", decision["notice"])

    def test_tool_error_and_timeout_do_not_become_normal_result(self):
        for error, status in ((RuntimeError("chroma down"), "error"), (TimeoutError("slow"), "timeout")):
            with self.subTest(status=status):
                _summarize, retrieve = self._tools(FakeRetriever(error=error))
                token = rag_evidence.begin_collection()
                with self.assertRaises(Exception):
                    retrieve.invoke({"query": "回充失败"})
                payload = rag_evidence.end_collection(token)
                self.assertEqual(payload["status"], status)
                self.assertIn("error", payload["records"][0])
                decision = review("", payload, recharge_scope=True)
                self.assertIn("检索", decision["notice"])

    def test_conflict_and_irrelevance_are_surfaced(self):
        docs = [
            Document(page_content="充电时间约 4 小时。", metadata={"source": "a.txt"}),
            Document(page_content="充电时间约 6 小时。", metadata={"source": "b.txt"}),
        ]
        decision = review(
            "约 4 小时 [a.txt]",
            {"status": "ok", "queries": ["充电"], "results": [
                {"source": "a.txt", "snippet": "充电时间约 4 小时。", "rank": 1},
                {"source": "b.txt", "snippet": "充电时间约 6 小时。", "rank": 2},
            ]},
            recharge_scope=True,
        )
        self.assertIsNotNone(decision["conflict"])
        irrelevant = review(
            "需要更多信息",
            {"status": "ok", "queries": ["回充"], "results": [
                {"source": "c.txt", "snippet": "选购时看房屋面积。", "rank": 1}
            ]},
            recharge_scope=True,
        )
        self.assertEqual(irrelevant["relevance"], "insufficient")
        self.assertIn("关联不足", irrelevant["notice"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

