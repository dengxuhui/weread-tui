"""
state.py — 本地状态持久化

存储路径：~/.config/weread-tui/state.json
原子写入：先写临时文件，再 os.replace() 保证不损坏已有文件。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# 路径常量
# --------------------------------------------------------------------------- #

STATE_DIR = Path.home() / ".config" / "weread-tui"
STATE_FILE = STATE_DIR / "state.json"

SHELF_CACHE_TTL = 5 * 60  # 5 分钟，单位秒


# --------------------------------------------------------------------------- #
# 底层读写
# --------------------------------------------------------------------------- #

def load_state() -> dict[str, Any]:
    """读取 state.json，文件不存在或损坏时返回空字典。"""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(data: dict[str, Any]) -> None:
    """原子写入 state.json（临时文件 + os.replace）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        # 写入失败时清理临时文件，不破坏原文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# 书架缓存
# --------------------------------------------------------------------------- #

def get_shelf_cache() -> list[dict] | None:
    """
    返回缓存的书架数据。
    若缓存不存在或已超过 TTL（5 分钟），返回 None。
    """
    state = load_state()
    cache = state.get("shelf_cache")
    if not cache:
        return None
    updated_at = cache.get("updated_at", 0)
    if time.time() - updated_at > SHELF_CACHE_TTL:
        return None
    return cache.get("books")


def set_shelf_cache(books: list[dict]) -> None:
    """写入带当前时间戳的书架缓存。"""
    state = load_state()
    state["shelf_cache"] = {
        "updated_at": int(time.time()),
        "books": books,
    }
    save_state(state)


def clear_shelf_cache() -> None:
    """清除书架缓存（登出时调用）。"""
    state = load_state()
    state.pop("shelf_cache", None)
    save_state(state)


# --------------------------------------------------------------------------- #
# 上次阅读位置
# --------------------------------------------------------------------------- #

def get_last_position() -> tuple[str, int] | None:
    """
    返回上次阅读位置 (book_id, chapter_uid)。
    未记录时返回 None。
    """
    state = load_state()
    book_id = state.get("last_book_id")
    chapter_uid = state.get("last_chapter_uid")
    if book_id is None or chapter_uid is None:
        return None
    return str(book_id), int(chapter_uid)


def set_last_position(book_id: str, chapter_uid: int) -> None:
    """记录上次阅读位置。"""
    state = load_state()
    state["last_book_id"] = book_id
    state["last_chapter_uid"] = chapter_uid
    save_state(state)
