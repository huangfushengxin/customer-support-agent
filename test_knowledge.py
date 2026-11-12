import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import knowledge_service
import rag_service
import ticket_service
from ticket_api import app


# 固定的测试资料，不修改项目中的真实 FAQ。
DOCUMENT = {
    "id": "FAQ-001",
    "title": "软件无法激活",
    "keywords": ["激活失败", "授权无效"],
    "content": "请检查激活码是否有多余空格，并检查网络连接。",
    "source": "测试资料：软件激活说明",
}


class KnowledgeSearchTests(unittest.TestCase):
    def test_activation_synonyms(self):
        """软件开通的常见表达应找到激活资料。"""
        questions = [
            "买过的软件一直开通不了，该怎么办？",
            "我的软件开通不了",
            "软件无法开通怎么办",
        ]

        for question in questions:
            with self.subTest(question=question):
                results = knowledge_service.search_knowledge(question)

                self.assertTrue(results)
                self.assertEqual(results[0]["id"], "FAQ-001")

    def test_unrelated_questions_do_not_match(self):
        """补充关键词后，无关问题仍不应命中激活资料。"""
        questions = [
            "会员开通不了怎么办",
            "银行卡无法开通怎么办",
            "软件支持深色主题吗",
        ]

        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(
                    knowledge_service.search_knowledge(question),
                    [],
                )
    def test_demo_faq_questions(self):
        """实际演示 FAQ 能检索到对应资料，未知问题不返回资料。"""
        cases = [
            ("软件激活不了，提示授权无效", "FAQ-001"),
            ("我忘记密码了怎么办", "FAQ-002"),
            ("找不到订单怎么办", "FAQ-003"),
            ("软件支持深色主题吗", None),
        ]
        for question, expected_id in cases:
            with self.subTest(question=question):
                results = knowledge_service.search_knowledge(question)
                if expected_id is None:
                    self.assertEqual(results, [])
                else:
                    self.assertTrue(results)
                    self.assertEqual(results[0]["id"], expected_id)

    def test_best_match_and_limit(self):
        """资料有多个匹配时，优先返回匹配更充分的资料。"""
        weaker = {
            **DOCUMENT,
            "id": "FAQ-OTHER",
            "keywords": ["激活失败"],
        }
        with patch.object(
            knowledge_service, "load_knowledge", return_value=[weaker, DOCUMENT]
        ):
            results = knowledge_service.search_knowledge(
                "激活失败，提示授权无效", limit=1
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "FAQ-001")

    def test_invalid_search_input(self):
        """空问题和非法的返回数量应被拒绝。"""
        with self.assertRaises(ValueError):
            knowledge_service.search_knowledge("   ")
        with self.assertRaises(ValueError):
            knowledge_service.search_knowledge("激活失败", limit=0)


class KnowledgeAnswerTests(unittest.TestCase):
    def setUp(self):
        # 仅替换当前测试进程中的密钥，不修改本地 .env 文件。
        self.enterContext(
            patch.dict("os.environ", {"DEEPSEEK_API_KEY": "mock-test-key"})
        )
        self.search = self.enterContext(
            patch.object(rag_service, "search_knowledge", return_value=[DOCUMENT])
        )
        # 替换 HTTP 客户端，所有模型请求均被拦截。
        self.http = self.enterContext(patch.object(rag_service.httpx, "Client"))
        self.post = self.http.return_value.__enter__.return_value.post

    def set_response(self, data=None, *, content=None, finish_reason="stop"):
        if content is None:
            content = json.dumps(data, ensure_ascii=False)
        self.post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://example.test/completions"),
            json={
                "choices": [{
                    "finish_reason": finish_reason,
                    "message": {"content": content},
                }]
            },
        )

    def test_no_match_does_not_call_model(self):
        """没有资料时直接返回，不发送模型请求。"""
        self.search.return_value = []
        result = rag_service.answer_question("软件支持深色主题吗")
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["sources"], [])
        self.http.assert_not_called()

    def test_answer_uses_local_source(self):
        """回答保留引用，来源名称由本地资料确定。"""
        self.set_response({
            "answer": "请检查激活码是否有多余空格，并检查网络连接。",
            "supported": True,
            "citation_ids": ["FAQ-001"],
        })
        result = rag_service.answer_question("激活失败，怎么办")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["sources"], [{
            "id": "FAQ-001",
            "title": DOCUMENT["title"],
            "source": DOCUMENT["source"],
        }])
        self.assertIn("检查网络连接", result["answer"])

    def test_insufficient_evidence_discards_model_answer(self):
        """依据不足时使用固定提示，不展示模型生成的具体承诺。"""
        self.set_response({
            "answer": "保证五分钟解决（此内容不应展示）",
            "supported": False,
            "citation_ids": [],
        })
        result = rag_service.answer_question("激活失败，保证多久解决")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["sources"], [])
        self.assertNotIn("五分钟", result["answer"])

    def test_invalid_citations_are_rejected(self):
        """缺失引用、虚构引用、未检索到的引用均不能作为有效回答。"""
        for citation_ids in ([], ["FAKE-999"], ["FAQ-002"]):
            with self.subTest(citation_ids=citation_ids):
                self.set_response({
                    "answer": "请检查网络。",
                    "supported": True,
                    "citation_ids": citation_ids,
                })
                with self.assertRaises(ValueError):
                    rag_service.answer_question("激活失败")

    def test_bad_model_output_is_rejected(self):
        """空输出、非法 JSON、错误字段和截断输出均不能正常返回。"""
        for content in ("   ", "这不是JSON", "{}"):
            with self.subTest(content=content):
                self.set_response(content=content)
                with self.assertRaises(ValueError):
                    rag_service.answer_question("激活失败")

        self.set_response(
            {"answer": "检查网络", "supported": True, "citation_ids": ["FAQ-001"]},
            finish_reason="length",
        )
        with self.assertRaises(ValueError):
            rag_service.answer_question("激活失败")


class KnowledgeApiTests(unittest.TestCase):
    def setUp(self):
        temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(
            ticket_service, "DB_PATH", Path(temp_dir) / "test_tickets.db"
        ))
        self.answer = self.enterContext(
            patch("ticket_api.rag_service.answer_question")
        )
        self.client = self.enterContext(TestClient(app))
        # 预先放一张工单，检查问答既不新增，也不改变已有工单。
        ticket_service.create_ticket("已有的测试工单")
        self.original_tickets = ticket_service.list_tickets()

    def test_answer_does_not_change_tickets(self):
        """正常问答返回回答和来源，工单数据保持原样。"""
        expected = {
            "status": "answered",
            "answer": "请检查激活码和网络连接。",
            "sources": [{"id": "FAQ-001", "title": DOCUMENT["title"],
                         "source": DOCUMENT["source"]}],
        }
        self.answer.return_value = expected
        response = self.client.post(
            "/knowledge/ask", json={"question": "  激活失败  "}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        self.answer.assert_called_once_with("激活失败")
        self.assertEqual(ticket_service.list_tickets(), self.original_tickets)

    def test_invalid_questions_do_not_call_service(self):
        """空问题、缺失问题、超长问题不进入问答流程。"""
        for data in ({}, {"question": "   "}, {"question": None},
                     {"question": "长" * 2001}):
            with self.subTest(data_type=list(data)):
                response = self.client.post("/knowledge/ask", json=data)
                self.assertEqual(response.status_code, 422)
        self.answer.assert_not_called()
        self.assertEqual(ticket_service.list_tickets(), self.original_tickets)

    def test_errors_have_clear_status_codes(self):
        """超时、网络故障、资料读取失败和错误输出有明确错误响应。"""
        platform_error = httpx.HTTPStatusError(
            "模拟平台错误",
            request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(503),
        )
        cases = [
            (httpx.ReadTimeout("模拟超时"), 504),
            (platform_error, 502),
            (httpx.ConnectError("模拟连接失败"), 502),
            (FileNotFoundError("模拟知识库缺失"), 500),
            (ValueError("模拟错误引用"), 502),
        ]
        for error, expected_status in cases:
            with self.subTest(error=type(error).__name__):
                self.answer.side_effect = error
                response = self.client.post(
                    "/knowledge/ask", json={"question": "激活失败"}
                )
                self.assertEqual(response.status_code, expected_status)
                self.assertTrue(response.json()["detail"])
                self.assertEqual(
                    ticket_service.list_tickets(), self.original_tickets
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
