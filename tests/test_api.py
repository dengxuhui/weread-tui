"""tests/test_api.py — WeReadClient 单元测试（全部使用 mock，不需要真实网络）。"""

from __future__ import annotations

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from weread.api import (
    WeReadClient,
    CookieExpiredError,
    NetworkError,
    DRMChapterError,
    _check_cookie_expired,
)


# ---------------------------------------------------------------------------
# 辅助：构造带 request 的 httpx.Response（避免 raise_for_status 报 RuntimeError）
# ---------------------------------------------------------------------------

def _make_response(
    json_body: dict | list | None = None,
    status_code: int = 200,
) -> httpx.Response:
    dummy_request = httpx.Request("GET", "https://weread.qq.com/test")
    return httpx.Response(
        status_code=status_code,
        json=json_body if json_body is not None else {},
        request=dummy_request,
    )


# ---------------------------------------------------------------------------
# _check_cookie_expired
# ---------------------------------------------------------------------------

class TestCheckCookieExpired:
    def test_raises_on_http_401(self):
        resp = _make_response(status_code=401)
        with pytest.raises(CookieExpiredError):
            _check_cookie_expired(resp)

    def test_raises_on_err_code_minus_2012(self):
        resp = _make_response({"errCode": -2012})
        with pytest.raises(CookieExpiredError):
            _check_cookie_expired(resp)

    def test_no_raise_on_200_ok(self):
        resp = _make_response({"books": []})
        _check_cookie_expired(resp)  # 不应抛异常

    def test_no_raise_on_other_err_code(self):
        resp = _make_response({"errCode": -1})
        _check_cookie_expired(resp)  # 不应抛异常


# ---------------------------------------------------------------------------
# WeReadClient — 初始化与上下文管理器
# ---------------------------------------------------------------------------

class TestWeReadClientInit:
    def test_headers_contain_cookie(self):
        client = WeReadClient("vid1", "skey1")
        assert client._headers["Cookie"] == "wr_vid=vid1; wr_skey=skey1"
        assert "User-Agent" in client._headers
        assert "Referer" in client._headers

    def test_client_is_none_before_open(self):
        client = WeReadClient("v", "s")
        assert client._client is None

    @pytest.mark.asyncio
    async def test_aopen_creates_async_client(self):
        client = WeReadClient("v", "s")
        await client.aopen()
        assert client._client is not None
        await client.aclose()

    @pytest.mark.asyncio
    async def test_aclose_sets_client_to_none(self):
        client = WeReadClient("v", "s")
        await client.aopen()
        await client.aclose()
        assert client._client is None

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with WeReadClient("v", "s") as client:
            assert client._client is not None
        assert client._client is None

    @pytest.mark.asyncio
    async def test_get_without_open_raises_runtime_error(self):
        client = WeReadClient("v", "s")
        with pytest.raises(RuntimeError, match="未初始化"):
            await client._get("/some/path")


# ---------------------------------------------------------------------------
# 内部 _get：网络错误处理
# ---------------------------------------------------------------------------

class TestGetErrorHandling:
    @pytest.mark.asyncio
    async def test_timeout_raises_network_error(self):
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(  # type: ignore[union-attr]
                side_effect=httpx.TimeoutException("timeout")
            )
            with pytest.raises(NetworkError, match="超时"):
                await client._get("/web/shelf/sync")

    @pytest.mark.asyncio
    async def test_network_error_raises_network_error(self):
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(  # type: ignore[union-attr]
                side_effect=httpx.NetworkError("conn refused")
            )
            with pytest.raises(NetworkError, match="网络错误"):
                await client._get("/web/shelf/sync")

    @pytest.mark.asyncio
    async def test_401_raises_cookie_expired(self):
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(  # type: ignore[union-attr]
                return_value=_make_response(status_code=401)
            )
            with pytest.raises(CookieExpiredError):
                await client._get("/web/shelf/sync")

    @pytest.mark.asyncio
    async def test_err_code_2012_raises_cookie_expired(self):
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(  # type: ignore[union-attr]
                return_value=_make_response({"errCode": -2012})
            )
            with pytest.raises(CookieExpiredError):
                await client._get("/web/shelf/sync")

    @pytest.mark.asyncio
    async def test_500_raises_http_status_error(self):
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(  # type: ignore[union-attr]
                return_value=_make_response(status_code=500)
            )
            with pytest.raises(httpx.HTTPStatusError):
                await client._get("/web/shelf/sync")


# ---------------------------------------------------------------------------
# get_shelf
# ---------------------------------------------------------------------------

class TestGetShelf:
    @pytest.mark.asyncio
    async def test_returns_dict(self):
        payload = {"books": [{"bookId": "123", "title": "测试书"}], "bookProgress": []}
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            result = await client.get_shelf()
        assert result["books"][0]["bookId"] == "123"

    @pytest.mark.asyncio
    async def test_passes_correct_params(self):
        async with WeReadClient("v", "s") as client:
            mock_get = AsyncMock(return_value=_make_response({}))
            client._client.get = mock_get  # type: ignore
            await client.get_shelf()
        call_kwargs = mock_get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else {}
        # 验证端点路径正确
        assert mock_get.call_args.args[0] == "/web/shelf/sync"


# ---------------------------------------------------------------------------
# get_book_info
# ---------------------------------------------------------------------------

class TestGetBookInfo:
    @pytest.mark.asyncio
    async def test_returns_book_info(self):
        payload = {"bookId": "695233", "title": "三体全集", "author": "刘慈欣"}
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            result = await client.get_book_info("695233")
        assert result["title"] == "三体全集"

    @pytest.mark.asyncio
    async def test_passes_book_id_param(self):
        async with WeReadClient("v", "s") as client:
            mock_get = AsyncMock(return_value=_make_response({}))
            client._client.get = mock_get  # type: ignore
            await client.get_book_info("abc123")
        assert mock_get.call_args.args[0] == "/web/book/info"
        assert mock_get.call_args.kwargs["params"]["bookId"] == "abc123"


# ---------------------------------------------------------------------------
# get_chapter_list
# ---------------------------------------------------------------------------

class TestGetChapterList:
    @pytest.mark.asyncio
    async def test_returns_chapter_list(self):
        """GET 回退路径：响应包含 chapterInfos 字段。"""
        chapters = [{"chapterUid": 1, "title": "第一章"}, {"chapterUid": 2, "title": "第二章"}]
        payload = {"data": [{"chapterInfos": chapters}]}
        async with WeReadClient("v", "s") as client:
            # POST 失败 → 降级至 GET
            client._post_json = AsyncMock(side_effect=NetworkError("mock"))  # type: ignore
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            result = await client.get_chapter_list("695233")
        assert len(result) == 2
        assert result[0]["chapterUid"] == 1

    @pytest.mark.asyncio
    async def test_returns_from_post_with_updated_field(self):
        """POST 优先路径：i.weread.qq.com 响应包含 updated 字段。"""
        chapters = [{"chapterUid": 10, "title": "前言"}, {"chapterUid": 11, "title": "第一章"}]
        payload = {"data": [{"updated": chapters}]}
        async with WeReadClient("v", "s") as client:
            client._post_json = AsyncMock(return_value=_make_response(payload))  # type: ignore
            result = await client.get_chapter_list("695233")
        assert len(result) == 2
        assert result[0]["chapterUid"] == 10

    @pytest.mark.asyncio
    async def test_passes_book_id_param(self):
        """GET 回退路径，验证 bookId 参数。"""
        async with WeReadClient("v", "s") as client:
            client._post_json = AsyncMock(side_effect=NetworkError("mock"))  # type: ignore
            mock_get = AsyncMock(return_value=_make_response({"data": []}))
            client._client.get = mock_get  # type: ignore
            await client.get_chapter_list("book999")
        assert mock_get.call_args.args[0] == "/web/book/chapterInfos"
        assert mock_get.call_args.kwargs["params"]["bookIds"] == "book999"

    @pytest.mark.asyncio
    async def test_fallback_returns_data_directly_when_no_chapterInfos(self):
        """若 data 不含已知章节字段，直接返回 data 本身。"""
        raw_data = [{"something": "else"}]
        payload = {"data": raw_data}
        async with WeReadClient("v", "s") as client:
            client._post_json = AsyncMock(side_effect=NetworkError("mock"))  # type: ignore
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            result = await client.get_chapter_list("x")
        assert result == raw_data


# ---------------------------------------------------------------------------
# get_chapter_content
# ---------------------------------------------------------------------------

class TestGetChapterContent:
    @pytest.mark.asyncio
    async def test_returns_html_string(self):
        html = "<p>这是<strong>正文</strong>内容。</p>"
        payload = {"chapterUid": 109, "data": html}
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            result = await client.get_chapter_content("695233", 109)
        assert result == html

    @pytest.mark.asyncio
    async def test_passes_correct_params(self):
        """优先尝试 i.weread.qq.com 端点，验证参数。"""
        async with WeReadClient("v", "s") as client:
            mock_get = AsyncMock(return_value=_make_response({"data": "<p>text</p>"}))
            client._client.get = mock_get  # type: ignore
            await client.get_chapter_content("bid1", 42)
        # 第一次调用优先使用 i.weread.qq.com
        first_url = mock_get.call_args_list[0].args[0]
        assert "i.weread.qq.com" in first_url
        params = mock_get.call_args_list[0].kwargs["params"]
        assert params["bookId"] == "bid1"
        assert params["chapterUid"] == 42

    @pytest.mark.asyncio
    async def test_fallback_to_web_endpoint_on_first_404(self):
        """i.weread.qq.com 返回 404 时，应自动回退到 /web/book/chapter/e3。"""
        html = "<p>来自 web 端点的正文</p>"
        resp_404 = _make_response(status_code=404)
        resp_ok = _make_response({"data": html})

        call_count = 0

        async def get_side_effect(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return resp_404
            return resp_ok

        async with WeReadClient("v", "s") as client:
            client._client.get = get_side_effect  # type: ignore
            result = await client.get_chapter_content("bid1", 42)
        assert result == html
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_raises_drm_on_encrypt_flag(self):
        payload = {"encrypt": 1, "encryptType": "aes"}
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            with pytest.raises(DRMChapterError):
                await client.get_chapter_content("b", 1)

    @pytest.mark.asyncio
    async def test_raises_drm_on_err_code_2010(self):
        payload = {"errCode": -2010}
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            with pytest.raises(DRMChapterError):
                await client.get_chapter_content("b", 1)

    @pytest.mark.asyncio
    async def test_raises_drm_when_no_data_field(self):
        """响应体里没有 data/html/content，视为加密章节。"""
        payload = {"chapterUid": 1, "status": "ok"}
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            with pytest.raises(DRMChapterError):
                await client.get_chapter_content("b", 1)

    @pytest.mark.asyncio
    async def test_accepts_html_field_as_fallback(self):
        """支持 html 字段作为正文备选。"""
        html = "<p>备选字段</p>"
        payload = {"html": html}
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            result = await client.get_chapter_content("b", 1)
        assert result == html

    @pytest.mark.asyncio
    async def test_raises_drm_on_404(self):
        """章节端点返回 404 时，应转换为 DRMChapterError（而非原始 httpx 错误）。"""
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(  # type: ignore
                return_value=_make_response(status_code=404)
            )
            client._browser_fallback = AsyncMock(return_value=None)  # type: ignore
            with pytest.raises(DRMChapterError, match="404"):
                await client.get_chapter_content("3300054813", 315)

    @pytest.mark.asyncio
    async def test_retries_with_book_key_on_404(self):
        """数字 bookId 两端点均 404 时，应用 get_book_info 返回的 bookKey 重试并成功。"""
        html_content = "<p>通过 bookKey 获取的正文</p>"
        side_effects = [
            _make_response(status_code=404),               # i.weread.qq.com + 数字 bookId
            _make_response(status_code=404),               # /web + 数字 bookId
            _make_response({"encodeId": "71e32c00813ab7be9g013f0e"}),  # get_book_info
            _make_response({"data": html_content}),        # i.weread.qq.com + bookKey → 成功
        ]
        # 在 async with 外保存 mock 引用，避免 aclose() 后 _client 变 None
        mock_get = AsyncMock(side_effect=side_effects)
        async with WeReadClient("v", "s") as client:
            client._client.get = mock_get  # type: ignore
            result = await client.get_chapter_content("3300054813", 315)
        assert result == html_content
        # 第 4 次调用（index 3）应使用 bookKey 而非数字 bookId
        fourth_call_params = mock_get.call_args_list[3].kwargs["params"]
        assert fourth_call_params["bookId"] == "71e32c00813ab7be9g013f0e"
        assert fourth_call_params["chapterUid"] == 315

    @pytest.mark.asyncio
    async def test_skips_book_key_when_book_info_fails(self):
        """get_book_info 请求失败时，直接跳过 bookKey 重试，进入 browser fallback。"""
        async with WeReadClient("v", "s") as client:
            # 所有 HTTP 请求均 404
            client._client.get = AsyncMock(  # type: ignore
                return_value=_make_response(status_code=404)
            )
            # browser fallback 返回 None（模拟 playwright 未安装）
            client._browser_fallback = AsyncMock(return_value=None)  # type: ignore
            with pytest.raises(DRMChapterError):
                await client.get_chapter_content("3300054813", 315)
        # browser_fallback 应被调用一次
        client._browser_fallback.assert_called_once()

    @pytest.mark.asyncio
    async def test_browser_fallback_called_when_book_key_also_404(self):
        """bookKey 重试也 404 时，应继续进入 browser fallback。"""
        html_content = "<p>浏览器获取的正文</p>"
        side_effects = [
            _make_response(status_code=404),                          # i.weread.qq.com + 数字 bookId
            _make_response(status_code=404),                          # /web + 数字 bookId
            _make_response({"encodeId": "71e32c00813ab7be9g013f0e"}),  # get_book_info
            _make_response(status_code=404),                          # i.weread.qq.com + bookKey
            _make_response(status_code=404),                          # /web + bookKey
        ]
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(side_effect=side_effects)  # type: ignore
            client._browser_fallback = AsyncMock(return_value=html_content)  # type: ignore
            client._fetch_chapter_html_segmented = AsyncMock(return_value=None)  # type: ignore
            result = await client.get_chapter_content("3300054813", 315)
        assert result == html_content
        # browser_fallback 调用时应传入已解析的 book_key，避免重复请求
        call_kwargs = client._browser_fallback.call_args.kwargs
        assert call_kwargs["book_key"] == "71e32c00813ab7be9g013f0e"


# ---------------------------------------------------------------------------
# _fetch_chapter_html（新辅助方法）
# ---------------------------------------------------------------------------

class TestFetchChapterHtml:
    @pytest.mark.asyncio
    async def test_returns_html_from_first_endpoint(self):
        html = "<p>正文</p>"
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(return_value=_make_response({"data": html}))  # type: ignore
            result = await client._fetch_chapter_html("695233", 1)
        assert result == html

    @pytest.mark.asyncio
    async def test_falls_back_to_second_endpoint(self):
        """第一个端点 404 时自动尝试第二个端点。"""
        html = "<p>来自 web 端点</p>"
        side_effects = [
            _make_response(status_code=404),
            _make_response({"data": html}),
        ]
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(side_effect=side_effects)  # type: ignore
            result = await client._fetch_chapter_html("695233", 1)
        assert result == html

    @pytest.mark.asyncio
    async def test_returns_none_when_both_endpoints_404(self):
        """两个端点均 404 时返回 None（由调用方决定后续策略）。"""
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(  # type: ignore
                return_value=_make_response(status_code=404)
            )
            result = await client._fetch_chapter_html("695233", 1)
        assert result is None

    @pytest.mark.asyncio
    async def test_raises_drm_on_encrypt_flag(self):
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(  # type: ignore
                return_value=_make_response({"encrypt": 1, "encryptType": "aes"})
            )
            with pytest.raises(DRMChapterError):
                await client._fetch_chapter_html("b", 1)

    @pytest.mark.asyncio
    async def test_raises_drm_when_no_content_field(self):
        """响应 200 但无 data/html/content 字段时，视为异常并抛出 DRMChapterError。"""
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(  # type: ignore
                return_value=_make_response({"chapterUid": 1, "status": "ok"})
            )
            with pytest.raises(DRMChapterError):
                await client._fetch_chapter_html("b", 1)
