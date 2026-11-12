"""多轮记忆测试：模拟模型回复，不访问 DeepSeek、不保存正式工单。"""
import copy
import json
import unittest
from unittest.mock import patch

import httpx

import agent_service


def prepare_call(description):
    return {"tool_calls": [{
        "id": "draft_call", "type": "function",
        "function": {"name": "prepare_ticket_draft",
                     "arguments": json.dumps({"description": description})},
    }]}


def final_reply(status="need_info", citation_ids=None):
    return {"content": json.dumps({
        "status": status, "reply": "请核对信息。",
        "citation_ids": citation_ids or [],
    })}


def initial_draft():
    return {"title": "软件激活失败", "category": "激活问题",
            "description": "软件升级后无法激活",
            "missing_info": ["软件版本", "操作系统"]}


class AgentMemoryTests(unittest.TestCase):
    def setUp(self):
        self.request = self.enterContext(patch.object(agent_service, "request_model"))
        self.execute = self.enterContext(patch.object(agent_service.agent_tools, "execute_tool"))

    def test_followup_keeps_original_user_facts_and_updates_draft(self):
        """补充输入与原问题合并，不使用路由模型擅自编造的工具参数。"""
        session = agent_service.AgentSession()
        snapshots = []
        first = "软件升级后无法激活，帮我整理工单"
        followup = "版本2.0，Windows 11，提示授权无效"
        updated = {**initial_draft(), "description": first + "；" + followup,
                   "missing_info": []}
        replies = [prepare_call("模型改写的原问题"), final_reply(),
                   prepare_call("版本3.0，用户已经付款"), final_reply("draft_ready")]

        def fake_request(messages, *, final=False):
            snapshots.append(copy.deepcopy(messages))
            return replies[len(snapshots) - 1]

        self.request.side_effect = fake_request
        self.execute.side_effect = [
            {"status": "draft_ready", "draft": initial_draft(), "saved": False},
            {"status": "draft_ready", "draft": updated, "saved": False},
        ]
        with patch("ticket_service.create_ticket") as save:
            agent_service.run_agent(first, session)
            result = agent_service.run_agent(followup, session)
        save.assert_not_called()
        actual_description = self.execute.call_args.args[1]["description"]
        self.assertIn(first, actual_description)
        self.assertIn(followup, actual_description)
        self.assertNotIn("版本3.0", actual_description)
        self.assertNotIn("用户已经付款", actual_description)
        self.assertIn({"role": "user", "content": first}, snapshots[2])
        state = json.loads(snapshots[2][1]["content"].split("\n", 1)[1])
        self.assertEqual(state["draft"], initial_draft())
        self.assertEqual(session.draft, updated)
        self.assertEqual(len(session.history), 4)
        self.assertEqual(result["status"], "draft_ready")
        self.assertFalse(result["saved"])
        # 返回结果不应与会话中的草稿共享可变对象。
        result["draft"]["missing_info"].append("外部修改")
        self.assertEqual(session.draft["missing_info"], [])

    def test_separate_sessions_do_not_share_history(self):
        first, second = agent_service.AgentSession(), agent_service.AgentSession()
        self.request.side_effect = [
            {"content": "请补充描述"}, final_reply(),
            {"content": "请补充描述"}, final_reply(),
        ]
        agent_service.run_agent("第一个会话的私人问题", first)
        agent_service.run_agent("第二个会话", second)
        second_messages = self.request.call_args_list[2].args[0]
        self.assertNotIn("第一个会话的私人问题", json.dumps(second_messages, ensure_ascii=False))
        self.assertIsNot(first.history, second.history)
        self.assertEqual(len(first.history), 2)
        self.assertEqual(len(second.history), 2)

    def test_failed_turn_preserves_old_history_and_draft(self):
        session = agent_service.AgentSession(
            history=[{"role": "user", "content": "无法激活"},
                     {"role": "assistant", "content": "请提供版本"}],
            draft=initial_draft(),
        )
        before = copy.deepcopy(session)
        self.request.side_effect = [prepare_call("版本2.0"), httpx.ReadTimeout("模拟超时")]
        self.execute.return_value = {"status": "draft_ready", "draft": {
            **initial_draft(), "description": "更新后的草稿"}, "saved": False}
        with self.assertRaises(httpx.TimeoutException):
            agent_service.run_agent("版本2.0", session)
        self.assertEqual(session, before)

    def test_knowledge_question_does_not_replace_existing_draft(self):
        session = agent_service.AgentSession(draft=initial_draft())
        self.request.side_effect = [
            {"tool_calls": [{"id": "search_call", "type": "function", "function": {
                "name": "search_knowledge", "arguments": json.dumps({"question": "忘记密码"})}}]},
            final_reply("answer", ["FAQ-002"]),
        ]
        self.execute.return_value = {"status": "found", "documents": [{
            "id": "FAQ-002", "title": "密码找回", "source": "测试来源", "content": "通过注册邮箱找回"}]}
        result = agent_service.run_agent("忘记密码怎么办", session)
        self.assertIsNone(result["draft"])
        self.assertEqual(result["status"], "answer")
        self.assertEqual(session.draft, initial_draft())
        self.assertEqual(result["sources"][0]["id"], "FAQ-002")

    def test_previous_citation_cannot_replace_current_retrieval(self):
        session = agent_service.AgentSession(history=[
            {"role": "user", "content": "激活说明"},
            {"role": "assistant", "content": "之前引用过FAQ-001"},
        ])
        before = copy.deepcopy(session)
        self.request.side_effect = [{"content": "直接使用旧引用"}, final_reply("answer", ["FAQ-001"])]
        with self.assertRaises(ValueError):
            agent_service.run_agent("再讲一次", session)
        self.execute.assert_not_called()
        self.assertEqual(session, before)

    def test_limits_stop_without_silently_dropping_history(self):
        with self.subTest("达到轮数上限，在请求模型前停止"):
            session = agent_service.AgentSession(history=[
                {"role": role, "content": "历史内容"}
                for _ in range(agent_service.MAX_TURNS) for role in ("user", "assistant")
            ])
            before = copy.deepcopy(session)
            with self.assertRaises(agent_service.SessionLimitError):
                agent_service.run_agent("继续", session)
            self.request.assert_not_called()
            self.assertEqual(session, before)
        with self.subTest("草稿上下文超长，在执行工具前停止"):
            session = agent_service.AgentSession(history=[
                {"role": "user", "content": "原问题" * 500},
                {"role": "assistant", "content": "请补充"},
            ])
            before = copy.deepcopy(session)
            self.request.return_value = prepare_call("简短摘要")
            with self.assertRaises(agent_service.SessionLimitError):
                agent_service.run_agent("补充" * 300, session)
            self.execute.assert_not_called()
            self.assertEqual(session, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
