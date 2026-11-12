"""持久会话入口：SQLite 检查点、恢复草稿、人工确认、防重复保存。"""
import copy
import json
import os
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError
from langgraph.checkpoint.sqlite import SqliteSaver

import agent_graph
import agent_service
from agent_observability import observe


DB_PATH = Path(os.getenv("AGENT_DATA_DIR") or Path(__file__).resolve().parent) / "agent_sessions.db"


class ConfirmationStateError(RuntimeError):
    """工单已入库，但检查点写入失败，需要用原编号重试确认。"""


class PersistentAgent:
    """面向单个本地使用者的会话管理，恢复和查看都不调用模型。"""
    def __init__(self, checkpointer, *, log_path=None):
        self.checkpointer = checkpointer
        self.graph = agent_graph.build_graph(checkpointer=checkpointer)
        self.log_path = log_path

    @staticmethod
    def config(thread_id):
        if not isinstance(thread_id, str) or not re.fullmatch(r"[0-9a-f]{32}", thread_id):
            raise ValueError("会话编号格式不正确，请复制 /sessions 中的完整编号")
        return {"configurable": {"thread_id": thread_id}}

    def new_session(self):
        thread_id = uuid4().hex
        self.graph.update_state(self.config(thread_id), {
            "session": asdict(agent_service.AgentSession()), "messages": [],
            "user_text": "", "prepared": [], "documents": {}, "draft": None,
            "trace": [], "flow_nodes": [], "result": {}, "draft_token": None,
        }, as_node="reply")
        return thread_id

    def snapshot(self, thread_id):
        snapshot = self.graph.get_state(self.config(thread_id))
        if not snapshot.values or "session" not in snapshot.values:
            raise ValueError("没有找到该会话，请用 /sessions 查看已有编号")
        return snapshot

    def load_session(self, thread_id):
        return agent_service.AgentSession(**copy.deepcopy(self.snapshot(thread_id).values["session"]))

    def list_sessions(self):
        records, seen = [], set()
        # SqliteSaver 的 list 迭代器持有数据库锁，先收集编号并结束迭代，
        # 再 get_state，避免同一连接在持锁期间再次读取而阻塞。
        configs = [item.config["configurable"] for item in self.checkpointer.list(None)]
        for config in configs:
            thread_id = config["thread_id"]
            if config.get("checkpoint_ns", "") or thread_id in seen:
                continue
            seen.add(thread_id)
            if not re.fullmatch(r"[0-9a-f]{32}", thread_id):
                continue
            snapshot = self.snapshot(thread_id)
            session = snapshot.values["session"]
            records.append({"thread_id": thread_id, "turns": len(session["history"]) // 2,
                            "has_draft": session["draft"] is not None,
                            "ticket_id": session["saved_ticket_id"],
                            "incomplete": bool(snapshot.next)})
        return records

    def run_turn(self, thread_id, user_text):
        with observe(self.log_path, "turn", thread_id) as event:
            result = self._run_turn(thread_id, user_text)
            event.update(
                status=result["status"],
                flow_nodes=result["flow_nodes"],
                tools=[item["name"] for item in result["tool_trace"]],
                source_count=len(result["sources"]),
                has_draft=result["draft"] is not None,
            )
            return result

    def _run_turn(self, thread_id, user_text):
        snapshot = self.snapshot(thread_id)
        session = self.load_session(thread_id)
        initial = agent_service.prepare_turn(user_text, session)
        # 每次新输入都清空本轮结果，不能把上轮来源或草稿当成新结果。
        initial.update({"result": {}, "draft_token": None})
        if snapshot.next:
            # 本步不自动重放收费请求。放弃未完成的一轮，保留最后成功的会话，
            # 由用户重新输入；原检查点仍可用于排查。
            self.graph.update_state(self.config(thread_id), initial, as_node="reply")
        completed = self.graph.invoke(initial, self.config(thread_id), durability="sync")
        return {**completed["result"], "flow_nodes": completed["flow_nodes"]}

    def confirm(self, thread_id, token):
        with observe(self.log_path, "confirm", thread_id) as event:
            try:
                result = self._confirm(thread_id, token)
            except ConfirmationStateError:
                event["error_type"] = "confirmation_state_error"
                event["ticket_committed"] = True
                raise
            event.update(ticket_id=result["ticket_id"], already_saved=result["already_saved"])
            return result

    def _confirm(self, thread_id, token):
        session = self.load_session(thread_id)
        result = agent_service.confirm_ticket(session, token)
        try:
            self.graph.update_state(self.config(thread_id),
                                    {"session": asdict(session)}, as_node="reply")
        except (sqlite3.Error, OSError) as error:
            raise ConfirmationStateError(
                f"工单已保存，编号 {result['ticket_id']}，但会话状态未更新。"
                "请重试原来的 /confirm 命令，提交编号会防止重复创建。"
            ) from error
        return result


def show_sessions(agent):
    records = agent.list_sessions()
    if not records:
        print("还没有保存的会话，输入 /new 创建。")
    for record in records:
        status = (f"已保存工单 {record['ticket_id']}" if record["ticket_id"] is not None
                  else "有未确认草稿" if record["has_draft"] else "尚无草稿")
        print(f"{record['thread_id']} | {record['turns']}轮 | {status}")


def show_draft(session):
    if session.draft is None:
        print("当前没有草稿。")
        return
    print(json.dumps(session.draft, ensure_ascii=False, indent=2))
    if session.saved_ticket_id is not None:
        print("已保存，工单编号：", session.saved_ticket_id)
    else:
        print("核对草稿后保存：/confirm " + session.draft_token)


def run_cli(agent):
    current_id = None
    print("会话恢复版：普通消息会调用模型；会话管理、查看和确认不调用模型。")
    print("/new 新会话；/sessions 列表；/resume 编号 恢复；/draft 草稿；/history 历史；exit 退出。")
    print("会话和草稿会自动保存，正式工单仍需 /confirm 编号 确认。")
    show_sessions(agent)
    while True:
        try:
            user_text = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出；已有检查点保留。")
            break
        if user_text.lower() in ("exit", "退出"):
            print("已退出，下次可用 /sessions 和 /resume 恢复。")
            break
        if not user_text:
            continue
        try:
            parts = user_text.split()
            command = parts[0]
            if user_text == "/new":
                current_id = agent.new_session()
                print("新会话编号：", current_id)
                print("旧会话仍保留，可用 /resume 恢复。")
            elif user_text == "/sessions":
                show_sessions(agent)
            elif command == "/resume":
                if len(parts) != 2:
                    raise ValueError("格式：/resume 会话编号")
                snapshot = agent.snapshot(parts[1])
                current_id = parts[1]
                print("已恢复会话：", current_id)
                if snapshot.next:
                    print("上次消息未完成，已取回最后成功的会话。不会自动重试，请重新输入该条消息。")
                show_draft(agent.load_session(current_id))
            elif current_id is None:
                print("请先输入 /new，或用 /resume 会话编号 恢复已有会话。")
            elif user_text == "/draft":
                show_draft(agent.load_session(current_id))
            elif user_text == "/history":
                history = agent.load_session(current_id).history
                if not history:
                    print("当前会话还没有成功完成的对话。")
                for message in history:
                    text = message["content"]
                    if message["role"] == "assistant":
                        text = json.loads(text)["reply"]
                    print(("你：" if message["role"] == "user" else "Agent：") + text)
            elif command == "/confirm":
                if len(parts) != 2:
                    raise ValueError("请用 /draft 核对草稿，并复制完整 /confirm 编号 命令")
                result = agent.confirm(current_id, parts[1])
                print("这份草稿已保存，未重复创建。" if result["already_saved"] else "工单保存成功。")
                print("工单编号：", result["ticket_id"])
            elif user_text.startswith("/"):
                print("未知命令，请使用 /new、/sessions、/resume、/draft、/history 或 /confirm。")
            else:
                result = agent.run_turn(current_id, user_text)
                print("流程节点：" + " → ".join(result["flow_nodes"]))
                print("Agent：" + result["reply"])
                for source in result["sources"]:
                    print(f"来源：[{source['id']}] {source['source']}")
                if result["draft"] is not None:
                    show_draft(agent.load_session(current_id))
                print("会话已保存：", current_id)
        except httpx.TimeoutException:
            print("模型请求超时。最后成功的会话已保留，本条输入未记入成功历史。")
        except httpx.HTTPStatusError as error:
            print("模型平台请求失败，状态码：", error.response.status_code)
        except httpx.RequestError:
            print("无法连接模型平台，最后成功的会话已保留。")
        except ConfirmationStateError as error:
            print(error)
        except ValidationError:
            print("工具参数或模型回复格式不正确，本轮未提交。最后成功的会话和草稿已保留。")
        except (sqlite3.Error, OSError):
            print("数据库读写失败，请检查文件路径和占用情况；确认保存失败时请重试原确认命令。")
        except ValueError as error:
            print(error)
        except (KeyError, IndexError, TypeError):
            print("返回数据格式不正确。最后成功的会话已保留。")


def main(*, db_path=DB_PATH):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = db_path.parent / "logs" / (db_path.stem + ".jsonl")
    print("运行日志：", log_path)
    with SqliteSaver.from_conn_string(str(db_path)) as checkpointer:
        run_cli(PersistentAgent(checkpointer, log_path=log_path))


if __name__ == "__main__":
    main()
