def create_ticket(ticket_id, description):
    description = description.strip()

    if description == "":
        raise ValueError("问题描述不能为空")

    return {
        "id": ticket_id,
        "description": description,
        "category": "未分类",
        "status": "待处理",
    }


def show_ticket(ticket):
    print("\n工单信息：")
    print("编号：", ticket["id"])
    print("描述：", ticket["description"])
    print("类别：", ticket["category"])
    print("状态：", ticket["status"])


# 遍历列表，查找编号对应的工单
def find_ticket(tickets, ticket_id):
    for ticket in tickets:
        if ticket["id"] == ticket_id:
            return ticket

    # 遍历完仍未找到
    return None


def main():
    # 保存本次运行期间创建的所有工单
    tickets = []

    # 下一张工单的编号
    next_id = 1

    while True:
        print("\n===== 工单管理 =====")
        print("1. 创建工单")
        print("2. 查看全部工单")
        print("3. 按编号查询")
        print("4. 退出")

        choice = input("请选择操作：").strip()

        if choice == "1":
            description = input("请输入问题描述：")

            try:
                ticket = create_ticket(next_id, description)
            except ValueError as error:
                print("创建失败：", error)
            else:
                tickets.append(ticket)
                next_id += 1
                print("创建成功，编号：", ticket["id"])

        elif choice == "2":
            if not tickets:
                print("暂无工单")
            else:
                for ticket in tickets:
                    show_ticket(ticket)

        elif choice == "3":
            try:
                ticket_id = int(input("请输入工单编号："))
            except ValueError:
                print("编号必须是整数")
                continue

            ticket = find_ticket(tickets, ticket_id)

            if ticket is None:
                print("未找到该工单")
            else:
                show_ticket(ticket)

        elif choice == "4":
            print("程序已退出")
            break

        else:
            print("请输入 1 到 4")


if __name__ == "__main__":
    main()