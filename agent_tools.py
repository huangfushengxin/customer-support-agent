"""第五阶段第一步：工具定义、参数校验和受控执行。"""
import json

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import knowledge_service
import llm_service


class KnowledgeArguments(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True, extra="forbid", strict=True
    )
    question: str = Field(min_length=1, max_length=2000)


class DraftArguments(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True, extra="forbid", strict=True
    )
    description: str = Field(min_length=1, max_length=2000)


def search_knowledge_tool(question):
    """读取 FAQ，返回资料和来源，不调用模型、不创建工单。"""
    documents = knowledge_service.search_knowledge(question, limit=3)
    return {
        "status": "found" if documents else "no_match",
        "documents": documents,
    }


def prepare_ticket_draft_tool(description):
    """调用已有模型函数整理草稿，返回待补充信息，不写入数据库。"""
    draft = llm_service.generate_ticket_draft(description)
    return {
        "status": "draft_ready",
        "draft": draft.model_dump(),
        "saved": False,
    }


# 白名单：程序只允许执行这里登记的工具。
TOOL_REGISTRY = {
    "search_knowledge": {
        "description": (
            "根据用户问题检索产品 FAQ，返回相关资料和来源。"
            "涉及产品规则或排查建议时先查询；无结果时不能编造产品信息。"
        ),
        "arguments_model": KnowledgeArguments,
        "handler": search_knowledge_tool,
    },
    "prepare_ticket_draft": {
        "description": (
            "用户希望整理工单草稿时，根据其已提供的事实生成草稿。"
            "不创建正式工单；草稿包含 missing_info，可能需要继续追问。"
            "不能编造用户未提供的细节，也不能声称已经提交。"
        ),
        "arguments_model": DraftArguments,
        "handler": prepare_ticket_draft_tool,
    },
}


# 告诉模型：有哪些工具、工具的用途、参数需要什么结构。
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": definition["description"],
            "parameters": definition["arguments_model"].model_json_schema(),
        },
    }
    for name, definition in TOOL_REGISTRY.items()
]


def execute_tool(tool_name, arguments):
    """先检查工具白名单和参数，再执行已有 Python 函数。"""
    if not isinstance(tool_name, str) or tool_name not in TOOL_REGISTRY:
        raise ValueError("不支持的工具名称")

    definition = TOOL_REGISTRY[tool_name]
    validated = definition["arguments_model"].model_validate(arguments)
    return definition["handler"](**validated.model_dump())


def main():
    print("1. 检索知识库（不调用模型）")
    print("2. 整理工单草稿（调用 DeepSeek，消耗额度）")
    choice = input("请选择工具：").strip()

    if choice == "1":
        name = "search_knowledge"
        arguments = {"question": input("请输入问题：")}
    elif choice == "2":
        name = "prepare_ticket_draft"
        arguments = {"description": input("请输入问题描述：")}
    else:
        print("请输入1或2")
        return

    try:
        print("正在执行工具：", name)
        result = execute_tool(name, arguments)
    except httpx.TimeoutException:
        print("模型请求超时，请稍后重试")
    except httpx.HTTPStatusError as error:
        print("模型平台请求失败，状态码：", error.response.status_code)
    except httpx.RequestError:
        print("无法连接模型平台，请检查网络")
    except ValidationError:
        print("输入参数或模型草稿格式不正确，请检查内容")
    except OSError:
        print("无法读取知识库，请检查 knowledge/faq.json")
    except (KeyError, IndexError, TypeError, ValueError):
        print("工具执行失败，请检查参数、资料格式或后端配置")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
