# weread-tui — 技术规格文档 SPEC

> 一个 macOS 命令行工具，将微信读书的书架与阅读内容带入终端，供开发者在 vibe coding 过程中随时翻书，无需切换应用。
>
> **核心定位：**
> - 无需安装 App，浏览器扫码一次，后续全程 CLI
> - 基于微信读书网页版非官方 API，无第三方服务依赖
> - 富文本渲染：保留粗体、斜体、引用块等基本格式
> - 阅读进度与微信读书账号自动同步（读取云端位置，本地缓存辅助）
> - 开源协议：MIT

---

## 一、项目定位与边界

**适用场景：** 开发者在终端工作时，利用碎片时间翻阅非技术类书籍

**不适用：**
- 图文混排书籍（漫画、图册等）
- 需要 DRM 解密的加密章节（部分版权书籍）
- 离线阅读（需联网拉取章节内容）

**支持平台：** macOS（主要目标）；代码结构对 Linux 友好，Windows 不作保证

**分发方式：** `pipx install weread-tui`（PyPI 发布）或 `pipx install git+https://github.com/...`

---

## 二、系统架构

### 整体数据流

```
┌─────────────────────────────────────────┐
│              weread-tui                  │
│                                         │
│  ┌──────────┐    ┌────────────────────┐ │
│  │  auth.py │    │      api.py        │ │
│  │          │    │                    │ │
│  │ 浏览器扫码│    │ GET /web/shelf/sync│ │
│  │ Cookie   │───▶│ GET /web/book/     │ │
│  │ 持久化   │    │      chapterInfos  │ │
│  └──────────┘    │ GET /web/book/     │ │
│                  │      chapter/e3    │ │
│                  └────────┬───────────┘ │
│                           │ HTML 章节内容│
│                  ┌────────▼───────────┐ │
│                  │    parser.py       │ │
│                  │                   │ │
│                  │ HTML → Markup      │ │
│                  │ 粗体/斜体/引用块   │ │
│                  └────────┬───────────┘ │
│                           │ Rich Markup │
│  ┌────────────────────────▼───────────┐ │
│  │              tui/                  │ │
│  │                                   │ │
│  │  ShelfView   ←→   ReaderView      │ │
│  │  书架浏览         全宽阅读界面     │ │
│  └───────────────────────────────────┘ │
└─────────────────────────────────────────┘
            ↕ Cookie（~/.config/weread-cli/）
    微信读书服务器 weread.qq.com
```

### 模块职责

| 模块 | 职责 |
|------|------|
| `auth.py` | 浏览器扫码登录，提取并持久化 Cookie，检测过期并提示重新登录 |
| `api.py` | 封装书架/目录 API 请求，章节正文走 Playwright 主链路 |
| `browser.py` | Playwright 打开阅读器页面，提取前端解密后的章节 DOM |
| `parser.py` | 将章节 HTML 转换为 Textual Rich Markup，处理粗体、斜体、引用块 |
| `state.py` | 本地阅读位置缓存，辅助快速恢复上次进度（云端同步为主） |
| `tui/app.py` | Textual 主 App，管理视图切换与全局键盘绑定 |
| `tui/shelf.py` | 书架视图，展示书籍列表、分组、阅读进度 |
| `tui/reader.py` | 阅读视图，全宽正文 + 按需唤出的目录浮层，底部状态栏 |

---

## 三、项目结构

```
weread-tui/
├── pyproject.toml              ← 依赖声明，entry point: weread
├── README.md
├── SPEC.md
├── CHANGELOG.md
├── LICENSE                     ← MIT
│
└── weread/
    ├── __init__.py
    ├── __main__.py             ← python -m weread 入口
    ├── cli.py                  ← click 命令解析（weread / weread login / weread clear）
    ├── auth.py                 ← 登录与 Cookie 管理
    ├── api.py                  ← API 封装
    ├── parser.py               ← HTML → Rich Markup
    ├── state.py                ← 本地状态持久化
    └── tui/
        ├── __init__.py
        ├── app.py              ← Textual App 主类
        ├── shelf.py            ← 书架视图 Widget
        └── reader.py           ← 阅读视图 Widget
```

### pyproject.toml 关键依赖

```toml
[project]
name = "weread-tui"
version = "0.1.0"
requires-python = ">=3.11"
license = {text = "MIT"}

dependencies = [
    "textual>=0.60.0",      # TUI 框架
    "httpx>=0.27.0",        # HTTP 客户端（支持 async）
    "click>=8.1.0",         # CLI 参数解析
    "beautifulsoup4>=4.12", # HTML 解析
    "keyring>=25.0.0",      # 安全存储 Cookie（系统钥匙串）
    "qrcode>=7.4.0",        # 终端二维码（登录引导用）
]

[project.scripts]
weread = "weread.cli:main"
```

---

## 四、核心模块详细设计

### 4.1 认证：auth.py

**首次登录流程：**

```
1. 调用 https://weread.qq.com/web/login 获取二维码 token
2. 在终端渲染二维码（qrcode 库，ASCII 输出）
3. 轮询扫码状态（每 2 秒一次，最多等待 120 秒）
4. 扫码确认后，从响应 Set-Cookie 中提取关键字段：
   wr_vid, wr_skey（核心认证字段）
5. 通过 keyring 存入系统钥匙串（macOS Keychain）
```

**关键 Cookie 字段：**

| 字段 | 说明 |
|------|------|
| `wr_vid` | 用户 ID，相对稳定 |
| `wr_skey` | 会话密钥，约 7~30 天过期 |

**过期检测：**

API 返回 `{"errCode": -2012}` 或 HTTP 401 时，判定为 Cookie 失效，自动触发重新登录提示。

**CLI 命令：**

```bash
weread login    # 手动触发重新登录（显示二维码）
weread logout   # 清除本地 Cookie
weread          # 直接启动 TUI（若未登录则自动引导）
```

---

### 4.2 API 封装：api.py

所有请求基于 `httpx.AsyncClient`，统一携带以下 Headers：

```python
headers = {
    "Cookie": f"wr_vid={vid}; wr_skey={skey}",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://weread.qq.com/",
}
```

**核心接口：**

```
GET /web/shelf/sync
    ?synckey=0&teenmode=0&albumTypes=1&crossDeviceSync=1
    → 书架数据（书籍列表 + 阅读进度 + 分组归档）

GET /web/book/info
    ?bookId={bookId}
    → 书籍元信息（书名、作者、封面URL、章节数）

GET /web/book/chapterInfos
    ?bookIds={bookId}&synckeys=0
    → 章节列表（chapterUid、标题、字数）

章节正文：
    通过 Playwright 打开 /web/reader/{bookKey}?chapterUid={uid}
    等待前端请求 e_N 并渲染后，从 DOM 提取章节 HTML
```

**书架返回结构（关键字段）：**

```json
{
  "books": [
    {
      "bookId": "695233",
      "title": "三体全集",
      "author": "刘慈欣",
      "cover": "https://...",
      "progress": 100
    }
  ],
  "bookProgress": [
    {
      "bookId": "695233",
      "progress": 100,
      "chapterUid": 194,
      "chapterOffset": 2305,
      "chapterIdx": 109,
      "readingTime": 201814
    }
  ],
  "archive": [
    {
      "name": "小说名著",
      "bookIds": ["695233", ...]
    }
  ]
}
```

**章节内容说明：**

正文不再依赖 `GET /web/book/chapter/e3` 直连响应，
而是由浏览器上下文触发微信读书前端解密流程后，从页面 DOM 中提取。

---

### 4.3 内容解析：parser.py

章节内容为 HTML，需转换为 Textual 可渲染的 Rich Markup。

**处理规则：**

| HTML 标签 / 属性 | 终端渲染 | 说明 |
|-----------------|---------|------|
| `<strong>`, `<b>` | `[bold]...[/bold]` | 粗体 |
| `<em>`, `<i>` | `[italic]...[/italic]` | 斜体 |
| `<p class="quote">` 或 blockquote | 缩进 + 左边框样式 | 引用块 |
| `<p>` | 段落间距（一个空行） | 正文段落 |
| `<br>` | 换行 | |
| 其他标签 | 剥离标签，保留文本 | 不支持的格式降级处理 |

**解析示例：**

```python
# 输入
html = "<p>这是<strong>粗体</strong>与<em>斜体</em>的示例。</p>"

# 输出（Textual Rich Markup）
markup = "这是[bold]粗体[/bold]与[italic]斜体[/italic]的示例。\n"
```

**引用块处理：**

微信读书中引用块的 HTML class 不统一，通过以下启发式规则识别：
- `<p class="wr_blockquote">` 或包含 `blockquote` class
- `<blockquote>` 标签
- 识别为引用后，在 Textual 中用 `Rule` 或带颜色的左边距模拟

---

### 4.4 本地状态：state.py

存储路径：`~/.config/weread-tui/state.json`

**存储内容：**

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

**设计原则：**
- 书架数据本地缓存，TTL 为 5 分钟（避免每次启动都请求 API）
- 阅读位置以云端 `bookProgress` 为准，本地只记录上次打开的书
- 章节内容不缓存（避免内容过期与存储膨胀）

---

### 4.5 TUI 界面：tui/

基于 [Textual](https://github.com/Textualize/textual) 框架实现。

#### 书架视图（ShelfView）

```
┌──────────────────────────────────────────────────┐
│  weread-tui                              v0.1.0  │
├──────────────────────────────────────────────────┤
│  最近阅读                                         │
│                                                  │
│  ▶ 三体全集          刘慈欣    ██████████ 100%   │
│    社会心理学        迈尔斯    ██░░░░░░░░  8%    │
│    置身事内          兰小欢    ██████░░░░  61%   │
│    原则：应对变化…   达利欧    ████████░░  87%   │
│    城乡中国          周其仁    █████░░░░░  51%   │
│                                                  │
│  ─── 工作相关 ────────────────────────────────── │
│    游戏编程模式      Nystrom   ████████░░  99%   │
│    架构整洁之道      马丁      ░░░░░░░░░░   0%   │
│    ...                                           │
├──────────────────────────────────────────────────┤
│  ↑↓ 选择   Enter 打开   / 搜索   q 退出          │
└──────────────────────────────────────────────────┘
```

**交互：**

| 按键 | 行为 |
|------|------|
| `j` / `k` 或 `↑` / `↓` | 移动选中 |
| `Enter` | 打开书籍，跳转至上次读到的章节 |
| `/` | 过滤搜索书名 |
| `g` | 展开 / 收起分组 |
| `r` | 刷新书架（重新请求 API） |
| `q` | 退出程序 |

#### 阅读视图（ReaderView）

**设计原则：全宽优先，目录按需唤出。**

默认状态下正文占满全部宽度，在 80 列窄终端下与宽屏下表现一致。目录通过 `t` 键以浮层形式临时展开，选完章节后自动收起，不长期占用横向空间。

**默认状态（全宽正文）：**

```
┌──────────────────────────────────────┐
│  三体全集  第109章 · 归宿   100%     │  ← 顶部单行信息
├──────────────────────────────────────┤
│                                      │
│  罗辑从水晶球中看到了宇宙的最终归    │
│  宿，那是一片永恒的黑暗，所有的文    │
│  明之光都已熄灭。                    │
│                                      │
│  「宇宙的尺度让一切文明都显得渺      │
│  小，」他想，「但渺小并不意味着      │
│  没有意义。」                        │
│                                      │
│  他闭上了眼睛。                      │
│                                      │
├──────────────────────────────────────┤
│  j/k 滚动  []章节  t 目录  b 书架   │
└──────────────────────────────────────┘
```

**按 `t` 后，目录以浮层覆盖左侧（选完自动收起）：**

```
┌──────────────────────────────────────┐
│  三体全集  第109章 · 归宿   100%     │
├────────────┬─────────────────────────┤
│ 目录       │  罗辑从水晶球中看到了  │
│            │  宇宙的最终归宿，那是  │
│ 第107章    │  一片永恒的黑暗，所有  │
│ 第108章    │  的文明之光都已熄灭。  │
│▶ 第109章   │                        │
│            │  「宇宙的尺度让一切文  │
│ ← Esc关闭  │  明都显得渺小，」      │
├────────────┴─────────────────────────┤
│  j/k 滚动  []章节  t 目录  b 书架   │
└──────────────────────────────────────┘
```

**交互：**

| 按键 | 行为 |
|------|------|
| `j` / `k` | 上下滚动（行） |
| `Space` / `Shift+Space` | 向下翻页 / 向上翻页 |
| `[` / `]` | 上一章 / 下一章 |
| `t` | 唤出目录浮层 |
| `Esc` | 收起目录浮层 |
| `Enter`（目录中） | 跳转至选中章节，浮层自动收起 |
| `b` | 返回书架视图 |
| `q` | 退出程序 |

**宽度自适应行为：**

| 终端宽度 | 行为 |
|---------|------|
| < 80 列 | 全宽正文，目录浮层宽度固定 20 列 |
| 80～120 列 | 全宽正文，目录浮层宽度固定 24 列 |
| > 120 列 | 全宽正文，正文区加左右 padding 提升可读性，目录浮层宽度 28 列 |

目录浮层始终以绝对定位覆盖在正文上方，不影响正文区域宽度。

---

## 五、阅读进度同步

**读取进度（启动时）：**

从 `/web/shelf/sync` 返回的 `bookProgress` 中读取：
- `chapterUid`：上次读到的章节 ID
- `chapterOffset`：章节内的字节偏移量（用于定位段落，尽力而为）

**写入进度（阅读时）：**

微信读书网页版本身不提供公开的进度上报 API。当前版本 **不主动上报进度**，用户在 CLI 中的阅读位置不会同步回微信读书客户端。本地用 `state.json` 记录上次打开的书和章节。

> 后续版本可研究网页版的 `/web/book/read` 接口，实现双向同步。

---

## 六、已知限制与风险

| 风险 | 影响 | 说明 |
|------|------|------|
| 非官方 API | API 可能随时变更 | 腾讯未公开此 API，维护需跟进变化 |
| Cookie 有效期 7~30 天 | 需定期重新扫码登录 | 在状态栏显示登录剩余时间（如可获取）|
| 部分书籍 DRM 加密 | 章节内容无法获取 | 检测到加密内容时提示用户，跳过该章节 |
| 进度不回写 | CLI 阅读记录不同步至手机 | MVP 范围内接受，文档说明 |
| 图文书籍 | 终端无法渲染图片 | 遇到 `<img>` 标签时显示占位符 `[图片]` |
| 网络依赖 | 无法离线阅读 | 属于设计限制，不在 MVP 解决范围 |

---

## 七、用户使用流程

### 首次安装

```bash
# 通过 pipx 安装（推荐，隔离环境）
pipx install weread-tui

# 首次运行，自动引导登录
weread
```

```
✦ 欢迎使用 weread-tui
  请用微信扫描以下二维码登录：

  █████████████
  █ ▄▄▄▄▄▄▄ █
  █ █ ▄▀▄ █ █
  ...（二维码）

  等待扫码中... ⠋
```

### 日常使用

```bash
weread          # 启动 TUI，直接进入上次阅读的书
weread login    # 重新登录（Cookie 过期时使用）
weread logout   # 退出登录，清除本地凭证
```

---

## 八、后续可扩展功能（MVP 范围外）

| 功能 | 说明 |
|------|------|
| 进度回写 | 研究 `/web/book/read` 接口，将 CLI 阅读位置同步回微信读书 |
| 章节内容本地缓存 | 缓存最近阅读的若干章节，支持短暂离线 |
| 划线 / 想法 | 读取并展示个人划线与笔记 |
| Linux 支持 | 测试并修复 Linux 下的兼容性问题 |
| Homebrew 分发 | 提供 `brew install weread-tui` 安装方式 |
| 字体 / 主题配置 | 支持切换配色方案（亮色 / 暗色 / 护眼色） |
