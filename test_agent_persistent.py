"""会话持久化验证：真实 SQLite 检查点，模拟模型，不访问外部 API。"""
import copy
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver

import agent_persistent
import agent_service
import demo_agent_memory
import ticket_service


class AgentPersistentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "sessions.db"
        self.enterContext(patch.object(ticket_service, "DB_PATH", self.root / "tickets.db"))
        self.model = self.enterContext(patch.object(agent_service, "request_model",
                                                  side_effect=demo_agent_memory.demo_model))
        self.enterContext(patch.object(demo_agent_memory.llm_service, "generate_ticket_draft",
                                      side_effect=demo_agent_memory.demo_draft))

    def test_draft_and_history_survive_closing_and_reopening_database(self):
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            thread_id = agent.new_session()
            result = agent.run_turn(thread_id, "升级后无法激活")
            before = agent.load_session(thread_id)
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            restored = agent.load_session(thread_id)
            self.assertEqual(restored, before)
            self.assertEqual(len(restored.history), 2)
            self.assertEqual(result["confirmation_command"], "/confirm " + restored.draft_token)
            self.assertFalse((self.root / "tickets.db").exists())
        self.assertEqual(self.model.call_count, 2)

    def test_followup_after_restart_keeps_old_facts_and_invalidates_old_token(self):
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            thread_id = agent.new_session()
            agent.run_turn(thread_id, "升级后无法激活")
            old_token = agent.load_session(thread_id).draft_token
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            result = agent.run_turn(thread_id, "版本2.0，Windows 11")
            self.assertIn("升级后无法激活", result["draft"]["description"])
            self.assertIn("版本2.0，Windows 11", result["draft"]["description"])
            self.assertEqual(len(agent.load_session(thread_id).history), 4)
            with self.assertRaises(ValueError):
                agent.confirm(thread_id, old_token)

    def test_confirmed_ticket_stays_saved_after_restart_and_retry(self):
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            thread_id = agent.new_session()
            agent.run_turn(thread_id, "无法激活")
            token = agent.load_session(thread_id).draft_token
            first = agent.confirm(thread_id, token)
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            self.assertEqual(agent.load_session(thread_id).saved_ticket_id, first["ticket_id"])
            second = agent.confirm(thread_id, token)
            self.assertTrue(second["already_saved"])
            self.assertEqual(second["ticket_id"], first["ticket_id"])
            with self.assertRaises(agent_service.SessionClosedError):
                agent.run_turn(thread_id, "修改成其他问题")
            self.assertEqual(len(ticket_service.list_tickets()), 1)
        self.assertEqual(self.model.call_count, 2)

    def test_new_session_preserves_old_session_without_sharing_history(self):
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            first = agent.new_session()
            agent.run_turn(first, "第一个问题")
            before = agent.load_session(first)
            second = agent.new_session()
            self.assertEqual(agent.load_session(first), before)
            self.assertEqual(agent.load_session(second).history, [])
            records = {item["thread_id"]: item for item in agent.list_sessions()}
            self.assertEqual(set(records), {first, second})
            self.assertEqual(records[first]["turns"], 1)
            self.assertEqual(records[second]["turns"], 0)

    def test_missing_session_does_not_create_or_call_model(self):
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            for thread_id in ("not-an-id", "0" * 32):
                with self.subTest(thread_id=thread_id):
                    with self.assertRaises(ValueError):
                        agent.load_session(thread_id)
            self.assertEqual(agent.list_sessions(), [])
        self.model.assert_not_called()

    def test_failed_turn_restores_successful_history_and_next_input_can_continue(self):
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            thread_id = agent.new_session()
            agent.run_turn(thread_id, "原问题：无法激活")
            before = copy.deepcopy(agent.load_session(thread_id))
            def fail_final(messages, *, final=False):
                if final:
                    raise httpx.ReadTimeout("模拟平台超时")
                return demo_agent_memory.demo_model(messages)
            self.model.side_effect = fail_final
            with self.assertRaises(httpx.TimeoutException):
                agent.run_turn(thread_id, "失败的补充信息")
            self.assertTrue(agent.snapshot(thread_id).next)
            self.assertEqual(agent.load_session(thread_id), before)
        self.model.side_effect = demo_agent_memory.demo_model
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            self.assertEqual(agent.load_session(thread_id), before)
            result = agent.run_turn(thread_id, "重新补充：版本2.0")
            self.assertIn("原问题：无法激活", result["draft"]["description"])
            self.assertIn("重新补充：版本2.0", result["draft"]["description"])
            self.assertNotIn("失败的补充信息", result["draft"]["description"])
            self.assertEqual(len(agent.load_session(thread_id).history), 4)
            self.assertFalse(agent.snapshot(thread_id).next)

    def test_failed_draft_update_does_not_invalidate_previous_confirmation(self):
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            thread_id = agent.new_session()
            agent.run_turn(thread_id, "旧草稿内容")
            old = agent.load_session(thread_id)
            self.model.side_effect = httpx.ReadTimeout("模拟超时")
            with self.assertRaises(httpx.TimeoutException):
                agent.run_turn(thread_id, "没有成功的新消息")
            result = agent.confirm(thread_id, old.draft_token)
            self.assertEqual(ticket_service.find_ticket(result["ticket_id"])["description"], "旧草稿内容")
            self.assertFalse(agent.snapshot(thread_id).next)

    def test_checkpoint_failure_after_ticket_commit_can_retry_without_duplicate(self):
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            thread_id = agent.new_session()
            agent.run_turn(thread_id, "激活失败")
            token = agent.load_session(thread_id).draft_token
            with patch.object(agent.graph, "update_state", side_effect=sqlite3.OperationalError("模拟写入失败")):
                with self.assertRaises(agent_persistent.ConfirmationStateError):
                    agent.confirm(thread_id, token)
            self.assertEqual(len(ticket_service.list_tickets()), 1)
            self.assertIsNone(agent.load_session(thread_id).saved_ticket_id)
            agent.confirm(thread_id, token)
            self.assertEqual(len(ticket_service.list_tickets()), 1)
            self.assertIsNotNone(agent.load_session(thread_id).saved_ticket_id)

    def test_local_cli_commands_work_without_model(self):
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            agent = agent_persistent.PersistentAgent(saver)
            with patch("builtins.input", side_effect=["/new", "/sessions", "/draft", "/history", "exit"]):
                with redirect_stdout(io.StringIO()):
                    agent_persistent.run_cli(agent)
            self.assertEqual(len(agent.list_sessions()), 1)
        self.model.assert_not_called()

    def test_offline_demo_restores_and_confirms_across_two_python_processes(self):
        # 子进程只使用已明确替换的模拟模型，演示数据库也位于临时文件夹。
        import_paths = [path for path in sys.path if path]
        if str(Path(agent_persistent.__file__).parent) not in import_paths:
            import_paths.insert(0, str(Path(agent_persistent.__file__).parent))
        code = ("import sys; sys.path[:0] = " + repr(import_paths) + "; "
                "from pathlib import Path; import demo_agent_memory; "
                "demo_agent_memory.ROOT = Path(" + repr(str(self.root)) + "); "
                "demo_agent_memory.main()")
        command = [sys.executable, "-B", "-X", "utf8", "-c", code]
        first = subprocess.run(command, input="/new\n离线演示：升级后无法激活\nexit\n",
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr)
        thread_id = re.search(r"新会话编号：\s*([0-9a-f]{32})", first.stdout).group(1)
        token = re.search(r"/confirm ([0-9a-f]{32})", first.stdout).group(1)
        second = subprocess.run(command,
            input=f"/resume {thread_id}\n/draft\n/confirm {token}\n/confirm {token}\nexit\n",
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("离线演示：升级后无法激活", second.stdout)
        self.assertIn("未重复创建", second.stdout)
        with closing(sqlite3.connect(self.root / "tickets_demo.db")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
        self.assertFalse((self.root / "tickets.db").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
