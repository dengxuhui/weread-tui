# weread-tui

macOS 终端微信读书阅读器。将书架与阅读内容带入终端，无需切换应用。

基于微信读书网页版非官方 API，使用 Python + [Textual](https://github.com/Textualize/textual) 构建 TUI。

---

## 截图

```
┌─────────────────────────────────────────────────┐
│  书架                                            │
├─────────────────────────────────────────────────┤
│  ─── 最近阅读 ───                               │
│  ▶ 三体全集              刘慈欣  ██████████ 100%│
│    社会心理学            迈尔斯  █░░░░░░░░░   8%│
│  ─── 工作相关 ───                               │
│    游戏编程模式          Nystrom █████████░  99%│
├─────────────────────────────────────────────────┤
│  Enter 打开  r 刷新  / 搜索  g 展开/收起  q 退出 │
└─────────────────────────────────────────────────┘
```

---

## 安装

**推荐方式（pipx）**

```bash
pipx install weread-tui
```

**或使用 pip**

```bash
pip install weread-tui
```

**从源码开发安装**

```bash
git clone https://github.com/yourname/weread-tui.git
cd weread-tui
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 安装 Playwright 浏览器内核（首次必需）
.venv/bin/playwright install chromium
```

---

## 使用

**首次使用（扫码登录）**

```bash
weread
```

若本地未存储 Cookie，会自动引导扫码登录。

**强制重新登录**

```bash
weread login
```

**退出登录**

```bash
weread logout
```

---

## 键盘操作

### 书架（ShelfView）

| 按键 | 说明 |
|------|------|
| `j` / `k` 或 `↑` / `↓` | 移动选中 |
| `Enter` | 打开书籍（跳至上次阅读章节） |
| `/` | 搜索书名 |
| `g` | 展开 / 收起当前分组 |
| `r` | 刷新书架 |
| `q` | 退出程序 |

### 阅读（ReaderView）

| 按键 | 说明 |
|------|------|
| `j` / `k` | 上下滚动 |
| `Space` / `Shift+Space` | 向下 / 向上翻页 |
| `[` / `]` | 上一章 / 下一章 |
| `t` | 唤出目录浮层 |
| `Esc` | 收起目录浮层 |
| `Enter`（目录中） | 跳转章节 |
| `b` | 返回书架 |
| `q` | 退出程序 |

---

## 已知限制

- **进度不回写**：阅读记录不同步至微信读书客户端（MVP 范围内接受）
- **DRM 加密章节**：显示提示，无法渲染内容
- **图文书籍**：`<img>` 以 `[图片]` 占位，不渲染图片
- **离线阅读**：不支持，章节内容不缓存

---

## 章节正文链路

- 当前版本将 **Playwright 作为章节正文主链路**：打开网页阅读器并从 DOM 提取已解密内容
- 原因：微信读书网页版的 `e3/e_N` 直连 HTTP 端点在不同书籍上波动较大，稳定性不足
- 首次运行前需执行：`playwright install chromium`

---

## 依赖

- Python ≥ 3.11
- [textual](https://github.com/Textualize/textual) ≥ 0.60
- [httpx](https://www.python-httpx.org/) ≥ 0.27
- [click](https://click.palletsprojects.com/) ≥ 8.1
- [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) ≥ 4.12
- [keyring](https://github.com/jaraco/keyring) ≥ 25（Cookie 存入 macOS Keychain）
- [qrcode](https://github.com/lincolnloop/python-qrcode) ≥ 7.4

---

## 免责声明

本项目使用微信读书网页版**非官方 API**，仅供个人学习与研究。API 结构可能随时变更，作者不对因此造成的功能损坏负责。请遵守微信读书用户协议。
