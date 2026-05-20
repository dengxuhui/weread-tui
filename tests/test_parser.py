"""tests/test_parser.py — parse_chapter() 单元测试（不需要网络，直接传 HTML 字符串）。"""

from __future__ import annotations

import pytest
from weread.parser import parse_chapter, _escape


# ---------------------------------------------------------------------------
# _escape
# ---------------------------------------------------------------------------

class TestEscape:
    def test_escapes_square_brackets(self):
        assert _escape("[foo]") == r"\[foo\]"

    def test_escapes_open_bracket_only(self):
        assert _escape("[hello") == r"\[hello"

    def test_no_change_for_plain_text(self):
        assert _escape("hello world") == "hello world"

    def test_empty_string(self):
        assert _escape("") == ""


# ---------------------------------------------------------------------------
# 基本格式标签
# ---------------------------------------------------------------------------

class TestInlineTags:
    def test_strong(self):
        result = parse_chapter("<p><strong>粗体</strong></p>")
        assert "[bold]粗体[/bold]" in result

    def test_b_tag(self):
        result = parse_chapter("<p><b>粗体B</b></p>")
        assert "[bold]粗体B[/bold]" in result

    def test_em(self):
        result = parse_chapter("<p><em>斜体</em></p>")
        assert "[italic]斜体[/italic]" in result

    def test_i_tag(self):
        result = parse_chapter("<p><i>斜体I</i></p>")
        assert "[italic]斜体I[/italic]" in result

    def test_mixed_inline(self):
        html = "<p>这是<strong>粗体</strong>与<em>斜体</em>的示例。</p>"
        result = parse_chapter(html)
        assert "[bold]粗体[/bold]" in result
        assert "[italic]斜体[/italic]" in result
        assert "这是" in result

    def test_nested_bold_italic(self):
        html = "<p><strong><em>粗斜体</em></strong></p>"
        result = parse_chapter(html)
        assert "[bold]" in result
        assert "[italic]" in result
        assert "粗斜体" in result


# ---------------------------------------------------------------------------
# 段落 <p>
# ---------------------------------------------------------------------------

class TestParagraph:
    def test_paragraph_ends_with_double_newline(self):
        result = parse_chapter("<p>第一段</p><p>第二段</p>")
        # 两段之间应有空行分隔
        assert "第一段" in result
        assert "第二段" in result
        assert result.index("第一段") < result.index("第二段")

    def test_empty_paragraph_ignored(self):
        result = parse_chapter("<p></p><p>有内容</p>")
        assert "有内容" in result

    def test_plain_text_paragraph(self):
        result = parse_chapter("<p>普通文本</p>")
        assert "普通文本" in result


# ---------------------------------------------------------------------------
# 换行 <br> 与图片 <img>
# ---------------------------------------------------------------------------

class TestBrAndImg:
    def test_br_becomes_newline(self):
        result = parse_chapter("<p>第一行<br/>第二行</p>")
        assert "\n" in result
        assert "第一行" in result
        assert "第二行" in result

    def test_img_becomes_placeholder(self):
        result = parse_chapter('<p><img src="https://example.com/img.png"/></p>')
        assert "[图片]" in result


# ---------------------------------------------------------------------------
# 引用块
# ---------------------------------------------------------------------------

class TestBlockquote:
    def test_blockquote_tag(self):
        result = parse_chapter("<blockquote>这是引用</blockquote>")
        assert "│ " in result
        assert "这是引用" in result
        assert "[dim]" in result

    def test_p_with_wr_blockquote_class(self):
        result = parse_chapter('<p class="wr_blockquote">微信引用块</p>')
        assert "│ " in result
        assert "微信引用块" in result

    def test_p_with_blockquote_in_class(self):
        result = parse_chapter('<p class="readerChapter_blockquote_style">含blockquote类</p>')
        assert "│ " in result
        assert "含blockquote类" in result

    def test_p_class_case_insensitive(self):
        result = parse_chapter('<p class="Blockquote_main">大写引用</p>')
        assert "│ " in result

    def test_regular_p_not_blockquote(self):
        result = parse_chapter('<p class="regular">普通段落</p>')
        assert "│ " not in result


# ---------------------------------------------------------------------------
# 特殊字符转义
# ---------------------------------------------------------------------------

class TestEscaping:
    def test_square_brackets_in_text_escaped(self):
        result = parse_chapter("<p>[重要] 这是测试</p>")
        # 文字中的 '[' 和 ']' 必须被转义，避免 Textual 误解为 markup
        assert r"\[重要\]" in result

    def test_square_brackets_inside_bold_escaped(self):
        result = parse_chapter("<p><strong>[加粗含括号]</strong></p>")
        assert r"\[加粗含括号\]" in result
        assert "[bold]" in result


# ---------------------------------------------------------------------------
# 其他标签剥离
# ---------------------------------------------------------------------------

class TestUnknownTagsStripped:
    def test_span_stripped(self):
        result = parse_chapter("<p><span>普通span</span></p>")
        assert "普通span" in result
        assert "span" not in result.replace("普通span", "")

    def test_div_with_text(self):
        result = parse_chapter("<div><p>div内段落</p></div>")
        assert "div内段落" in result

    def test_unknown_tag_text_preserved(self):
        result = parse_chapter("<article><p>保留文本</p></article>")
        assert "保留文本" in result


# ---------------------------------------------------------------------------
# 多余空行压缩
# ---------------------------------------------------------------------------

class TestExcessiveNewlines:
    def test_no_triple_newlines(self):
        html = "<p>段落一</p><p>段落二</p><p>段落三</p>"
        result = parse_chapter(html)
        assert "\n\n\n" not in result

    def test_result_stripped(self):
        result = parse_chapter("  <p>文本</p>  ")
        assert not result.startswith("\n")
        assert not result.endswith("\n\n")


# ---------------------------------------------------------------------------
# 综合示例（模拟真实章节结构）
# ---------------------------------------------------------------------------

class TestFullChapter:
    def test_full_chapter_structure(self):
        html = """
        <p>罗辑从水晶球中看到了宇宙的最终归宿。</p>
        <p class="wr_blockquote">「宇宙的尺度让一切文明都显得渺小。」</p>
        <p>他闭上了眼睛，心中充满了<strong>宁静</strong>与<em>释然</em>。</p>
        <p><img src="https://img.weread.qq.com/cover.jpg"/>图注：宇宙全景</p>
        """
        result = parse_chapter(html)

        assert "罗辑从水晶球" in result
        assert "│ " in result  # 引用块
        assert "[bold]宁静[/bold]" in result
        assert "[italic]释然[/italic]" in result
        assert "[图片]" in result
        assert "图注：宇宙全景" in result
        assert "\n\n\n" not in result
