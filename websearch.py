"""Standard-library web search for Python 3.11; no disk cache or query logging.

DuckDuckGo HTML is tried first, then Bing on an error or an empty result set.
``timeout`` applies to each HTTP request, so a fallback can take additional time.
Search pages may change or require a CAPTCHA; this module does not bypass them.
"""
from __future__ import annotations

import gzip
import io
import math
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

__all__ = ["search", "format_results"]

_DDG_URL = "https://html.duckduckgo.com/html/"
_BING_URL = "https://www.bing.com/search"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    "Accept-Encoding": "gzip",
}
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list = field(default_factory=list)


class _TreeParser(HTMLParser):
    """A small tolerant HTML tree, sufficient for the two result formats."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _classes(node: _Node) -> set[str]:
    return set(node.attrs.get("class", "").split())


def _is_ad(node: _Node) -> bool:
    classes = _classes(node)
    return bool(classes & {"b_ad", "b_ads", "b_adTop", "b_adBottom"}) or any(
        name.startswith("result--ad") or name == "result--sponsored"
        for name in classes
    )


def _walk(root: _Node):
    stack = [root]
    while stack:
        node = stack.pop()
        if node.tag in {"script", "style", "noscript"} or _is_ad(node):
            continue
        yield node
        stack.extend(child for child in reversed(node.children) if isinstance(child, _Node))


def _text(root: _Node) -> str:
    parts = []
    stack = [root]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
        elif item.tag not in {"script", "style", "noscript"}:
            if item.tag in {"br", "div", "p", "li", "h1", "h2", "h3"}:
                parts.append(" ")
                stack.append(" ")
            stack.extend(reversed(item.children))
    return _clean_text("".join(parts))


def _tree(source: str) -> _Node:
    parser = _TreeParser()
    parser.feed(source)
    parser.close()
    return parser.root


def _http_url(value: str, base_url: str) -> str:
    try:
        if not value.strip():
            return ""
        value = urllib.parse.urljoin(base_url, unescape(value).strip())
        parts = urllib.parse.urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        # Strip anchors for deduplication, retaining the query string verbatim.
        return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))
    except ValueError:
        return ""


def _unwrap_ddg_url(value: str) -> str:
    value = _http_url(value, "https://duckduckgo.com/")
    if not value:
        return ""
    parts = urllib.parse.urlsplit(value)
    if (parts.hostname or "").lower() in {"duckduckgo.com", "www.duckduckgo.com", "html.duckduckgo.com"}:
        target = urllib.parse.parse_qs(parts.query).get("uddg", [""])[0]
        if target:
            return _http_url(target, "https://duckduckgo.com/")
    return value


def _unique_results(results: list[dict]) -> list[dict]:
    unique, seen = [], set()
    for row in results:
        parts = urllib.parse.urlsplit(row["url"])
        key = (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _parse_duckduckgo(source: str) -> list[dict]:
    results = []
    for container in _walk(_tree(source)):
        if "result" not in _classes(container):
            continue
        nodes = list(_walk(container))
        anchor = next((n for n in nodes if n.tag == "a" and "result__a" in _classes(n)), None)
        if anchor is None:
            continue
        title = _text(anchor)
        url = _unwrap_ddg_url(anchor.attrs.get("href", ""))
        snippet = next((n for n in nodes if "result__snippet" in _classes(n)), None)
        if title and url:
            results.append({"title": title, "url": url, "snippet": _text(snippet) if snippet else ""})
    return _unique_results(results)


def _parse_bing(source: str) -> list[dict]:
    results = []
    for container in _walk(_tree(source)):
        if container.tag != "li" or "b_algo" not in _classes(container):
            continue
        nodes = list(_walk(container))
        heading = next((n for n in nodes if n.tag == "h2"), None)
        anchor = next((n for n in _walk(heading) if n.tag == "a"), None) if heading else None
        if anchor is None:
            continue
        title = _text(anchor)
        url = _http_url(anchor.attrs.get("href", ""), _BING_URL)
        paragraphs = [_text(n) for n in nodes if n.tag == "p"]
        snippet = next((text for text in paragraphs if text), "")
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return _unique_results(results)


def _network_detail(error: Exception) -> str:
    """Do not echo request URLs, arbitrary remote errors, or tracebacks."""
    if isinstance(error, urllib.error.HTTPError):
        return f"服务返回 HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return _network_detail(error.reason) if isinstance(error.reason, Exception) else "无法连接搜索服务"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "请求超时"
    if isinstance(error, ssl.SSLError):
        return "安全连接失败"
    if isinstance(error, (gzip.BadGzipFile, EOFError)):
        return "响应解压失败"
    if isinstance(error, RuntimeError) and str(error).startswith("联网搜索失败："):
        return str(error).removeprefix("联网搜索失败：")
    return "无法获取搜索结果"


def _fetch_html(url: str, *, data: bytes | None = None, timeout: float = 8.0) -> str:
    try:
        headers = dict(_HEADERS)
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise RuntimeError("联网搜索失败：响应内容过大")
            if "gzip" in response.headers.get("Content-Encoding", "").lower():
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                    raw = compressed.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise RuntimeError("联网搜索失败：响应内容过大")
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                return raw.decode(charset, errors="replace")
            except LookupError:
                return raw.decode("utf-8", errors="replace")
    except Exception as error:
        raise RuntimeError("联网搜索失败：" + _network_detail(error)) from None


def search(query: str, max_results: int = 5, timeout: float = 8.0) -> list[dict]:
    """Search two public HTML endpoints, returning normalized, unique results.

    A successful Bing response with no organic results returns an empty list.
    When both requests fail, a Chinese RuntimeError is raised without chaining.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise ValueError("搜索内容不能为空，且不能超过200字")
    query = re.sub(r"\s+", " ", query).strip()
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 0:
        raise ValueError("搜索结果数量必须是非负整数")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("搜索超时必须是正数")
    if max_results == 0:
        return []

    errors = []
    sources = (
        ("DuckDuckGo", _DDG_URL, urllib.parse.urlencode({"q": query}).encode("utf-8"), _parse_duckduckgo),
        ("Bing", _BING_URL + "?" + urllib.parse.urlencode({"q": query, "setlang": "zh-Hans"}), None, _parse_bing),
    )
    for name, url, data, parser in sources:
        try:
            results = parser(_fetch_html(url, data=data, timeout=float(timeout)))
        except Exception as error:
            errors.append(f"{name}：{_network_detail(error)}")
            continue
        if results:
            return results[:max_results]
        if name == "Bing":
            return []
        errors.append("DuckDuckGo：没有可用结果")
    raise RuntimeError("联网搜索失败：" + "；".join(errors)) from None


def format_results(results: list[dict], limit_chars: int = 1500) -> str:
    """Numbered Chinese plain text: title, hostname and snippet, strictly bounded."""
    if isinstance(limit_chars, bool) or not isinstance(limit_chars, int) or limit_chars < 0:
        raise ValueError("文本长度上限必须是非负整数")
    if limit_chars == 0:
        return ""
    blocks = []
    for index, row in enumerate(results, 1):
        try:
            domain = urllib.parse.urlsplit(row.get("url", "")).hostname or "未知来源"
        except ValueError:
            domain = "未知来源"
        title = _clean_text(str(row.get("title") or domain))
        snippet = _clean_text(str(row.get("snippet") or "暂无摘要"))
        blocks.append(f"{index}. {title}（{domain}）\n摘要：{snippet}")
    text = "\n\n".join(blocks) if blocks else "未找到可用的搜索结果。"
    return text if len(text) <= limit_chars else text[:limit_chars - 1].rstrip() + "…"
