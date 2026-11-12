"""用子进程验证数据目录配置，模拟 CLI 输入，不访问模型。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DataDirectoryTests(unittest.TestCase):
    def run_code(self, code, data_dir):
        env = os.environ.copy()
        if data_dir is None:
            env.pop("AGENT_DATA_DIR", None)
        else:
            env["AGENT_DATA_DIR"] = str(data_dir)
        env["PYTHONPATH"] = os.pathsep.join(str(path) for path in sys.path if path)
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code],
                                env=env, capture_output=True, text=True,
                                encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_configured_directory_is_created_and_data_survives_a_new_process(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "nested" / "data"
            code = '''
import agent_persistent, demo_agent_memory, ticket_service
from unittest.mock import patch
ticket_service.init_db()
ticket_service.create_ticket("容器持久化验收")
assert ticket_service.DB_PATH.parent == demo_agent_memory.ROOT
assert agent_persistent.DB_PATH.parent == demo_agent_memory.ROOT
with patch("builtins.input", side_effect=["/new", "exit"]):
    agent_persistent.main()
'''
            self.run_code(code, data_dir)
            self.assertTrue((data_dir / "tickets.db").exists())
            self.assertTrue((data_dir / "agent_sessions.db").exists())
            self.run_code('''
import ticket_service
import agent_persistent
from langgraph.checkpoint.sqlite import SqliteSaver
assert ticket_service.list_tickets()[0]["description"] == "容器持久化验收"
with SqliteSaver.from_conn_string(str(agent_persistent.DB_PATH)) as saver:
    assert len(agent_persistent.PersistentAgent(saver).list_sessions()) == 1
''', data_dir)

    def test_unset_directory_preserves_existing_local_paths(self):
        output = self.run_code('''
import json
from pathlib import Path
import ticket_service, agent_persistent, demo_agent_memory
print(json.dumps({"tickets": ticket_service.DB_PATH.parent == Path(ticket_service.__file__).parent,
                  "sessions": agent_persistent.DB_PATH.parent == Path(agent_persistent.__file__).parent,
                  "demo": demo_agent_memory.ROOT == Path(demo_agent_memory.__file__).parent}))
''', None)
        self.assertEqual(json.loads(output), {"tickets": True, "sessions": True, "demo": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
