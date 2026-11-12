"""本地单进程网页 Agent：复用 SQLite 会话、版本确认与运行日志。"""
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Path
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import agent_persistent
import agent_service


router = APIRouter(prefix="/agent", tags=["Agent 对话"])
ThreadId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
# 避免同一网页进程的请求交错覆盖会话。当前部署只启动一个 worker。
_lock = threading.Lock()


class MessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=2000)


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    draft_token: str = Field(pattern=r"^[0-9a-f]{32}$")


@contextmanager
def connection():
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "Agent 正在处理请求，请稍后再操作。")
    try:
        path = agent_persistent.DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteSaver.from_conn_string(str(path)) as saver:
            yield agent_persistent.PersistentAgent(saver, log_path=path.parent/"logs"/(path.stem+".jsonl"))
    except HTTPException:
        raise
    except httpx.TimeoutException as error:
        raise HTTPException(504, "模型请求超时，最后成功的对话和草稿已保留。请查看后再决定是否重新发送。") from error
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        raise HTTPException(502, "暂时无法完成模型请求，最后成功的对话和草稿已保留。") from error
    except agent_persistent.ConfirmationStateError as error:
        raise HTTPException(503, str(error)) from error
    except agent_service.SessionClosedError as error:
        raise HTTPException(409, "这张工单已保存，请点击“新会话”开始新问题。") from error
    except agent_service.SessionLimitError as error:
        raise HTTPException(409, "本会话已达到长度限制，请点击“新会话”并提供精简的完整描述。") from error
    except (agent_service.ReplyFormatError, ValidationError, KeyError, IndexError, TypeError, ValueError) as error:
        raise HTTPException(502, "模型或工具返回格式不符合要求，本轮未提交；最后成功的对话和草稿已保留。") from error
    except (sqlite3.Error, OSError) as error:
        raise HTTPException(503, "会话或工单存储暂时不可用，请稍后刷新查看；请勿重复生成草稿来重试保存。") from error
    finally:
        _lock.release()


def require_session(agent, thread_id):
    try:
        return agent.snapshot(thread_id)
    except ValueError as error:
        raise HTTPException(404, "没有找到这个会话，请刷新会话列表。") from error


def reply_text(text):
    return re.sub(r"\n确认按这份草稿创建正式工单时，请输入：\n/confirm [0-9a-f]{32}",
                  "\n请在右侧草稿区域核对并确认保存。", text)


def public_session(agent, thread_id):
    snapshot = require_session(agent, thread_id)
    session = agent.load_session(thread_id)
    history = []
    for message in session.history:
        content, sources = message["content"], []
        if message["role"] == "assistant":
            try:
                stored = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                stored = None
            if isinstance(stored, dict) and isinstance(stored.get("reply"), str):
                content = stored["reply"]
                sources = stored.get("sources", [])
            content = reply_text(content)
        history.append({"role": message["role"], "content": content, "sources": sources})
    return {"thread_id": thread_id, "history": history, "draft": session.draft,
            "draft_token": session.draft_token, "saved_ticket_id": session.saved_ticket_id,
            "incomplete": bool(snapshot.next)}


@router.get("/sessions")
def list_sessions():
    with connection() as agent:
        return {"sessions": agent.list_sessions()}


@router.post("/sessions", status_code=201)
def create_session():
    with connection() as agent:
        return public_session(agent, agent.new_session())


@router.get("/sessions/{thread_id}")
def get_session(thread_id: ThreadId):
    with connection() as agent:
        return public_session(agent, thread_id)


@router.post("/sessions/{thread_id}/messages")
def send_message(thread_id: ThreadId, data: MessageInput):
    with connection() as agent:
        require_session(agent, thread_id)
        agent.run_turn(thread_id, data.message)
        return public_session(agent, thread_id)


@router.post("/sessions/{thread_id}/confirm")
def confirm_draft(thread_id: ThreadId, data: ConfirmInput):
    with connection() as agent:
        require_session(agent, thread_id)
        try:
            result = agent.confirm(thread_id, data.draft_token)
        except ValidationError:
            raise
        except ValueError as error:
            raise HTTPException(409, "当前草稿与确认编号不匹配，或没有待确认草稿。请刷新并核对最新草稿。") from error
        return {**public_session(agent, thread_id), "confirmation": result}
