import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import ticket_service
from ticket_api import app


class TicketApiTests(unittest.TestCase):
    def setUp(self):
        # 创建临时目录，测试结束后自动清理
        temp_dir = self.enterContext(
            tempfile.TemporaryDirectory()
        )

        # 临时替换数据库位置，结束后自动恢复
        self.enterContext(
            patch.object(
                ticket_service,
                "DB_PATH",
                Path(temp_dir) / "test_tickets.db",
            )
        )

        # 启动测试客户端，同时执行 lifespan 中的建表逻辑
        self.client = self.enterContext(TestClient(app))

    def test_create_and_query(self):
        """创建后，能通过查询接口读出同一张工单。"""
        response = self.client.post(
            "/tickets",
            json={"description": "接口测试：软件无法激活"},
        )

        self.assertEqual(response.status_code, 201)
        created = response.json()

        response = self.client.get(
            f"/tickets/{created['id']}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), created)

        response = self.client.get("/tickets")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [created])

    def test_invalid_create(self):
        """错误输入应被拒绝，而且不产生记录。"""
        response = self.client.post(
            "/tickets",
            json={"description": "   "},
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.post("/tickets", json={})
        self.assertEqual(response.status_code, 422)

        response = self.client.post(
            "/tickets",
            json={"description": None},
        )
        self.assertEqual(response.status_code, 422)

        response = self.client.get("/tickets")
        self.assertEqual(response.json(), [])

    def test_missing_ticket(self):
        """查询或修改不存在的工单，应返回 404。"""
        response = self.client.get("/tickets/999")
        self.assertEqual(response.status_code, 404)

        response = self.client.patch(
            "/tickets/999/status",
            json={"status": "处理中"},
        )
        self.assertEqual(response.status_code, 404)

    def test_update_status(self):
        """合法状态能保存，非法状态不会覆盖原状态。"""
        response = self.client.post(
            "/tickets",
            json={"description": "无法修改密码"},
        )
        self.assertEqual(response.status_code, 201)
        ticket_id = response.json()["id"]

        response = self.client.patch(
            f"/tickets/{ticket_id}/status",
            json={"status": "处理中"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["status"], "处理中"
        )

        response = self.client.patch(
            f"/tickets/{ticket_id}/status",
            json={"status": "随便"},
        )
        self.assertEqual(response.status_code, 422)

        response = self.client.get(
            f"/tickets/{ticket_id}"
        )
        self.assertEqual(
            response.json()["status"], "处理中"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
