"""离线学习演示：模拟模型，真实执行 LangGraph 和 SQLite 检查点。"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import agent_persistent
import agent_service
import llm_service
import ticket_service


ROOT = Path(os.getenv("AGENT_DATA_DIR") or Path(__file__).resolve().parent)


def demo_model(messages, *, final=False):
    if final:
        return {"content": json.dumps({"status": "draft_ready", "reply": "离线演示草稿",
                                       "citation_ids": []}, ensure_ascii=False)}
    latest = next(message["content"] for message in reversed(messages)
                  if message["role"] == "user")
    return {"content": None, "tool_calls": [{
        "id": "offline_draft", "type": "function", "function": {
            "name": "prepare_ticket_draft",
            "arguments": json.dumps({"description": latest}, ensure_ascii=False),
        },
    }]}


def demo_draft(description):
    # 不生成虚构的版本、系统或处理结论；直接展示原始用户上下文。
    return llm_service.TicketDraft(title="离线演示草稿", category="其他问题",
                                   description=description, missing_info=[])


def main():
    print("【离线演示】模型回复为模拟数据，不访问 DeepSeek、不消耗 API 额度。")
    print("普通输入会整理为演示草稿，仅用于验证记忆和恢复。")
    print("会话：agent_sessions_demo.db；确认后的演示工单：tickets_demo.db。")
    with patch.object(agent_service, "request_model", side_effect=demo_model):
        with patch.object(llm_service, "generate_ticket_draft", side_effect=demo_draft):
            with patch.object(ticket_service, "DB_PATH", ROOT / "tickets_demo.db"):
                agent_persistent.main(db_path=ROOT / "agent_sessions_demo.db")


if __name__ == "__main__":
    main()
