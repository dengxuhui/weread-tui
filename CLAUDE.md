# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## 项目概述

macOS 终端微信读书阅读器，基于微信读书**网页版非官方 API**，使用 Python + [Textual](https://github.com/Textualize/textual) 构建 TUI。

**权威规格文档：** `SPEC.md`（实现细节以此为准）  
**开发约束文档：** `AGENTS.md`（核心约束、API 要点、文档维护规则）

---

## 常用命令

```bash
# 创建并激活虚拟环境
python3 -m venv .venv

# 开发安装（含测试依赖）
.venv/bin/pip install -e ".[dev]"

# 本地运行
.venv/bin/python -m weread
# 或
.venv/bin/weread

# 运行全部测试
.venv/bin/pytest

# 运行单个测试文件
.venv/bin/pytest tests/test_parser.py

# 运行单个测试类 / 用例
.venv/bin/pytest tests/test_parser.py::TestBlockquote
.venv/bin/pytest tests/test_parser.py::TestBlockquote::test_blockquote_tag

# 安装可选 Playwright 依赖（browser fallback 功能）
.venv/bin/pip install -e ".[browser]"
.venv/bin/playwright install chromium
```

---

## 架构概览

```
CLI 入口（cli.py）
    ↓ 读取 Cookie / 引导登录
auth.py  ←→  macOS Keychain（security CLI / keyring）
    ↓ (vid, skey)
tui/app.py（WeReadApp）
    ↓ 管理屏幕栈
    ├── tui/shelf.py（ShelfScreen）  ← 书架列表 + 分组 + 进度
    └── tui/reader.py（ReaderScreen）← 全宽正文 + 目录浮层
             ↓ 请求章节内容
         api.py（WeReadClient）
             ↓ 返回 HTML
         parser.py（parse_chapter）
             ↓ Rich Markup
         Textual Static 渲染

state.py → ~/.config/weread-tui/state.json（本地缓存）
```

### 模块职责

| 模块 | 职责 |
|------|------|
| `cli.py` | Click 命令：`weread` / `weread login` / `weread logout` |
| `auth.py` | 扫码登录（长轮询），Cookie 存取（macOS Keychain 优先） |
| `api.py` | `WeReadClient` — 封装所有 httpx 异步请求，含三级回退策略 |
| `browser.py` | Playwright headless Chromium，作为章节 API 404 时的最终 fallback |
| `parser.py` | BeautifulSoup HTML → Textual Rich Markup（仅 `parse_chapter` 为公开接口）|
| `state.py` | `~/.config/weread-tui/state.json`：书架缓存（TTL 5 分钟）+ 上次阅读位置 |
| `tui/app.py` | `WeReadApp` — 持有 `WeReadClient` 单例，管理 `ShelfScreen`/`ReaderScreen` 屏幕切换 |
| `tui/shelf.py` | `ShelfScreen` — 解析书架 API 数据，显示分组书目，`@work` 后台异步加载 |
| `tui/reader.py` | `ReaderScreen` — 全宽正文滚动，`TOCOverlay` 目录浮层，宽度自适应 |

---

## 关键设计决策

**章节内容获取三级回退（`api.py:get_chapter_content`）：**
1. `GET https://i.weread.qq.com/book/chapter/e3`（移动端 API，优先）
2. `GET /web/book/chapter/e3`（网页版 API，备用）
3. Playwright browser fallback（仅两个端点均 404 时触发）

DRM 检测：响应含 `encrypt`/`encryptType` 字段或 `errCode == -2010` → 立即抛 `DRMChapterError`，不走 browser fallback。

**章节列表获取双端点（`api.py:get_chapter_list`）：**
- 优先 `POST https://i.weread.qq.com/book/chapterInfos`（JSON body）
- 回退 `GET /web/book/chapterInfos`
- 两种响应格式均支持：`updated` / `chapterInfos` / `chapters` 字段

**Cookie 过期判断：** HTTP 401 或响应 JSON `errCode == -2012` → `CookieExpiredError`

**书架缓存：** `state.py` 缓存完整书架 API 响应 dict（非仅书目列表），TTL 5 分钟。`ShelfScreen` 启动时优先读缓存，`r` 键强制刷新。

**TUI 屏幕导航：** `WeReadApp` 用 Textual 屏幕栈，`show_reader()` 执行 `push_screen`，`show_shelf()` 执行 `pop_screen`。HTTP client 在 `WeReadApp` 实例上共享，子屏幕通过 `self.app.client` 访问。

**`ShelfScreen` 列表渲染：** `ListView` 的 `ListItem` 不设 id，通过并行维护的 `_item_map: list[BookEntry | None]` 按索引识别选中书目（避免 Textual 的 `DuplicateIds` 问题）。`_render_list` 必须 `await book_list.clear()` 后再重建。

**`parser.py` 转义规则：** 纯文本节点中 `[` `]` 必须转义为 `\[` `\]`，避免被 Textual Rich 解析为 markup tag。`[图片]` 占位符是字面字符串，不走 `_escape`（直接写入 parts）。

---

## 测试说明

- `tests/` 下每个模块对应一个测试文件，全部使用 `unittest.mock`，不依赖真实网络
- `test_parser.py`：无需 mock，直接传 HTML 字符串测试
- `test_api.py`：用 `_make_response()` 辅助函数构造带 `request` 的 `httpx.Response` 对象
- `test_tui.py`：使用 `pytest-asyncio` 和 Textual 的 `App.run_test()` 进行 TUI 集成测试
- 调试日志写入 `~/.config/weread-tui/debug.log`（静默失败，不影响运行）

---

## 文档维护

完成较大功能或某开发阶段后，需同步更新：
- `PLAN.md`：将对应阶段复选框改为 `[x]`
- `AGENTS.md`：若目录结构、约束、已知问题有变化则同步修订
