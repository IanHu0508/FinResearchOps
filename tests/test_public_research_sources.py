"""Public-source routing with synthetic HTTP replies, never real issuer data."""

from collections import Counter
from datetime import date, timedelta
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from finauditgate.adapters.public_research_sources import collect_sources
from finauditgate.adapters import public_research_sources
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex


def bundle(symbol="301999.SZ", as_of="2025-11-03", name="合成甲"):
    content = "SYNTHETIC_ONLY: frozen financial and price observations; no real investment facts."
    return {"schema_version": "finresearchops.thesis-sources/v2", "symbol": symbol,
        "as_of": as_of, "identity": {"company_name": name, "company_short_name": name},
        "sources": [{"id": "S01", "use": "research", "origin": "SYNTHETIC_BASE",
            "availability_note": "SYNTHETIC_ONLY frozen base.", "content": content,
            "sha256": sha256_hex(content.encode())}]}


def qa(identifier="100001", *, code="301999", name="合成甲", kind="1",
       question="2025-11-01 09:00:00", reply="2025-11-02 10:00:00"):
    return {"id": identifier, "securityShortName": name,
        "title": name + " SYNTHETIC_QUESTION_" + identifier,
        "content": "SYNTHETIC_REPLY_" + identifier + "：本回答是合成公司回应，不代表事实认证或投资者共识。",
        "createTime": question, "responseTime": reply, "type": kind,
        "headCharacter": name, "gubaId": code,
        "url": f"https://guba.eastmoney.com/news,{code},{identifier}.html"}


def media(identifier="100001", *, name="合成甲", published="2025-11-02 10:00:00"):
    return {"date": published, "title": name + " SYNTHETIC_NEWS_" + identifier,
        "content": name + " SYNTHETIC_MEDIA_EXCERPT_" + identifier,
        "mediaName": "SYNTHETIC_MEDIA",
        "url": f"https://finance.eastmoney.com/a/20251102{identifier}.html"}


class SyntheticFetch:
    def __init__(self, *, news=None, releases=None, questions=None, failure=False,
                 redirects=False, robots_disallow=False, forum=None, details=None):
        self.news, self.releases, self.questions = news, releases or [], questions or []
        self.failure, self.redirects, self.robots_disallow = failure, redirects, robots_disallow
        self.forum, self.details = forum, details or {}
        self.calls = []

    def __call__(self, url, *, timeout, max_bytes):
        self.calls.append({"url": url, "timeout": timeout, "max_bytes": max_bytes})
        parsed = urlsplit(url)
        if parsed.path == "/robots.txt":
            return {"status": 200 if self.robots_disallow else 404,
                    "body": b"User-agent: *\nDisallow: /\n" if self.robots_disallow else b"",
                    "headers": {"Content-Type": "text/plain"}, "url": url}
        if self.redirects:
            return {"status": 302, "body": b"", "url": url,
                    "headers": {"Location": "https://synthetic-unapproved.invalid/redirect"}}
        if self.failure:
            raise TimeoutError("SYNTHETIC_TRANSPORT_FAILURE")
        if parsed.path.startswith("/list,") and self.forum is not None:
            body = ("<script>var article_list = " + json.dumps({"re": self.forum}) + ";</script>").encode()
            return {"status": 200, "body": body, "headers": {}, "url": url}
        if parsed.path.startswith("/news,"):
            identifier = int(parsed.path.rsplit(",", 1)[-1].removesuffix(".html"))
            if identifier in self.details:
                body = ("<script>var post_article = " + json.dumps(self.details[identifier]) + ";</script>").encode()
                return {"status": 200, "body": body, "headers": {}, "url": url}
        query = parse_qs(parsed.query)
        if "param" not in query:
            return {"status": 503, "body": b"SYNTHETIC_FORUM_UNAVAILABLE", "headers": {}, "url": url}
        request = json.loads(query["param"][0])
        kind = request["type"][0]
        if kind == "cmsArticleWebOld":
            if self.news is None:
                return {"status": 503, "body": b"SYNTHETIC_MEDIA_UNAVAILABLE", "headers": {}, "url": url}
            rows = self.news
        elif kind == "wenDongMiWeb":
            scope = request["param"][kind]["webSearchScope"]
            rows = self.releases if str(scope) == "3" else self.questions
        else:
            raise AssertionError("UNREGISTERED_SYNTHETIC_ROUTE:" + kind)
        response = {"code": 0, "result": {kind: rows}, "hitsTotal": len(rows)}
        callback = query.get("cb", query.get("callback", ["sourceprobe"]))[0]
        body = (callback + "(" + json.dumps(response, ensure_ascii=False) + ")").encode()
        return {"status": 200, "body": body, "headers": {"Content-Type": "application/javascript"}, "url": url}


class PublicResearchSourcesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        (self.workspace / "finaudit-gate/.git").mkdir(parents=True)
        (self.workspace / "private").mkdir()
        self.output = self.workspace / "private/source-probes"

    def collect(self, transport, base=None, name="run"):
        base = bundle() if base is None else base
        original = canonical_json_bytes(base)
        result = collect_sources(base, self.output / name, fetch=transport)
        self.assertEqual(original, canonical_json_bytes(base))
        for row in result["rows"]:
            self.assertEqual({"id", "use", "origin", "availability_note", "content", "sha256"}, set(row))
            self.assertEqual("research", row["use"])
            self.assertEqual(sha256_hex(row["content"].encode()), row["sha256"])
            self.assertNotIn(row["id"], {item["id"] for item in base["sources"]})
        return result

    def test_symbols_and_research_dates_drive_requests_without_mutating_the_base(self):
        for index, (symbol, as_of, company) in enumerate((
                ("301999.SZ", "2025-11-03", "合成甲"),
                ("600999.SH", "2024-04-09", "合成乙"))):
            with self.subTest(symbol=symbol):
                day = date.fromisoformat(as_of)
                published = (day - timedelta(days=1)).isoformat() + " 10:00:00"
                transport = SyntheticFetch(releases=[qa("200001", code=symbol[:6], name=company, kind="3",
                    question=published, reply=published)], questions=[qa("200002", code=symbol[:6], name=company,
                    question=published, reply=published)])
                result = self.collect(transport, bundle(symbol, as_of, company), str(index))
                qa_queries = []
                for call in transport.calls:
                    query = parse_qs(urlsplit(call["url"]).query)
                    if "param" in query:
                        decoded = json.loads(query["param"][0])
                        self.assertEqual(symbol[:6] if decoded["type"] == ["cmsArticleWebOld"] else company,
                                         decoded["keyword"])
                        if decoded["type"] == ["wenDongMiWeb"]:
                            qa_queries.append(decoded["param"]["wenDongMiWeb"])
                self.assertEqual({3, 4}, {int(q["webSearchScope"]) for q in qa_queries})
                for query in qa_queries:
                    self.assertEqual(symbol[:6], query["gubaId"])
                    self.assertTrue(query["endTime"].startswith(as_of))
                    self.assertLess(query["startTime"], query["endTime"])
                self.assertEqual(1, result["channels"]["news"]["count"])
                self.assertEqual(1, result["channels"]["social"]["count"])

    def test_investor_qa_requires_matching_stock_and_known_in_window_question_and_reply(self):
        rows = [qa("300001"), qa("300002", code="600999", name="合成乙"),
                qa("300003", question="2025-11-04 09:00:00"),
                qa("300004", question=""), qa("300005", question="2024-01-01 09:00:00"),
                qa("300006", reply=""), qa("300007", reply="2025-11-04 09:00:00")]
        result = self.collect(SyntheticFetch(questions=rows))
        self.assertEqual(1, result["channels"]["social"]["count"])
        self.assertEqual("investor_qa", result["channels"]["social"]["items"][0]["source_type"])
        content = "\n".join(row["content"] for row in result["rows"])
        self.assertIn("SYNTHETIC_QUESTION_300001", content)
        self.assertIn("SYNTHETIC_REPLY_300001", content)
        for identifier in ("300002", "300003", "300004", "300005", "300006", "300007"):
            self.assertNotIn("SYNTHETIC_QUESTION_" + identifier, content)
            self.assertNotIn("SYNTHETIC_REPLY_" + identifier, content)

    def test_company_release_is_news_fallback_and_never_a_social_post(self):
        release = qa("400001", kind="3")
        result = self.collect(SyntheticFetch(releases=[release], questions=[release]))
        self.assertEqual(1, result["channels"]["news"]["count"])
        self.assertEqual("company_event_index_excerpt", result["channels"]["news"]["items"][0]["source_type"])
        self.assertEqual(0, result["channels"]["social"]["count"])
        self.assertTrue(result["channels"]["social"]["gaps"])
        self.assertFalse(any(item["source_type"] == "public_investor_post"
                             for channel in result["channels"].values() for item in channel["items"]))

    def test_current_or_undated_media_cannot_fill_a_historical_news_gap(self):
        rows = [media("500001", published="2025-11-04 09:00:00"),
                media("500002", published=""), media("500003", published="2024-01-01 10:00:00"),
                media("500004", name="合成乙")]
        result = self.collect(SyntheticFetch(news=rows, releases=[qa("500005", kind="3")]))
        self.assertEqual(1, result["channels"]["news"]["count"])
        self.assertEqual("company_event_index_excerpt", result["channels"]["news"]["items"][0]["source_type"])
        content = "\n".join(row["content"] for row in result["rows"])
        for identifier in ("500001", "500002", "500003", "500004"):
            self.assertNotIn("SYNTHETIC_MEDIA_EXCERPT_" + identifier, content)

    def test_media_identity_requires_a_complete_code_not_a_numeric_substring(self):
        amount = media("970001", name="合成乙")
        amount["content"] = "合成乙公布资产3019990万元，主要用于工厂扩建。"
        identifier = media("970002", name="合成乙")
        identifier["content"] = "合成乙文件编号0301999，本文未提及其他证券。"
        exact = media("970003", name="合成乙")
        exact["content"] = "此合成报道同时提到证券代码301999.SZ的公开事项。"
        result = self.collect(SyntheticFetch(news=[amount, identifier, exact]))
        self.assertEqual(1, result["channels"]["news"]["count"])
        self.assertEqual(exact["url"], result["channels"]["news"]["items"][0]["url"])
        content = "\n".join(row["content"] for row in result["rows"])
        self.assertNotIn(amount["content"], content)
        self.assertNotIn(identifier["content"], content)
        self.assertIn(exact["content"], content)
        rejected = result["channels"]["news"]["rejections"]
        self.assertEqual(2, sum(row["reason"] == "OTHER_ISSUER" for row in rejected))

    def test_available_media_remains_labeled_as_an_index_excerpt(self):
        result = self.collect(SyntheticFetch(news=[media("600001"), media("600002"), media("600003")]))
        self.assertEqual(3, result["channels"]["news"]["count"])
        self.assertEqual({"media_news_index_excerpt"}, {i["source_type"] for i in result["channels"]["news"]["items"]})

    def test_forum_requires_a_matching_historical_investor_post_and_fetches_its_body(self):
        candidate = {"post_id": 700001, "post_type": 0, "stockbar_code": "301999",
                     "post_publish_time": "2025-11-02 10:00:00"}
        forum = [candidate,
                 {**candidate, "post_id": 700002, "post_type": 3},
                 {**candidate, "post_id": 700003, "post_publish_time": "2025-11-04 09:00:00"},
                 {**candidate, "post_id": 700004, "stockbar_code": "600999"},
                 {**candidate, "post_id": 700005, "post_publish_time": ""}]
        detail = {**candidate, "post_guba": {"stockbar_code": "301999"},
                  "post_title": "合成甲 SYNTHETIC_INVESTOR_OPINION", "post_content": "SYNTHETIC_ORIGINAL_POST_BODY",
                  "post_user": {"user_nickname": "synthetic_user"}}
        transport = SyntheticFetch(forum=forum, details={700001: detail})
        result = self.collect(transport)
        self.assertEqual(1, result["channels"]["social"]["count"])
        self.assertEqual("public_investor_post", result["channels"]["social"]["items"][0]["source_type"])
        self.assertIn("SYNTHETIC_ORIGINAL_POST_BODY", "\n".join(r["content"] for r in result["rows"]))
        details_requested = [c["url"] for c in transport.calls if urlsplit(c["url"]).path.startswith("/news,")]
        self.assertEqual(["https://guba.eastmoney.com/news,301999,700001.html"], details_requested)

    def test_all_sources_failing_returns_bounded_explicit_coverage_gaps(self):
        transport = SyntheticFetch(failure=True)
        result = self.collect(transport)
        self.assertEqual([], result["rows"])
        for name in ("news", "social"):
            self.assertEqual(0, result["channels"][name]["count"])
            self.assertEqual("PARTIAL", result["channels"][name]["status"])
            self.assertTrue(result["channels"][name]["gaps"])
        self.assertLessEqual(len(transport.calls), 12)
        self.assertEqual(len(transport.calls), len(result["requests"]))
        self.assertTrue(all(count == 1 for count in Counter(c["url"] for c in transport.calls).values()))
        self.assertTrue(all(0 < c["timeout"] <= 15 and 0 < c["max_bytes"] <= 2_000_000 for c in transport.calls))
        self.assertEqual(12, result["limits"]["max_requests"])

    def test_redirect_responses_are_not_implicitly_followed(self):
        transport = SyntheticFetch(redirects=True)
        result = self.collect(transport)
        self.assertEqual([], result["rows"])
        self.assertFalse(any(urlsplit(c["url"]).hostname == "synthetic-unapproved.invalid" for c in transport.calls))
        self.assertLessEqual(len(transport.calls), 12)
        self.assertTrue(all(count == 1 for count in Counter(c["url"] for c in transport.calls).values()))

    def test_default_transport_installs_no_redirect_handler_and_opens_only_once(self):
        url = "https://guba.eastmoney.com/list,301999,f.html"
        redirect = HTTPError(url, 302, "SYNTHETIC_REDIRECT",
                             {"Location": "https://synthetic-unapproved.invalid/redirect"}, BytesIO(b"redirect"))
        with patch.object(public_research_sources, "build_opener") as factory:
            factory.return_value.open.side_effect = redirect
            result = public_research_sources._default_fetch(url, timeout=15, max_bytes=128)
        self.assertEqual(302, result["status"])
        self.assertEqual(url, result["url"])
        self.assertEqual(b"redirect", result["body"])
        factory.return_value.open.assert_called_once()
        handler = factory.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 302, "SYNTHETIC_REDIRECT", {},
                                                  "https://synthetic-unapproved.invalid/redirect"))

    def test_explicit_robots_denial_does_not_turn_into_an_implicit_fetch(self):
        transport = SyntheticFetch(robots_disallow=True)
        result = self.collect(transport)
        self.assertEqual([], result["rows"])
        self.assertTrue(all(urlsplit(c["url"]).path == "/robots.txt" for c in transport.calls))
        self.assertTrue(result["channels"]["news"]["gaps"])
        self.assertTrue(result["channels"]["social"]["gaps"])


if __name__ == "__main__":
    unittest.main()
