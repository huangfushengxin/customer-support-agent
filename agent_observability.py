"""运行事件日志：一行一个 JSON，只记录明确允许的元数据。"""
import json
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx


def error_fields(error):
    # 不保存 str(error) 或 traceback，避免模型返回、请求头等进入日志。
    if isinstance(error, httpx.TimeoutException):
        return {"error_type": "model_timeout"}
    if isinstance(error, httpx.HTTPStatusError):
        return {"error_type": "model_http_error", "http_status": error.response.status_code}
    if isinstance(error, httpx.RequestError):
        return {"error_type": "model_connection_error"}
    if isinstance(error, (sqlite3.Error, OSError)):
        return {"error_type": "storage_error"}
    if isinstance(error, (ValueError, KeyError, IndexError, TypeError)):
        return {"error_type": "validation_error"}
    return {"error_type": "internal_error"}


def write_event(path, record):
    if path is None:
        return
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # 诊断日志故障不能把已经保存成功的工单变成失败。
        print("运行日志写入失败，请检查 logs 目录权限和磁盘空间。", file=sys.stderr)


@contextmanager
def observe(path, operation, thread_id):
    started = perf_counter()
    record = {
        "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "event_id": uuid4().hex,
        "operation": operation,
        "thread_id": thread_id if isinstance(thread_id, str)
        and re.fullmatch(r"[0-9a-f]{32}", thread_id) else None,
        "outcome": "success",
    }
    try:
        yield record
    except Exception as error:
        fields = error_fields(error)
        record.setdefault("error_type", fields.pop("error_type"))
        record.update(outcome="error", **fields)
        raise
    finally:
        record["duration_ms"] = round((perf_counter() - started) * 1000, 3)
        write_event(path, record)
