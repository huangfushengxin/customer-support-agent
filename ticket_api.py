from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi.responses import FileResponse
import httpx
from pydantic import ValidationError
import rag_service
import llm_service
from fastapi import FastAPI, HTTPException

import ticket_service
import agent_api


@asynccontextmanager
async def lifespan(_application: FastAPI):
    # 服务启动时检查并创建表，已有数据会保留
    ticket_service.init_db()
    yield


app = FastAPI(
    title="工单管理系统",
    lifespan=lifespan,
)
app.include_router(agent_api.router)


@app.get("/agent-page", response_class=FileResponse)
def get_agent_page():
    return FileResponse(Path(__file__).resolve().parent / "agent.html", media_type="text/html")


class TicketCreate(BaseModel):
    description: str
class TicketStatusUpdate(BaseModel):
    status: Literal["待处理", "处理中", "已完成"]
@app.get("/")
def home():
    return {"message": "工单接口已启动"}


@app.get("/tickets")
def get_tickets():
    # 调用第一阶段写好的函数
    return ticket_service.list_tickets()


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int):
    ticket = ticket_service.find_ticket(ticket_id)

    if ticket is None:
        raise HTTPException(
            status_code=404,
            detail="未找到该工单",
        )

    return ticket
@app.post("/tickets", status_code=201)
def create_ticket_api(data: TicketCreate):
    try:
        ticket_id = ticket_service.create_ticket(
            data.description
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return ticket_service.find_ticket(ticket_id)
@app.patch("/tickets/{ticket_id}/status")
def update_ticket_status_api(
    ticket_id: int,
    data: TicketStatusUpdate,
):
    updated = ticket_service.update_ticket_status(
        ticket_id,
        data.status,
    )

    if not updated:
        raise HTTPException(
            status_code=404,
            detail="未找到该工单",
        )

    return ticket_service.find_ticket(ticket_id)
@app.get("/page", response_class=FileResponse)
def get_page():
    html_path = Path(__file__).resolve().parent / "index.html"
    return FileResponse(html_path, media_type="text/html")
@app.post(
    "/ticket-drafts",
    response_model=llm_service.TicketDraft,
)
def generate_ticket_draft_api(data: TicketCreate):
    description = data.description.strip()

    if not description:
        raise HTTPException(
            status_code=400,
            detail="问题描述不能为空",
        )

    try:
        draft = llm_service.generate_ticket_draft(description)
    except httpx.TimeoutException as error:
        raise HTTPException(
            status_code=504,
            detail="AI 整理超时，请稍后重试，也可以手动创建工单",
        ) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=502,
            detail="模型平台请求失败，请检查后端配置和平台账户",
        ) from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail="暂时无法连接模型平台，请稍后重试",
        ) from error
    except ValidationError as error:
        raise HTTPException(
            status_code=502,
            detail="AI 返回的草稿格式不正确，请重试或手动填写",
        ) from error
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=502,
            detail="AI 草稿生成失败，请检查后端配置或稍后重试",
        ) from error

    return draft
class TicketDraftSave(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
    )

    title: str = Field(min_length=1, max_length=60)
    category: Literal[
        "账号问题",
        "激活问题",
        "订单问题",
        "其他问题",
    ]
    description: str = Field(min_length=1, max_length=2000)


@app.post("/tickets/from-draft", status_code=201)
def save_ticket_draft_api(data: TicketDraftSave):
    """保存用户核对后的草稿，不再调用模型。"""
    try:
        ticket_id = ticket_service.create_ticket(
            data.description,
            title=data.title,
            category=data.category,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return ticket_service.find_ticket(ticket_id)
class KnowledgeQuestion(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
    )

    question: str = Field(min_length=1, max_length=2000)


@app.post("/knowledge/ask")
def ask_knowledge_api(data: KnowledgeQuestion):
    """根据知识库回答问题，不创建工单。"""
    try:
        return rag_service.answer_question(data.question)

    except httpx.TimeoutException as error:
        raise HTTPException(
            status_code=504,
            detail="AI 回答超时，请稍后重试",
        ) from error

    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=502,
            detail="模型平台请求失败，请检查配置和平台账户",
        ) from error

    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail="无法连接模型平台，请稍后重试",
        ) from error

    except OSError as error:
        raise HTTPException(
            status_code=500,
            detail="无法读取知识库，请检查知识库文件",
        ) from error

    except (
        ValidationError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ) as error:
        raise HTTPException(
            status_code=502,
            detail="问答处理失败，请检查知识库格式、模型返回或后端配置",
        ) from error
