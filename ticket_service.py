import hashlib
import json
import os
import sqlite3
from pathlib import Path


# 本机默认保存在项目目录；容器可指定挂载的数据目录。
DB_PATH = Path(os.getenv("AGENT_DATA_DIR") or Path(__file__).resolve().parent) / "tickets.db"
def init_db():
    """创建工单表，并为旧数据库补充标题字段。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    try:
        with conn:
            # 新数据库直接创建包含 title 的表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL
                )
            """)

            # 获取当前表中已有的字段名称
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(tickets)"
                ).fetchall()
            }

            # 旧数据库没有 title 时，补充这个字段
            if "title" not in columns:
                conn.execute("""
                    ALTER TABLE tickets
                    ADD COLUMN title TEXT NOT NULL DEFAULT ''
                """)

            # 确认编号与正式工单的对应关系，和工单一起持久保存。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticket_submissions (
                    request_key TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id)
                )
            """)
    finally:
        conn.close()


def create_ticket(
    description,
    *,
    title="",
    category="未分类",
    request_key=None,
):
    """创建工单；同一 request_key 的相同内容只保存一次。"""
    description = description.strip()
    title = title.strip()
    category = category.strip()

    if description == "":
        raise ValueError("问题描述不能为空")

    if len(title) > 60:
        raise ValueError("标题不能超过60个字符")

    allowed_categories = [
        "未分类",
        "账号问题",
        "激活问题",
        "订单问题",
        "其他问题",
    ]

    if category not in allowed_categories:
        raise ValueError("工单类别不合法")

    if request_key is not None:
        if (not isinstance(request_key, str) or not request_key.strip()
                or len(request_key) > 128):
            raise ValueError("提交编号必须是1到128个字符的非空字符串")

    # 使用规范化后的实际入库字段，不能只凭标题判断重复。
    payload_hash = hashlib.sha256(json.dumps(
        [title, description, category], ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    conn = sqlite3.connect(DB_PATH)

    try:
        with conn:
            if request_key is not None:
                # 锁定本次写入事务，两个同时到达的确认也只能创建一张工单。
                conn.execute("BEGIN IMMEDIATE")
                submitted = conn.execute(
                    "SELECT payload_hash, ticket_id FROM ticket_submissions WHERE request_key = ?",
                    (request_key,),
                ).fetchone()
                if submitted is not None:
                    if submitted[0] != payload_hash:
                        raise ValueError("该提交编号已用于其他内容，请重新核对草稿")
                    return submitted[1]

            cursor = conn.execute(
                """
                INSERT INTO tickets (
                    title, description, category, status
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    title,
                    description,
                    category,
                    "待处理",
                ),
            )

            ticket_id = cursor.lastrowid
            if request_key is not None:
                # 工单与提交记录在同一事务提交，失败时一起回滚。
                conn.execute(
                    "INSERT INTO ticket_submissions (request_key, payload_hash, ticket_id) VALUES (?, ?, ?)",
                    (request_key, payload_hash, ticket_id),
                )

        return ticket_id
    finally:
        conn.close()


def find_ticket(ticket_id):
    """按编号查询工单，找不到时返回 None。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()

        if row is None:
            return None

        return dict(row)
    finally:
        conn.close()


def list_tickets():
    """查询全部工单，按编号排序。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY id"
        ).fetchall()

        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_ticket_status(ticket_id, new_status):
    """修改工单状态，返回是否匹配到工单。"""
    new_status = new_status.strip()
    allowed_statuses = ["待处理", "处理中", "已完成"]

    if new_status not in allowed_statuses:
        raise ValueError("状态只能是：待处理、处理中、已完成")

    conn = sqlite3.connect(DB_PATH)

    try:
        with conn:
            cursor = conn.execute(
                "UPDATE tickets SET status = ? WHERE id = ?",
                (new_status, ticket_id),
            )

            updated = cursor.rowcount > 0

        return updated
    finally:
        conn.close()
