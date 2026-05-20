"""
weread/api.py — 封装微信读书网页版 API 的所有请求。

所有方法均为 async，调用方需在异步上下文中使用，例如：
    async with WeReadClient(vid, skey) as client:
        shelf = await client.get_shelf()

端点说明：
- 书架 / 书籍信息 / 章节目录：使用 https://weread.qq.com/web/... 和
  https://i.weread.qq.com/... 组合回退
- 章节正文：统一通过 Playwright 打开网页版阅读器，由前端解密后从 DOM 提取
  （不再依赖 e3 / e_N 直连 HTTP 返回正文）
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


def _build_chapters_from_outline(uids: list[int], items_array: list[dict]) -> list[dict]:
    """
    从 /web/book/outline 的 itemsArray 响应构建章节列表。

    uids[i] 与 items_array[i] 是位置对应关系（positional mapping）。
    每个条目的第一个 level<=2 的 items.text 作为章节标题；
    若该条目无 items 字段（纯占位节点）则回退到 "章节 {uid}"。
    """
    chapters: list[dict] = []
    for i, uid in enumerate(uids):
        entry = items_array[i] if i < len(items_array) else {}
        title = ""
        for item in entry.get("items", []):
            if isinstance(item, dict) and item.get("level", 99) <= 2:
                title = item.get("text", "").strip()
                if title:
                    break
        if not title:
            title = f"章节 {uid}"
        chapters.append({"chapterUid": uid, "title": title})
    return chapters


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
        _debug_log(f"[GET] {path} params={params}")
        try:
            resp = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            _debug_log(f"[GET] TIMEOUT {path}")
            raise NetworkError(f"请求超时：{path}") from exc
        except httpx.NetworkError as exc:
            _debug_log(f"[GET] NETWORK_ERROR {path}: {exc}")
            raise NetworkError(f"网络错误：{path} — {exc}") from exc
        _debug_log(
            f"[GET] {path} → {resp.status_code} "
            f"ct={resp.headers.get('content-type', '')!r} "
            f"body[:3000]={resp.text[:3000]!r}"
        )
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
        _debug_log(f"[POST] {url} body_sent={body!r}")
        try:
            resp = await self._client.post(url, json=body)
        except httpx.TimeoutException as exc:
            _debug_log(f"[POST] TIMEOUT {url}")
            raise NetworkError(f"请求超时：{url}") from exc
        except httpx.NetworkError as exc:
            _debug_log(f"[POST] NETWORK_ERROR {url}: {exc}")
            raise NetworkError(f"网络错误：{url} — {exc}") from exc
        _debug_log(
            f"[POST] {url} → {resp.status_code} "
            f"ct={resp.headers.get('content-type', '')!r} "
            f"body[:3000]={resp.text[:3000]!r}"
        )
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

        依次尝试多个端点，全部失败时返回 []（不抛异常）：
          1. POST https://i.weread.qq.com/book/chapterInfos（JSON body）
          2. GET  /web/book/chapterInfos?bookIds={book_id}&synckeys=0
          3. POST /web/book/outline（bookId 在 body）
          4. POST /web/book/outline（bookKey 在 body，需先解析 encodeId）

        支持的响应格式：
          {"data": [{"updated": [...]}]}       ← i.weread.qq.com POST
          {"data": [{"chapterInfos": [...]}]}  ← weread.qq.com/web GET
          {"chapterInfos": [...]}              ← /web/book/outline
        """

        def _extract_chapters(body: dict) -> list:
            """从各种响应格式中提取章节列表。"""
            # 顶层直接是列表字段
            chapters = (
                body.get("chapterInfos")
                or body.get("updated")
                or body.get("chapters")
                or []
            )
            if chapters:
                return chapters
            # 带 data 包装
            inner = body.get("data") or []
            if inner and isinstance(inner, list):
                first = inner[0] if isinstance(inner[0], dict) else {}
                chapters = (
                    first.get("chapterInfos")
                    or first.get("updated")
                    or first.get("chapters")
                    or []
                )
            return chapters

        # ---- 1. i.weread.qq.com POST ----
        try:
            _debug_log(f"get_chapter_list [1] POST i.weread bookId={book_id}")
            resp = await self._post_json(
                f"{_BASE_I}/book/chapterInfos",
                {"bookIds": [book_id], "synckeys": [0], "teenmode": 0},
            )
            body: dict = resp.json()
            _debug_log(f"get_chapter_list [1] top-level keys={list(body.keys())}")
            chapters = _extract_chapters(body)
            if chapters:
                _debug_log(f"get_chapter_list [1] OK: {len(chapters)} chapters, first={chapters[0]!r}")
                return chapters
            _debug_log(f"get_chapter_list [1] no chapters in response, full_body={resp.text!r}")
        except Exception as exc:
            _debug_log(f"get_chapter_list [1] failed: {type(exc).__name__}: {exc}")

        # ---- 2. weread.qq.com/web GET ----
        try:
            _debug_log(f"get_chapter_list [2] GET /web/book/chapterInfos bookId={book_id}")
            resp = await self._get(
                "/web/book/chapterInfos",
                bookIds=book_id,
                synckeys=0,
            )
            body = resp.json()
            _debug_log(f"get_chapter_list [2] top-level keys={list(body.keys())}")
            chapters = _extract_chapters(body)
            if chapters:
                _debug_log(f"get_chapter_list [2] OK: {len(chapters)} chapters, first={chapters[0]!r}")
                return chapters
            _debug_log(f"get_chapter_list [2] no chapters in response, full_body={resp.text!r}")
        except Exception as exc:
            _debug_log(f"get_chapter_list [2] failed: {type(exc).__name__}: {exc}")

        # ---- 3. POST /web/book/outline（bookId）----
        try:
            _debug_log(f"get_chapter_list [3] POST /web/book/outline bookId={book_id}")
            resp = await self._post_json(
                f"{_BASE}/web/book/outline",
                {"bookId": book_id},
            )
            body = resp.json()
            _debug_log(f"get_chapter_list [3] top-level keys={list(body.keys())}")
            chapters = _extract_chapters(body)
            if chapters:
                _debug_log(f"get_chapter_list [3] OK: {len(chapters)} chapters, first={chapters[0]!r}")
                return chapters
            _debug_log(f"get_chapter_list [3] no chapters in response, full_body={resp.text!r}")
        except Exception as exc:
            _debug_log(f"get_chapter_list [3] failed: {type(exc).__name__}: {exc}")

        # ---- 4. POST /web/book/outline（bookKey / encodeId）----
        book_key: str | None = None
        _book_info: dict = {}
        try:
            _book_info = await self.get_book_info(book_id)
            book_key = _book_info.get("encodeId") or None
            _debug_log(
                f"get_chapter_list [4] get_book_info OK: "
                f"encodeId={book_key!r} "
                f"lastChapterIdx={_book_info.get('lastChapterIdx')!r} "
                f"chapterCount={_book_info.get('chapterCount')!r}"
            )
        except Exception as exc:
            _debug_log(f"get_chapter_list [4] get_book_info failed: {type(exc).__name__}: {exc}")

        if book_key and book_key != book_id:
            try:
                _debug_log(f"get_chapter_list [4] POST /web/book/outline bookKey={book_key}")
                resp = await self._post_json(
                    f"{_BASE}/web/book/outline",
                    {"bookId": book_key},
                )
                body = resp.json()
                _debug_log(f"get_chapter_list [4] top-level keys={list(body.keys())}")
                chapters = _extract_chapters(body)
                if chapters:
                    _debug_log(f"get_chapter_list [4] OK: {len(chapters)} chapters, first={chapters[0]!r}")
                    return chapters
                _debug_log(f"get_chapter_list [4] no chapters in response, full_body={resp.text!r}")
            except Exception as exc:
                _debug_log(f"get_chapter_list [4] failed: {type(exc).__name__}: {exc}")

        # ---- 5. POST /web/book/outline（bookId + chapterUids list）----
        # 浏览器实际发送的请求：{"bookId": "827159", "chapterUids": [2, 3, ..., 59]}
        # lastChapterIdx 来自 get_book_info，uids = range(2, lastChapterIdx+1)
        last_idx = (
            _book_info.get("lastChapterIdx")
            or _book_info.get("chapterCount")
            or 0
        )
        if last_idx and int(last_idx) > 1:
            uids = list(range(2, int(last_idx) + 1))
            try:
                _debug_log(
                    f"get_chapter_list [5] POST /web/book/outline bookId={book_id} "
                    f"uids[0:5]={uids[:5]} total={len(uids)}"
                )
                resp = await self._post_json(
                    f"{_BASE}/web/book/outline",
                    {"bookId": book_id, "chapterUids": uids},
                )
                body = resp.json()
                _debug_log(f"get_chapter_list [5] top-level keys={list(body.keys())}")
                items_array = body.get("itemsArray") or []
                if items_array:
                    chapters = _build_chapters_from_outline(uids, items_array)
                    if chapters:
                        _debug_log(
                            f"get_chapter_list [5] OK: {len(chapters)} chapters, "
                            f"first={chapters[0]!r}"
                        )
                        return chapters
                _debug_log(
                    f"get_chapter_list [5] no chapters: "
                    f"itemsArray len={len(items_array)}, "
                    f"body[:500]={resp.text[:500]!r}"
                )
            except Exception as exc:
                _debug_log(f"get_chapter_list [5] failed: {type(exc).__name__}: {exc}")

        _debug_log(f"get_chapter_list all attempts failed for bookId={book_id}, returning []")
        return []

    async def get_chapter_content(self, book_id: str, chapter_uid: int) -> str:
        """
        获取章节正文 HTML（Playwright 主链路）。

        说明：
          - 由于网页版章节 HTTP 端点（e3 / e_N）经常变更、401/404 波动，
            本方法不再走直连 HTTP 拉正文。
          - 统一使用浏览器上下文触发微信读书前端解密并从 DOM 提取正文。
        """
        book_key: str | None = None
        try:
            info = await self.get_book_info(book_id)
            book_key = info.get("encodeId") or None
            if book_key:
                _debug_log(f"Resolved bookKey={book_key!r} for bookId={book_id}")
        except Exception as exc:
            _debug_log(f"get_book_info failed (bookId={book_id}): {exc}")

        _debug_log(
            f"Using Playwright primary pipeline for chapter content: "
            f"bookId={book_id} bookKey={book_key} chapterUid={chapter_uid}"
        )
        html = await self._browser_fallback(book_id, chapter_uid, book_key=book_key)
        if html is not None:
            return html

        raise DRMChapterError(
            f"章节 {chapter_uid} 暂不可用（浏览器链路未获取到正文，"
            "可能为 DRM 章节或页面结构变更）"
        )

    async def _browser_fallback(
        self, book_id: str, chapter_uid: int, *, book_key: str | None = None
    ) -> str | None:
        """
        尝试 Playwright browser fallback 获取章节正文。
        book_key 若已在上层解析，直接传入避免重复请求 get_book_info。
        任何失败均静默降级：返回 None，调用方负责抛出 DRMChapterError。
        """
        if book_key is None:
            try:
                info = await self.get_book_info(book_id)
                book_key = info.get("encodeId") or None
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
