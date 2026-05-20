"""
weread/browser.py — Playwright headless Chromium 方案获取章节正文。

当直接 HTTP API 所有端点均返回 404 时，使用 Playwright 启动无头 Chromium，
注入 Cookie 后导航至微信读书网页阅读器，提取章节 HTML。

支持两种章节端点格式：
  - 旧格式：/book/chapter/e3（JSON 响应，直接拦截 XHR）
  - 新格式：/book/chapter/e_N（AES 加密响应，由 JS 解密后渲染到 DOM，
    从 DOM 提取渲染结果）

工作流程：
  1. 启动 headless Chromium（playwright.async_api）
  2. 在新 BrowserContext 中注入 wr_vid / wr_skey Cookie
  3. 监听 response 事件：
     - 旧格式（e3）：直接捕获 JSON 响应
     - 新格式（e_N）：检测到请求后等待 JS 渲染完成，从 DOM 提取 HTML
  4. 导航至 https://weread.qq.com/web/reader/{book_key|book_id}
"""

from __future__ import annotations

import asyncio
import re as _re

__all__ = ["BrowserFetchError", "fetch_chapter_via_browser"]

_BROWSER_TIMEOUT = 40.0   # 总超时秒数（含 DOM 渲染等待）
_NAV_TIMEOUT_MS = 20_000  # Playwright 页面导航超时 ms
_DOM_RENDER_WAIT = 3.0    # e_N 最后一个请求后等待 JS 渲染完成的秒数
_DOM_POLL_INTERVAL = 0.5  # 轮询 DOM 内容的间隔秒数
_DOM_MIN_TEXT_LEN = 300   # DOM 文本量超过此值才认为内容已渲染


class BrowserFetchError(Exception):
    """Playwright 方案获取章节失败（未安装 / 超时 / 无响应）。"""


async def _extract_dom_content(page, debug_log) -> str | None:
    """
    等待 JS 将章节 HTML 渲染到 DOM，然后提取内容。

    WeRead 新版 API 对 e_N 响应做 AES-128 加密，JS 解密后写入 DOM。
    本函数轮询 .readerChapterContent 出现并有内容后提取其 innerHTML。
    """
    # 等待网络空闲（所有 XHR 完成）
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass

    # 轮询等待 .readerChapterContent 出现并有内容（JS 解密渲染完毕）
    # 使用 textContent（含隐藏元素），因 headless 模式下 innerText 可能为空
    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        content_len = await page.evaluate("""
            () => {
                const el = document.querySelector('.readerChapterContent');
                if (!el) return 0;
                return (el.textContent || '').trim().length;
            }
        """)
        debug_log(f"readerChapterContent textContent length: {content_len}")
        if content_len >= _DOM_MIN_TEXT_LEN:
            break
        await asyncio.sleep(_DOM_POLL_INTERVAL)

    # 从 DOM 提取章节 HTML，在 JS 端精简为语义标签（去除大量内联 style/span）
    html: str | None = await page.evaluate("""
        () => {
            // 将 DOM 节点精简为只含语义标签的 HTML 字符串
            function simplify(node) {
                if (node.nodeType === 3) {  // Text
                    return node.textContent || '';
                }
                if (node.nodeType !== 1) return '';
                const tag = node.tagName.toLowerCase();
                // 跳过 style/script/head
                if (tag === 'style' || tag === 'script' || tag === 'head') return '';
                const children = [...node.childNodes].map(simplify).join('');
                if (tag === 'p')   return '<p>' + children + '</p>';
                if (tag === 'br')  return '<br>';
                if (tag === 'strong' || tag === 'b') return '<strong>' + children + '</strong>';
                if (tag === 'em'  || tag === 'i')    return '<em>' + children + '</em>';
                if (tag === 'img') return '<img>';
                if (tag === 'blockquote') return '<blockquote>' + children + '</blockquote>';
                if (tag === 'h1') return '<h1>' + children + '</h1>';
                if (tag === 'h2') return '<h2>' + children + '</h2>';
                if (tag === 'h3') return '<h3>' + children + '</h3>';
                // span / div / section 等容器：直接穿透，只返回子内容
                return children;
            }

            // 已知 WeRead 阅读器容器选择器
            const selectors = [
                '.readerChapterContent',
                '.readerContent',
                '.reader_content',
                '.wr_absolute',
                '[class*="chapterContent"]',
                '[class*="chapter_content"]',
                '[class*="readerChapter"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && (el.textContent || '').trim().length >= 100) {
                    return simplify(el);
                }
            }
            return null;
        }
    """)

    if html and len(html) >= 100:
        debug_log(f"DOM extraction succeeded: {len(html)} chars of HTML")
        return html

    # 提取失败时记录 DOM 结构以便调试
    dom_info = await page.evaluate("""
        () => {
            const items = [];
            for (const el of document.querySelectorAll('div, section, article')) {
                const text = (el.innerText || el.textContent || '').trim();
                if (text.length > 80) {
                    items.push({
                        tag: el.tagName,
                        classes: el.className.slice(0, 100),
                        id: el.id.slice(0, 40),
                        textLen: text.length,
                        childCount: el.querySelectorAll('*').length,
                    });
                }
                if (items.length >= 20) break;
            }
            return items;
        }
    """)
    debug_log(f"DOM extraction failed, DOM structure: {dom_info}")
    return None


async def fetch_chapter_via_browser(
    vid: str,
    skey: str,
    book_id: str,
    chapter_uid: int,
    *,
    book_key: str | None = None,
    timeout: float = _BROWSER_TIMEOUT,
) -> str:
    """
    使用 Playwright headless Chromium 获取章节正文 HTML。

    参数
    -----
    vid, skey    : 微信读书认证 Cookie 字段
    book_id      : 书籍数字 ID 字符串（如 "695233"）
    chapter_uid  : 章节 UID（整数）
    book_key     : 微信读书阅读器 URL 中的 bookKey；为 None 时回退使用 book_id
    timeout      : 总超时秒数（默认 40s）

    返回
    -----
    str: 章节正文 HTML 字符串

    抛出
    -----
    BrowserFetchError: playwright 未安装 / 超时 / 无内容
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserFetchError(
            "playwright 未安装，无法使用浏览器回退。\n"
            "请执行以下命令安装：\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        ) from exc

    from weread.api import _debug_log

    reader_path = book_key if book_key else book_id

    result: str | None = None
    e3_done = asyncio.Event()      # 旧格式：e3 JSON 响应已捕获
    en_detected = asyncio.Event()  # 新格式：检测到至少一个 e_N 请求

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )

            await context.add_cookies(
                [
                    {
                        "name": "wr_vid",
                        "value": str(vid),
                        "domain": ".weread.qq.com",
                        "path": "/",
                        "httpOnly": False,
                        "secure": False,
                    },
                    {
                        "name": "wr_skey",
                        "value": str(skey),
                        "domain": ".weread.qq.com",
                        "path": "/",
                        "httpOnly": False,
                        "secure": False,
                    },
                ]
            )

            page = await context.new_page()

            async def _on_response(response) -> None:
                nonlocal result
                url: str = response.url

                if "weread.qq.com" in url and "/book/" in url:
                    _debug_log(f"Browser API [{response.status}]: {url}")

                # ---- 旧格式：e3（JSON 响应，直接捕获）----
                if "/book/chapter/e3" in url and not e3_done.is_set():
                    def _id_in_url(bid: str) -> bool:
                        return f"bookId={bid}" in url or f"bookId%3D{bid}" in url

                    if not _id_in_url(book_id) and not (book_key and _id_in_url(book_key)):
                        return

                    uid_str = str(chapter_uid)
                    if (
                        f"chapterUid={uid_str}" not in url
                        and f"chapterUid%3D{uid_str}" not in url
                    ):
                        return

                    try:
                        body: dict = await response.json()
                    except Exception:
                        return

                    html: str | None = (
                        body.get("data") or body.get("html") or body.get("content")
                    )
                    if html:
                        result = html
                        e3_done.set()
                        _debug_log(f"Browser e3 captured: {len(html)} chars")
                    return

                # ---- 新格式：e_N（AES 加密，由 JS 解密渲染到 DOM）----
                # 检测到 e_N 请求后只记录信号；实际内容从 DOM 提取。
                if _re.search(r"/book/chapter/e_\d+", url):
                    _debug_log(f"Browser e_N detected: {url}")
                    en_detected.set()

            page.on("response", _on_response)

            try:
                await page.goto(
                    f"https://weread.qq.com/web/reader/{reader_path}",
                    wait_until="domcontentloaded",
                    timeout=_NAV_TIMEOUT_MS,
                )
            except Exception:
                pass

            # 等待 e3 完成 或 检测到 e_N，或超时
            e3_task = asyncio.ensure_future(e3_done.wait())
            en_task = asyncio.ensure_future(en_detected.wait())
            await asyncio.wait(
                {e3_task, en_task},
                timeout=timeout * 0.6,  # 留 40% 时间给 DOM 提取
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in (e3_task, en_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

            # 新格式：DOM 提取
            if result is None and en_detected.is_set():
                _debug_log(f"e_N detected, waiting {_DOM_RENDER_WAIT}s for JS render...")
                await asyncio.sleep(_DOM_RENDER_WAIT)
                result = await _extract_dom_content(page, _debug_log)

        finally:
            await browser.close()

    if result is None:
        raise BrowserFetchError(
            f"Browser fallback 超时（{timeout:.0f}s）：\n"
            f"未能获取书 {book_id!r} 章节 {chapter_uid} 的内容。\n"
            "可能原因：bookKey 不正确、页面结构变更，或章节受 DRM 保护。"
        )

    return result
