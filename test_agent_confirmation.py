"""确认与防重复保存测试：使用临时数据库，模型请求全部模拟。"""
import copy
import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import agent_service
import ticket_service


def draft():
    return {"title": "软件激活失败", "category": "激活问题",
            "description": "软件2.0在Windows 11下提示授权无效",
            "missing_info": []}


def prepared_session(token="test-draft-001"):
    return agent_service.AgentSession(draft=draft(), draft_token=token)


class AgentConfirmationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(ticket_service, "DB_PATH", Path(directory) / "tickets.db"))
        self.model = self.enterContext(patch.object(agent_service, "request_model"))
        self.execute = self.enterContext(patch.object(agent_service.agent_tools, "execute_tool"))
        ticket_service.init_db()

    def plan_new_draft(self, value=None):
        self.model.side_effect = [
            {"tool_calls": [{"id": "draft_call", "type": "function", "function": {
                "name": "prepare_ticket_draft",
                "arguments": json.dumps({"description": "模型整理的描述"})}}]},
            {"content": json.dumps({"status": "draft_ready", "reply": "草稿已准备好",
                                     "citation_ids": []})},
        ]
        self.execute.return_value = {"status": "draft_ready", "draft": value or draft(), "saved": False}

    def test_generating_draft_does_not_save_but_confirmation_does(self):
        self.plan_new_draft()
        session = agent_service.AgentSession()
        response = agent_service.run_agent("帮我整理工单，激活失败", session)
        self.assertEqual(ticket_service.list_tickets(), [])
        self.assertEqual(response["confirmation_command"], "/confirm " + session.draft_token)
        self.assertIsNone(session.saved_ticket_id)
        result = agent_service.confirm_ticket(session, session.draft_token)
        ticket = ticket_service.find_ticket(result["ticket_id"])
        self.assertEqual(ticket["title"], draft()["title"])
        self.assertEqual(ticket["category"], draft()["category"])
        self.assertEqual(ticket["description"], draft()["description"])
        self.assertEqual(ticket["status"], "待处理")
        self.assertEqual(self.model.call_count, 2)  # 确认没有再调用模型。
        self.assertTrue(result["saved"])

    def test_repeated_confirmation_returns_same_ticket_after_state_rebuild(self):
        session = prepared_session()
        first = agent_service.confirm_ticket(session, session.draft_token)
        ticket_service.update_ticket_status(first["ticket_id"], "处理中")
        second = agent_service.confirm_ticket(session, session.draft_token)
        # 即使内存里的“已保存”标记丢失，只要保留相同提交编号，数据库也能识别重试。
        rebuilt = prepared_session()
        third = agent_service.confirm_ticket(rebuilt, rebuilt.draft_token)
        self.assertEqual(first["ticket_id"], second["ticket_id"])
        self.assertEqual(first["ticket_id"], third["ticket_id"])
        self.assertTrue(second["already_saved"])
        self.assertEqual(len(ticket_service.list_tickets()), 1)
        self.assertEqual(ticket_service.find_ticket(first["ticket_id"])["status"], "处理中")
        self.model.assert_not_called()

    def test_different_confirmation_keys_represent_separate_submissions(self):
        first, second = prepared_session("case-one"), prepared_session("case-two")
        a = agent_service.confirm_ticket(first, first.draft_token)
        b = agent_service.confirm_ticket(second, second.draft_token)
        self.assertNotEqual(a["ticket_id"], b["ticket_id"])
        self.assertEqual(len(ticket_service.list_tickets()), 2)

    def test_reusing_key_with_different_content_is_rejected(self):
        session = prepared_session()
        agent_service.confirm_ticket(session, session.draft_token)
        changed = prepared_session()
        changed.draft["description"] = "另一件不同的问题"
        with self.assertRaisesRegex(ValueError, "其他内容"):
            agent_service.confirm_ticket(changed, changed.draft_token)
        self.assertIsNone(changed.saved_ticket_id)
        self.assertEqual(len(ticket_service.list_tickets()), 1)

    def test_concurrent_same_key_creates_only_one_ticket(self):
        def save(_):
            return ticket_service.create_ticket("激活失败", title="激活问题",
                                                category="激活问题", request_key="concurrent-one")
        with ThreadPoolExecutor(max_workers=3) as pool:
            ids = list(pool.map(save, range(3)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(ticket_service.list_tickets()), 1)

    def test_new_draft_invalidates_previous_confirmation_command(self):
        session = prepared_session()
        old_token = session.draft_token
        self.plan_new_draft({**draft(), "description": "版本更正为2.1，激活失败"})
        agent_service.run_agent("更正一下，版本是2.1", session)
        self.assertNotEqual(session.draft_token, old_token)
        with self.assertRaisesRegex(ValueError, "不匹配"):
            agent_service.confirm_ticket(session, old_token)
        self.assertEqual(ticket_service.list_tickets(), [])
        result = agent_service.confirm_ticket(session, session.draft_token)
        self.assertIn("2.1", ticket_service.find_ticket(result["ticket_id"])["description"])

    def test_absent_draft_wrong_token_and_invalid_draft_do_not_write(self):
        invalid = prepared_session()
        invalid.draft["category"] = "模型编造的类别"
        cases = [(agent_service.AgentSession(), "none"),
                 (prepared_session(), "wrong"), (invalid, invalid.draft_token)]
        with patch.object(ticket_service, "init_db") as initialize:
            for session, token in cases:
                with self.subTest(token=token, draft=session.draft):
                    with self.assertRaises(ValueError):
                        agent_service.confirm_ticket(session, token)
            initialize.assert_not_called()
        self.assertEqual(ticket_service.list_tickets(), [])
        self.model.assert_not_called()

    def test_failed_submission_record_insert_rolls_back_ticket_as_well(self):
        with closing(sqlite3.connect(ticket_service.DB_PATH)) as connection, connection:
            connection.execute("""CREATE TRIGGER reject_submission
                BEFORE INSERT ON ticket_submissions BEGIN
                SELECT RAISE(ABORT, 'simulated failure'); END""")
        session = prepared_session()
        before = copy.deepcopy(session)
        with self.assertRaises(sqlite3.IntegrityError):
            agent_service.confirm_ticket(session, session.draft_token)
        self.assertEqual(session, before)
        self.assertEqual(ticket_service.list_tickets(), [])
        with closing(sqlite3.connect(ticket_service.DB_PATH)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_submissions").fetchone()[0], 0)
            connection.execute("DROP TRIGGER reject_submission")
        agent_service.confirm_ticket(session, session.draft_token)
        self.assertEqual(len(ticket_service.list_tickets()), 1)

    def test_retry_after_commit_but_lost_response_does_not_duplicate(self):
        session = prepared_session()
        original_create = ticket_service.create_ticket

        def save_then_fail(*args, **kwargs):
            original_create(*args, **kwargs)
            raise sqlite3.OperationalError("模拟提交成功后响应丢失")

        with patch.object(ticket_service, "create_ticket", side_effect=save_then_fail):
            with self.assertRaises(sqlite3.OperationalError):
                agent_service.confirm_ticket(session, session.draft_token)
        self.assertIsNone(session.saved_ticket_id)
        result = agent_service.confirm_ticket(session, session.draft_token)
        self.assertEqual(len(ticket_service.list_tickets()), 1)
        self.assertEqual(result["ticket_id"], ticket_service.list_tickets()[0]["id"])

    def test_saved_session_requires_new_case_before_more_model_calls(self):
        session = prepared_session()
        agent_service.confirm_ticket(session, session.draft_token)
        with self.assertRaises(agent_service.SessionClosedError):
            agent_service.run_agent("把版本改成3.0", session)
        self.model.assert_not_called()
        self.assertEqual(ticket_service.list_tickets()[0]["description"], draft()["description"])

    def test_cli_confirmation_is_local_and_repeated_command_is_safe(self):
        session = prepared_session()
        command = "/confirm " + session.draft_token
        output = io.StringIO()
        with patch.object(agent_service, "AgentSession", return_value=session):
            with patch("builtins.input", side_effect=["/draft", "/confirm", command, command, "exit"]):
                with redirect_stdout(output):
                    agent_service.main()
        self.model.assert_not_called()
        self.execute.assert_not_called()
        self.assertEqual(len(ticket_service.list_tickets()), 1)
        self.assertIn("未重复创建", output.getvalue())

    def test_missing_details_can_be_saved_only_with_explicit_confirmation(self):
        session = prepared_session()
        session.draft["missing_info"] = ["用户暂时无法提供软件版本"]
        self.assertEqual(ticket_service.list_tickets(), [])
        agent_service.confirm_ticket(session, session.draft_token)
        self.assertEqual(len(ticket_service.list_tickets()), 1)
        self.model.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
