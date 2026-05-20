"""
weread/parser.py — 将微信读书章节 HTML 转换为 Textual Rich Markup 字符串。

公开接口：
    parse_chapter(html: str) -> str

转换规则（见 SPEC.md §4.3）：
  - <strong> / <b>           → [bold]...[/bold]
  - <em> / <i>               → [italic]...[/italic]
  - <p class="wr_blockquote"> / class 含 "blockquote" / <blockquote>
                             → 引用块（前缀 "│ "，淡色）
  - <p>                      → 段落后追加空行
  - <br>                     → "\\n"
  - <img>                    → "[图片]"
  - 其他标签                 → 剥离标签，保留文本
  - Textual Markup 特殊字符 '[' ']' → 转义为 "\\[" 和 "\\]"
    （转义在生成 markup 之前对纯文字节点执行，已包裹在 markup tag 中的不重复转义）
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

__all__ = ["parse_chapter"]

# ---------------------------------------------------------------------------
# 诊断日志（与 api.py 共用同一文件）
# ---------------------------------------------------------------------------

_LOG_PATH = Path.home() / ".config" / "weread-tui" / "debug.log"


def _debug_log(message: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _escape(text: str) -> str:
    """对纯文本中的 Textual markup 特殊字符进行转义。"""
    # Textual 使用 Rich 语法，'[' 和 ']' 需转义为 '\[' 和 '\]'
    return text.replace("[", r"\[").replace("]", r"\]")


def _is_blockquote_tag(tag: Tag) -> bool:
    """判断一个 Tag 是否应渲染为引用块。"""
    if tag.name == "blockquote":
        return True
    classes: list[str] = tag.get("class", [])  # type: ignore[assignment]
    for cls in classes:
        if "blockquote" in cls.lower() or cls == "wr_blockquote":
            return True
    return False


def _process_inline(node: Tag | NavigableString) -> str:
    """
    递归处理行内节点，返回 Rich Markup 字符串。
    对文本节点直接转义；对 inline 标签转换为对应 markup。
    """
    if isinstance(node, NavigableString):
        return _escape(str(node))

    if not isinstance(node, Tag):
        return ""

    tag = node.name

    if tag in ("strong", "b"):
        inner = "".join(_process_inline(c) for c in node.children)
        return f"[bold]{inner}[/bold]"

    if tag in ("em", "i"):
        inner = "".join(_process_inline(c) for c in node.children)
        return f"[italic]{inner}[/italic]"

    if tag == "br":
        return "\n"

    if tag == "img":
        return r"\[图片\]"

    # 其他内联标签：剥离，递归处理子节点
    return "".join(_process_inline(c) for c in node.children)


def _process_block(tag: Tag, parts: list[str]) -> None:
    """
    处理一个块级 Tag，将渲染结果 append 到 parts。
    目前处理：<p>（含引用变体）、<blockquote>、<br>、<img>。
    其他块级标签递归处理子节点。
    """
    name = tag.name

    if name in ("p", "blockquote", "div"):
        if _is_blockquote_tag(tag):
            # 引用块：每行加前缀，整体用 dim 颜色
            inner = "".join(_process_inline(c) for c in tag.children).strip()
            if inner:
                # 将多行内容的每行加 "│ " 前缀
                lines = inner.split("\n")
                prefixed = "\n".join(f"[dim]│ {line}[/dim]" for line in lines)
                parts.append(prefixed + "\n\n")
        else:
            # 普通段落
            inner = "".join(_process_inline(c) for c in tag.children).strip()
            if inner:
                parts.append(inner + "\n\n")

    elif name == "br":
        parts.append("\n")

    elif name == "img":
        parts.append(r"\[图片\]")

    elif name in ("h1", "h2", "h3"):
        inner = "".join(_process_inline(c) for c in tag.children).strip()
        if inner:
            parts.append(f"[bold]{inner}[/bold]\n\n")

    elif name in ("style", "script", "head"):
        # 不渲染样式/脚本/头部
        pass

    else:
        # 未识别的块级标签：递归处理子节点
        for child in tag.children:
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if text:
                    parts.append(_escape(text))
            elif isinstance(child, Tag):
                _process_block(child, parts)


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def parse_chapter(html: str) -> str:
    """
    将章节 HTML 转换为 Textual Rich Markup 字符串。

    Args:
        html: 章节正文 HTML（来自 API 的 data 字段）。

    Returns:
        可直接传入 Textual Static / Label 的 markup 字符串。
    """
    _debug_log(
        f"parse_chapter: input html_len={len(html)} "
        f"html_head={html[:500]!r}"
    )

    soup = BeautifulSoup(html, "html.parser")

    parts: list[str] = []

    # 顶层遍历所有子节点
    for node in soup.children:
        if isinstance(node, NavigableString):
            text = str(node).strip()
            if text:
                parts.append(_escape(text))
        elif isinstance(node, Tag):
            _process_block(node, parts)

    result = "".join(parts)
    # 合并多余的连续空行（超过 2 个换行符压缩为 2 个）
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = result.strip()

    _debug_log(
        f"parse_chapter: output markup_len={len(result)} "
        f"markup_head={result[:500]!r}"
    )
    return result
