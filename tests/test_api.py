"""tests/test_api.py — WeReadClient 单元测试（mock 驱动）。"""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock

from weread.api import (
    CookieExpiredError,
    DRMChapterError,
    NetworkError,
    WeReadClient,
    _check_cookie_expired,
)


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


class TestCheckCookieExpired:
    def test_raises_on_http_401(self):
        with pytest.raises(CookieExpiredError):
            _check_cookie_expired(_make_response(status_code=401))

    def test_raises_on_err_code_minus_2012(self):
        with pytest.raises(CookieExpiredError):
            _check_cookie_expired(_make_response({"errCode": -2012}))

    def test_no_raise_on_200_ok(self):
        _check_cookie_expired(_make_response({"books": []}))


class TestWeReadClientBasics:
    def test_headers_contain_cookie(self):
        client = WeReadClient("vid1", "skey1")
        assert client._headers["Cookie"] == "wr_vid=vid1; wr_skey=skey1"

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with WeReadClient("v", "s") as client:
            assert client._client is not None
        assert client._client is None


class TestGetAndErrorHandling:
    @pytest.mark.asyncio
    async def test_timeout_raises_network_error(self):
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))  # type: ignore[union-attr]
            with pytest.raises(NetworkError, match="超时"):
                await client._get("/web/shelf/sync")

    @pytest.mark.asyncio
    async def test_network_error_raises_network_error(self):
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(side_effect=httpx.NetworkError("boom"))  # type: ignore[union-attr]
            with pytest.raises(NetworkError, match="网络错误"):
                await client._get("/web/shelf/sync")


class TestShelfAndBookInfo:
    @pytest.mark.asyncio
    async def test_get_shelf(self):
        payload = {"books": [{"bookId": "123"}], "bookProgress": []}
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            result = await client.get_shelf()
        assert result["books"][0]["bookId"] == "123"

    @pytest.mark.asyncio
    async def test_get_book_info(self):
        payload = {"bookId": "695233", "title": "三体全集"}
        async with WeReadClient("v", "s") as client:
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            result = await client.get_book_info("695233")
        assert result["title"] == "三体全集"


class TestGetChapterList:
    @pytest.mark.asyncio
    async def test_prefers_post_updated_field(self):
        chapters = [{"chapterUid": 10, "title": "前言"}]
        payload = {"data": [{"updated": chapters}]}
        async with WeReadClient("v", "s") as client:
            client._post_json = AsyncMock(return_value=_make_response(payload))  # type: ignore
            result = await client.get_chapter_list("695233")
        assert result[0]["chapterUid"] == 10

    @pytest.mark.asyncio
    async def test_falls_back_to_get_chapter_infos(self):
        chapters = [{"chapterUid": 1, "title": "第一章"}]
        payload = {"data": [{"chapterInfos": chapters}]}
        async with WeReadClient("v", "s") as client:
            client._post_json = AsyncMock(side_effect=NetworkError("mock"))  # type: ignore
            client._client.get = AsyncMock(return_value=_make_response(payload))  # type: ignore
            result = await client.get_chapter_list("695233")
        assert result[0]["chapterUid"] == 1


class TestGetChapterContentPlaywrightPrimary:
    @pytest.mark.asyncio
    async def test_uses_browser_pipeline_with_resolved_book_key(self):
        async with WeReadClient("v", "s") as client:
            client.get_book_info = AsyncMock(return_value={"encodeId": "book_key_abc"})  # type: ignore
            client._browser_fallback = AsyncMock(return_value="<p>ok</p>")  # type: ignore
            result = await client.get_chapter_content("3300054813", 318)
        assert result == "<p>ok</p>"
        kwargs = client._browser_fallback.call_args.kwargs
        assert kwargs["book_key"] == "book_key_abc"

    @pytest.mark.asyncio
    async def test_still_uses_browser_when_get_book_info_fails(self):
        async with WeReadClient("v", "s") as client:
            client.get_book_info = AsyncMock(side_effect=Exception("boom"))  # type: ignore
            client._browser_fallback = AsyncMock(return_value="<p>ok</p>")  # type: ignore
            result = await client.get_chapter_content("3300054813", 318)
        assert result == "<p>ok</p>"
        kwargs = client._browser_fallback.call_args.kwargs
        assert kwargs["book_key"] is None

    @pytest.mark.asyncio
    async def test_raises_drm_when_browser_returns_none(self):
        async with WeReadClient("v", "s") as client:
            client.get_book_info = AsyncMock(return_value={"encodeId": "book_key_abc"})  # type: ignore
            client._browser_fallback = AsyncMock(return_value=None)  # type: ignore
            with pytest.raises(DRMChapterError, match="浏览器链路"):
                await client.get_chapter_content("3300054813", 318)
