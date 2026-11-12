# 定义函数：负责创建工单
def create_ticket(ticket_id, description):
    description = description.strip()

    if description == "":
        raise ValueError("问题描述不能为空")

    ticket = {
        "id": ticket_id,
        "description": description,
        "category": "未分类",
        "status": "待处理",
    }

    return ticket


# 定义函数：负责显示工单
def show_ticket(ticket):
    print("\n工单信息：")
    print("编号：", ticket["id"])
    print("描述：", ticket["description"])
    print("类别：", ticket["category"])
    print("状态：", ticket["status"])


# 主程序：接收输入，再调用函数
description = input("请输入问题描述：")

try:
    ticket = create_ticket(1, description)
    show_ticket(ticket)
except ValueError as error:
    print("创建失败：", error)
