"""
tests/test_browser.py — weread.browser 模块单元测试。

所有测试通过 sys.modules 注入 mock playwright 模块实现，
不依赖 playwright 真实安装，也不启动真实浏览器。

覆盖场景：
  1. playwright 未安装 → BrowserFetchError（提示安装命令）
  2. 成功拦截 XHR → 返回正文 HTML
  3. XHR 等待超时 → BrowserFetchError（含"超时"）
  4. 响应 URL 的 bookId 不匹配 → 不捕获，超时
  5. 响应 URL 的 chapterUid 不匹配 → 不捕获，超时
  6. 提供 book_key 时使用 book_key 构造导航 URL
  7. book_key 为 None 时回退使用 book_id 构造导航 URL
  8. 导航抛异常时继续等待 XHR（SPA 可能已开始请求）
  9. XHR 响应 JSON 解析失败时忽略该响应
 10. 响应含 html 字段（非 data）时也能正确提取
 11. XHR URL 使用 bookKey（非数字 bookId）时，提供 book_key 后成功捕获
 12. XHR URL 的 bookId 既不匹配数字 ID 也不匹配 bookKey → 超时
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from weread.browser import BrowserFetchError, fetch_chapter_via_browser


# ---------------------------------------------------------------------------
# 辅助：构建完整的 Playwright mock 链
# ---------------------------------------------------------------------------

def _build_playwright_mock(
    *,
    response_url: str = "https://i.weread.qq.com/book/chapter/e3?bookId=12345&chapterUid=1",
    response_json: dict | None = None,
    fire_response: bool = True,
    nav_raises: Exception | None = None,
) -> tuple[MagicMock, list]:
    """
    构造 mock Playwright 对象链，返回 (mock_playwright_module, callbacks_list)。

    callbacks_list 在 page.on("response", cb) 被调用时会收集 cb，
    可在测试中直接检查。

    fire_response=True：page.goto() 执行后触发一次 response 事件。
    nav_raises：page.goto() 抛出此异常（用于测试导航失败场景）。
    """
    if response_json is None:
        response_json = {"data": "<p>章节内容</p>"}

    callbacks: list = []

    # --- mock Response ---
    mock_response = MagicMock()
    mock_response.url = response_url
    mock_response.json = AsyncMock(return_value=response_json)

    # --- mock Page ---
    mock_page = MagicMock()

    def _page_on(event: str, cb) -> None:
        if event == "response":
            callbacks.append(cb)

    mock_page.on = _page_on

    async def _goto(url: str, **kwargs) -> None:
        if nav_raises is not None:
            # 即使导航抛异常，也要在 sleep 后触发 response（模拟 SPA 已开始请求）
            if fire_response and callbacks:
                await asyncio.sleep(0)
                for cb in callbacks:
                    await cb(mock_response)
            raise nav_raises
        if fire_response and callbacks:
            await asyncio.sleep(0)
            for cb in callbacks:
                await cb(mock_response)

    mock_page.goto = _goto

    # --- mock BrowserContext ---
    mock_context = MagicMock()
    mock_context.add_cookies = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    # --- mock Browser ---
    mock_browser = MagicMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    # --- mock Playwright instance ---
    mock_p = MagicMock()
    mock_p.chromium.launch = AsyncMock(return_value=mock_browser)

    # --- mock playwright module (async_playwright 是上下文管理器) ---
    @asynccontextmanager
    async def mock_async_playwright():
        yield mock_p

    mock_pw_module = MagicMock()
    mock_pw_module.async_playwright = mock_async_playwright

    return mock_pw_module, callbacks


def _inject_playwright(mock_module: MagicMock):
    """将 mock playwright 模块注入 sys.modules，返回 patch.dict context。"""
    from unittest.mock import patch
    return patch.dict(
        sys.modules,
        {
            "playwright": mock_module,
            "playwright.async_api": mock_module,
        },
    )


# ---------------------------------------------------------------------------
# 1. playwright 未安装
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_playwright_not_installed():
    """playwright 未安装时，应立即抛出 BrowserFetchError 并提示安装命令。"""
    with pytest.raises(BrowserFetchError, match="未安装"):
        # 将 playwright.async_api 设为 None 模拟 ImportError
        from unittest.mock import patch
        with patch.dict(sys.modules, {"playwright": None, "playwright.async_api": None}):
            await fetch_chapter_via_browser("vid", "skey", "12345", 1)


# ---------------------------------------------------------------------------
# 2. 成功拦截 XHR → 返回正文 HTML
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_success_returns_html():
    """正常拦截 bookId + chapterUid 匹配的 XHR，返回 data 字段。"""
    mock_module, _ = _build_playwright_mock(
        response_url=(
            "https://i.weread.qq.com/book/chapter/e3"
            "?bookId=12345&chapterUid=1"
        ),
        response_json={"data": "<p>章节内容</p>"},
    )
    with _inject_playwright(mock_module):
        result = await fetch_chapter_via_browser("vid", "skey", "12345", 1)
    assert result == "<p>章节内容</p>"


# ---------------------------------------------------------------------------
# 3. XHR 等待超时
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_raises_error():
    """未能在 timeout 内捕获 XHR 时，应抛出 BrowserFetchError（含"超时"）。"""
    mock_module, _ = _build_playwright_mock(fire_response=False)  # 不触发 XHR

    with _inject_playwright(mock_module):
        with pytest.raises(BrowserFetchError, match="超时"):
            await fetch_chapter_via_browser(
                "vid", "skey", "12345", 1, timeout=0.05
            )


# ---------------------------------------------------------------------------
# 4. bookId 不匹配 → 不捕获，超时
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_book_id_not_captured():
    """XHR URL 中 bookId 与目标不符时，应忽略并超时。"""
    mock_module, _ = _build_playwright_mock(
        response_url=(
            "https://i.weread.qq.com/book/chapter/e3"
            "?bookId=99999&chapterUid=1"  # bookId 不匹配
        ),
    )
    with _inject_playwright(mock_module):
        with pytest.raises(BrowserFetchError, match="超时"):
            await fetch_chapter_via_browser(
                "vid", "skey", "12345", 1, timeout=0.05
            )


# ---------------------------------------------------------------------------
# 5. chapterUid 不匹配 → 不捕获，超时
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_chapter_uid_not_captured():
    """XHR URL 中 chapterUid 与目标不符时，应忽略并超时。"""
    mock_module, _ = _build_playwright_mock(
        response_url=(
            "https://i.weread.qq.com/book/chapter/e3"
            "?bookId=12345&chapterUid=999"  # chapterUid 不匹配
        ),
    )
    with _inject_playwright(mock_module):
        with pytest.raises(BrowserFetchError, match="超时"):
            await fetch_chapter_via_browser(
                "vid", "skey", "12345", 1, timeout=0.05
            )


# ---------------------------------------------------------------------------
# 6. book_key 提供时，导航 URL 使用 book_key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uses_book_key_in_url():
    """book_key 提供时，应以 book_key 作为阅读器 URL 路径段。"""
    goto_urls: list[str] = []
    mock_module, _ = _build_playwright_mock(
        response_url=(
            "https://i.weread.qq.com/book/chapter/e3"
            "?bookId=12345&chapterUid=1"
        ),
    )

    # 拦截 goto 以记录实际导航 URL
    mock_p_obj: MagicMock | None = None

    original_cm = mock_module.async_playwright

    @asynccontextmanager
    async def recording_cm():
        async with original_cm() as p:
            # 包装 chromium.launch 以捕获 page.goto
            original_launch = p.chromium.launch

            async def recording_launch(**kwargs):
                browser = await original_launch(**kwargs)
                original_new_context = browser.new_context

                async def recording_new_context(**kw):
                    ctx = await original_new_context(**kw)
                    original_new_page = ctx.new_page

                    async def recording_new_page():
                        page = await original_new_page()
                        original_goto = page.goto

                        async def recording_goto(url, **gkw):
                            goto_urls.append(url)
                            return await original_goto(url, **gkw)

                        page.goto = recording_goto
                        return page

                    ctx.new_page = recording_new_page
                    return ctx

                browser.new_context = recording_new_context
                return browser

            p.chromium.launch = recording_launch
            yield p

    mock_module.async_playwright = recording_cm

    with _inject_playwright(mock_module):
        await fetch_chapter_via_browser(
            "vid", "skey", "12345", 1, book_key="myBookKey123"
        )

    assert any("myBookKey123" in url for url in goto_urls), (
        f"Expected 'myBookKey123' in goto URLs, got: {goto_urls}"
    )


# ---------------------------------------------------------------------------
# 7. book_key 为 None 时回退使用 book_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uses_book_id_when_no_book_key():
    """book_key 为 None 时，导航 URL 应使用 book_id 作为路径段。"""
    goto_urls: list[str] = []
    mock_module, _ = _build_playwright_mock(
        response_url=(
            "https://i.weread.qq.com/book/chapter/e3"
            "?bookId=12345&chapterUid=1"
        ),
    )

    original_cm = mock_module.async_playwright

    @asynccontextmanager
    async def recording_cm():
        async with original_cm() as p:
            orig_launch = p.chromium.launch

            async def wrap_launch(**kw):
                br = await orig_launch(**kw)
                orig_nc = br.new_context

                async def wrap_nc(**kw2):
                    ctx = await orig_nc(**kw2)
                    orig_np = ctx.new_page

                    async def wrap_np():
                        pg = await orig_np()
                        orig_gt = pg.goto

                        async def wrap_gt(url, **kw3):
                            goto_urls.append(url)
                            return await orig_gt(url, **kw3)

                        pg.goto = wrap_gt
                        return pg

                    ctx.new_page = wrap_np
                    return ctx

                br.new_context = wrap_nc
                return br

            p.chromium.launch = wrap_launch
            yield p

    mock_module.async_playwright = recording_cm

    with _inject_playwright(mock_module):
        await fetch_chapter_via_browser(
            "vid", "skey", "12345", 1, book_key=None
        )

    assert any("12345" in url for url in goto_urls), (
        f"Expected '12345' in goto URLs, got: {goto_urls}"
    )


# ---------------------------------------------------------------------------
# 8. 导航抛异常时仍能捕获 XHR
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_navigation_failure_still_captures_xhr():
    """page.goto 抛出异常时，如果 XHR 已被触发，仍应成功返回正文。"""
    mock_module, _ = _build_playwright_mock(
        response_url=(
            "https://i.weread.qq.com/book/chapter/e3"
            "?bookId=12345&chapterUid=1"
        ),
        response_json={"data": "<p>导航异常但 XHR 成功</p>"},
        fire_response=True,
        nav_raises=Exception("Navigation timeout"),
    )
    with _inject_playwright(mock_module):
        result = await fetch_chapter_via_browser("vid", "skey", "12345", 1)
    assert result == "<p>导航异常但 XHR 成功</p>"


# ---------------------------------------------------------------------------
# 9. XHR 响应 JSON 解析失败时忽略该响应
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_json_parse_failure_is_ignored():
    """response.json() 抛出异常时，应跳过该响应并最终超时。"""
    mock_module, _ = _build_playwright_mock(
        response_url=(
            "https://i.weread.qq.com/book/chapter/e3"
            "?bookId=12345&chapterUid=1"
        ),
    )
    # 让 response.json 抛出异常
    mock_module.async_playwright  # just reference

    original_cm = mock_module.async_playwright

    @asynccontextmanager
    async def cm_with_bad_json():
        async with original_cm() as p:
            orig_launch = p.chromium.launch

            async def wrap_launch(**kw):
                br = await orig_launch(**kw)
                orig_nc = br.new_context

                async def wrap_nc(**kw2):
                    ctx = await orig_nc(**kw2)
                    orig_np = ctx.new_page

                    async def wrap_np():
                        pg = await orig_np()
                        orig_on = pg.on

                        captured_cbs: list = []

                        def patched_on(event, cb):
                            orig_on(event, cb)
                            if event == "response":
                                captured_cbs.append(cb)

                        pg.on = patched_on
                        orig_goto = pg.goto

                        async def bad_goto(url, **gkw):
                            # 触发响应但 JSON 解析失败
                            bad_resp = MagicMock()
                            bad_resp.url = (
                                "https://i.weread.qq.com/book/chapter/e3"
                                "?bookId=12345&chapterUid=1"
                            )
                            bad_resp.json = AsyncMock(
                                side_effect=ValueError("bad json")
                            )
                            await asyncio.sleep(0)
                            for cb in captured_cbs:
                                await cb(bad_resp)

                        pg.goto = bad_goto
                        return pg

                    ctx.new_page = wrap_np
                    return ctx

                br.new_context = wrap_nc
                return br

            p.chromium.launch = wrap_launch
            yield p

    mock_module.async_playwright = cm_with_bad_json

    with _inject_playwright(mock_module):
        with pytest.raises(BrowserFetchError, match="超时"):
            await fetch_chapter_via_browser(
                "vid", "skey", "12345", 1, timeout=0.05
            )


# ---------------------------------------------------------------------------
# 10. 响应含 html 字段（非 data）时正确提取
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_html_field_is_accepted():
    """响应 JSON 使用 html 字段时，也能正确提取正文。"""
    mock_module, _ = _build_playwright_mock(
        response_url=(
            "https://i.weread.qq.com/book/chapter/e3"
            "?bookId=12345&chapterUid=1"
        ),
        response_json={"html": "<p>来自 html 字段</p>"},
    )
    with _inject_playwright(mock_module):
        result = await fetch_chapter_via_browser("vid", "skey", "12345", 1)
    assert result == "<p>来自 html 字段</p>"


# ---------------------------------------------------------------------------
# 11. XHR URL 使用 bookKey 时仍能捕获（新版书籍场景）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_book_key_in_xhr_url_is_captured():
    """
    新版书籍 SPA 的 XHR 以 bookKey 作为 bookId 参数。
    传入 book_key 后过滤器应能匹配，成功返回正文。
    """
    book_id = "3300054813"
    book_key = "71e32c00813ab7be9g013f0e"
    mock_module, _ = _build_playwright_mock(
        response_url=(
            f"https://i.weread.qq.com/book/chapter/e3"
            f"?bookId={book_key}&chapterUid=315"  # SPA 用 bookKey 作为 bookId
        ),
        response_json={"data": "<p>通过 bookKey 获取的正文</p>"},
    )
    with _inject_playwright(mock_module):
        result = await fetch_chapter_via_browser(
            "vid", "skey", book_id, 315, book_key=book_key
        )
    assert result == "<p>通过 bookKey 获取的正文</p>"


# ---------------------------------------------------------------------------
# 12. XHR bookId 既不匹配数字 ID 也不匹配 bookKey → 超时
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_xhr_with_unmatched_id_not_captured():
    """
    XHR URL 的 bookId 既不是数字 bookId 也不是 bookKey，
    即使提供了 book_key 也不应捕获，最终超时。
    """
    mock_module, _ = _build_playwright_mock(
        response_url=(
            "https://i.weread.qq.com/book/chapter/e3"
            "?bookId=99999999&chapterUid=315"  # 完全不匹配的 ID
        ),
    )
    with _inject_playwright(mock_module):
        with pytest.raises(BrowserFetchError, match="超时"):
            await fetch_chapter_via_browser(
                "vid", "skey", "3300054813", 315,
                book_key="71e32c00813ab7be9g013f0e",
                timeout=0.05,
            )
