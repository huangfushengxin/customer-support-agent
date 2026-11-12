"""默认只评测检索；添加 --live 后才调用真实模型。"""
import argparse
import json
from datetime import datetime
from pathlib import Path
from time import perf_counter

import httpx

from knowledge_service import search_knowledge


BASE_DIR = Path(__file__).resolve().parent


def check_case(case, retrieved_ids, result, error, live):
    """只检查可机械判断的规则，不判断回答内容是否忠于资料。"""
    checks = {
        "retrieval_ok": set(retrieved_ids) == set(case["expected_document_ids"])
    }
    if live:
        checks["status_ok"] = (
            error is None and result is not None
            and result["status"] in case["expected_statuses"]
        )
        if result is None or error is not None:
            checks["citations_ok"] = False
        else:
            cited_ids = {item["id"] for item in result["sources"]}
            if result["status"] == "answered":
                checks["citations_ok"] = (
                    bool(cited_ids)
                    and cited_ids.issubset(set(retrieved_ids))
                    and set(case["required_citation_ids"]).issubset(cited_ids)
                )
            else:
                checks["citations_ok"] = not cited_ids
    return checks


def run_cases(cases, answer_fn=None):
    rows = []
    for case in cases:
        started = perf_counter()
        retrieved_ids = []
        result = None
        error = None
        retrieval_succeeded = False
        try:
            retrieved_ids = [
                item["id"] for item in search_knowledge(case["question"])
            ]
            retrieval_succeeded = True
            if answer_fn is not None:
                # 每题调用一次，不自动重试；无匹配时问答函数不会请求模型。
                result = answer_fn(case["question"])
        except (httpx.HTTPError, OSError, ValueError, KeyError,
                IndexError, TypeError) as exception:
            # 不把原始请求、密钥或错误响应正文写进报告。
            error = type(exception).__name__
        checks = check_case(
            case, retrieved_ids, result, error, answer_fn is not None
        )
        checks["retrieval_ok"] = checks["retrieval_ok"] and retrieval_succeeded
        rows.append({
            **case,
            "retrieved_ids": retrieved_ids,
            "result": result,
            "error": error,
            "checks": checks,
            "seconds": round(perf_counter() - started, 2),
        })
        verdict = "PASS" if all(checks.values()) and error is None else "FAIL"
        print(f"{case['id']}: {verdict}", flush=True)
    return rows


def write_reports(rows, output_dir, live):
    output_dir.mkdir(parents=True, exist_ok=True)
    mode = "live" if live else "retrieval"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = output_dir / f"knowledge_eval_{stamp}_{mode}"
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    json_path.write_text(
        json.dumps({"mode": mode, "cases": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# 知识库问答评测", "",
        f"模式：{mode}；题目数：{len(rows)}", "",
        "自动检查只验证检索、状态及来源编号，不能代表回答内容正确。",
        "请逐题人工判断相关性、事实依据和是否编造产品规则。", "",
    ]
    for name in ("retrieval_ok", "status_ok", "citations_ok"):
        measured = [row["checks"][name] for row in rows if name in row["checks"]]
        if measured:
            lines.append(f"- {name}：{sum(measured)}/{len(measured)}")
    for row in rows:
        lines.extend([
            "", f"## {row['id']}：{row['question']}", "",
            f"期望资料：{', '.join(row['expected_document_ids']) or '无'}",
            f"实际检索：{', '.join(row['retrieved_ids']) or '无'}",
            f"自动检查：{json.dumps(row['checks'], ensure_ascii=False)}",
            f"耗时：{row['seconds']} 秒", "",
        ])
        if row["error"]:
            lines.append(f"运行错误类型：{row['error']}")
        elif row["result"] is not None:
            result = row["result"]
            lines.extend([f"状态：{result['status']}", "", "回答：", "", result["answer"], ""])
            for source in result["sources"]:
                lines.append(f"- [{source['id']}] {source['source']}")
        else:
            lines.append("未调用模型，仅评测检索。")
        lines.extend([
            "", f"人工核对要点：{row['review_points']}", "",
            "人工结论：待填写（通过 / 不通过 / 检索失败无法评判回答）",
            "", "问题记录：待填写", "",
        ])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="调用真实模型，消耗 API 额度")
    args = parser.parse_args()
    cases = json.loads((BASE_DIR / "eval_cases.json").read_text(encoding="utf-8"))
    answer_fn = None
    if args.live:
        from rag_service import answer_question
        answer_fn = answer_question
        print("真实模型评测模式：会消耗 API 额度，不自动重试。", flush=True)
    else:
        print("仅检索评测模式：不会调用模型。", flush=True)
    rows = run_cases(cases, answer_fn)
    json_path, md_path = write_reports(rows, BASE_DIR / "eval_results", args.live)
    print("\nJSON 报告：", json_path)
    print("人工评阅表：", md_path)


if __name__ == "__main__":
    main()
