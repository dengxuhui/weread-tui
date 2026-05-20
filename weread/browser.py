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
import html as _html
import hashlib
import json
import re as _re

__all__ = ["BrowserFetchError", "fetch_chapter_via_browser"]

_BROWSER_TIMEOUT = 40.0   # 总超时秒数（含 DOM 渲染等待）
_NAV_TIMEOUT_MS = 20_000  # Playwright 页面导航超时 ms
_DOM_RENDER_WAIT = 3.0    # e_N 最后一个请求后等待 JS 渲染完成的秒数
_DOM_POLL_INTERVAL = 0.5  # 轮询 DOM 内容的间隔秒数
_DOM_MIN_TEXT_LEN = 300   # DOM 文本量超过此值才认为内容已渲染

_CANVAS_CAPTURE_INIT_SCRIPT = r"""
(() => {
  if (window.__wrCanvasCaptureInstalled) return;
  window.__wrCanvasCaptureInstalled = true;

  const state = {
    records: [],
    maxRecords: 120000,
    page: 0,
    lastPageAt: 0,
  };

  function pushRecord(text, x, y, font, color) {
    const t = String(text || "").trim();
    if (!t) return;
    if (state.records.length >= state.maxRecords) return;
    state.records.push({
      text: t,
      x: Number.isFinite(Number(x)) ? Number(x) : 0,
      y: Number.isFinite(Number(y)) ? Number(y) : 0,
      font: String(font || ""),
      color: String(color || ""),
      ts: Date.now(),
      page: state.page,
      idx: state.records.length,
    });
  }

  function markNewPage() {
    const now = Date.now();
    if (now - state.lastPageAt < 120) return;
    state.page += 1;
    state.lastPageAt = now;
  }

  function wrapContext(ctx) {
    if (!ctx || ctx.__wrCanvasWrapped || typeof ctx.fillText !== "function") return ctx;
    const origFillText = ctx.fillText.bind(ctx);
    const origClearRect = typeof ctx.clearRect === "function"
      ? ctx.clearRect.bind(ctx)
      : null;
    ctx.fillText = function (text, x, y, maxWidth) {
      try {
        pushRecord(text, x, y, ctx.font, ctx.fillStyle);
      } catch (_) {}
      return origFillText(text, x, y, maxWidth);
    };
    if (origClearRect) {
      ctx.clearRect = function (x, y, w, h) {
        try {
          markNewPage();
        } catch (_) {}
        return origClearRect(x, y, w, h);
      };
    }
    ctx.__wrCanvasWrapped = true;
    return ctx;
  }

  const origGetContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (...args) {
    const ctx = origGetContext.apply(this, args);
    return wrapContext(ctx);
  };

  window.__wrCanvasCapture = {
    clear() {
      state.records = [];
    },
    stats() {
      return {
        count: state.records.length,
        page: state.page,
      };
    },
    exportText() {
      const groups = new Map();
      for (const row of state.records) {
        const key = String(row.page || 0);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(row);
      }

      const pageKeys = [...groups.keys()]
        .map((x) => Number(x))
        .sort((a, b) => a - b);
      const cleaned = [];
      let columnMode = "single";

      for (const pageNo of pageKeys) {
        const pageRows = (groups.get(String(pageNo)) || []).slice().sort((a, b) => {
          if (Math.abs(a.y - b.y) > 1) return a.y - b.y;
          return a.x - b.x;
        });

        const lines = [];
        let cur = [];
        let curY = null;
        for (const row of pageRows) {
          if (curY === null || Math.abs(row.y - curY) <= 2) {
            cur.push(row);
            curY = curY === null ? row.y : (curY + row.y) / 2;
            continue;
          }
          cur.sort((a, b) => a.x - b.x);
          lines.push({
            text: cur.map((x) => x.text).join(""),
            x: cur.length ? cur[0].x : 0,
            y: curY || 0,
            idx: cur.length ? cur[0].idx : 0,
          });
          cur = [row];
          curY = row.y;
        }
        if (cur.length) {
          cur.sort((a, b) => a.x - b.x);
          lines.push({
            text: cur.map((x) => x.text).join(""),
            x: cur.length ? cur[0].x : 0,
            y: curY || 0,
            idx: cur.length ? cur[0].idx : 0,
          });
        }

        let pageLines = lines
          .map((x) => ({
            text: x.text.replace(/\s+/g, " ").trim(),
            x: x.x,
            y: x.y,
            idx: x.idx,
          }))
          .filter((x) => x.text.length > 0);

        if (pageLines.length >= 10) {
          const xs = pageLines.map((x) => x.x).sort((a, b) => a - b);
          let splitX = null;
          let maxGap = 0;
          for (let i = 1; i < xs.length; i += 1) {
            const gap = xs[i] - xs[i - 1];
            if (gap > maxGap) {
              maxGap = gap;
              splitX = (xs[i] + xs[i - 1]) / 2;
            }
          }

          if (splitX !== null && maxGap >= 120) {
            const left = pageLines
              .filter((x) => x.x < splitX)
              .sort((a, b) => (Math.abs(a.y - b.y) > 1 ? a.y - b.y : a.idx - b.idx));
            const right = pageLines
              .filter((x) => x.x >= splitX)
              .sort((a, b) => (Math.abs(a.y - b.y) > 1 ? a.y - b.y : a.idx - b.idx));

            if (left.length >= 4 && right.length >= 4) {
              columnMode = "dual";
              const leftAvg = left.reduce((acc, x) => acc + x.x, 0) / left.length;
              const rightAvg = right.reduce((acc, x) => acc + x.x, 0) / right.length;
              pageLines = leftAvg <= rightAvg ? [...left, ...right] : [...right, ...left];
            } else {
              pageLines.sort((a, b) => (Math.abs(a.y - b.y) > 1 ? a.y - b.y : a.idx - b.idx));
            }
          } else {
            pageLines.sort((a, b) => (Math.abs(a.y - b.y) > 1 ? a.y - b.y : a.idx - b.idx));
          }
        } else {
          pageLines.sort((a, b) => (Math.abs(a.y - b.y) > 1 ? a.y - b.y : a.idx - b.idx));
        }

        for (const line of pageLines) {
          const last = cleaned.length ? cleaned[cleaned.length - 1] : "";
          if (last !== line.text) cleaned.push(line.text);
        }
      }

      const text = cleaned.join("\n");
      return {
        lineCount: cleaned.length,
        text,
        columnMode,
      };
    },
  };
})();
"""


def _looks_like_finished_overlay_html(html: str) -> bool:
    """粗略判断提取结果是否是“全书完”覆盖层，而非章节正文。"""
    sample = html[:1200]
    compact = "".join(sample.split())
    return (
        "全书完" in compact
        and "已阅读" in compact
        and ("推荐值" in compact or "点评" in compact)
    )


def _plain_text_to_html(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    blocks = [line for line in lines if line]
    if not blocks:
        return ""
    return "".join(f"<p>{_html.escape(line)}</p>" for line in blocks)


def _wr_hash(s: str) -> str:
    """与网页 reader 路由兼容的章节 hash（参考 weread-exporter）。"""
    hash_str = hashlib.md5(s.encode()).hexdigest()
    result = hash_str[:3] + "32" + hash_str[-2:]
    chunks: list[str] = []
    for i in range(0, len(s), 9):
        chunks.append(f"{int(s[i : min(i + 9, len(s))]):x}")
    for i, item in enumerate(chunks):
        item_len = f"{len(item):x}"
        if len(item_len) == 1:
            item_len = "0" + item_len
        result += item_len + item
        if i < len(chunks) - 1:
            result += "g"
    if len(result) < 20:
        result += hash_str[: 20 - len(result)]
    result += hashlib.md5(result.encode()).hexdigest()[:3]
    return result


def _build_navigation_urls(reader_path: str, book_id: str, chapter_uid: int) -> list[str]:
    """构建章节跳转 URL 候选，优先 bookKey，再尝试 chapter hash 路由。"""
    ts = int(asyncio.get_event_loop().time() * 1000)
    chapter_hash = _wr_hash(str(chapter_uid))
    return [
        f"https://weread.qq.com/web/reader/{reader_path}?chapterUid={chapter_uid}&_ts={ts}",
        f"https://weread.qq.com/web/reader/{reader_path}k{chapter_hash}?_ts={ts}",
        f"https://weread.qq.com/web/reader/{book_id}?chapterUid={chapter_uid}&_ts={ts}",
        f"https://weread.qq.com/web/reader/{book_id}k{chapter_hash}?_ts={ts}",
    ]


def _is_canvas_noise_line(line: str) -> bool:
    compact = line.replace(" ", "")
    if "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" in compact:
        return True
    ascii_chars = sum(1 for ch in line if ord(ch) < 128)
    if len(line) >= 60 and ascii_chars / max(len(line), 1) >= 0.9:
        return True
    return False


async def _extract_canvas_content(page, debug_log) -> str | None:
    """从 Canvas fillText 捕获数据恢复章节纯文本。"""
    async def _canvas_stats() -> dict[str, int]:
        payload = await page.evaluate(
            """
            () => {
                const cap = window.__wrCanvasCapture;
                if (!cap || !cap.stats) return {count: 0, page: 0};
                return cap.stats();
            }
            """
        )
        if not isinstance(payload, dict):
            return {"count": 0, "page": 0}
        return {
            "count": int(payload.get("count", 0) or 0),
            "page": int(payload.get("page", 0) or 0),
        }

    async def _turn_next_page() -> str:
        return await page.evaluate(
            """
            () => {
                const btn = document.querySelector('button.readerFooter_button');
                if (!btn) return 'no_button';
                const text = (btn.innerText || '').trim();
                if (text.includes('下一页')) {
                    btn.click();
                    return 'clicked';
                }
                if (text.includes('下一章') || text.includes('全书完')) return 'end';
                if (text.includes('登录')) return 'login';
                return 'other';
            }
            """
        )

    async def _wait_canvas_growth(before_count: int, before_page: int) -> dict[str, int]:
        deadline = asyncio.get_event_loop().time() + 2.2
        latest = {"count": before_count, "page": before_page}
        while asyncio.get_event_loop().time() < deadline:
            latest = await _canvas_stats()
            if latest["count"] > before_count + 8 or latest["page"] > before_page:
                return latest
            await asyncio.sleep(0.2)
        return latest

    # 自适应分页：优先点“下一页”，无按钮时退化 PageDown，直到长期无新增。
    no_growth_rounds = 0
    for _ in range(80):
        before = await _canvas_stats()
        action = await _turn_next_page()
        used_pagedown = False
        debug_log(
            f"canvas paginate: action={action} before_count={before['count']} before_page={before['page']}"
        )

        if action == "login":
            break

        if action == "clicked":
            await asyncio.sleep(0.35)
            after = await _wait_canvas_growth(before["count"], before["page"])
        elif action in {"no_button", "other"}:
            used_pagedown = True
            await page.keyboard.press("PageDown")
            await asyncio.sleep(0.25)
            after = await _wait_canvas_growth(before["count"], before["page"])
        else:
            break

        action_label = f"{action}+pagedown" if used_pagedown else action
        debug_log(
            f"canvas paginate: after_count={after['count']} after_page={after['page']} action={action_label}"
        )

        count_growth = after["count"] - before["count"]
        page_growth = after["page"] - before["page"]
        if count_growth < 8 and page_growth <= 0:
            no_growth_rounds += 1
        else:
            no_growth_rounds = 0
        if no_growth_rounds >= 5:
            break

    # 收尾补滚动，兼容 footer 不可见但可继续渲染的页面。
    for _ in range(2):
        await page.keyboard.press("PageDown")
        await asyncio.sleep(0.25)

    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        stats = await _canvas_stats()
        count = stats["count"]
        page_no = stats["page"]
        debug_log(f"canvas capture records: {count} pages={page_no}")
        if count >= 200:
            break
        await asyncio.sleep(_DOM_POLL_INTERVAL)

    payload = await page.evaluate(
        """
        () => {
            const cap = window.__wrCanvasCapture;
            if (!cap || !cap.exportText) return null;
            return cap.exportText();
        }
        """
    )
    if not isinstance(payload, dict):
        return None
    text = str(payload.get("text", ""))
    column_mode = str(payload.get("columnMode", "unknown"))
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    lines = [x for x in lines if not _is_canvas_noise_line(x)]
    text = "\n".join(lines)
    line_count = len(lines)
    if line_count < 5 or len(text) < 300:
        return None
    if _looks_like_finished_overlay_html(text):
        return None
    debug_log(
        f"Canvas extraction succeeded: lines={line_count} text_len={len(text)} "
        f"column_mode={column_mode}"
    )
    return _plain_text_to_html(text)


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
    # 注意：跳过"全书完"完结页——如有，继续轮询直到实际章节内容出现。
    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        content_len = await page.evaluate("""
            () => {
                // 先尝试找非完结页的内容元素
                for (const el of document.querySelectorAll('.readerChapterContent')) {
                    const t = (el.textContent || '').trim();
                    if (t.length >= 300 && !(t.includes('全书完') && t.includes('已阅读'))) {
                        return t.length;
                    }
                }
                // 降级：返回第一个内容长度（可能是完结页）
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

            // 判断元素是否为"全书完"完结页（用户已读完该书时 WeRead 显示的
            // 覆盖层）。完结页同样含有 .readerChapterContent 类，但内容与
            // 章节正文无关，应跳过。
            function isFinishedBookOverlay(el) {
                const t = (el.textContent || '').trim();
                // 完结页特征：同时含"全书完"与"已阅读"
                return t.includes('全书完') && t.includes('已阅读');
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

            // 收集所有候选元素，优先选择非完结页的候选
            const seen = new Set();
            const candidates = [];
            for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) {
                    if (seen.has(el)) continue;
                    seen.add(el);
                    const text = (el.textContent || '').trim();
                    if (text.length >= 100) {
                        candidates.push(el);
                    }
                }
            }

            // 第一轮：找到不是完结页的候选
            for (const el of candidates) {
                if (!isFinishedBookOverlay(el)) {
                    return simplify(el);
                }
            }

            // 所有候选均为完结页时返回 null，交给上层重试导航。
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
    seen_underlines_uids: set[int] = set()
    seen_read_ci: list[int] = []

    def _chapter_hit() -> bool:
        return chapter_uid in seen_underlines_uids or chapter_uid in seen_read_ci

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
            await page.add_init_script(_CANVAS_CAPTURE_INIT_SCRIPT)

            async def _on_request(request) -> None:
                """拦截所有 /book/ API 请求，记录 method / URL / POST body。"""
                url: str = request.url
                if "weread.qq.com" in url and "/book/" in url:
                    try:
                        post_data = request.post_data  # None if GET
                        _debug_log(
                            f"Browser REQUEST [{request.method}]: {url} "
                            f"post_data={post_data!r}"
                        )

                        m = _re.search(r"/web/book/underlines\?[^#]*\bchapterUid=(\d+)", url)
                        if m:
                            seen_underlines_uids.add(int(m.group(1)))

                        if "/web/book/read" in url and post_data:
                            payload = json.loads(post_data)
                            ci = payload.get("ci")
                            if isinstance(ci, int):
                                seen_read_ci.append(ci)
                    except Exception as exc:
                        _debug_log(f"Browser request log error: {exc}")

            async def _on_response(response) -> None:
                nonlocal result
                url: str = response.url

                if "weread.qq.com" in url and "/book/" in url:
                    _debug_log(f"Browser API [{response.status}]: {url}")
                    # 非章节内容端点：记录完整响应体（章节内容可能是加密 blob，跳过）
                    if response.status == 200 and "/book/chapter" not in url:
                        try:
                            body_text = await response.text()
                            _debug_log(
                                f"Browser API body ({len(body_text)} chars): "
                                f"{body_text[:2000]!r}"
                            )
                        except Exception as exc:
                            _debug_log(f"Browser API body read failed: {exc}")

                # ---- outline 端点：记录完整响应体供分析 ----
                if "/web/book/outline" in url and response.status == 200:
                    try:
                        body_text = await response.text()
                        _debug_log(
                            f"Browser outline response ({len(body_text)} chars): "
                            f"{body_text[:3000]!r}"
                        )
                    except Exception as exc:
                        _debug_log(f"Browser outline body read failed: {exc}")

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

            page.on("request", _on_request)
            page.on("response", _on_response)

            async def _navigate(url: str) -> None:
                try:
                    await page.evaluate(
                        """
                        () => {
                            const cap = window.__wrCanvasCapture;
                            if (cap && cap.clear) cap.clear();
                        }
                        """
                    )
                except Exception:
                    pass
                _debug_log(f"Browser navigating to: {url}")
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=_NAV_TIMEOUT_MS,
                )

            async def _extract_with_fallback() -> str | None:
                extracted: str | None = None
                if en_detected.is_set():
                    _debug_log(f"e_N detected, waiting {_DOM_RENDER_WAIT}s for JS render...")
                    await asyncio.sleep(_DOM_RENDER_WAIT)
                    extracted = await _extract_dom_content(page, _debug_log)

                if extracted is None or _looks_like_finished_overlay_html(extracted):
                    if extracted is not None:
                        _debug_log("DOM extraction looks like finished overlay, switch to canvas")
                    canvas_html = await _extract_canvas_content(page, _debug_log)
                    if canvas_html:
                        extracted = canvas_html

                if extracted is not None and _looks_like_finished_overlay_html(extracted):
                    _debug_log(
                        "Extraction still looks like finished overlay; treat as invalid"
                    )
                    extracted = None
                return extracted

            try:
                nav_url = _build_navigation_urls(reader_path, book_id, chapter_uid)[0]
                await _navigate(nav_url)
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

            result = await _extract_with_fallback()

            # 章节命中校验：如果未观察到目标 chapterUid，使用当前 reader 路径重试一次。
            chapter_hit = chapter_uid in seen_underlines_uids or chapter_uid in seen_read_ci
            if result is not None and not chapter_hit:
                _debug_log(
                    "Target chapter not observed in browser signals "
                    f"(target={chapter_uid}, underlines={sorted(seen_underlines_uids)}, "
                    f"read_ci={seen_read_ci}); retry with multi-route strategy"
                )
                retry_urls = _build_navigation_urls(reader_path, book_id, chapter_uid)
                for idx, retry_url in enumerate(retry_urls, start=1):
                    try:
                        _debug_log(f"Browser retry[{idx}] navigating to: {retry_url}")
                        await _navigate(retry_url)
                        result = await _extract_with_fallback()
                        if result is not None and _chapter_hit():
                            break
                    except Exception as exc:
                        _debug_log(f"Browser retry[{idx}] navigation failed: {exc}")

            if result is not None and not _chapter_hit():
                _debug_log(
                    "chapter miss after extraction, reject result "
                    f"(target={chapter_uid}, underlines={sorted(seen_underlines_uids)}, "
                    f"read_ci={seen_read_ci})"
                )
                result = None
            elif result is not None:
                _debug_log(
                    "chapter hit confirmed "
                    f"(target={chapter_uid}, underlines={sorted(seen_underlines_uids)}, "
                    f"read_ci={seen_read_ci})"
                )

        finally:
            await browser.close()

    if result is None:
        raise BrowserFetchError(
            f"Browser fallback 超时（{timeout:.0f}s）：\n"
            f"未能获取书 {book_id!r} 章节 {chapter_uid} 的内容。\n"
            "可能原因：bookKey 不正确、页面结构变更，或章节受 DRM 保护。"
        )

    return result
