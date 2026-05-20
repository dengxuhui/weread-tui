"""
weread/tui/shelf.py — 书架视图屏幕。

布局：
    ┌─────────────────────────────────────┐
    │  Header (书架)                       │
    ├─────────────────────────────────────┤
    │  [搜索框，/ 激活时显示]               │
    │                                     │
    │  最近阅读                            │
    │  ▶ 三体全集          刘慈欣  ██ 100% │
    │    社会心理学        迈尔斯  ░░   8% │
    │  ─── 工作相关 ──                    │
    │    游戏编程模式      Nystrom ██  99% │
    │  ...                                │
    ├─────────────────────────────────────┤
    │  Footer (按键提示)                   │
    └─────────────────────────────────────┘

按键：
    j/k / ↑/↓  移动选中
    Enter       打开书籍
    /           激活搜索框
    Escape      退出搜索框
    g           展开/收起当前分组
    r           刷新书架
    b / Escape  （无搜索时）返回上层（实际无上层，仅退出搜索）
    q           退出程序
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

if TYPE_CHECKING:
    from weread.tui.app import WeReadApp


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class BookEntry:
    """书架中一本书的展示数据。"""
    book_id: str
    title: str
    author: str
    progress: int          # 0-100
    chapter_uid: int       # 上次阅读章节
    chapter_offset: int = 0
    group_name: str = ""   # 所属分组名，空字符串表示"最近阅读"


@dataclass
class ShelfData:
    """解析后的书架数据。"""
    recent: list[BookEntry] = field(default_factory=list)    # 未分组书籍
    groups: dict[str, list[BookEntry]] = field(default_factory=dict)  # 分组名 → 书籍列表


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _progress_bar(progress: int, width: int = 10) -> str:
    """生成进度条字符串，e.g. '████████░░'。"""
    filled = max(0, min(width, round(progress * width / 100)))
    return "█" * filled + "░" * (width - filled)


def _parse_shelf(raw: dict) -> ShelfData:
    """
    将 API 原始 dict 解析为 ShelfData。

    raw 结构（SPEC §4.2）：
      books:        [{bookId, title, author, ...}, ...]
      bookProgress: [{bookId, progress, chapterUid, chapterOffset, ...}, ...]
      archive:      [{name, bookIds: [...]}, ...]
    """
    books_list: list[dict] = raw.get("books", [])
    progress_list: list[dict] = raw.get("bookProgress", [])
    archive_list: list[dict] = raw.get("archive", [])

    # bookId → progress info
    progress_map: dict[str, dict] = {}
    for p in progress_list:
        bid = p.get("bookId") or ""
        if bid:
            progress_map[bid] = p

    # bookId → group name
    group_map: dict[str, str] = {}
    for arc in archive_list:
        name = arc.get("name") or "未分组"
        for bid in arc.get("bookIds", []):
            group_map[bid] = name

    # bookId → BookEntry
    entries: dict[str, BookEntry] = {}
    for b in books_list:
        bid = b.get("bookId") or ""
        if not bid:
            continue
        prog_info = progress_map.get(bid, {})
        entries[bid] = BookEntry(
            book_id=bid,
            title=b.get("title") or "（无题）",
            author=b.get("author") or "",
            progress=prog_info.get("progress") or 0,
            chapter_uid=prog_info.get("chapterUid") or 0,
            chapter_offset=prog_info.get("chapterOffset") or 0,
            group_name=group_map.get(bid, ""),
        )

    data = ShelfData()
    # 按 archive 顺序构建分组
    for arc in archive_list:
        name = arc.get("name") or "未分组"
        grp: list[BookEntry] = []
        for bid in arc.get("bookIds", []):
            if bid in entries:
                grp.append(entries[bid])
        if grp:
            data.groups[name] = grp

    # 未归档的书归入"最近阅读"
    archived_ids = {bid for arc in archive_list for bid in arc.get("bookIds", [])}
    for bid, entry in entries.items():
        if bid not in archived_ids:
            data.recent.append(entry)

    return data


# ---------------------------------------------------------------------------
# 行显示
# ---------------------------------------------------------------------------




def _book_label(entry: BookEntry, selected: bool = False) -> str:
    """生成书目行文字（Rich Markup）。"""
    bar = _progress_bar(entry.progress)
    title = entry.title[:20].ljust(20)
    author = (entry.author or "")[:8].ljust(8)
    pct = f"{entry.progress:3d}%"
    prefix = "▶ " if selected else "  "
    return f"{prefix}{title}  {author}  {bar} {pct}"


# ---------------------------------------------------------------------------
# 屏幕
# ---------------------------------------------------------------------------

class ShelfScreen(Screen):
    """书架视图屏幕。"""

    BINDINGS = [
        Binding("j", "cursor_down", "向下", show=False),
        Binding("k", "cursor_up", "向上", show=False),
        Binding("enter", "open_book", "打开", show=True),
        Binding("r", "refresh", "刷新", show=True),
        Binding("g", "toggle_group", "展开/收起", show=True),
        Binding("q", "quit_app", "退出", show=True),
        Binding("/", "search", "搜索", show=True),
        Binding("escape", "escape_search", "退出搜索", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._shelf_data: ShelfData | None = None
        # 分组展开状态：group_name → bool
        self._group_collapsed: dict[str, bool] = {}
        # 搜索关键字
        self._filter: str = ""
        # 当前列表中各 item 对应的 BookEntry（group header 为 None）
        self._item_map: list[BookEntry | None] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="shelf-container"):
            yield Input(placeholder="搜索书名... (Esc 退出)", id="search-input")
            yield Static("加载书架中...", id="loading-label")
            yield ListView(id="book-list")
        yield Footer()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        # 默认隐藏搜索框
        self.query_one("#search-input", Input).display = False
        self._load_shelf()

    # ------------------------------------------------------------------
    # 数据加载（后台 worker）
    # ------------------------------------------------------------------

    @work(exclusive=True)
    async def _load_shelf(self, force_refresh: bool = False) -> None:
        """从缓存或 API 加载书架数据。"""
        from weread.api import CookieExpiredError, NetworkError
        from weread.state import get_shelf_cache, set_shelf_cache
        from weread.tui.app import WeReadApp

        loading = self.query_one("#loading-label", Static)
        book_list = self.query_one("#book-list", ListView)

        loading.update("加载书架中...")
        loading.display = True
        book_list.display = False

        raw: dict | None = None

        if not force_refresh:
            cached = get_shelf_cache()
            if isinstance(cached, dict):
                raw = cached
            elif isinstance(cached, list):
                # 旧格式兼容
                raw = {"books": cached, "bookProgress": [], "archive": []}

        if raw is None:
            app: WeReadApp = self.app  # type: ignore[assignment]
            if app.client is None:
                loading.update("[red]错误：HTTP client 未初始化[/red]")
                return
            try:
                raw = await app.client.get_shelf()
                set_shelf_cache(raw)
            except CookieExpiredError:
                loading.update(
                    "[red]Cookie 已过期，请执行 [bold]weread login[/bold] 重新登录[/red]"
                )
                return
            except NetworkError as exc:
                loading.update(f"[red]网络错误：{exc}[/red]")
                return
            except Exception as exc:
                loading.update(f"[red]加载失败：{exc}[/red]")
                return

        self._shelf_data = _parse_shelf(raw)
        loading.display = False
        book_list.display = True
        await self._render_list()

    async def _render_list(self) -> None:
        """根据当前书架数据和过滤条件重建 ListView。

        必须 async 并 await clear()，否则旧 ListItem 残留 DOM 会在下次渲染
        时触发 DuplicateIds。ListItem 不设置 id，通过 _item_map 按索引识别
        选中书目，彻底规避 BadIdentifier / DuplicateIds 问题。
        """
        if self._shelf_data is None:
            return

        book_list = self.query_one("#book-list", ListView)
        await book_list.clear()   # 必须 await，确保旧节点从 DOM 彻底移除
        self._item_map = []

        flt = self._filter.lower()
        new_items: list[ListItem] = []

        def _matches(entry: BookEntry) -> bool:
            if not flt:
                return True
            return flt in entry.title.lower() or flt in (entry.author or "").lower()

        def _build_section(section_name: str, entries: list[BookEntry]) -> None:
            visible = [e for e in entries if _matches(e)]
            if not visible:
                return
            collapsed = self._group_collapsed.get(section_name, False)

            # 分组标题行（不设 id，通过 _item_map 识别）
            header_markup = f"[bold]─── {section_name} ───[/bold]"
            if collapsed:
                header_markup += " [dim](已收起)[/dim]"
            header_item = ListItem(Label(header_markup))
            header_item.disabled = True
            new_items.append(header_item)
            self._item_map.append(None)

            if not collapsed:
                for entry in visible:
                    li = ListItem(Label(_book_label(entry)))
                    new_items.append(li)
                    self._item_map.append(entry)

        # 最近阅读（未分组）
        recent_visible = [e for e in self._shelf_data.recent if _matches(e)]
        if recent_visible:
            header = ListItem(Label("[bold]─── 最近阅读 ───[/bold]"))
            header.disabled = True
            new_items.append(header)
            self._item_map.append(None)
            for entry in recent_visible:
                li = ListItem(Label(_book_label(entry)))
                new_items.append(li)
                self._item_map.append(entry)

        # 各分组
        for group_name, entries in self._shelf_data.groups.items():
            _build_section(group_name, entries)

        if new_items:
            await book_list.extend(new_items)

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def action_cursor_down(self) -> None:
        self.query_one("#book-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#book-list", ListView).action_cursor_up()

    def action_open_book(self) -> None:
        book_list = self.query_one("#book-list", ListView)
        idx = book_list.index
        if idx is None or idx >= len(self._item_map):
            return
        entry = self._item_map[idx]
        if entry is None:
            return  # 分组标题，不可打开
        self._open_book(entry)

    def action_refresh(self) -> None:
        self._load_shelf(force_refresh=True)

    async def action_toggle_group(self) -> None:
        """展开/收起当前选中行所在的分组。"""
        book_list = self.query_one("#book-list", ListView)
        idx = book_list.index
        if idx is None or idx >= len(self._item_map):
            return
        entry = self._item_map[idx]
        if entry is None:
            return  # 选中的已是分组标题

        group = entry.group_name or "__recent__"
        self._group_collapsed[group] = not self._group_collapsed.get(group, False)
        await self._render_list()

    def action_search(self) -> None:
        search_input = self.query_one("#search-input", Input)
        search_input.display = True
        search_input.focus()

    async def action_escape_search(self) -> None:
        search_input = self.query_one("#search-input", Input)
        if search_input.display:
            search_input.display = False
            self._filter = ""
            await self._render_list()
            self.query_one("#book-list", ListView).focus()

    def action_quit_app(self) -> None:
        self.app.exit()

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-input":
            self._filter = event.value
            await self._render_list()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            # 提交搜索框时，焦点回到列表
            event.input.display = False
            self.query_one("#book-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """ListView 确认选中（Enter）时触发。"""
        book_list = self.query_one("#book-list", ListView)
        idx = book_list.index
        if idx is None or idx >= len(self._item_map):
            return
        entry = self._item_map[idx]
        if entry is not None:
            self._open_book(entry)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _open_book(self, entry: BookEntry) -> None:
        """打开书籍：记录本地位置并推入阅读屏幕。"""
        from weread.state import set_last_position

        chapter_uid = entry.chapter_uid or 0
        set_last_position(entry.book_id, chapter_uid)

        # 异步推入 ReaderScreen
        self.run_worker(
            self._push_reader(entry, chapter_uid),
            exclusive=True,
        )

    async def _push_reader(self, entry: BookEntry, chapter_uid: int) -> None:
        from weread.api import NetworkError, CookieExpiredError
        from weread.tui.app import WeReadApp

        app: WeReadApp = self.app  # type: ignore[assignment]
        chapters: list = []

        if app.client:
            try:
                chapters = await app.client.get_chapter_list(entry.book_id)
            except Exception:
                # 404 / 网络错误 / Cookie 过期等均降级为空章节列表，ReaderScreen 自行处理
                pass

        await app.show_reader(
            book_id=entry.book_id,
            chapter_uid=chapter_uid,
            book_title=entry.title,
            chapters=chapters,
        )
