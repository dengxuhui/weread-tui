# 章节内容抓取调试方案（分步执行）

目标：稳定获取**目标章节**正文，并保证解析结果是正文而不是封面/完结页覆盖层。

## 核心原则

先判断“是否命中目标章节”，再做正文提取与解析。

## 分步方案

### Step 1：章节命中门禁（必须先通过）

在 browser 链路中收集并校验以下命中信号：

- `GET /web/book/underlines?...chapterUid={uid}`
- `POST /web/book/read` 请求体中的 `ci`

判定规则：

- 命中：出现 `chapterUid == target_uid` 或 `ci == target_uid`
- 未命中：即使 DOM 有内容，也不返回正文；优先重试导航一次
- 重试后仍未命中：直接失败并抛出可诊断错误（不再返回错误内容）

验收标准：

- 日志可见 `chapter hit` / `chapter miss` 明确信号
- 切到 `chapterUid=7` 时，不再出现“命中 6 但返回 7 内容”的假成功

### Step 2：正文提取分层

仅在 Step 1 命中后执行：

1. DOM 主链路提取正文容器
2. DOM 不可用或疑似覆盖层时，进入 Canvas 捕获回退

注意：覆盖层识别只能作为辅助手段，不能替代章节命中校验。

验收标准：

- 日志可见 DOM/Canvas 使用路径
- DOM 命中覆盖层时会切换到 Canvas

### Step 3：解析正确性

把提取内容转换为 parser 可处理的 HTML 后再进入 `parse_chapter`。

验收标准：

- `parse_chapter` 输入 head 不再是“全书完/推荐值”模板
- 内容与目标章节一致

## 当前执行状态

- [x] Step 1：章节命中门禁
- [x] Step 2：正文提取分层
- [ ] Step 3：解析正确性

### Step 1 已落地改动

- 强化章节命中校验：最终返回前必须满足 `target_uid` 命中 `underlines` 或 `read.ci`
- 重试后增加二次门禁：未命中则拒绝结果，不再“假成功”
- 补充诊断日志：
  - `chapter hit confirmed`
  - `chapter miss after extraction`

### Step 2 已落地改动

- DOM 命中覆盖层时自动切换 Canvas 回退
- Canvas 输出增加噪声行过滤（过滤字母/符号探针行）
- 重试导航改为多路由策略：
  - `reader/{bookKey}?chapterUid=...`
  - `reader/{bookKey}k{hash(chapterUid)}`
  - `reader/{bookId}?chapterUid=...`
  - `reader/{bookId}k{hash(chapterUid)}`

## 调试日志建议（关键字）

- `chapter hit`
- `chapter miss`
- `retry navigation`
- `dom extraction`
- `canvas extraction`
