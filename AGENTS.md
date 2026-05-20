# AGENTS.md — weread-tui

> macOS 终端微信读书阅读器。基于微信读书网页版非官方 API，使用 Python + Textual 构建 TUI。  
> **当前状态：P0–P9 全部完成。**

---

## 语言要求

**所有回复必须使用中文。** 无论用户使用何种语言提问，AI 助手均应以中文作答。

---

## 项目核心约束

- **Python ≥ 3.11**，主要目标平台 macOS，Linux 友好但不保证，不支持 Windows
- **非官方 API**（`weread.qq.com` 网页版），API 结构可能随时变更，实现时需做好错误处理
- **进度不回写**：当前版本 CLI 阅读记录不同步回微信读书客户端（MVP 内接受）
- **章节内容不缓存**：避免内容过期与存储膨胀；书架数据缓存 TTL 为 5 分钟

---

## 目录结构（已实现）

```
weread-tui/
├── pyproject.toml
├── SPEC.md                    ← 权威规格文档，实现细节以此为准
└── weread/
    ├── __init__.py
    ├── __main__.py            ← python -m weread 入口
    ├── cli.py                 ← click 命令：weread / weread login / weread logout
    ├── auth.py                ← 登录与 Cookie 管理（keyring 存 macOS Keychain）
    ├── api.py                 ← 书架/目录 API 封装 + 章节正文调度
    ├── browser.py             ← Playwright headless Chromium 章节正文主链路
    ├── parser.py              ← BeautifulSoup HTML → Textual Rich Markup
    ├── state.py               ← ~/.config/weread-tui/state.json 持久化
    └── tui/
        ├── app.py             ← Textual App 主类，管理视图切换
        ├── shelf.py           ← ShelfView：书架列表 + 分组 + 进度
        └── reader.py          ← ReaderView：全宽正文 + 目录浮层
```

---

## 关键依赖（pyproject.toml）

```toml
requires-python = ">=3.11"

dependencies = [
    "textual>=0.60.0",      # TUI 框架
    "httpx>=0.27.0",        # 异步 HTTP 客户端
    "click>=8.1.0",         # CLI 参数解析
    "beautifulsoup4>=4.12", # HTML 解析
    "keyring>=25.0.0",      # Cookie 存系统钥匙串
    "qrcode>=7.4.0",        # 终端渲染二维码（登录用）
    "playwright>=1.40.0",   # 章节正文主链路
]

[project.scripts]
weread = "weread.cli:main"
```

---

## API 要点（api.py）

所有请求使用 `httpx.AsyncClient`，必须携带：

```python
headers = {
    "Cookie": f"wr_vid={vid}; wr_skey={skey}",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://weread.qq.com/",
}
```

核心端点（base: `https://weread.qq.com`）：

| 端点 | 用途 |
|------|------|
| `GET /web/shelf/sync?synckey=0&teenmode=0&albumTypes=1&crossDeviceSync=1` | 书架 + 阅读进度 + 分组 |
| `GET /web/book/info?bookId={bookId}` | 书籍元信息 |
| `GET /web/book/chapterInfos?bookIds={bookId}&synckeys=0` | 章节列表 |
| Playwright `/web/reader/{bookKey}?chapterUid={uid}` | 章节正文（前端解密后 DOM 提取） |

**Cookie 过期判断**：响应体含 `{"errCode": -2012}` 或 HTTP 401 → 触发重新登录提示。

---

## 认证要点（auth.py）

- Cookie 关键字段：`wr_vid`（用户 ID）和 `wr_skey`（会话密钥，7~30 天过期）
- 存储：`keyring` 写入 macOS Keychain，**不要**明文写文件
- 登录流程：获取二维码 token → 终端渲染 QR → 每 2 秒轮询（最多 120 秒）→ 提取 Set-Cookie

---

## 本地状态（state.py）

路径：`~/.config/weread-tui/state.json`

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

- 书架缓存 TTL 5 分钟（避免每次启动都请求 API）
- 阅读位置以云端 `bookProgress.chapterUid` + `chapterOffset` 为准

---

## HTML 解析规则（parser.py）

| HTML | Textual 输出 |
|------|-------------|
| `<strong>`, `<b>` | `[bold]...[/bold]` |
| `<em>`, `<i>` | `[italic]...[/italic]` |
| `<p class="wr_blockquote">` / `<blockquote>` | 缩进 + 左边框样式 |
| `<p>` | 段落间距（空一行） |
| `<img>` | 占位符 `[图片]` |
| 其他标签 | 剥离标签，保留文本 |

微信读书引用块 class 不统一，需同时匹配 `wr_blockquote`、含 `blockquote` 的 class、以及 `<blockquote>` 标签。

---

## TUI 键盘绑定

**ShelfView（书架）：**

| 按键 | 行为 |
|------|------|
| `j`/`k` 或 `↑`/`↓` | 移动选中 |
| `Enter` | 打开书籍（跳至上次章节） |
| `/` | 过滤搜索书名 |
| `g` | 展开/收起分组 |
| `r` | 刷新书架（重新请求 API） |
| `q` | 退出程序 |

**ReaderView（阅读）：**

| 按键 | 行为 |
|------|------|
| `j`/`k` | 上下滚动 |
| `Space`/`Shift+Space` | 向下/向上翻页 |
| `[`/`]` | 上一章/下一章 |
| `t` | 唤出目录浮层 |
| `Esc` | 收起目录浮层 |
| `Enter`（目录中） | 跳转章节，浮层自动收起 |
| `b` | 返回书架视图 |
| `q` | 退出程序 |

目录浮层以绝对定位覆盖正文左侧，不影响正文宽度。宽终端（>120 列）正文区加左右 padding。

---

## 已知限制（实现时注意）

- **DRM 加密章节**：检测到时显示提示并跳过，不报错崩溃
- **图文书籍**：`<img>` 用 `[图片]` 占位，不尝试渲染
- **进度回写**：MVP 内不实现，`/web/book/read` 接口留待后续研究
- **离线阅读**：设计上不支持，章节内容不缓存

---

## 开发与分发

```bash
# 创建虚拟环境（推荐，避免系统 pip 版本问题）
python3 -m venv .venv

# 开发安装
.venv/bin/pip install -e ".[dev]"

# 本地运行
.venv/bin/python -m weread
# 或
.venv/bin/weread

# 分发
pipx install weread-tui
```

> **注意**：系统自带 pip 可能是 Python 2，务必使用 `python3` 或虚拟环境内的 pip。

首次运行前需安装浏览器内核：

```bash
.venv/bin/playwright install chromium
```

完整规格见 [SPEC.md](./SPEC.md)。开发进度见 [PLAN.md](./PLAN.md)。

---

## 文档维护规则

每完成一个开发阶段（P0–P9）或较大的功能后，必须同步更新以下文档：

- **`PLAN.md`**：将对应阶段的任务复选框改为 `[x]`，并更新底部「当前进度」表格的状态
- **`AGENTS.md`**：若目录结构、命令、约束或已知问题有变化，同步修订对应章节

不需要每次小改动都更新，以下情况必须更新：
- 某阶段所有任务全部完成
- 新增或删除了模块/文件
- 发现了 SPEC 未提及的实现约束或坑点
