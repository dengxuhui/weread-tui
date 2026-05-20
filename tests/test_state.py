"""tests/test_state.py — state.py 单元测试"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import weread.state as state_mod


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """将 STATE_DIR / STATE_FILE 重定向到 pytest 临时目录，测试互不影响。"""
    state_dir = tmp_path / "weread-tui"
    state_file = state_dir / "state.json"
    monkeypatch.setattr(state_mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(state_mod, "STATE_FILE", state_file)


# --------------------------------------------------------------------------- #
# load_state / save_state
# --------------------------------------------------------------------------- #

class TestLoadSave:
    def test_load_missing_file_returns_empty(self):
        assert state_mod.load_state() == {}

    def test_save_then_load_roundtrip(self):
        data = {"key": "value", "num": 42}
        state_mod.save_state(data)
        assert state_mod.load_state() == data

    def test_save_creates_parent_dir(self, tmp_path: Path):
        # STATE_DIR 尚不存在时也能正常写入
        assert not state_mod.STATE_DIR.exists()
        state_mod.save_state({"x": 1})
        assert state_mod.STATE_FILE.exists()

    def test_load_corrupted_file_returns_empty(self):
        state_mod.STATE_DIR.mkdir(parents=True)
        state_mod.STATE_FILE.write_text("不是 JSON {{{", encoding="utf-8")
        assert state_mod.load_state() == {}

    def test_atomic_write_preserves_old_on_failure(self, monkeypatch: pytest.MonkeyPatch):
        """模拟 os.replace 失败时，原文件不被破坏。"""
        state_mod.save_state({"original": True})

        def bad_replace(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr("os.replace", bad_replace)
        with pytest.raises(OSError):
            state_mod.save_state({"new": True})

        # 原文件内容应保持不变
        assert state_mod.load_state() == {"original": True}


# --------------------------------------------------------------------------- #
# shelf cache
# --------------------------------------------------------------------------- #

class TestShelfCache:
    BOOKS = [{"bookId": "1", "title": "三体"}]

    def test_no_cache_returns_none(self):
        assert state_mod.get_shelf_cache() is None

    def test_set_then_get(self):
        state_mod.set_shelf_cache(self.BOOKS)
        assert state_mod.get_shelf_cache() == self.BOOKS

    def test_expired_cache_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        state_mod.set_shelf_cache(self.BOOKS)
        # 把 TTL 设为 0 秒，让缓存立刻过期
        monkeypatch.setattr(state_mod, "SHELF_CACHE_TTL", 0)
        # 等一个 tick 确保 time.time() 超过 updated_at
        time.sleep(0.01)
        assert state_mod.get_shelf_cache() is None

    def test_clear_shelf_cache(self):
        state_mod.set_shelf_cache(self.BOOKS)
        state_mod.clear_shelf_cache()
        assert state_mod.get_shelf_cache() is None

    def test_set_cache_preserves_other_fields(self):
        state_mod.save_state({"last_book_id": "99"})
        state_mod.set_shelf_cache(self.BOOKS)
        assert state_mod.load_state()["last_book_id"] == "99"


# --------------------------------------------------------------------------- #
# get/set_last_position
# --------------------------------------------------------------------------- #

class TestLastPosition:
    def test_no_position_returns_none(self):
        assert state_mod.get_last_position() is None

    def test_set_then_get(self):
        state_mod.set_last_position("695233", 109)
        assert state_mod.get_last_position() == ("695233", 109)

    def test_returns_str_and_int(self):
        state_mod.set_last_position("42", 7)
        book_id, chapter_uid = state_mod.get_last_position()
        assert isinstance(book_id, str)
        assert isinstance(chapter_uid, int)

    def test_overwrite_position(self):
        state_mod.set_last_position("A", 1)
        state_mod.set_last_position("B", 2)
        assert state_mod.get_last_position() == ("B", 2)

    def test_position_preserves_shelf_cache(self):
        books = [{"bookId": "1"}]
        state_mod.set_shelf_cache(books)
        state_mod.set_last_position("X", 5)
        assert state_mod.get_shelf_cache() == books
