"""真实 LangGraph 调度测试；模型使用模拟回复，数据库使用临时文件。"""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

import agent_graph
import agent_service
import ticket_service


def call(name, arguments, call_id="call_1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def final(status="need_info", ids=None):
    return {"content": json.dumps({"status": status, "reply": "请核对内容。",
                                   "citation_ids": ids or []})}


class AgentGraphTests(unittest.TestCase):
    def setUp(self):
        self.model = self.enterContext(patch.object(agent_service, "request_model"))
        self.execute = self.enterContext(patch.object(agent_service.agent_tools, "execute_tool"))
        self.document = {"id": "FAQ-001", "title": "激活说明",
                         "source": "测试FAQ", "content": "请检查网络连接。"}

    def test_knowledge_route_executes_tool_and_preserves_source(self):
        self.model.side_effect = [
            {"tool_calls": [call("search_knowledge", {"question": "激活失败"})]},
            final("answer", ["FAQ-001"]),
        ]
        self.execute.return_value = {"status": "found", "documents": [self.document]}
        result = agent_graph.run_graph_agent("激活失败怎么办")
        self.assertEqual(result["flow_nodes"], ["plan", "tools", "reply"])
        self.execute.assert_called_once_with("search_knowledge", {"question": "激活失败"})
        self.assertEqual(result["sources"][0]["source"], "测试FAQ")
        tool_messages = [message for message in self.model.call_args.args[0]
                         if message["role"] == "tool"]
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_1")
        self.assertEqual(self.model.call_count, 2)

    def test_clarification_route_skips_tools(self):
        self.model.side_effect = [{"content": "请描述具体问题"}, final()]
        session = agent_service.AgentSession()
        result = agent_graph.run_graph_agent("你好", session)
        self.assertEqual(result["flow_nodes"], ["plan", "reply"])
        self.execute.assert_not_called()
        self.assertEqual(len(session.history), 2)

    def test_followup_updates_draft_and_explicit_confirmation_saves_once(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(ticket_service, "DB_PATH", Path(directory) / "tickets.db"))
        ticket_service.init_db()
        session = agent_service.AgentSession()
        first = {"title": "激活失败", "category": "激活问题", "description": "升级后无法激活",
                 "missing_info": ["软件版本和系统"]}
        second = {**first, "description": "升级后无法激活；版本2.0，Windows 11", "missing_info": []}
        self.model.side_effect = [
            {"tool_calls": [call("prepare_ticket_draft", {"description": "激活失败"})]}, final(),
            {"tool_calls": [call("prepare_ticket_draft", {"description": "2.0"})]}, final("draft_ready"),
        ]
        self.execute.side_effect = [
            {"status": "draft_ready", "draft": first, "saved": False},
            {"status": "draft_ready", "draft": second, "saved": False},
        ]
        agent_graph.run_graph_agent("升级后无法激活，帮我整理工单", session)
        old_token = session.draft_token
        result = agent_graph.run_graph_agent("版本2.0，Windows 11", session)
        actual = self.execute.call_args.args[1]["description"]
        self.assertIn("升级后无法激活", actual)
        self.assertIn("版本2.0，Windows 11", actual)
        self.assertEqual(ticket_service.list_tickets(), [])
        self.assertEqual(result["draft"], second)
        with self.assertRaises(ValueError):
            agent_service.confirm_ticket(session, old_token)
        a = agent_service.confirm_ticket(session, session.draft_token)
        b = agent_service.confirm_ticket(session, session.draft_token)
        self.assertEqual(a["ticket_id"], b["ticket_id"])
        self.assertEqual(len(ticket_service.list_tickets()), 1)
        self.assertEqual(self.model.call_count, 4)

    def test_invalid_tool_batch_fails_before_any_execution(self):
        self.model.return_value = {"tool_calls": [
            call("search_knowledge", {"question": "激活失败"}),
            call("save_ticket", {}, "call_2"),
        ]}
        session = agent_service.AgentSession()
        with self.assertRaises(ValueError):
            agent_graph.run_graph_agent("帮我处理问题", session)
        self.execute.assert_not_called()
        self.assertEqual(session.history, [])

    def test_failed_reply_preserves_session_without_automatic_retry(self):
        for failed_reply in (httpx.ReadTimeout("模拟超时"), final("answer", ["FAQ-999"])):
            with self.subTest(reply=failed_reply):
                session = agent_service.AgentSession(
                    history=[{"role": "user", "content": "激活失败"},
                             {"role": "assistant", "content": "请补充版本"}],
                    draft={"title": "旧草稿"}, draft_token="old-token",
                )
                before = copy.deepcopy(session)
                self.model.reset_mock()
                self.model.side_effect = [{"content": "需要补充描述"}, failed_reply]
                with self.assertRaises((httpx.TimeoutException, ValueError)):
                    agent_graph.run_graph_agent("版本2.0", session)
                self.assertEqual(session, before)
                self.assertEqual(self.model.call_count, 2)

    def test_compiled_graph_does_not_leak_previous_turn_sources(self):
        self.model.side_effect = [
            {"tool_calls": [call("search_knowledge", {"question": "激活失败"})]},
            final("answer", ["FAQ-001"]),
            {"content": "使用上一轮的来源"}, final("answer", ["FAQ-001"]),
        ]
        self.execute.return_value = {"status": "found", "documents": [self.document]}
        session = agent_service.AgentSession()
        agent_graph.run_graph_agent("激活失败怎么办", session)
        before = copy.deepcopy(session)
        with self.assertRaises(ValueError):
            agent_graph.run_graph_agent("不检索，再说一次", session)
        self.assertEqual(session, before)
        self.assertEqual(self.execute.call_count, 1)

    def test_cli_uses_graph_runner_and_prints_actual_nodes(self):
        self.model.side_effect = [{"content": "请描述具体问题"}, final()]
        output = io.StringIO()
        with patch("builtins.input", side_effect=["你好", "exit"]):
            with redirect_stdout(output):
                agent_service.main(run_turn=agent_graph.run_graph_agent)
        self.assertIn("流程节点：plan → reply", output.getvalue())
        self.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
