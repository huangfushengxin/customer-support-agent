import unittest
from unittest.mock import patch

from pydantic import ValidationError

import agent_tools
import llm_service


class AgentToolTests(unittest.TestCase):
    def test_search_tool_returns_real_faq(self):
        """通过统一执行入口，可以读取已有 FAQ 和来源。"""
        result = agent_tools.execute_tool(
            "search_knowledge", {"question": "  软件激活不了  "}
        )
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["documents"][0]["id"], "FAQ-001")
        self.assertTrue(result["documents"][0]["source"])

    def test_unknown_or_write_tools_are_blocked(self):
        """模型不能通过自造名称执行保存、删除等未开放工具。"""
        with patch.object(agent_tools.llm_service, "generate_ticket_draft") as generate:
            for name in ("save_ticket", "delete_ticket", "unknown_tool", None):
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        agent_tools.execute_tool(name, {})
            generate.assert_not_called()

    def test_bad_arguments_are_rejected_before_execution(self):
        """缺参数、空参数、错误类型和额外参数不能进入业务函数。"""
        with patch.object(agent_tools.knowledge_service, "search_knowledge") as search:
            for arguments in ({}, {"question": "   "}, {"question": 123},
                              {"question": "激活失败", "save": True}):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValidationError):
                        agent_tools.execute_tool("search_knowledge", arguments)
            search.assert_not_called()
        with patch.object(agent_tools.llm_service, "generate_ticket_draft") as generate:
            with self.assertRaises(ValidationError):
                agent_tools.execute_tool("prepare_ticket_draft", {"description": "   "})
            generate.assert_not_called()

    def test_draft_is_prepared_without_database_write(self):
        """工具仅返回草稿，不能调用正式工单创建函数。"""
        draft = llm_service.TicketDraft(
            title="软件激活失败", category="激活问题",
            description="用户反馈软件无法激活。", missing_info=["完整报错提示"],
        )
        with patch.object(agent_tools.llm_service, "generate_ticket_draft", return_value=draft) as generate:
            with patch("ticket_service.create_ticket") as create:
                result = agent_tools.execute_tool(
                    "prepare_ticket_draft", {"description": "  软件无法激活  "}
                )
        generate.assert_called_once_with("软件无法激活")
        create.assert_not_called()
        self.assertFalse(result["saved"])
        self.assertEqual(result["draft"], draft.model_dump())


if __name__ == "__main__":
    unittest.main(verbosity=2)
