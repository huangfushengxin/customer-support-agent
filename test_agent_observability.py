"""真实图和临时 SQLite；验证日志与业务结果一致，模型全部模拟。"""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver

import agent_persistent
import agent_service
import demo_agent_memory
import ticket_service


class AgentObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log_path = self.root / "logs" / "agent.jsonl"
        self.enterContext(patch.object(ticket_service, "DB_PATH", self.root / "tickets.db"))
        self.model = self.enterContext(patch.object(agent_service, "request_model",
                                                  side_effect=demo_agent_memory.demo_model))
        self.enterContext(patch.object(demo_agent_memory.llm_service, "generate_ticket_draft",
                                      side_effect=demo_agent_memory.demo_draft))
        saver = self.enterContext(SqliteSaver.from_conn_string(str(self.root / "sessions.db")))
        self.agent = agent_persistent.PersistentAgent(saver, log_path=self.log_path)
        self.thread_id = self.agent.new_session()

    def records(self):
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    def test_success_and_repeated_confirmation_log_real_outcomes_without_content(self):
        secret_text = "客户私密描述-example-secret"
        self.agent.run_turn(self.thread_id, secret_text)
        token = self.agent.load_session(self.thread_id).draft_token
        first = self.agent.confirm(self.thread_id, token)
        self.agent.confirm(self.thread_id, token)
        records = self.records()
        self.assertEqual([r["operation"] for r in records], ["turn", "confirm", "confirm"])
        self.assertEqual(records[0]["flow_nodes"], ["plan", "tools", "reply"])
        self.assertEqual(records[0]["tools"], ["prepare_ticket_draft"])
        self.assertTrue(records[0]["has_draft"])
        self.assertFalse(records[1]["already_saved"])
        self.assertTrue(records[2]["already_saved"])
        self.assertEqual(records[2]["ticket_id"], first["ticket_id"])
        self.assertEqual(len(ticket_service.list_tickets()), 1)
        self.assertEqual(len({r["event_id"] for r in records}), 3)
        for record in records:
            self.assertEqual(record["outcome"], "success")
            self.assertGreaterEqual(record["duration_ms"], 0)
            self.assertEqual(record["thread_id"], self.thread_id)
        raw = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn(secret_text, raw)
        self.assertNotIn(token, raw)

    def test_timeout_logs_failure_preserves_draft_and_does_not_retry(self):
        self.agent.run_turn(self.thread_id, "旧问题")
        before = self.agent.load_session(self.thread_id)
        self.model.reset_mock()
        self.model.side_effect = httpx.ReadTimeout("secret-key 不应进入日志")
        with self.assertRaises(httpx.TimeoutException):
            self.agent.run_turn(self.thread_id, "新问题")
        self.assertEqual(self.agent.load_session(self.thread_id), before)
        self.assertEqual(self.model.call_count, 1)
        record = self.records()[-1]
        self.assertEqual(record["outcome"], "error")
        self.assertEqual(record["error_type"], "model_timeout")
        self.assertNotIn("secret-key", self.log_path.read_text(encoding="utf-8"))

    def test_http_error_logs_status_without_request_or_response_body(self):
        response = httpx.Response(503, request=httpx.Request("POST", "https://example.com"))
        self.model.side_effect = httpx.HTTPStatusError("secret-body", request=response.request,
                                                    response=response)
        with self.assertRaises(httpx.HTTPStatusError):
            self.agent.run_turn(self.thread_id, "问题")
        record = self.records()[0]
        self.assertEqual(record["http_status"], 503)
        self.assertEqual(record["error_type"], "model_http_error")
        self.assertNotIn("secret-body", self.log_path.read_text(encoding="utf-8"))

    def test_wrong_confirmation_logs_rejection_and_does_not_write_ticket(self):
        self.agent.run_turn(self.thread_id, "问题")
        with self.assertRaises(ValueError):
            self.agent.confirm(self.thread_id, "wrong-secret-token")
        record = self.records()[-1]
        self.assertEqual(record["error_type"], "validation_error")
        self.assertEqual(record["outcome"], "error")
        self.assertFalse((self.root / "tickets.db").exists())
        self.assertNotIn("wrong-secret-token", self.log_path.read_text(encoding="utf-8"))

    def test_checkpoint_failure_reports_ticket_committed_and_retry_is_safe(self):
        self.agent.run_turn(self.thread_id, "问题")
        token = self.agent.load_session(self.thread_id).draft_token
        with patch.object(self.agent.graph, "update_state", side_effect=sqlite3.OperationalError()):
            with self.assertRaises(agent_persistent.ConfirmationStateError):
                self.agent.confirm(self.thread_id, token)
        record = self.records()[-1]
        self.assertEqual(record["error_type"], "confirmation_state_error")
        self.assertTrue(record["ticket_committed"])
        self.assertEqual(record["outcome"], "error")
        self.agent.confirm(self.thread_id, token)
        self.assertEqual(len(ticket_service.list_tickets()), 1)

    def test_unwritable_log_does_not_change_successful_confirmation(self):
        self.agent.run_turn(self.thread_id, "问题")
        token = self.agent.load_session(self.thread_id).draft_token
        self.agent.log_path = self.root  # 目录不能作为日志文件打开。
        with redirect_stderr(io.StringIO()) as output:
            result = self.agent.confirm(self.thread_id, token)
        self.assertIn("日志写入失败", output.getvalue())
        self.assertIsNotNone(ticket_service.find_ticket(result["ticket_id"]))
        self.assertEqual(self.agent.load_session(self.thread_id).saved_ticket_id, result["ticket_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
