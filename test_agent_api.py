"""网页协议回归：真实图和临时 SQLite，模型全部替换为离线演示。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import agent_api
import agent_persistent
import agent_service
import demo_agent_memory
import ticket_api
import ticket_service


class AgentApiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(agent_persistent, "DB_PATH", self.root / "sessions.db"))
        self.enterContext(patch.object(ticket_service, "DB_PATH", self.root / "tickets.db"))
        self.model = self.enterContext(patch.object(agent_service, "request_model",
                                      side_effect=demo_agent_memory.demo_model))
        self.enterContext(patch.object(demo_agent_memory.llm_service, "generate_ticket_draft",
                                      side_effect=demo_agent_memory.demo_draft))
        self.client = self.enterContext(TestClient(ticket_api.app))
        response = self.client.post("/agent/sessions")
        self.assertEqual(response.status_code, 201)
        self.id = response.json()["thread_id"]
        self.url = "/agent/sessions/" + self.id

    def send(self, text):
        response = self.client.post(self.url + "/messages", json={"message": text})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_sessions_are_isolated_and_restored_without_calling_model(self):
        second = self.client.post("/agent/sessions").json()["thread_id"]
        self.assertNotEqual(self.id, second)
        self.assertEqual(self.client.get(self.url).json()["history"], [])
        records = self.client.get("/agent/sessions").json()["sessions"]
        self.assertEqual({item["thread_id"] for item in records}, {self.id, second})
        self.model.assert_not_called()

    def test_followup_survives_new_http_client_and_public_reply_hides_cli_storage(self):
        first = self.send("软件升级后无法激活")
        second = self.send("补充：2.0，Windows 11")
        description = second["draft"]["description"]
        self.assertIn("软件升级后无法激活", description)
        self.assertIn("2.0，Windows 11", description)
        self.assertNotEqual(first["draft_token"], second["draft_token"])
        with TestClient(ticket_api.app) as other:
            self.assertEqual(other.get(self.url).json(), second)
        self.assertEqual(len(second["history"]), 4)
        self.assertNotIn("/confirm ", second["history"][-1]["content"])
        self.assertNotIn('"draft":', second["history"][-1]["content"])
        self.assertEqual(ticket_service.list_tickets(), [])

    def test_confirm_is_explicit_idempotent_and_closes_session(self):
        state = self.send("激活失败，请整理工单")
        self.assertEqual(ticket_service.list_tickets(), [])
        calls = self.model.call_count
        body = {"draft_token": state["draft_token"]}
        first = self.client.post(self.url + "/confirm", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        ticket_id = first.json()["saved_ticket_id"]
        second = self.client.post(self.url + "/confirm", json=body).json()
        self.assertTrue(second["confirmation"]["already_saved"])
        self.assertEqual(second["saved_ticket_id"], ticket_id)
        ticket = self.client.get(f"/tickets/{ticket_id}").json()
        self.assertEqual(ticket["description"], state["draft"]["description"])
        self.assertEqual(len(ticket_service.list_tickets()), 1)
        closed = self.client.post(self.url + "/messages", json={"message": "再改一下"})
        self.assertEqual(closed.status_code, 409)
        self.assertEqual(self.model.call_count, calls)

    def test_stale_confirmation_is_rejected_without_creating_ticket(self):
        old = self.send("无法激活")
        new = self.send("补充系统 Windows 11")
        response = self.client.post(self.url + "/confirm", json={"draft_token": old["draft_token"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ticket_service.list_tickets(), [])
        self.assertEqual(self.client.get(self.url).json()["draft_token"], new["draft_token"])

    def test_missing_and_invalid_requests_do_not_call_model(self):
        self.assertEqual(self.client.get("/agent/sessions/" + "0" * 32).status_code, 404)
        self.assertEqual(self.client.get("/agent/sessions/invalid").status_code, 422)
        for body in ({"message": "  "}, {"message": "x" * 2001}, {"message": 3},
                     {"message": "合法文本", "draft": {}}):
            self.assertEqual(self.client.post(self.url + "/messages", json=body).status_code, 422)
        self.assertEqual(self.client.post(self.url + "/confirm", json={"draft_token": "bad"}).status_code, 422)
        self.assertEqual(self.client.post(self.url + "/confirm", json={"draft_token": "0" * 32}).status_code, 409)
        self.model.assert_not_called()

    def test_timeout_keeps_last_successful_state_and_has_no_automatic_retry(self):
        before = self.send("原问题：激活失败")
        self.model.reset_mock()
        self.model.side_effect = httpx.ReadTimeout("private-provider-message")
        response = self.client.post(self.url + "/messages", json={"message": "失败的补充"})
        self.assertEqual(response.status_code, 504)
        self.assertNotIn("private-provider-message", response.text)
        self.assertEqual(self.model.call_count, 1)
        restored = self.client.get(self.url).json()
        for key in ("history", "draft", "draft_token", "saved_ticket_id"):
            self.assertEqual(restored[key], before[key])
        self.assertTrue(restored["incomplete"])
        self.model.side_effect = demo_agent_memory.demo_model
        after = self.send("重新补充：版本2.0")
        self.assertFalse(after["incomplete"])
        self.assertNotIn("失败的补充", after["draft"]["description"])

    def test_invalid_model_reply_returns_safe_error_and_keeps_draft(self):
        before = self.send("原问题")
        def invalid(messages, *, final=False):
            if final:
                return {"content": json.dumps({"reply": "private-output", "draft": {}, "sources": []})}
            return demo_agent_memory.demo_model(messages)
        self.model.side_effect = invalid
        response = self.client.post(self.url + "/messages", json={"message": "补充"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private-output", response.text)
        self.assertEqual(self.client.get(self.url).json()["draft_token"], before["draft_token"])

    def test_busy_request_is_rejected_and_lock_recovers(self):
        self.assertTrue(agent_api._lock.acquire(blocking=False))
        try:
            self.assertEqual(self.client.get(self.url).status_code, 409)
        finally:
            agent_api._lock.release()
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.model.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
