import copy
import json
import unittest
from unittest.mock import patch

import httpx

import agent_service


def tool_call(name, arguments, call_id="call_1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def final_reply(status="answer", reply="请检查网络连接。", citation_ids=None):
    return {"content": json.dumps({"status": status, "reply": reply,
                                   "citation_ids": citation_ids or []})}


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.request = self.enterContext(patch.object(agent_service, "request_model"))
        self.execute = self.enterContext(patch.object(agent_service.agent_tools, "execute_tool"))
        self.document = {"id": "FAQ-001", "title": "激活说明", "source": "测试FAQ",
                         "content": "请检查网络连接。"}

    def test_search_result_is_sent_back_with_call_id(self):
        """执行模型选中的工具，并把对应编号的结果交回模型。"""
        snapshots = []
        replies = [
            {"content": None, "tool_calls": [tool_call("search_knowledge", {"question": "激活失败"})]},
            final_reply(citation_ids=["FAQ-001"]),
        ]
        def fake_request(messages, *, final=False):
            snapshots.append((copy.deepcopy(messages), final))
            return replies[len(snapshots) - 1]
        self.request.side_effect = fake_request
        self.execute.return_value = {"status": "found", "documents": [self.document]}
        result = agent_service.run_agent("软件激活失败怎么办")
        self.execute.assert_called_once_with("search_knowledge", {"question": "激活失败"})
        returned = [m for m in snapshots[1][0] if m["role"] == "tool"]
        self.assertEqual(returned[0]["tool_call_id"], "call_1")
        self.assertEqual(json.loads(returned[0]["content"])["documents"][0], self.document)
        self.assertTrue(snapshots[1][1])
        self.assertEqual(result["sources"][0]["source"], "测试FAQ")
        self.assertFalse(result["saved"])

    def test_draft_uses_tool_data_and_does_not_save(self):
        """草稿事实来自工具，保存状态不能由模型自行声称。"""
        draft = {"title": "激活失败", "category": "激活问题", "description": "软件无法激活",
                 "missing_info": ["软件版本号"]}
        self.request.side_effect = [
            {"tool_calls": [tool_call("prepare_ticket_draft", {"description": "软件无法激活"})]},
            final_reply("draft_ready", "已提交（这个文本不应展示）"),
        ]
        self.execute.return_value = {"status": "draft_ready", "draft": draft, "saved": False}
        with patch("ticket_service.create_ticket") as create:
            result = agent_service.run_agent("请帮我整理工单，软件无法激活")
        create.assert_not_called()
        self.assertEqual(result["draft"], draft)
        self.assertEqual(result["status"], "need_info")
        self.assertNotIn("已提交", result["reply"])
        self.assertIn("软件版本号", result["reply"])
        self.assertFalse(result["saved"])

    def test_invalid_batch_is_rejected_before_any_execution(self):
        """自造保存工具、坏参数、重复编号和过多调用都不能执行。"""
        valid = tool_call("search_knowledge", {"question": "激活失败"})
        cases = [
            [tool_call("save_ticket", {})],
            [tool_call("search_knowledge", {"question": "   "})],
            [valid, tool_call("save_ticket", {}, "call_2")],
            [valid, tool_call("search_knowledge", {"question": "激活失败"})],
            [tool_call("search_knowledge", {"question": "激活失败"}, str(i)) for i in range(3)],
        ]
        for calls in cases:
            with self.subTest(calls=calls):
                self.request.side_effect = None
                self.request.return_value = {"tool_calls": calls}
                with self.assertRaises(ValueError):
                    agent_service.run_agent("请处理问题")
                self.execute.assert_not_called()

    def test_invalid_citations_are_rejected(self):
        """最终知识回答必须引用本轮查到的真实资料。"""
        self.execute.return_value = {"status": "found", "documents": [self.document]}
        for ids in ([], ["FAQ-999"]):
            with self.subTest(ids=ids):
                self.request.side_effect = [
                    {"tool_calls": [tool_call("search_knowledge", {"question": "激活失败"})]},
                    final_reply(citation_ids=ids),
                ]
                with self.assertRaises(ValueError):
                    agent_service.run_agent("激活失败怎么办")

    def test_missing_evidence_has_clear_fallback(self):
        self.request.side_effect = [
            {"tool_calls": [tool_call("search_knowledge", {"question": "主题颜色"})]},
            final_reply("no_evidence", "资料不足"),
        ]
        self.execute.return_value = {"status": "no_match", "documents": []}
        result = agent_service.run_agent("软件支持深色主题吗")
        self.assertEqual(result["status"], "no_evidence")
        self.assertEqual(result["sources"], [])
        self.assertIn("没有足够", result["reply"])

    def test_empty_input_and_fake_draft_are_rejected(self):
        with self.assertRaises(ValueError):
            agent_service.run_agent("   ")
        self.request.assert_not_called()
        self.request.side_effect = [{"content": "需要描述问题"}, final_reply("draft_ready")]
        with self.assertRaises(ValueError):
            agent_service.run_agent("你好")

    def test_timeout_is_not_automatically_retried(self):
        self.request.side_effect = httpx.ReadTimeout("模拟超时")
        with self.assertRaises(httpx.TimeoutException):
            agent_service.run_agent("激活失败")
        self.assertEqual(self.request.call_count, 1)
        self.execute.assert_not_called()

    def test_final_stage_cannot_request_more_tools(self):
        self.request.side_effect = [
            {"content": "请补充描述"},
            {"tool_calls": [tool_call("prepare_ticket_draft", {"description": "激活失败"})]},
        ]
        with self.assertRaises(ValueError):
            agent_service.run_agent("你好")
        self.execute.assert_not_called()


class AgentTransportTests(unittest.TestCase):
    def test_request_switches_tool_choice_and_final_json_mode(self):
        """实际 HTTP 包装层发送正确的两阶段参数。"""
        messages = [{"role": "user", "content": "你好"}]
        response = httpx.Response(
            200, request=httpx.Request("POST", "https://example.test"),
            json={"choices": [{"finish_reason": "stop", "message": {"content": "你好"}}]},
        )
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "mock-test-key"}):
            with patch("httpx.Client") as client:
                post = client.return_value.__enter__.return_value.post
                post.return_value = response
                agent_service.request_model(messages)
                planning_payload = post.call_args.kwargs["json"]
                agent_service.request_model(messages, final=True)
                final_payload = post.call_args.kwargs["json"]
        self.assertEqual(planning_payload["tool_choice"], "auto")
        self.assertNotIn("response_format", planning_payload)
        self.assertEqual(final_payload["tool_choice"], "none")
        self.assertEqual(final_payload["response_format"], {"type": "json_object"})
        self.assertEqual(final_payload["thinking"], {"type": "disabled"})
        self.assertEqual(post.call_count, 2)

    def test_truncated_transport_response_is_rejected(self):
        response = httpx.Response(
            200, request=httpx.Request("POST", "https://example.test"),
            json={"choices": [{"finish_reason": "length", "message": {"content": "截断内容"}}]},
        )
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "mock-test-key"}):
            with patch("httpx.Client") as client:
                client.return_value.__enter__.return_value.post.return_value = response
                with self.assertRaises(ValueError):
                    agent_service.request_model([{"role": "user", "content": "你好"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
