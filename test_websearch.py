"""Offline by default. Set LIVE_SEARCH=1 to include one real HTTP smoke test."""
import gzip
import io
import os
import unittest
import urllib.error
import urllib.parse
from email.message import Message
from unittest.mock import patch

import websearch


DDG_HTML = """
<!doctype html><html><body>
<div class="result result--ad">
 <h2><a class="result__a" href="https://ads.example/">广告</a></h2>
</div>
<div class="result results_links results_links_deep web-result">
 <h2 class="result__title"><a class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3.11%2Flibrary%2Funittest.html%3Fx%3D1%26y%3D2&amp;rut=tracking">
 Python &amp; <b>unittest</b> 文档
 </a></h2>
 <a class="result__snippet">标准库&nbsp;测试 <b>框架</b>，\n 包含断言。</a>
</div>
<div class="result"><a class="result__a"
 href="https://docs.python.org/3.11/library/unittest.html?x=1&amp;y=2#top">重复结果</a></div>
<div class="result"><h2><a class="result__a" href="https://example.org/guide">另一篇指南</a></h2>
 <div class="result__snippet">简洁的 <em>入门</em> 内容。</div></div>
<div class="result"><a class="result__a" href="javascript:alert(1)">坏链接</a></div>
</body></html>
"""

BING_HTML = """
<html><body><ol id="b_results">
 <li class="b_ad"><h2><a href="https://ads.example/">广告</a></h2><p>广告摘要</p></li>
 <li class="b_algo b_ad"><h2><a href="https://ads.example/2">伪装广告</a></h2></li>
 <li class="b_algo">
  <div><h2><a href="https://www.python.org/about/?a=1&amp;b=2">Python &amp; <strong>介绍</strong></a></h2></div>
  <div class="b_caption"><p>了解&nbsp;Python\n 的特点。<script>ignore_this()</script></p></div>
 </li>
 <li class="b_algo"><h2><a href="https://www.python.org/about/?a=1&amp;b=2#section">重复结果</a></h2></li>
 <li class="b_algo"><h2><a href="https://docs.python.org/3/">Python 文档</a></h2><p>标准库与教程。</p></li>
 <li class="b_algo"><h2><a href="https://example.net/">无摘要结果</a></h2></li>
 <li class="b_algo"><a href="https://example.net/no-heading">不是标题链接</a></li>
</ol></body></html>
"""


class FakeResponse(io.BytesIO):
    def __init__(self, body, *, encoding=None, charset="utf-8"):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = f"text/html; charset={charset}"
        if encoding:
            self.headers["Content-Encoding"] = encoding


class ParserTests(unittest.TestCase):
    def test_duckduckgo_title_snippet_entities_and_dedup(self):
        rows = websearch._parse_duckduckgo(DDG_HTML)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {
            "title": "Python & unittest 文档",
            "url": "https://docs.python.org/3.11/library/unittest.html?x=1&y=2",
            "snippet": "标准库 测试 框架， 包含断言。",
        })
        self.assertEqual(rows[1]["title"], "另一篇指南")
        self.assertTrue(all(set(row) == {"title", "url", "snippet"} for row in rows))

    def test_bing_h2_p_entities_dedup_and_no_ads(self):
        rows = websearch._parse_bing(BING_HTML)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], {
            "title": "Python & 介绍", "url": "https://www.python.org/about/?a=1&b=2",
            "snippet": "了解 Python 的特点。",
        })
        self.assertEqual(rows[-1]["snippet"], "")
        self.assertNotIn("ignore_this", str(rows))
        self.assertFalse(any("ads.example" in row["url"] for row in rows))

    def test_ad_parent_hides_nested_result(self):
        html = '<div class="b_ad"><li class="b_algo"><h2><a href="https://ads.example">广告</a></h2></li></div>'
        self.assertEqual(websearch._parse_bing(html), [])

    def test_uddg_unpack_preserves_target_query(self):
        target = "https://example.org/中文?q=a%26b&lang=zh"
        redirect = "//duckduckgo.com/l/?" + urllib.parse.urlencode({"uddg": target, "rut": "ignored"})
        self.assertEqual(websearch._unwrap_ddg_url(redirect), target)

    def test_relative_uddg_and_direct_link(self):
        self.assertEqual(websearch._unwrap_ddg_url('/l/?uddg=https%3A%2F%2Fexample.com%2F'), 'https://example.com/')
        self.assertEqual(websearch._unwrap_ddg_url('https://example.com/?a=1&amp;b=2'), 'https://example.com/?a=1&b=2')
        self.assertEqual(websearch._unwrap_ddg_url('javascript:alert(1)'), '')

    def test_parsers_return_empty_when_layout_has_no_results(self):
        for parser in (websearch._parse_duckduckgo, websearch._parse_bing):
            self.assertEqual(parser('<html><p>没有结果</p></html>'), [])

    def test_inline_chinese_and_missing_links(self):
        html = '<div class="result"><a class="result__a" href="https://example.org/">中文<b>标题</b></a><p class="result__snippet">测试<b>框架</b>。<br>第二句。</p></div>'
        self.assertEqual(websearch._parse_duckduckgo(html)[0]['title'], '中文标题')
        self.assertEqual(websearch._parse_duckduckgo(html)[0]['snippet'], '测试框架。 第二句。')
        self.assertEqual(websearch._parse_duckduckgo('<div class="result"><a class="result__a">无链接</a></div>'), [])
        self.assertEqual(websearch._parse_bing('<li class="b_algo"><h2><a>无链接</a></h2></li>'), [])


class FormatTests(unittest.TestCase):
    def test_compact_chinese_plain_text(self):
        rows = websearch._parse_bing(BING_HTML)
        text = websearch.format_results(rows)
        self.assertIn('1. Python & 介绍（www.python.org）', text)
        self.assertIn('2. Python 文档（docs.python.org）', text)
        self.assertIn('摘要：了解 Python 的特点。', text)
        self.assertNotIn('<strong>', text)
        self.assertLessEqual(len(text), 1500)

    def test_truncation_is_strict_even_for_tiny_limits(self):
        rows = [{"title": "标题" * 100, "url": "https://example.com/", "snippet": "内容" * 1000}]
        for limit in (0, 1, 2, 20, 100, 1500):
            with self.subTest(limit=limit):
                text = websearch.format_results(rows, limit)
                self.assertLessEqual(len(text), limit)
                if limit:
                    self.assertTrue(text.endswith('…'))

    def test_empty_results_are_also_bounded(self):
        self.assertEqual(websearch.format_results([]), '未找到可用的搜索结果。')
        self.assertLessEqual(len(websearch.format_results([], 3)), 3)


class SearchTests(unittest.TestCase):
    def test_invalid_query_does_not_access_network(self):
        with patch('websearch._fetch_html') as fetch:
            for query in ('', ' \n\t ', '字' * 201, ' ' + '字' * 200):
                with self.subTest(query_length=len(query)), self.assertRaises(ValueError):
                    websearch.search(query)
            fetch.assert_not_called()

    def test_ddg_success_no_fallback_and_limit(self):
        with patch('websearch._fetch_html', return_value=DDG_HTML) as fetch:
            rows = websearch.search('中文 测试', max_results=1, timeout=3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(fetch.call_count, 1)
        call = fetch.call_args
        self.assertEqual(call.args[0], 'https://html.duckduckgo.com/html/')
        self.assertEqual(urllib.parse.parse_qs(call.kwargs['data'].decode()), {'q': ['中文 测试']})
        self.assertEqual(call.kwargs['timeout'], 3.0)

    def test_ddg_failure_falls_back_to_bing(self):
        with patch('websearch._fetch_html', side_effect=[urllib.error.URLError('blocked'), BING_HTML]) as fetch:
            rows = websearch.search('Python 中文')
        self.assertEqual(len(rows), 3)
        self.assertEqual(fetch.call_count, 2)
        call = fetch.call_args_list[1]
        url = urllib.parse.urlsplit(call.args[0])
        self.assertEqual(url.netloc, 'www.bing.com')
        self.assertEqual(urllib.parse.parse_qs(url.query), {'q': ['Python 中文'], 'setlang': ['zh-Hans']})
        self.assertIsNone(call.kwargs['data'])

    def test_ddg_empty_falls_back_to_bing(self):
        with patch('websearch._fetch_html', side_effect=['<html></html>', BING_HTML]) as fetch:
            self.assertEqual(len(websearch.search('Python')), 3)
        self.assertEqual(fetch.call_count, 2)

    def test_all_network_failures_raise_chinese_runtime_error(self):
        with patch('websearch._fetch_html', side_effect=[TimeoutError(), urllib.error.URLError('secret query')]):
            with self.assertRaises(RuntimeError) as caught:
                websearch.search('secret query')
        message = str(caught.exception)
        self.assertTrue(message.startswith('联网搜索失败：'))
        self.assertIn('DuckDuckGo', message)
        self.assertIn('Bing', message)
        self.assertNotIn('Traceback', message)
        self.assertNotIn('secret query', message)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_two_empty_responses_return_empty_list(self):
        with patch('websearch._fetch_html', return_value='<html></html>'):
            self.assertEqual(websearch.search('没有匹配项'), [])

    def test_zero_limit_does_not_access_network(self):
        with patch('websearch._fetch_html') as fetch:
            self.assertEqual(websearch.search('Python', max_results=0), [])
            fetch.assert_not_called()


class FetchTests(unittest.TestCase):
    def test_gzip_charset_headers_and_post(self):
        source = '<html>中文 &amp; 页面</html>'
        response = FakeResponse(gzip.compress(source.encode('gb18030')), encoding='gzip', charset='gb18030')
        with patch('websearch.urllib.request.urlopen', return_value=response) as open_url:
            text = websearch._fetch_html('https://html.duckduckgo.com/html/', data=b'q=python', timeout=2)
        self.assertEqual(text, source)
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), 'POST')
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertIn('Mozilla/5.0', headers['user-agent'])
        self.assertTrue(headers['accept-language'].startswith('zh-CN'))
        self.assertEqual(headers['accept-encoding'], 'gzip')
        self.assertEqual(open_url.call_args.kwargs['timeout'], 2)

    def test_http_error_is_wrapped_without_remote_url(self):
        error = urllib.error.HTTPError('https://example.org/?q=private', 503, 'unavailable', None, None)
        with patch('websearch.urllib.request.urlopen', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, '联网搜索失败：服务返回 HTTP 503') as caught:
                websearch._fetch_html('https://example.org/')
        self.assertNotIn('private', str(caught.exception))

    def test_bad_gzip_is_wrapped(self):
        with patch('websearch.urllib.request.urlopen', return_value=FakeResponse(b'not gzip', encoding='gzip')):
            with self.assertRaisesRegex(RuntimeError, '联网搜索失败：响应解压失败'):
                websearch._fetch_html('https://example.org/')


class LiveSearchTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('LIVE_SEARCH') == '1', '设置 LIVE_SEARCH=1 才进行真实联网测试')
    def test_live_search(self):
        rows = websearch.search('Python 3.11 unittest documentation', max_results=3, timeout=8.0)
        self.assertGreater(len(rows), 0, '搜索页没有解析出结果，可能受到验证码或页面结构变化影响')
        self.assertLessEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(set(row), {'title', 'url', 'snippet'})
            self.assertTrue(row['title'])
            self.assertIn(urllib.parse.urlsplit(row['url']).scheme, ('http', 'https'))
        self.assertLessEqual(len(websearch.format_results(rows)), 1500)


if __name__ == '__main__':
    unittest.main()
