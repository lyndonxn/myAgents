"""Web 搜索工具：无 Key 的 Bing 网页搜索（requests + 正则解析）。

- 请求 cn.bing.com/search（自动跟随重定向，UA + Accept-Language 模拟浏览器）
- 解析 `<li class="b_algo">` 结果块：标题 / URL / 摘要
- 进程内 TTL 缓存，避免短时间内重复请求
- 网络失败时抛 WebSearchError，由工具层捕获，不中断问答
"""
from __future__ import annotations

import html as _html
import re
import time
from dataclasses import dataclass, field

import requests

BING_URL = "https://cn.bing.com/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_RESULT_RE = re.compile(r'<li class="b_algo".*?</li>', re.DOTALL)
_HREF_RE = re.compile(r'<a[^>]+href="([^"]+)"')
_TITLE_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.DOTALL)
_SNIPPET_RE = re.compile(r'<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_NO_RESULT_RE = re.compile(r"没有找到|未找到|没有与此相关|No results")


class WebSearchError(RuntimeError):
    pass


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str = ""


def _strip_tags(text: str) -> str:
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


class WebSearch:
    def __init__(self, timeout: float = 15.0, cache_ttl: float = 300.0):
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, list[WebResult]]] = {}

    def search(self, query: str, max_results: int = 5) -> dict:
        """搜索并返回 {"text", "results", "sources"}。"""
        query = query.strip()
        if not query:
            raise WebSearchError("查询不能为空")
        max_results = max(1, min(int(max_results), 10))
        key = f"{query}|{max_results}"

        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.cache_ttl:
            results = cached[1]
        else:
            results = self._fetch(query, max_results)
            self._cache[key] = (time.monotonic(), results)

        sources = [r.url for r in results]
        if not results:
            text = f"关于「{query}」的 Web 搜索没有返回结果。"
        else:
            lines = [f"关于「{query}」的 Web 搜索结果："]
            for i, r in enumerate(results, 1):
                lines.append(f"[{i}] {r.title}\n    URL: {r.url}\n    摘要: {r.snippet[:200]}")
            text = "\n".join(lines)
        return {"text": text, "results": [r.__dict__ for r in results], "sources": sources}

    def _fetch(self, query: str, max_results: int) -> list[WebResult]:
        try:
            resp = requests.get(
                BING_URL,
                params={"q": query, "setlang": "zh-Hans"},
                headers=HEADERS,
                timeout=self.timeout,
                allow_redirects=True,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise WebSearchError(f"Web 搜索请求失败: {exc}") from exc

        html_text = resp.text
        if _NO_RESULT_RE.search(html_text) and "b_algo" not in html_text:
            return []

        results: list[WebResult] = []
        seen_urls: set[str] = set()
        for block in _RESULT_RE.findall(html_text):
            href_m = _HREF_RE.search(block)
            title_m = _TITLE_RE.search(block)
            if not href_m or not title_m:
                continue
            url = href_m.group(1)
            if not url.startswith("http") or url in seen_urls:
                continue
            seen_urls.add(url)
            title = _strip_tags(title_m.group(1))
            snippet_m = _SNIPPET_RE.search(block)
            snippet = _strip_tags(snippet_m.group(1)) if snippet_m else ""
            results.append(WebResult(title=title, url=url, snippet=snippet))
            if len(results) >= max_results:
                break
        return results
