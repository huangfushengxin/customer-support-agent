from ticket_service import (
    DB_PATH,
    init_db,
    create_ticket,
    find_ticket,
    list_tickets,
    update_ticket_status,
)


def show_ticket(ticket):
    """显示一张工单。"""
    print("\n工单信息：")
    print("编号：", ticket["id"])
    print("描述：", ticket["description"])
    print("类别：", ticket["category"])
    print("状态：", ticket["status"])


def main():
    init_db()

    print("数据库位置：", DB_PATH)

    while True:
        print("\n===== 工单管理 =====")
        print("1. 创建工单")
        print("2. 查看全部工单")
        print("3. 按编号查询")
        print("4. 修改工单状态")
        print("5. 退出")

        choice = input("请选择操作：").strip()

        if choice == "1":
            description = input("请输入问题描述：")

            try:
                ticket_id = create_ticket(description)
            except ValueError as error:
                print("创建失败：", error)
            else:
                print("创建成功，编号：", ticket_id)

        elif choice == "2":
            tickets = list_tickets()

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

            ticket = find_ticket(ticket_id)

            if ticket is None:
                print("未找到该工单")
            else:
                show_ticket(ticket)

        elif choice == "4":
            try:
                ticket_id = int(input("请输入要修改的工单编号："))
            except ValueError:
                print("编号必须是整数")
                continue

            new_status = input(
                "请输入新状态（待处理/处理中/已完成）："
            )

            try:
                updated = update_ticket_status(ticket_id, new_status)
            except ValueError as error:
                print("修改失败：", error)
            else:
                if updated:
                    print("状态已更新")
                else:
                    print("未找到该工单")

        elif choice == "5":
            print("程序已退出")
            break

        else:
            print("请输入 1 到 5")


if __name__ == "__main__":
    main()