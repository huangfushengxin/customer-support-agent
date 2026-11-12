description = input("请输入问题描述：").strip()

if description == "":
    print("问题描述不能为空")
else:
    ticket = {
        "id": 1,
        "description": description,
        "category": "未分类",
        "status": "待处理",
    }

    print("\n工单已创建：")
    print("编号：", ticket["id"])
    print("描述：", ticket["description"])
    print("类别：", ticket["category"])
    print("状态：", ticket["status"])