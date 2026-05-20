"""
weread/api.py — 封装微信读书网页版 API 的所有请求。

所有方法均为 async，调用方需在异步上下文中使用，例如：
    async with WeReadClient(vid, skey) as client:
        shelf = await client.get_shelf()

端点优先级说明：
- 书架 / 书籍信息：使用 https://weread.qq.com/web/...（已验证可用）
- 章节列表：优先 POST https://i.weread.qq.com/book/chapterInfos，
            回退 GET /web/book/chapterInfos
- 章节正文：优先 GET https://i.weread.qq.com/book/chapter/e3，
            回退 GET /web/book/chapter/e3
"""

from __future__ import annotations

import datetime
from pathlib import Path

import httpx

__all__ = [
    "WeReadClient",
    "CookieExpiredError",
    "NetworkError",
    "DRMChapterError",
]

_BASE = "https://weread.qq.com"
# i.weread.qq.com 为移动端/内部 API 域名，部分端点仅在此可用
_BASE_I = "https://i.weread.qq.com"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_LOG_PATH = Path.home() / ".config" / "weread-tui" / "debug.log"


def _debug_log(message: str) -> None:
    """写入诊断日志到 ~/.config/weread-tui/debug.log（静默失败）。"""
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 自定义异常
# ---------------------------------------------------------------------------

class CookieExpiredError(Exception):
    """Cookie 已过期（服务器返回 errCode -2012 或 HTTP 401）。"""


class NetworkError(Exception):
    """网络超时或连接失败。"""


class DRMChapterError(Exception):
    """章节内容受 DRM 保护，无法读取。"""


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _check_cookie_expired(resp: httpx.Response) -> None:
    """
    检查响应是否表示 Cookie 过期，是则抛 CookieExpiredError。
    判断依据：HTTP 401 或响应体 errCode == -2012。
    """
    if resp.status_code == 401:
        raise CookieExpiredError("HTTP 401：Cookie 已过期，请执行 weread login 重新登录")
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("errCode") == -2012:
            raise CookieExpiredError("errCode -2012：Cookie 已过期，请执行 weread login 重新登录")
    except Exception as exc:
        # 若已经是 CookieExpiredError 则继续抛出
        if isinstance(exc, CookieExpiredError):
            raise
        # JSON 解析失败时忽略（正文可能不是 JSON）


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class WeReadClient:
    """
    封装微信读书网页版 API。

    用法（推荐 async with 上下文管理器）：
        async with WeReadClient(vid, skey) as client:
            data = await client.get_shelf()

    也可手动管理：
        client = WeReadClient(vid, skey)
        await client.aopen()
        ...
        await client.aclose()
    """

    def __init__(self, vid: str, skey: str, *, timeout: float = 15.0) -> None:
        self._vid = vid
        self._skey = skey
        self._timeout = timeout
        self._headers = {
            "Cookie": f"wr_vid={vid}; wr_skey={skey}",
            "User-Agent": _USER_AGENT,
            "Referer": "https://weread.qq.com/",
        }
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # 上下文管理器
    # ------------------------------------------------------------------

    async def aopen(self) -> "WeReadClient":
        self._client = httpx.AsyncClient(
            base_url=_BASE,
            headers=self._headers,
            timeout=self._timeout,
            follow_redirects=True,
        )
        return self

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "WeReadClient":
        return await self.aopen()

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # 内部请求辅助
    # ------------------------------------------------------------------

    async def _get(self, path: str, **params: str | int) -> httpx.Response:
        """
        执行 GET 请求，统一处理网络超时与 Cookie 过期。
        path 可以是相对路径（相对 weread.qq.com）或绝对 URL。
        """
        if self._client is None:
            raise RuntimeError("WeReadClient 未初始化，请使用 async with 或先调用 aopen()")
        try:
            resp = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise NetworkError(f"请求超时：{path}") from exc
        except httpx.NetworkError as exc:
            raise NetworkError(f"网络错误：{path} — {exc}") from exc
        _check_cookie_expired(resp)
        resp.raise_for_status()
        return resp

    async def _post_json(self, url: str, body: dict) -> httpx.Response:
        """
        执行 POST JSON 请求（使用绝对 URL）。
        用于 i.weread.qq.com 需要 JSON body 的端点。
        """
        if self._client is None:
            raise RuntimeError("WeReadClient 未初始化，请使用 async with 或先调用 aopen()")
        try:
            resp = await self._client.post(url, json=body)
        except httpx.TimeoutException as exc:
            raise NetworkError(f"请求超时：{url}") from exc
        except httpx.NetworkError as exc:
            raise NetworkError(f"网络错误：{url} — {exc}") from exc
        _check_cookie_expired(resp)
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------
    # 公开 API 方法
    # ------------------------------------------------------------------

    async def get_shelf(self) -> dict:
        """
        获取书架数据（书籍列表 + 阅读进度 + 分组归档）。

        端点：GET /web/shelf/sync?synckey=0&teenmode=0&albumTypes=1&crossDeviceSync=1
        返回原始 JSON dict，包含 books / bookProgress / archive 等字段。
        """
        resp = await self._get(
            "/web/shelf/sync",
            synckey=0,
            teenmode=0,
            albumTypes=1,
            crossDeviceSync=1,
        )
        return resp.json()

    async def get_book_info(self, book_id: str) -> dict:
        """
        获取书籍元信息（书名、作者、封面 URL、章节数等）。

        端点：GET /web/book/info?bookId={book_id}
        """
        resp = await self._get("/web/book/info", bookId=book_id)
        return resp.json()

    async def get_chapter_list(self, book_id: str) -> list:
        """
        获取章节列表（chapterUid、标题、字数等）。

        优先 POST https://i.weread.qq.com/book/chapterInfos（JSON body）
        回退  GET  /web/book/chapterInfos?bookIds={book_id}&synckeys=0

        两种响应格式均支持：
          {"data": [{"updated": [...]}]}       ← i.weread.qq.com POST
          {"data": [{"chapterInfos": [...]}]}  ← weread.qq.com/web GET
        """
        # ---- 尝试 i.weread.qq.com POST ----
        try:
            resp = await self._post_json(
                f"{_BASE_I}/book/chapterInfos",
                {"bookIds": [book_id], "synckeys": [0], "teenmode": 0},
            )
            body: dict = resp.json()
            data = body.get("data") or []
            if data and isinstance(data, list):
                first = data[0]
                if isinstance(first, dict):
                    # 优先 "updated"，兼容 "chapterInfos" / "chapters"
                    chapters = (
                        first.get("updated")
                        or first.get("chapterInfos")
                        or first.get("chapters")
                        or []
                    )
                    if chapters:
                        return chapters
        except Exception:
            pass  # 静默降级至 GET

        # ---- 回退：weread.qq.com/web GET ----
        resp = await self._get(
            "/web/book/chapterInfos",
            bookIds=book_id,
            synckeys=0,
        )
        body = resp.json()
        data = body.get("data") or []
        if data and isinstance(data, list):
            first = data[0]
            if isinstance(first, dict):
                chapters = (
                    first.get("chapterInfos")
                    or first.get("updated")
                    or first.get("chapters")
                    or []
                )
                if chapters:
                    return chapters
        return data

    async def get_chapter_content(self, book_id: str, chapter_uid: int) -> str:
        """
        获取章节正文 HTML。

        三级回退策略：
          1. GET https://i.weread.qq.com/book/chapter/e3?bookId=…&chapterUid=…
          2. GET /web/book/chapter/e3?bookId=…&chapterUid=…
          3. Playwright browser fallback（仅当两个端点均返回 404 时）

        返回 HTML 字符串（取响应 JSON 的 data / html / content 字段）。
        检测到 DRM 加密标记时抛 DRMChapterError（不走 browser fallback）。
        两个端点均 404 时先尝试 browser fallback，browser 也失败则抛 DRMChapterError。
        """
        last_exc: Exception | None = None

        for url in (
            f"{_BASE_I}/book/chapter/e3",   # 优先 i.weread.qq.com
            "/web/book/chapter/e3",          # 回退 weread.qq.com/web
        ):
            try:
                resp = await self._get(url, bookId=book_id, chapterUid=chapter_uid)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    _debug_log(
                        f"404 from {url} bookId={book_id} chapterUid={chapter_uid}"
                    )
                    last_exc = exc
                    continue  # 尝试下一个端点
                raise
            except Exception:
                raise

            body: dict = resp.json()
            _debug_log(
                f"Response keys from {url} bookId={book_id} "
                f"chapterUid={chapter_uid}: {list(body.keys())[:10]}"
            )

            # DRM 检测：直接抛出，不走 browser fallback（内容已加密，浏览器也无法解密）
            if body.get("encrypt") or body.get("encryptType") or body.get("errCode") == -2010:
                _debug_log(
                    f"DRM detected at {url}: "
                    f"encrypt={body.get('encrypt')}, "
                    f"encryptType={body.get('encryptType')}, "
                    f"errCode={body.get('errCode')}"
                )
                raise DRMChapterError(
                    f"章节 {chapter_uid} 已加密（DRM），无法在终端阅读"
                )

            html: str | None = body.get("data") or body.get("html") or body.get("content")
            if html:
                return html

            # 响应 200 但无正文字段，视为 DRM 或 API 变更，不走 browser fallback
            _debug_log(
                f"No content field in response from {url}: keys={list(body.keys())}"
            )
            raise DRMChapterError(
                f"章节 {chapter_uid} 无法获取正文（可能为 DRM 章节或 API 变更）"
            )

        # 两个端点均返回 404 → 尝试 browser fallback
        _debug_log(
            f"Both HTTP endpoints returned 404 for bookId={book_id} "
            f"chapterUid={chapter_uid}; trying Playwright browser fallback"
        )
        html = await self._browser_fallback(book_id, chapter_uid)
        if html is not None:
            return html

        # browser 也失败，抛原始 404 错误（消息保留 "404" 供测试匹配）
        raise DRMChapterError(
            f"章节 {chapter_uid} 暂不可用（两个端点均返回 404，"
            "可能为加密章节或该书不支持网页版阅读）"
        ) from last_exc

    async def _browser_fallback(self, book_id: str, chapter_uid: int) -> str | None:
        """
        尝试 Playwright browser fallback 获取章节正文。

        先通过 get_book_info 获取 bookKey（用于构造阅读器 URL），
        再调用 weread.browser.fetch_chapter_via_browser。
        任何失败均静默降级：返回 None，调用方负责抛出 DRMChapterError。
        """
        # 尝试获取 bookKey（用于阅读器 URL；失败时回退 book_id）
        book_key: str | None = None
        try:
            info = await self.get_book_info(book_id)
            book_key = info.get("bookKey") or None
            if book_key:
                _debug_log(f"Resolved bookKey={book_key!r} for bookId={book_id}")
        except Exception as exc:
            _debug_log(f"get_book_info failed (bookId={book_id}): {exc}")

        try:
            from weread.browser import BrowserFetchError, fetch_chapter_via_browser

            html = await fetch_chapter_via_browser(
                self._vid,
                self._skey,
                book_id,
                chapter_uid,
                book_key=book_key,
            )
            _debug_log(
                f"Browser fallback succeeded for bookId={book_id} "
                f"chapterUid={chapter_uid}, html length={len(html)}"
            )
            return html
        except Exception as exc:
            _debug_log(
                f"Browser fallback failed for bookId={book_id} "
                f"chapterUid={chapter_uid}: {exc}"
            )
            return None
