import json
from pathlib import Path


KNOWLEDGE_PATH = (
    Path(__file__).resolve().parent
    / "knowledge"
    / "faq.json"
)


def load_knowledge():
    """读取本地知识库。"""
    with KNOWLEDGE_PATH.open(encoding="utf-8") as file:
        return json.load(file)


def search_knowledge(question, limit=3):
    """通过关键词匹配，返回相关资料。"""
    question = question.strip().casefold()

    if not question:
        raise ValueError("问题不能为空")

    if limit <= 0:
        raise ValueError("返回数量必须大于0")

    documents = load_knowledge()
    results = []

    for document in documents:
        matched_keywords = [
            keyword
            for keyword in document["keywords"]
            if keyword.casefold() in question
        ]

        # 没有匹配到关键词，就不返回这条资料
        if not matched_keywords:
            continue

        results.append({
            "id": document["id"],
            "title": document["title"],
            "content": document["content"],
            "source": document["source"],
            "score": len(matched_keywords),
            "matched_keywords": matched_keywords,
        })

    # 匹配关键词越多，排序越靠前
    results.sort(
        key=lambda item: (-item["score"], item["id"])
    )

    return results[:limit]


def main():
    question = input("请输入问题：")

    try:
        results = search_knowledge(question)
    except ValueError as error:
        print("输入错误：", error)
        return

    if not results:
        print("知识库中没有找到相关资料。")
        return

    print("\n找到以下资料：")

    for item in results:
        print("\n编号：", item["id"])
        print("标题：", item["title"])
        print("内容：", item["content"])
        print("来源：", item["source"])
        print("匹配关键词：", "、".join(item["matched_keywords"]))


if __name__ == "__main__":
    main()