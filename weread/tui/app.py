"""
weread/tui/app.py — Textual 主 App，管理屏幕切换与全局键绑定。

屏幕栈：
    on_mount  → push ShelfScreen
    打开书籍  → push ReaderScreen
    返回书架  → pop_screen()
    q         → 退出（关闭 HTTP client）
"""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from weread.api import WeReadClient


class WeReadApp(App):
    """微信读书终端阅读器主应用。"""

    TITLE = "weread-tui"
    SUB_TITLE = "微信读书终端阅读器"

    BINDINGS = [
        Binding("q", "quit", "退出", show=True),
    ]

    def __init__(self, vid: str, skey: str) -> None:
        super().__init__()
        self.vid = vid
        self.skey = skey
        # WeReadClient 在 on_mount 中初始化（需要 async context）
        self.client: WeReadClient | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        """初始化 HTTP client 并显示书架屏幕。"""
        from weread.tui.shelf import ShelfScreen

        self.client = WeReadClient(self.vid, self.skey)
        await self.client.aopen()
        await self.push_screen(ShelfScreen())

    async def action_quit(self) -> None:
        """退出前关闭 HTTP client。"""
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        self.exit()

    # ------------------------------------------------------------------
    # 屏幕导航（供子屏幕调用）
    # ------------------------------------------------------------------

    async def show_reader(
        self,
        book_id: str,
        chapter_uid: int,
        book_title: str = "",
        chapters: list | None = None,
    ) -> None:
        """推入阅读屏幕。"""
        from weread.tui.reader import ReaderScreen

        await self.push_screen(
            ReaderScreen(
                book_id=book_id,
                chapter_uid=chapter_uid,
                book_title=book_title,
                chapters=chapters or [],
            )
        )

    def show_shelf(self) -> None:
        """弹出当前屏幕（返回书架）。"""
        self.pop_screen()
