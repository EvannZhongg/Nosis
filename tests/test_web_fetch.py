import ipaddress
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from agent_core import Session, ToolExecutionContext, Workspace
from agent_core.tools.budget import output_fits
from agent_core.tools.builtin.web_fetch import (
    DEEPSEEK_FETCH_URL, DEEPSEEK_MAX_URL_CHARACTERS, WebFetchTool,
)
from agent_core.tools.config import ToolSettings, WebFetchConfig


class WebFetchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = WebFetchTool()
        self.url = "https://example.com/article"
        self.request = Mock(return_value=httpx.Response(200, json={
            "title": "Article", "content": "# Article\n\n正文 <tag> &amp;",
        }))
        client_type = httpx.Client
        self.client = self.enterContext(patch(
            "agent_core.tools.builtin.web_fetch.httpx.Client",
            side_effect=lambda **options: client_type(
                transport=httpx.MockTransport(self.request), **options,
            ),
        ))

        def resolve(host):
            try:
                return (str(ipaddress.ip_address(host)),)
            except ValueError:
                return ("93.184.216.34",)

        self.enterContext(patch(
            "agent_core.public_url._resolve_public_addresses", side_effect=resolve,
        ))

    def context(self, provider="deepseek", key="secret"):
        return ToolExecutionContext(
            workspace=Workspace(Path(__file__).parent), session=Session(),
            tool_settings=ToolSettings(web_fetch=WebFetchConfig(provider=provider, api_key=key)),
        )

    def fetch(self, provider="deepseek", key="secret"):
        return self.tool.execute({"url": self.url}, self.context(provider, key))

    def test_deepseek_posts_url_and_preserves_markdown_and_title(self) -> None:
        result = self.fetch()
        request = self.request.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), DEEPSEEK_FETCH_URL)
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(request.content), {"url": self.url})
        self.assertEqual(result, {
            "url": self.url, "title": "Article", "content_type": "text/markdown",
            "content": "# Article\n\n正文 <tag> &amp;", "truncated": False,
            "provider": "deepseek",
        })
        self.request.assert_called_once()

    def test_direct_fetch_retains_html_extraction_and_redirects(self) -> None:
        self.request.side_effect = [
            httpx.Response(302, headers={"location": "/final"}),
            httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                           content=b"<script>hidden</script><p>Hello &amp; world</p>"),
        ]
        result = self.fetch("direct")
        self.assertEqual(result, {
            "url": "https://example.com/final", "status_code": 200,
            "content_type": "text/html; charset=utf-8",
            "content": "Hello & world", "truncated": False,
        })
        for call in self.request.call_args_list:
            self.assertEqual(call.args[0].method, "GET")
            self.assertNotIn("authorization", call.args[0].headers)

    def test_missing_key_and_401_fall_back_once_without_leaking_key(self) -> None:
        for key, reason in ((None, "missing_api_key"), ("bad-key", "authentication_failed")):
            with self.subTest(key=key):
                self.request.reset_mock()
                self.request.side_effect = lambda request: (
                    httpx.Response(401) if request.url == DEEPSEEK_FETCH_URL
                    else httpx.Response(200, text="Direct page")
                )
                result = self.fetch(key=key)
                self.assertEqual(result["content"], "Direct page")
                self.assertEqual(result["provider"], "direct")
                self.assertEqual(result["fallback"], {"from": "deepseek", "reason": reason})
                self.assertEqual(self.request.call_count, 2 if key else 1)
                direct_request = self.request.call_args.args[0]
                self.assertEqual(str(direct_request.url), self.url)
                self.assertNotIn("authorization", direct_request.headers)
                self.assertNotIn("bad-key", json.dumps(result))

    def test_403_and_other_errors_do_not_fall_back_or_retry(self) -> None:
        for status in (400, 402, 403, 404, 408, 413, 429, 500, 504):
            with self.subTest(status=status):
                self.request.reset_mock()
                self.request.return_value = httpx.Response(status)
                with self.assertRaises(httpx.HTTPStatusError):
                    self.fetch()
                self.request.assert_called_once()

    def test_timeout_and_invalid_response_do_not_fall_back(self) -> None:
        self.request.side_effect = httpx.ReadTimeout("fetch timed out")
        with self.assertRaises(httpx.ReadTimeout):
            self.fetch()
        self.request.assert_called_once()
        self.request.side_effect = None
        for page in ([], {}, {"title": "a", "content": None}, {"title": 1, "content": "x"}):
            with self.subTest(page=page):
                self.request.reset_mock()
                self.request.return_value = httpx.Response(200, json=page)
                with self.assertRaisesRegex(ValueError, "title.*content"):
                    self.fetch()
                self.request.assert_called_once()

    def test_long_content_is_bounded_including_json_escaping(self) -> None:
        content = '正文\n"\\' * 12000
        self.request.return_value = httpx.Response(200, json={"title": "", "content": content})
        result = self.fetch()
        self.assertTrue(result["truncated"])
        self.assertTrue(content.startswith(result["content"]))
        self.assertTrue(output_fits(result))

    def test_response_bytes_remain_bounded(self) -> None:
        with patch("agent_core.tools.builtin.web_fetch.MAX_RESPONSE_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "response byte limit"):
                self.fetch()

    def test_schema_uses_context_and_rejects_invalid_provider(self) -> None:
        direct = self.tool.definition(self.context("direct"))
        deepseek = self.tool.definition(self.context())
        self.assertNotIn("maxLength", direct.parameters["properties"]["url"])
        self.assertEqual(deepseek.parameters["properties"]["url"]["maxLength"], 8192)
        self.assertEqual(set(deepseek.parameters["properties"]), {"url"})
        with self.assertRaisesRegex(ValueError, "Unsupported web fetch provider"):
            WebFetchConfig(provider="unknown")
        self.request.assert_not_called()

    def test_url_length_limit_is_enforced_without_schema_validation(self) -> None:
        context = self.context()
        limit = self.tool.definition(context).parameters["properties"]["url"]["maxLength"]
        self.assertEqual(limit, DEEPSEEK_MAX_URL_CHARACTERS)
        url = self.url + "x" * (limit - len(self.url))
        self.tool.execute({"url": url}, context)
        self.assertEqual(json.loads(self.request.call_args.args[0].content)["url"], url)
        self.request.reset_mock()
        with self.assertRaisesRegex(ValueError, "at most 8192"):
            self.tool.execute({"url": url + "x"}, context)
        self.request.assert_not_called()

    def test_rejects_invalid_urls_and_arguments_before_requests(self) -> None:
        for arguments in ({}, {"url": None}, {"url": ""}, {"url": self.url, "extra": True}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.tool.execute(arguments, self.context())
        for url in ("https://example.com/" + "x" * 8192, "file:///etc/passwd",
                    "http:///missing-host", "https://user:pass@example.com/",
                    "http://127.0.0.1/", "http://10.0.0.1/", "http://[::1]/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.tool.execute({"url": url}, self.context())
        self.request.assert_not_called()

    def test_fallback_still_rejects_private_redirects(self) -> None:
        self.request.side_effect = lambda request: (
            httpx.Response(401) if request.url == DEEPSEEK_FETCH_URL
            else httpx.Response(302, headers={"location": "http://127.0.0.1/"})
        )
        with self.assertRaisesRegex(ValueError, "non-public"):
            self.fetch()
        self.assertEqual(self.request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
