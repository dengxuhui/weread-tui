"""
tests/test_tui.py — TUI 模块单元测试（不依赖真实 API）

测试范围：
- shelf.py: _parse_shelf, _progress_bar, BookEntry
- reader.py: _toc_width, _content_padding, ReaderScreen._current_chapter_title,
             ReaderScreen._chapter_index_info, ReaderScreen._navigate_chapter

不测试 Textual 渲染（避免需要终端环境），只测试纯数据逻辑函数。
"""

from __future__ import annotations

import pytest

from weread.tui.shelf import (
    BookEntry,
    ShelfData,
    _parse_shelf,
    _progress_bar,
)
from weread.tui.reader import (
    ReaderScreen,
    _content_padding,
    _toc_width,
)


# ---------------------------------------------------------------------------
# _progress_bar
# ---------------------------------------------------------------------------

class TestProgressBar:
    def test_zero_progress(self):
        bar = _progress_bar(0, 10)
        assert bar == "░░░░░░░░░░"

    def test_full_progress(self):
        bar = _progress_bar(100, 10)
        assert bar == "██████████"

    def test_half_progress(self):
        bar = _progress_bar(50, 10)
        assert bar == "█████░░░░░"

    def test_bar_width(self):
        bar = _progress_bar(100, 5)
        assert len(bar) == 5
        assert bar == "█████"

    def test_rounding(self):
        # 8% of 10 = 0.8 → rounds to 1
        bar = _progress_bar(8, 10)
        assert bar[0] == "█"

    def test_clamp_above_100(self):
        bar = _progress_bar(150, 10)
        assert bar == "██████████"

    def test_clamp_below_0(self):
        bar = _progress_bar(-5, 10)
        assert bar == "░░░░░░░░░░"


# ---------------------------------------------------------------------------
# _parse_shelf
# ---------------------------------------------------------------------------

class TestParseShelf:
    def _make_raw(
        self,
        books=None,
        book_progress=None,
        archive=None,
    ) -> dict:
        return {
            "books": books or [],
            "bookProgress": book_progress or [],
            "archive": archive or [],
        }

    def test_empty_raw(self):
        data = _parse_shelf(self._make_raw())
        assert data.recent == []
        assert data.groups == {}

    def test_single_book_no_archive(self):
        raw = self._make_raw(
            books=[{"bookId": "1", "title": "三体", "author": "刘慈欣"}],
            book_progress=[{"bookId": "1", "progress": 50, "chapterUid": 10}],
        )
        data = _parse_shelf(raw)
        assert len(data.recent) == 1
        assert data.recent[0].title == "三体"
        assert data.recent[0].progress == 50
        assert data.recent[0].chapter_uid == 10

    def test_book_in_archive(self):
        raw = self._make_raw(
            books=[{"bookId": "1", "title": "三体", "author": "刘慈欣"}],
            book_progress=[{"bookId": "1", "progress": 100, "chapterUid": 20}],
            archive=[{"name": "科幻", "bookIds": ["1"]}],
        )
        data = _parse_shelf(raw)
        assert data.recent == []
        assert "科幻" in data.groups
        assert data.groups["科幻"][0].book_id == "1"

    def test_mixed_recent_and_archived(self):
        raw = self._make_raw(
            books=[
                {"bookId": "1", "title": "书A"},
                {"bookId": "2", "title": "书B"},
            ],
            book_progress=[
                {"bookId": "1", "progress": 20, "chapterUid": 1},
                {"bookId": "2", "progress": 80, "chapterUid": 5},
            ],
            archive=[{"name": "组", "bookIds": ["2"]}],
        )
        data = _parse_shelf(raw)
        assert len(data.recent) == 1
        assert data.recent[0].book_id == "1"
        assert len(data.groups["组"]) == 1
        assert data.groups["组"][0].book_id == "2"

    def test_book_missing_from_progress(self):
        raw = self._make_raw(
            books=[{"bookId": "1", "title": "书A", "author": "作者"}],
        )
        data = _parse_shelf(raw)
        assert data.recent[0].progress == 0
        assert data.recent[0].chapter_uid == 0

    def test_book_without_bookid_skipped(self):
        raw = self._make_raw(
            books=[{"title": "无ID书"}],
        )
        data = _parse_shelf(raw)
        assert data.recent == []

    def test_multiple_archives_order_preserved(self):
        raw = self._make_raw(
            books=[
                {"bookId": "1", "title": "书1"},
                {"bookId": "2", "title": "书2"},
            ],
            archive=[
                {"name": "甲", "bookIds": ["1"]},
                {"name": "乙", "bookIds": ["2"]},
            ],
        )
        data = _parse_shelf(raw)
        groups = list(data.groups.keys())
        assert groups == ["甲", "乙"]

    def test_author_defaults_to_empty(self):
        raw = self._make_raw(
            books=[{"bookId": "1", "title": "书A"}],
        )
        data = _parse_shelf(raw)
        assert data.recent[0].author == ""

    def test_title_defaults_to_placeholder(self):
        raw = self._make_raw(
            books=[{"bookId": "1"}],
        )
        data = _parse_shelf(raw)
        assert data.recent[0].title == "（无题）"


# ---------------------------------------------------------------------------
# _toc_width & _content_padding
# ---------------------------------------------------------------------------

class TestWidthHelpers:
    def test_toc_width_narrow(self):
        assert _toc_width(79) == 20

    def test_toc_width_medium(self):
        assert _toc_width(80) == 24
        assert _toc_width(120) == 24

    def test_toc_width_wide(self):
        assert _toc_width(121) == 28

    def test_content_padding_narrow(self):
        assert _content_padding(80) == 1

    def test_content_padding_wide(self):
        assert _content_padding(121) == 4


# ---------------------------------------------------------------------------
# ReaderScreen 纯数据方法
# ---------------------------------------------------------------------------

SAMPLE_CHAPTERS = [
    {"chapterUid": 1, "title": "序章"},
    {"chapterUid": 2, "title": "第一章"},
    {"chapterUid": 3, "title": "第二章"},
]


class TestReaderScreenDataMethods:
    def _make_screen(self, chapter_uid=2):
        return ReaderScreen(
            book_id="book1",
            chapter_uid=chapter_uid,
            book_title="测试书",
            chapters=SAMPLE_CHAPTERS,
        )

    def test_current_chapter_title_found(self):
        screen = self._make_screen(chapter_uid=2)
        assert screen._current_chapter_title() == "第一章"

    def test_current_chapter_title_not_found(self):
        screen = self._make_screen(chapter_uid=99)
        assert "99" in screen._current_chapter_title()

    def test_current_chapter_title_zero_uid(self):
        screen = self._make_screen(chapter_uid=0)
        assert screen._current_chapter_title() == ""

    def test_chapter_index_info(self):
        screen = self._make_screen(chapter_uid=2)
        idx, total = screen._chapter_index_info()
        assert idx == 2
        assert total == 3

    def test_chapter_index_info_first(self):
        screen = self._make_screen(chapter_uid=1)
        idx, total = screen._chapter_index_info()
        assert idx == 1
        assert total == 3

    def test_chapter_index_info_last(self):
        screen = self._make_screen(chapter_uid=3)
        idx, total = screen._chapter_index_info()
        assert idx == 3
        assert total == 3

    def test_chapter_index_info_not_found(self):
        screen = self._make_screen(chapter_uid=99)
        idx, total = screen._chapter_index_info()
        assert idx == 0
        assert total == 3

    def test_navigate_chapter_forward(self):
        screen = self._make_screen(chapter_uid=1)
        # 直接修改 chapter_uid，绕过 TUI 调用
        screen._navigate_chapter_uid(1)
        assert screen.chapter_uid == 2

    def test_navigate_chapter_backward(self):
        screen = self._make_screen(chapter_uid=2)
        screen._navigate_chapter_uid(-1)
        assert screen.chapter_uid == 1

    def test_navigate_chapter_at_first_no_prev(self):
        screen = self._make_screen(chapter_uid=1)
        screen._navigate_chapter_uid(-1)
        assert screen.chapter_uid == 1  # 未改变

    def test_navigate_chapter_at_last_no_next(self):
        screen = self._make_screen(chapter_uid=3)
        screen._navigate_chapter_uid(1)
        assert screen.chapter_uid == 3  # 未改变

    def test_navigate_chapter_empty_list(self):
        screen = ReaderScreen(book_id="b", chapter_uid=1, chapters=[])
        screen._navigate_chapter_uid(1)
        assert screen.chapter_uid == 1
