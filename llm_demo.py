import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


# 明确读取当前文件夹中的 .env
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)


def generate_ticket_title(description):
    """调用 DeepSeek，将问题描述整理成工单标题。"""
    description = description.strip()

    if not description:
        raise ValueError("问题描述不能为空")

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()

    if not api_key:
        raise ValueError("未找到密钥，请检查项目中的 .env 文件")

    # 请求头：告诉平台我们是谁，以及发送的数据格式
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 请求体：模型名称、任务要求和用户的问题
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是工单整理助手。"
                    "请根据用户描述生成一个简短的中文工单标题。"
                    "只输出标题，不要解释，不要添加描述中没有的事实。"
                ),
            },
            {
                "role": "user",
                "content": description,
            },
        ],
        "thinking": {"type": "disabled"},
        "max_tokens": 128,
        "stream": False,
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            "https://api.deepseek.com/chat/completions",
            headers=headers,
            json=payload,
        )

        # 如果平台返回错误状态码，报告异常
        response.raise_for_status()
        data = response.json()

    title = data["choices"][0]["message"]["content"]

    if not isinstance(title, str) or not title.strip():
        raise ValueError("模型没有返回有效标题")

    return title.strip()


def main():
    description = input("请输入问题描述：")

    try:
        print("正在生成标题……")
        title = generate_ticket_title(description)
    except httpx.TimeoutException:
        print("请求超时，请稍后重试")
    except httpx.HTTPStatusError as error:
        status = error.response.status_code

        messages = {
            401: "密钥验证失败，请检查 API Key",
            402: "账户余额不足，请检查 DeepSeek API 账户",
            429: "请求受到限流，请稍后重试",
        }

        print(messages.get(status, f"平台返回错误，状态码：{status}"))
    except httpx.RequestError:
        print("网络连接失败，请检查网络后重试")
    except (KeyError, IndexError, TypeError):
        print("返回数据格式与预期不符")
    except ValueError as error:
        print("生成失败：", error)
    else:
        print("生成的工单标题：", title)


if __name__ == "__main__":
    main()