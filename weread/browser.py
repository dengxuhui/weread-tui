"""
weread/browser.py — Playwright headless Chromium 方案获取章节正文。

当直接 HTTP API 两个端点均返回 404 时，使用 Playwright 启动无头 Chromium，
注入 Cookie 后导航至微信读书网页阅读器，拦截浏览器发出的 /book/chapter/e3
XHR 响应，提取已由浏览器签名鉴权的正文 JSON。

Playwright 属于可选依赖（[browser] extra），未安装时立即抛 BrowserFetchError，
调用方（api.py）负责捕获并降级至原始 DRMChapterError。

工作流程：
  1. 启动 headless Chromium（playwright.async_api）
  2. 在新 BrowserContext 中注入 wr_vid / wr_skey Cookie
  3. 监听所有 response 事件，过滤 /book/chapter/e3 且 bookId+chapterUid 匹配的请求
  4. 导航至 https://weread.qq.com/web/reader/{book_key|book_id}
     WeRead SPA 会自动请求上次阅读章节的正文
  5. 等待目标 XHR 被拦截（asyncio.Event），或超时
  6. 解析 JSON，返回 data/html/content 字段
"""

from __future__ import annotations

import asyncio

__all__ = ["BrowserFetchError", "fetch_chapter_via_browser"]

_BROWSER_TIMEOUT = 30.0   # 等待 XHR 响应最长秒数
_NAV_TIMEOUT_MS = 20_000  # Playwright 页面导航超时 ms


class BrowserFetchError(Exception):
    """Playwright 方案获取章节失败（未安装 / 超时 / 无响应）。"""


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
    使用 Playwright headless Chromium 拦截 WeRead XHR 获取章节正文。

    参数
    -----
    vid, skey    : 微信读书认证 Cookie 字段
    book_id      : 书籍数字 ID 字符串（如 "695233"）
    chapter_uid  : 章节 UID（整数）
    book_key     : 微信读书阅读器 URL 中的 bookKey
                   （如 "9df3234072013be69df3ae1"）；
                   为 None 时回退使用 book_id 作为 URL 路径段
    timeout      : 等待 XHR 被拦截的最长秒数（默认 30s）

    返回
    -----
    str: 章节正文 HTML 字符串

    抛出
    -----
    BrowserFetchError:
      - playwright 未安装（提示用户执行安装命令）
      - 页面导航失败且超时内未捕获到目标响应
      - 目标 XHR 超时未出现
    """
    # 延迟导入：仅在调用时检查 playwright 是否已安装
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserFetchError(
            "playwright 未安装，无法使用浏览器回退。\n"
            "请执行以下命令安装：\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        ) from exc

    # 阅读器 URL 路径段：优先使用 bookKey，回退使用 bookId
    reader_path = book_key if book_key else book_id

    # 捕获结果的共享状态
    result: str | None = None
    done_event = asyncio.Event()

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

            # 注入微信读书认证 Cookie
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
                """拦截 response 事件，过滤目标章节 XHR。"""
                nonlocal result
                if done_event.is_set():
                    return

                url: str = response.url

                # 仅关注章节正文端点
                if "/book/chapter/e3" not in url:
                    return

                # 确认 bookId 匹配（明文或 URL 编码形式）
                if (
                    f"bookId={book_id}" not in url
                    and f"bookId%3D{book_id}" not in url
                ):
                    return

                # 确认 chapterUid 匹配
                uid_str = str(chapter_uid)
                if (
                    f"chapterUid={uid_str}" not in url
                    and f"chapterUid%3D{uid_str}" not in url
                ):
                    return

                # 提取正文
                try:
                    body: dict = await response.json()
                except Exception:
                    return

                html: str | None = (
                    body.get("data")
                    or body.get("html")
                    or body.get("content")
                )
                if html:
                    result = html
                    done_event.set()

            page.on("response", _on_response)

            # 导航至阅读器页面；WeRead SPA 会自动请求上次阅读章节的正文
            try:
                await page.goto(
                    f"https://weread.qq.com/web/reader/{reader_path}",
                    wait_until="domcontentloaded",
                    timeout=_NAV_TIMEOUT_MS,
                )
            except Exception:
                # 导航异常（网络抖动 / 超时）时继续等待 XHR；
                # SPA JS 可能已开始请求章节数据
                pass

            # 等待目标 XHR 被拦截，或超时
            try:
                await asyncio.wait_for(done_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

        finally:
            await browser.close()

    if result is None:
        raise BrowserFetchError(
            f"Browser fallback 超时（{timeout:.0f}s）：\n"
            f"未能在 {timeout:.0f}s 内捕获书 {book_id!r} 章节 {chapter_uid} 的 XHR 响应。\n"
            "可能原因：bookKey 不正确、章节真正受 DRM 保护，或 WeRead 页面结构变更。"
        )

    return result
