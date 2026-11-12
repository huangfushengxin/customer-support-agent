import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

import llm_service
import ticket_service
from ticket_api import app


class TicketDraftTests(unittest.TestCase):
    def setUp(self):
        # 每个测试使用独立的临时数据库，保留日常使用的工单数据。
        temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(
            patch.object(
                ticket_service,
                "DB_PATH",
                Path(temp_dir) / "test_tickets.db",
            )
        )

        # 替换模型调用，测试期间不会发送 DeepSeek 请求。
        self.generate = self.enterContext(
            patch("ticket_api.llm_service.generate_ticket_draft")
        )
        self.draft = llm_service.TicketDraft(
            title="软件激活失败",
            category="激活问题",
            description="软件升级后激活时提示授权无效。",
            missing_info=["软件版本号"],
        )
        self.generate.return_value = self.draft
        self.client = self.enterContext(TestClient(app))

    def test_generate_draft_without_saving(self):
        """生成草稿返回完整字段，但不创建正式工单。"""
        response = self.client.post(
            "/ticket-drafts",
            json={"description": "  软件升级后无法激活  "},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.draft.model_dump())
        self.generate.assert_called_once_with("软件升级后无法激活")
        self.assertEqual(ticket_service.list_tickets(), [])

    def test_blank_description_does_not_call_model(self):
        """空描述应直接拒绝，不请求模型，也不写入数据库。"""
        response = self.client.post(
            "/ticket-drafts",
            json={"description": "   "},
        )

        self.assertEqual(response.status_code, 400)
        self.generate.assert_not_called()
        self.assertEqual(ticket_service.list_tickets(), [])

    def test_generation_failures_do_not_save(self):
        """超时和草稿格式错误返回明确错误，数据库保持为空。"""
        with self.assertRaises(ValidationError) as caught:
            llm_service.TicketDraft.model_validate({})

        cases = [
            (httpx.ReadTimeout("模拟超时"), 504),
            (caught.exception, 502),
            (ValueError("模拟无效的模型输出"), 502),
        ]

        for error, expected_status in cases:
            with self.subTest(error=type(error).__name__):
                self.generate.reset_mock()
                self.generate.side_effect = error
                response = self.client.post(
                    "/ticket-drafts",
                    json={"description": "软件无法激活"},
                )

                self.assertEqual(response.status_code, expected_status)
                self.assertTrue(response.json()["detail"])
                self.generate.assert_called_once()
                self.assertEqual(ticket_service.list_tickets(), [])

    def test_save_edited_draft(self):
        """保存人工修改后的内容，保存过程不再次调用模型。"""
        response = self.client.post(
            "/tickets/from-draft",
            json={
                "title": "  人工核对后的激活问题  ",
                "category": "激活问题",
                "description": "  已确认软件版本为2.0，升级后激活失败。  ",
            },
        )

        self.assertEqual(response.status_code, 201)
        saved = response.json()
        self.assertEqual(saved["title"], "人工核对后的激活问题")
        self.assertEqual(saved["category"], "激活问题")
        self.assertEqual(
            saved["description"],
            "已确认软件版本为2.0，升级后激活失败。",
        )
        self.assertEqual(saved["status"], "待处理")
        self.generate.assert_not_called()

        queried = self.client.get(f"/tickets/{saved['id']}")
        self.assertEqual(queried.status_code, 200)
        self.assertEqual(queried.json(), saved)
        self.assertEqual(ticket_service.list_tickets(), [saved])

    def test_invalid_draft_is_not_saved(self):
        """空字段、超长字段、非法类别不能生成正式工单。"""
        valid = {
            "title": "软件激活失败",
            "category": "激活问题",
            "description": "升级后激活失败。",
        }
        cases = [
            {"title": "   "},
            {"description": "   "},
            {"category": "随便填写的类别"},
            {"title": "长" * 61},
            {"description": "长" * 2001},
        ]

        for changed in cases:
            with self.subTest(field=next(iter(changed))):
                response = self.client.post(
                    "/tickets/from-draft",
                    json={**valid, **changed},
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(ticket_service.list_tickets(), [])

        self.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
