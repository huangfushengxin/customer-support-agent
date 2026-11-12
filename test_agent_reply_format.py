"""多轮历史包装与模型输出格式的回归测试，不请求真实 API。"""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver

import agent_persistent
import agent_service
import ticket_service


def draft_call():
    return {"tool_calls": [{"id": "draft_call", "type": "function", "function": {
        "name": "prepare_ticket_draft", "arguments": json.dumps({"description": "补充信息"})}}]}


class AgentReplyFormatTests(unittest.TestCase):
    def setUp(self):
        self.draft = {"title": "激活失败", "category": "激活问题", "description": "升级后激活失败",
                      "missing_info": ["软件版本"]}
        self.stored = json.dumps({"reply": "请补充版本。", "draft": self.draft, "sources": []},
                                 ensure_ascii=False)
        self.session = agent_service.AgentSession(history=[
            {"role": "user", "content": "升级后激活失败"},
            {"role": "assistant", "content": self.stored}],
            draft=copy.deepcopy(self.draft), draft_token="old-token")

    def test_legacy_wrapped_history_is_sent_as_reply_text_without_mutating_storage(self):
        before = copy.deepcopy(self.session)
        state = agent_service.prepare_turn("版本2.0，Windows 11", self.session)
        assistant = [m for m in state["messages"] if m["role"] == "assistant"]
        self.assertEqual(assistant, [{"role": "assistant", "content": "请补充版本。"}])
        context = json.loads(state["messages"][1]["content"].split("\n", 1)[1])
        self.assertEqual(context["draft"], self.draft)
        self.assertEqual(self.session, before)

    def test_plain_text_history_and_user_json_are_preserved(self):
        self.session.history = [{"role": "user", "content": '{"reply":"用户原文"}'},
                                {"role": "assistant", "content": "请提供版本"}]
        state = agent_service.prepare_turn("2.0", self.session)
        self.assertIn(self.session.history[0], state["messages"])
        self.assertIn(self.session.history[1], state["messages"])

    def test_screenshot_shaped_reply_rejected_without_retry_or_state_commit(self):
        before = copy.deepcopy(self.session)
        wrong = {"reply": "敏感测试内容", "draft": self.draft, "sources": []}
        with patch.object(agent_service, "request_model", side_effect=[
                draft_call(), {"content": json.dumps(wrong)}]) as model:
            with patch.object(agent_service.agent_tools, "execute_tool", return_value={
                    "status": "draft_ready", "draft": self.draft, "saved": False}):
                with patch.object(ticket_service, "create_ticket") as save:
                    with self.assertRaises(agent_service.ReplyFormatError) as error:
                        agent_service.run_agent("版本2.0，Windows 11", self.session)
        self.assertEqual(model.call_count, 2)
        save.assert_not_called()
        self.assertEqual(self.session, before)
        self.assertNotIn("敏感测试内容", str(error.exception))
        self.assertIn("/draft", str(error.exception))

    def test_persistent_cli_keeps_old_draft_and_prints_short_format_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with SqliteSaver.from_conn_string(str(Path(directory)/"sessions.db")) as saver:
                agent = agent_persistent.PersistentAgent(saver)
                thread_id = agent.new_session()
                agent.graph.update_state(agent.config(thread_id),
                    {"session": copy.deepcopy(agent_service.asdict(self.session))}, as_node="reply")
                wrong = {"reply": "隐藏回复", "draft": self.draft, "sources": []}
                with patch.object(agent_service, "request_model", side_effect=[
                        draft_call(), {"content": json.dumps(wrong)}]) as model:
                    with patch.object(agent_service.agent_tools, "execute_tool", return_value={
                            "status": "draft_ready", "draft": self.draft, "saved": False}):
                        with patch("builtins.input", side_effect=[f"/resume {thread_id}", "版本2.0",
                                                                 "/draft", "exit"]):
                            with redirect_stdout(io.StringIO()) as output:
                                agent_persistent.run_cli(agent)
                text = output.getvalue()
                self.assertIn("模型回复格式不符合约定", text)
                self.assertIn("升级后激活失败", text)
                self.assertNotIn("validation errors", text)
                self.assertNotIn("隐藏回复", text)
                self.assertEqual(agent.load_session(thread_id), self.session)
                self.assertEqual(model.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
