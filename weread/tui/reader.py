"""
weread/tui/reader.py — 阅读视图屏幕。

布局：
    ┌────────────────────────────────────┐
    │ [Header] 书名  第N章  XX%           │
    ├────────────────────────────────────┤
    │                                    │
    │  正文（VerticalScroll + Static）    │
    │                                    │
    ├────────────────────────────────────┤
    │ [Footer] j/k 滚动  []章节  t目录   │
    └────────────────────────────────────┘

目录浮层（按 t 激活，按 Esc / 选中后收起）：
    ┌──────┬─────────────────────────────┐
    │目录  │  正文（正常滚动）            │
    │      │                             │
    │第1章 │                             │
    │▶第2章│                             │
    │第3章 │                             │
    │      │                             │
    │Esc关│                             │
    └──────┴─────────────────────────────┘

宽度自适应：
    < 80 列   全宽正文，目录浮层 20 列
    80-120 列 全宽正文，目录浮层 24 列
    > 120 列  正文左右 padding=4，目录浮层 28 列
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

if TYPE_CHECKING:
    from weread.tui.app import WeReadApp


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
#reader-body {
    height: 1fr;
}

#content-scroll {
    height: 1fr;
    padding: 0 0;
}

#content-text {
    padding: 0 1;
}

#toc-overlay {
    display: none;
    width: 24;
    height: 1fr;
    background: $surface;
    border-right: solid $primary;
}

#toc-overlay.visible {
    display: block;
}

#toc-title {
    background: $primary;
    color: $text;
    padding: 0 1;
    height: 1;
}

#toc-list {
    height: 1fr;
}

#toc-hint {
    height: 1;
    padding: 0 1;
    color: $text-muted;
}

#status-bar {
    height: 1;
    background: $surface;
    padding: 0 1;
    color: $text-muted;
}
"""

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _toc_width(terminal_width: int) -> int:
    if terminal_width > 120:
        return 28
    elif terminal_width >= 80:
        return 24
    else:
        return 20


def _content_padding(terminal_width: int) -> int:
    """宽终端额外左右 padding。"""
    return 4 if terminal_width > 120 else 1


# ---------------------------------------------------------------------------
# 目录浮层 Widget
# ---------------------------------------------------------------------------

class TOCOverlay(Vertical):
    """目录侧边浮层，展示章节列表。"""

    DEFAULT_CSS = """
    TOCOverlay {
        display: none;
        width: 24;
        height: 1fr;
        background: $surface;
        border-right: solid $primary;
    }
    TOCOverlay.visible {
        display: block;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("目录", id="toc-title")
        yield ListView(id="toc-list")
        yield Static("Esc 关闭", id="toc-hint")

    def load_chapters(self, chapters: list[dict], current_uid: int) -> None:
        """填充章节列表，高亮当前章节。"""
        toc_list = self.query_one("#toc-list", ListView)
        toc_list.clear()
        for ch in chapters:
            uid = ch.get("chapterUid") or 0
            title = ch.get("title") or f"第{uid}章"
            prefix = "▶ " if uid == current_uid else "  "
            label = f"{prefix}{title}"
            toc_list.append(
                ListItem(Label(label), id=f"toc-ch-{uid}")
            )

    def show(self) -> None:
        self.add_class("visible")
        self.query_one("#toc-list", ListView).focus()

    def hide(self) -> None:
        self.remove_class("visible")


# ---------------------------------------------------------------------------
# 阅读视图屏幕
# ---------------------------------------------------------------------------

class ReaderScreen(Screen):
    """阅读视图：全宽正文 + 目录浮层。"""

    CSS = _CSS

    BINDINGS = [
        Binding("j", "scroll_down", "向下", show=False),
        Binding("k", "scroll_up", "向上", show=False),
        Binding("space", "page_down", "翻页", show=True),
        Binding("shift+space", "page_up", "上翻", show=False),
        Binding("[", "prev_chapter", "上一章", show=True),
        Binding("]", "next_chapter", "下一章", show=True),
        Binding("t", "toggle_toc", "目录", show=True),
        Binding("escape", "escape_action", "返回", show=False),
        Binding("b", "back_to_shelf", "书架", show=True),
        Binding("q", "quit_app", "退出", show=True),
    ]

    def __init__(
        self,
        book_id: str,
        chapter_uid: int,
        book_title: str = "",
        chapters: list | None = None,
    ) -> None:
        super().__init__()
        self.book_id = book_id
        self.chapter_uid = chapter_uid
        self.book_title = book_title
        self.chapters: list[dict] = chapters or []
        self._toc_visible = False

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="reader-body"):
            yield TOCOverlay(id="toc-overlay")
            with VerticalScroll(id="content-scroll"):
                yield Static("", id="content-text", markup=True)
        yield Static("", id="status-bar")
        yield Footer()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self._update_header()
        self._apply_width_style()
        self._load_chapter(self.chapter_uid)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_width_style()

    # ------------------------------------------------------------------
    # 样式自适应
    # ------------------------------------------------------------------

    def _apply_width_style(self) -> None:
        width = self.app.console.width
        pad = _content_padding(width)
        toc_w = _toc_width(width)
        # 更新内容区 padding
        try:
            self.query_one("#content-text", Static).styles.padding = (0, pad)
        except Exception:
            pass
        # 更新 TOC 宽度
        try:
            self.query_one(TOCOverlay).styles.width = toc_w
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 章节加载
    # ------------------------------------------------------------------

    @work(exclusive=True)
    async def _load_chapter(self, chapter_uid: int) -> None:
        from weread.api import CookieExpiredError, DRMChapterError, NetworkError
        from weread.parser import parse_chapter
        from weread.tui.app import WeReadApp

        content_text = self.query_one("#content-text", Static)
        content_text.update("[dim]加载章节中...[/dim]")

        app: WeReadApp = self.app  # type: ignore[assignment]
        if app.client is None:
            content_text.update("[red]错误：HTTP client 未初始化[/red]")
            return

        try:
            html = await app.client.get_chapter_content(self.book_id, chapter_uid)
        except DRMChapterError:
            content_text.update(
                "[yellow]「此章节已加密，无法在终端阅读」[/yellow]"
            )
            self._update_status()
            return
        except CookieExpiredError:
            content_text.update(
                "[red]Cookie 已过期，请执行 [bold]weread login[/bold] 重新登录[/red]"
            )
            return
        except NetworkError as exc:
            content_text.update(f"[red]网络错误：{exc}[/red]")
            return
        except Exception as exc:
            content_text.update(f"[red]加载失败：{exc}[/red]")
            return

        markup = parse_chapter(html)
        content_text.update(markup)

        # 滚回顶部
        self.query_one("#content-scroll", VerticalScroll).scroll_home(animate=False)
        self._update_status()

        # 更新 TOC 高亮
        try:
            self.query_one(TOCOverlay).load_chapters(self.chapters, self.chapter_uid)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Header 与状态栏
    # ------------------------------------------------------------------

    def _update_header(self) -> None:
        ch_title = self._current_chapter_title()
        self.title = f"{self.book_title}  {ch_title}"

    def _update_status(self) -> None:
        ch_title = self._current_chapter_title()
        idx, total = self._chapter_index_info()
        pct = f"{int(idx / total * 100)}%" if total else "-"
        status = f"  {self.book_title}  ·  {ch_title}  [{idx}/{total}  {pct}]"
        self.query_one("#status-bar", Static).update(status)

    def _current_chapter_title(self) -> str:
        for ch in self.chapters:
            if ch.get("chapterUid") == self.chapter_uid:
                return ch.get("title") or f"第{self.chapter_uid}章"
        return f"第{self.chapter_uid}章" if self.chapter_uid else ""

    def _chapter_index_info(self) -> tuple[int, int]:
        total = len(self.chapters)
        for i, ch in enumerate(self.chapters, 1):
            if ch.get("chapterUid") == self.chapter_uid:
                return i, total
        return 0, total

    # ------------------------------------------------------------------
    # 章节导航
    # ------------------------------------------------------------------

    def _navigate_chapter_uid(self, delta: int) -> None:
        """
        纯数据操作：按 delta 偏移更新 self.chapter_uid。
        不触发任何 TUI 渲染，供单元测试直接调用。
        """
        if not self.chapters:
            return
        uids = [ch.get("chapterUid") for ch in self.chapters]
        try:
            cur_idx = uids.index(self.chapter_uid)
        except ValueError:
            return
        new_idx = cur_idx + delta
        if new_idx < 0 or new_idx >= len(uids):
            return
        new_uid = uids[new_idx]
        if new_uid is None:
            return
        self.chapter_uid = new_uid

    def _navigate_chapter(self, delta: int) -> None:
        """按 delta 切换章节：更新 uid + 刷新 TUI + 保存位置。"""
        old_uid = self.chapter_uid
        self._navigate_chapter_uid(delta)
        if self.chapter_uid == old_uid:
            return  # 已到边界

        self._update_header()
        self._load_chapter(self.chapter_uid)

        # 保存本地位置
        from weread.state import set_last_position
        set_last_position(self.book_id, self.chapter_uid)

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def action_scroll_down(self) -> None:
        self.query_one("#content-scroll", VerticalScroll).scroll_down(animate=False)

    def action_scroll_up(self) -> None:
        self.query_one("#content-scroll", VerticalScroll).scroll_up(animate=False)

    def action_page_down(self) -> None:
        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        scroll = self.query_one("#content-scroll", VerticalScroll)
        scroll.scroll_page_up(animate=False)

    def action_prev_chapter(self) -> None:
        self._navigate_chapter(-1)

    def action_next_chapter(self) -> None:
        self._navigate_chapter(1)

    def action_toggle_toc(self) -> None:
        toc = self.query_one(TOCOverlay)
        if self._toc_visible:
            toc.hide()
            self._toc_visible = False
            self.query_one("#content-scroll", VerticalScroll).focus()
        else:
            toc.load_chapters(self.chapters, self.chapter_uid)
            toc.show()
            self._toc_visible = True

    def action_escape_action(self) -> None:
        if self._toc_visible:
            self.query_one(TOCOverlay).hide()
            self._toc_visible = False
            self.query_one("#content-scroll", VerticalScroll).focus()

    def action_back_to_shelf(self) -> None:
        from weread.tui.app import WeReadApp
        cast_app: WeReadApp = self.app  # type: ignore[assignment]
        cast_app.show_shelf()

    def action_quit_app(self) -> None:
        self.app.exit()

    # ------------------------------------------------------------------
    # TOC 章节选择事件
    # ------------------------------------------------------------------

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id: str = event.item.id or ""
        if item_id.startswith("toc-ch-"):
            uid_str = item_id[len("toc-ch-"):]
            try:
                uid = int(uid_str)
            except ValueError:
                return
            self.chapter_uid = uid
            self._update_header()
            self._load_chapter(uid)
            # 收起 TOC
            self.query_one(TOCOverlay).hide()
            self._toc_visible = False
            self.query_one("#content-scroll", VerticalScroll).focus()

            from weread.state import set_last_position
            set_last_position(self.book_id, uid)
