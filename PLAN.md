# weread-tui 开发计划

> 权威规格见 [SPEC.md](./SPEC.md)。本文件跟踪实现进度，每个阶段完成后在对应复选框打勾。

---

## 阶段总览

| 阶段 | 内容 | 前置依赖 |
|------|------|---------|
| P0 | 项目脚手架 | 无 |
| P1 | 本地状态 `state.py` | P0 |
| P2 | 认证 `auth.py` | P0 |
| P3 | API 层 `api.py` | P2 |
| P4 | HTML 解析 `parser.py` | P0 |
| P5 | CLI 入口 `cli.py` | P2、P3 |
| P6 | TUI 骨架 `tui/app.py` | P5 |
| P7 | 书架视图 `tui/shelf.py` | P3、P6 |
| P8 | 阅读视图 `tui/reader.py` | P3、P4、P6 |
| P9 | 集成与收尾 | P7、P8 |

---

## P0 — 项目脚手架

**目标**：能 `pip install -e .` 并执行 `weread --help`。

- [x] 创建 `pyproject.toml`（依赖、入口点、Python ≥ 3.11）
- [x] 创建包骨架（所有 `.py` 文件空实现，`__init__.py` 只写版本号）
- [x] `weread/__main__.py`：`python -m weread` 入口，调用 `cli.main()`
- [x] 验证：`pip install -e . && weread --help` 无报错

**关键细节**：
```toml
[project.scripts]
weread = "weread.cli:main"
```

---

## P1 — 本地状态（state.py）

**目标**：提供读写 `~/.config/weread-tui/state.json` 的接口。

- [x] `load_state() -> dict`：读文件，不存在则返回 `{}`
- [x] `save_state(data: dict)`：原子写入（写临时文件再 rename）
- [x] `get_shelf_cache()`：返回缓存书架；若 `updated_at` 距今 > 5 分钟则返回 `None`
- [x] `set_shelf_cache(books: list)`：写入带时间戳的缓存
- [x] `get_last_position() -> tuple[str, int] | None`：返回 `(book_id, chapter_uid)`
- [x] `set_last_position(book_id: str, chapter_uid: int)`

**状态文件结构**：
```json
{
  "last_book_id": "695233",
  "last_chapter_uid": 109,
  "shelf_cache": {
    "updated_at": 1779174051,
    "books": [...]
  }
}
```

---

## P2 — 认证（auth.py）

**目标**：扫码登录、Cookie 持久化、过期检测。

- [x] `load_cookie() -> tuple[str, str] | None`：从 keyring 读取 `(wr_vid, wr_skey)`
- [x] `save_cookie(vid: str, skey: str)`：写入 macOS Keychain（service name: `weread-tui`）
- [x] `clear_cookie()`：删除 keyring 条目
- [x] `login() -> tuple[str, str]`：完整扫码流程
  - 请求 `https://weread.qq.com/web/login` 获取二维码 token
  - 用 `qrcode` 库在终端输出 ASCII 二维码
  - 每 2 秒轮询扫码状态，超过 120 秒则超时报错
  - 从响应 `Set-Cookie` 中提取 `wr_vid`、`wr_skey`
  - 调用 `save_cookie()` 持久化
- [x] `is_cookie_expired(response_body: dict) -> bool`：检测 `errCode == -2012`

**注意**：
- Cookie 只存 keyring，**绝对不能**写到文件
- `keyring` 在无图形界面的 Linux 环境可能需要 `keyrings.alt` 后端

---

## P3 — API 层（api.py）

**目标**：封装全部 4 个端点，统一错误处理。

- [x] `WeReadClient` 类，构造函数接收 `(vid, skey)`，内部创建 `httpx.AsyncClient`
- [x] 公共 Headers（User-Agent、Referer、Cookie）在构造时注入
- [x] `async get_shelf() -> dict`：`GET /web/shelf/sync?synckey=0&teenmode=0&albumTypes=1&crossDeviceSync=1`
- [x] `async get_book_info(book_id: str) -> dict`：`GET /web/book/info?bookId={book_id}`
- [x] `async get_chapter_list(book_id: str) -> list`：`GET /web/book/chapterInfos?bookIds={book_id}&synckeys=0`
- [x] `async get_chapter_content(book_id: str, chapter_uid: int) -> str`：`GET /web/book/chapter/e3?bookId={book_id}&chapterUid={chapter_uid}`，返回 HTML 字符串
- [x] 错误处理：
  - HTTP 401 或响应体 `errCode == -2012` → 抛 `CookieExpiredError`
  - 网络超时 → 抛 `NetworkError`
  - DRM 章节（响应体有加密标记）→ 抛 `DRMChapterError`

**Headers 模板**：
```python
{
    "Cookie": f"wr_vid={vid}; wr_skey={skey}",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://weread.qq.com/",
}
```

---

## P4 — HTML 解析（parser.py）

**目标**：将章节 HTML 转换为 Textual 可渲染的 Rich Markup 字符串。

- [x] `parse_chapter(html: str) -> str`：主入口
- [x] 处理 `<strong>`、`<b>` → `[bold]...[/bold]`
- [x] 处理 `<em>`、`<i>` → `[italic]...[/italic]`
- [x] 处理 `<p>` → 段落后追加空行
- [x] 处理 `<br>` → 换行
- [x] 处理引用块（三种来源均需支持）：
  - `<p class="wr_blockquote">`
  - class 中含 `blockquote` 字样的 `<p>`
  - `<blockquote>` 标签
  - 渲染为缩进文本，前缀加 `│ ` 模拟左边框
- [x] `<img>` → 输出占位符 `[图片]`
- [x] 其他未知标签 → 剥离标签保留文本
- [x] 对 Textual markup 特殊字符（`[`、`]`）做转义，避免渲染错误

**验证方式**：写几个单元测试用例（不需要真实 API，直接传 HTML 字符串）。

---

## P5 — CLI 入口（cli.py）

**目标**：三条 click 命令正确串联认证与 TUI 启动。

- [x] `@click.group()` 定义 `main`
- [x] `weread`（默认命令）：
  - 读取 Cookie，未登录则自动调用 `login()`
  - 启动 `WeReadApp`
- [x] `weread login`：强制重新扫码（覆盖旧 Cookie）
- [x] `weread logout`：调用 `clear_cookie()`，清除 state 中的缓存

---

## P6 — TUI 骨架（tui/app.py）

**目标**：Textual App 能在 `ShelfView` 和 `ReaderView` 之间切换。

- [x] `WeReadApp(App)` 继承 `textual.app.App`
- [x] `on_mount()`：显示 `ShelfView`
- [x] `show_shelf()`：切换到书架视图
- [x] `show_reader(book_id, chapter_uid)`：切换到阅读视图
- [x] 全局按键 `q` → 退出
- [x] 将 `WeReadClient` 实例注入 App，供子视图访问

---

## P7 — 书架视图（tui/shelf.py）

**目标**：展示书籍列表、分组、阅读进度，支持键盘操作。

- [x] `ShelfView(Widget)` 继承 Textual Widget
- [x] `on_mount()`：从 `state.get_shelf_cache()` 读缓存；若过期则调用 `api.get_shelf()` 并刷新
- [x] 渲染书籍列表：书名、作者、进度条（`█` 填充，`░` 空余）
- [x] 支持分组（来自 `archive` 字段），`g` 键展开/收起
- [x] 键盘绑定：`j`/`k`/`↑`/`↓` 移动、`Enter` 打开、`/` 搜索、`r` 刷新、`q` 退出
- [x] `Enter`：读取云端 `bookProgress` 中该书的 `chapterUid`，调用 `app.show_reader()`
- [x] 加载中显示 spinner，加载失败显示错误提示

---

## P8 — 阅读视图（tui/reader.py）

**目标**：全宽正文渲染 + 目录浮层，支持键盘翻章节与滚动。

- [x] `ReaderView(Widget)` 继承 Textual Widget，接收 `book_id`、`chapter_uid`
- [x] `on_mount()`：调用 `api.get_chapter_content()` + `parser.parse_chapter()` 渲染正文
- [x] 顶部单行信息栏：书名 + 章节标题 + 进度百分比
- [x] 底部状态栏：按键提示
- [x] 键盘绑定：
  - `j`/`k` → 滚动
  - `Space`/`Shift+Space` → 翻页
  - `[`/`]` → 上一章/下一章（重新加载内容）
  - `b` → `app.show_shelf()`
  - `q` → 退出
- [x] 目录浮层（`t` 键触发）：
  - 绝对定位覆盖左侧，宽度按终端宽度决定（<80列:20，80-120:24，>120:28）
  - 列表显示章节标题，`j`/`k` 移动，`Enter` 跳转后自动收起，`Esc` 直接收起
- [x] DRM 章节：捕获 `DRMChapterError`，显示 `「此章节已加密，无法在终端阅读」` 提示
- [x] 宽终端（>120列）：正文区加左右 padding

---

## P9 — 集成与收尾

**目标**：端到端可用，异常处理完整。

- [ ] 端到端手动测试：登录 → 书架 → 打开书 → 翻章节 → 返回书架 → 退出（需真实 Cookie，留待用户验证）<!-- 需真实环境 -->
- [x] `browser.py`：Playwright headless Chromium 作为章节正文主链路（不再依赖直连正文 HTTP 端点）
- [x] `api.py`：`_debug_log` 写入 `~/.config/weread-tui/debug.log`；`_browser_fallback` 方法集成 browser.py
- [x] `pyproject.toml`：将 `playwright>=1.40.0` 升级为核心依赖
- [x] `tests/test_browser.py`：10 个测试用例，全部通过（168/168）
- [x] Cookie 过期场景：捕获 `CookieExpiredError`，提示用户执行 `weread login`
- [x] 网络断开场景：捕获 `NetworkError`，显示友好提示
- [x] `weread logout` 后立即再执行 `weread` 能正确触发登录流程
- [x] 更新 `README.md`：安装方式 + 快速开始
- [x] 检查 `pyproject.toml` 版本、分类器等元信息是否填写完整

---

## 当前进度

| 阶段 | 状态 |
|------|------|
| P0 项目脚手架 | ✅ 完成 |
| P1 本地状态 | ✅ 完成 |
| P2 认证 | ✅ 完成 |
| P3 API 层 | ✅ 完成 |
| P4 HTML 解析 | ✅ 完成 |
| P5 CLI 入口 | ✅ 完成 |
| P6 TUI 骨架 | ✅ 完成 |
| P7 书架视图 | ✅ 完成 |
| P8 阅读视图 | ✅ 完成 |
| P9 集成与收尾 | ✅ 完成（端到端手动测试留待用户验证）|
