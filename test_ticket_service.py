import tempfile
import unittest
from pathlib import Path

import ticket_service


class TicketServiceTests(unittest.TestCase):
    def setUp(self):
        """每个测试开始前，准备一个独立的临时数据库。"""
        self.temp_dir = tempfile.TemporaryDirectory()

        # 记住原数据库位置，测试结束后恢复
        self.original_db_path = ticket_service.DB_PATH

        ticket_service.DB_PATH = (
            Path(self.temp_dir.name) / "test_tickets.db"
        )

        # 即使测试失败，也会执行清理
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.restore_db_path)

        ticket_service.init_db()

    def restore_db_path(self):
        ticket_service.DB_PATH = self.original_db_path

    def test_create_and_find_ticket(self):
        """创建后，能从数据库中重新读出工单。"""
        ticket_id = ticket_service.create_ticket(
            "  软件无法激活  "
        )

        ticket = ticket_service.find_ticket(ticket_id)

        self.assertIsNotNone(ticket)
        self.assertEqual(ticket["id"], ticket_id)
        self.assertEqual(ticket["description"], "软件无法激活")
        self.assertEqual(ticket["category"], "未分类")
        self.assertEqual(ticket["status"], "待处理")

    def test_empty_description_is_rejected(self):
        """空描述应报错，而且不能产生记录。"""
        with self.assertRaises(ValueError):
            ticket_service.create_ticket("   ")

        self.assertEqual(ticket_service.list_tickets(), [])

    def test_update_status(self):
        """合法状态能保存，非法状态不能覆盖原值。"""
        ticket_id = ticket_service.create_ticket("无法修改密码")

        updated = ticket_service.update_ticket_status(
            ticket_id, "处理中"
        )

        self.assertTrue(updated)

        ticket = ticket_service.find_ticket(ticket_id)
        self.assertEqual(ticket["status"], "处理中")

        with self.assertRaises(ValueError):
            ticket_service.update_ticket_status(
                ticket_id, "随便写的状态"
            )

        ticket = ticket_service.find_ticket(ticket_id)
        self.assertEqual(ticket["status"], "处理中")

    def test_missing_ticket(self):
        """不存在的工单应返回明确结果。"""
        self.assertIsNone(ticket_service.find_ticket(999))

        self.assertFalse(
            ticket_service.update_ticket_status(999, "已完成")
        )
    def test_create_ticket_with_draft_fields(self):
        """草稿的标题、类别和描述可以保存并重新读出。"""
        ticket_id = ticket_service.create_ticket(
            "  升级后激活时提示授权无效  ",
            title="  软件升级后激活失败  ",
            category="激活问题",
        )

        # 重复初始化，不应清除已有数据
        ticket_service.init_db()

        ticket = ticket_service.find_ticket(ticket_id)

        self.assertIsNotNone(ticket)
        self.assertEqual(
            ticket["title"],
            "软件升级后激活失败",
        )
        self.assertEqual(
            ticket["description"],
            "升级后激活时提示授权无效",
        )
        self.assertEqual(ticket["category"], "激活问题")
        self.assertEqual(ticket["status"], "待处理")
        self.assertEqual(len(ticket_service.list_tickets()), 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)