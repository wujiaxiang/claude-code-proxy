# DESIGN.md — LLM Gateway Dashboard

> 本项目 UI 为 FastAPI 服务端渲染单页（`server.py` 内嵌 HTML/CSS/JS，非 React）。
> 本文档是 dashboard 的设计契约：所有颜色、字体、间距、组件模式必须追溯到此文件。

## 1. 设计定位

- **页面类型**：内部运维管理面板（operational dashboard）
- **受众**：技术用户（开发者/管理员），长时间盯屏排障
- **主题**：深色（唯一主题，不做浅色反转）
- **Dials**：`DESIGN_VARIANCE: 4` / `MOTION_INTENSITY: 3` / `VISUAL_DENSITY: 6`
  - 低动效：仅手风琴展开/收起与 hover/active 反馈，尊重 `prefers-reduced-motion`
  - 中高密度：数据面板，紧凑但留有呼吸

## 2. 色彩 Token

### 背景（中性冷灰蓝系，无纯黑）
| Token | 值 | 用途 |
|---|---|---|
| `--bg-page` | `#0f1117` | 页面背景 |
| `--bg-surface` | `#12142a` | 总览栏 / 管理面板 |
| `--bg-card` | `#1a1c2e` | 卡片表面 |
| `--bg-inset` | `#151827` | 表格斑马纹 / 标签 / 输入框 |
| `--bg-inset-2` | `#0f1117` | 输入框聚焦背景 |
| `--border` | `#2a2d3e` | 常规边框 |
| `--border-subtle` | `#1f2233` | 细分隔线（表格行、kv 分隔） |
| `--border-focus` | `#3b4060` | hover 边框 |

### 文本（单一家族灰阶）
| Token | 值 | 用途 |
|---|---|---|
| `--text-primary` | `#e0e0e0` | 主文本 / 数值 |
| `--text-secondary` | `#8b8fa3` | 次要文本 / kv 键 |
| `--text-tertiary` | `#6b7280` | 弱化文本 / 标签 / 说明 |
| `--text-body` | `#9ca3af` | 正文 / 描述 |

### 强调色（唯一强调：蓝；语义色仅用于状态）
| Token | 值 | 用途 |
|---|---|---|
| `--accent` | `#60a5fa` | 链接 / 焦点边框 / code / 主要强调（全页唯一主强调色） |
| `--success` | `#4ade80` | 存活 / ok / 绿色 badge |
| `--warning` | `#fbbf24` | 429 翻译 / 黄色 badge |
| `--danger` | `#f87171` | 离线 / 错误 / 红色 badge |
| `--violet` | `#c084fc` | 协议转换 / 紫色 badge（仅 badge 语义） |

> **色彩一致性锁**：全页主强调色锁定 `--accent`（蓝色系）。绿/黄/红/紫只作为**状态语义**出现（badge、进度条、状态点），不参与装饰。

### 卡片端口强调条（左/顶边 3px，来自现有 accent-{port} 模式）
| 端口 | 值 |
|---|---|
| 8082 (copilot) | `#3b82f6` 蓝 |
| 8090 (openrouter) | `#f59e0b` 橙 |
| 8091 (nvidia) | `#4ade80` 绿 |
| 8084 (codebuddy) | `#a78bfa` 紫 |
| 其余 | `--border` 默认 |

## 3. 字体 Token

- **主字体**：`-apple-system, "Segoe UI", ui-monospace, sans-serif`（内部工具，跟随系统，不引入 web font）
- **等宽字体**：`ui-monospace, monospace`（模型 ID、端口号、所有数字）
- **数值排版**：`font-variant-numeric: tabular-nums`（表格数字对齐）
- **字号阶梯**：
  - 页面标题 `20px/600`；区块标题 `14px/600 + 1px letter-spacing + uppercase`
  - 卡片名 `16px/600`；卡片注释 `13px/400` 弱化
  - 正文/kv `13px`；描述 `12.5px` 弱化；badge/标签 `11px/600`
  - 统计大数字 `24px/700` 等宽

## 4. 间距 / 圆角 / 阴影

- **页面内边距**：`32px`（桌面）→ `16px`（手机）
- **卡片内边距**：`18px 22px` → 手机 `14px 16px`
- **卡片间距**：`14px`（gap）
- **圆角（单一规则，锁定）**：容器/卡片 `10px`；内嵌小件（badge/输入/按钮/进度条）`6px`；进度条段 `4px`。禁止混用其他圆角体系。
- **阴影**：hover 时 `0 4px 20px rgba(0,0,0,0.3)`（偏黑、低透明度、带背景色倾向——不做纯黑高透明度）；常态无阴影。
- **hover 位移**：卡片 `translateY(-2px)`；按钮 `translateY(-1px)`；active `scale(0.98)`。
- **过渡**：`transition: 0.2s`（交互元素）；手风琴展开 `0.25s ease`（仅 height/opacity/transform）。

## 5. 组件规范

### 5.1 手风琴卡片（核心交互，本次重构新增）
- **行为**：任一时刻**只展开一个**卡片详情（手风琴互斥）；点击卡片头切换展开/收起
- **结构**：`卡片头（始终可见，可点击）` + `详情区（展开时显示）`
- **卡片头内容**：端口强调条 + 供应商名 + 端口号 + 分类 badge + 状态 badge + 摘要数据（请求数/存活点）+ 展开箭头（▼/▶，随状态旋转）
- **详情区内容**（展开时）：
  1. kv 元信息（分类/handler/上游/isFree/enabled）
  2. 流量统计块（总请求/成功率/运行时长 + 进度条 + ok/429/err 明细）
  3. 模型白名单表格（可编辑：每行删除按钮 + 底部添加输入框）
  4. token 编辑块（状态 + 输入 + 保存 + 重新破解）
- **移动端**：卡片全宽单列；卡片头不换行（供应商名可截断），badge 可换行

### 5.2 模型白名单表格（可编辑，本次新增）
- **展示**：`#` + 模型 ID（等宽）+ 美化名 + 状态统计（有数据时）+ 操作（删除按钮）
- **编辑**：每行末删除按钮（×）；表格底部一行"添加模型"输入框 + 添加按钮
- **保存**：按 target 独立保存（PUT `/api/targets/{label}`），成功后热生效
- **空状态**：`(暂无模型数据，在下方添加)` + 添加输入框仍可用
- **约束**：添加时去空白、去重（已存在则提示）；允许任意字符串（不拦截请求，仅白名单展示/编辑）

### 5.3 Token 编辑块（保留现有能力）
- 状态行：`✅ 已配置 <masked>` / `⚠️ 缺失`
- 输入框（password 类型）+ 保存按钮 + （crack 类）重新破解按钮
- 已配置时输入框显示 `******`，placeholder 提示"已配置，输入新值覆盖"
- 保存成功 → 按钮变绿"✅ 已保存" → 2s 后复原
- **错误提示**：禁止 `alert()`，用行内消息区（`admin-msg` 或卡片内状态行）

### 5.3a 破解按钮环境检测（新增）
- crack 类 provider 的"重新破解"按钮必须根据当前运行环境判断破解工具是否可用：
  - **copilot**：需要 `gh` CLI（GitHub CLI）在 PATH 中 → `shutil.which("gh")`
  - **codebuddy**：需要 CodeBuddy 客户端目录存在（Windows `%LOCALAPPDATA%`/`%APPDATA%`/home 下探测）
  - **qclaw**：需要 QClaw 客户端（Windows `%APPDATA%\QClaw\app-store.json`）或环境变量 `QCLAW_API_KEY`
  - **trae-work**：预留骨架，未实现 → 永远不可用
  - trae-work 是 `enabled=false` 的预留 provider，卡片不渲染监听端口；crackEnv 检测同样返回不可用
- **OS 支持**：codebuddy/qclaw 仅 Windows 本地破解；非 Windows 下 `_crack_env_check` 返回不可用，提示"仅支持 Windows，待后续补齐"
- **不可用状态**：按钮置灰（`disabled` + `.te-recrack:disabled` 样式），`title` 属性显示不可用原因（如"未检测到 gh CLI，无法自动破解"）
- **可用状态**：正常可点击
- 检测结果由后端 `/api/targets` 返回（`crackEnv: {available: bool, reason: str}`），前端渲染时直接使用，不做运行时二次检测

### 5.3b 模型展示开关（新增）
- 模型编辑通过**弹出 modal 编辑界面**完成（非内联编辑态）：卡片内「✏️ 编辑模型」→ 打开 overlay modal → fetch `/api/targets/{label}/models?edit=1` 渲染全部模型列表
- modal 内每行：模型 ID + 美化名 + **iOS 风格滑动开关**（`label.switch` + 隐藏 checkbox + `.switch-slider`）；**无删除按钮**（用户明确要求不要删除按钮）
- 开关样式：绿色渐变轨道（开）+ 灰色渐变轨道（关）+ 白色渐变圆滑块 + 弹簧动画（`cubic-bezier(0.34,1.56,0.64,1)`）+ 阴影
- modal 结构：head（标题+关闭）× body（模型行列表，可滚动）× foot（消息 + 取消 + 保存）
- 保存：收集 modal 内所有开关状态 → PUT `/api/targets/{label}` 提交完整 models（含 enabled 标志）→ 热生效
- 正常态卡片：只展示启用模型表格（无删除按钮），表格下方「✏️ 编辑模型」按钮 + 添加输入框
- models 支持三种形式：字符串（默认启用）、`{"id": "x", "enabled": true}`、dict 列表
- modal 关闭：右上 × / 取消按钮 / 点击遮罩；遮罩 `backdrop-filter: blur(3px)`

### 5.4 总览栏
- 内容：共 N 个服务 · 累计请求 · 存活端口 x/y · 状态点串（绿=在线/红=离线，title 提示端口）· **重载配置按钮** · 刷新按钮
- **重载按钮合并到总览栏**（原底部 admin-panel 的重载能力上移，底部 admin-panel 移除）
- 手机端：`flex-wrap` 换行，分隔线隐藏

### 5.5 管理面板（已合并到总览栏）
- 底部 sticky admin-panel **移除**：手动重载按钮合并到顶部总览栏；isFree 编辑能力并入卡片内（isFree kv 行旁加开关），不再需要独立管理表
- `admin-msg` 操作消息区保留（可放总览栏下方或卡片内）

## 6. 响应式规则

- **断点**：`--bp-narrow: 768px`（手机/平板竖屏）、`--bp-wide: 1200px`（桌面）
- **卡片布局**：**单列纵向排列**（`flex-direction: column`，不做自适应多列 grid）——卡片宽度恒等于容器宽度，保证模型表格/编辑区宽度充裕；三档视口行为一致
- **表格**：手机端模型表格保持全宽（卡片全宽所以表格宽度有保障），列不强制压缩；长模型 ID 用 `overflow-wrap: anywhere`
- **总览栏 / 管理表**：手机端允许横向滚动容器
- **viewport**：必须 `<meta name="viewport" content="width=device-width, initial-scale=1">`
- **禁止**：任何 `h-screen`/`100vh` 全屏容器（本页无全屏场景）；卡片多列自适应布局

## 7. 可达性与状态

- **键盘可达**：卡片头为 `<button>` 或带 `tabindex` 的可聚焦元素，Enter/Space 可开合；焦点环 `outline: 2px solid var(--accent)` 可见
- **对比度**：正文 ≥ 4.5:1（`#9ca3af` on `#0f1117` 达标）；弱化文本 `#6b7280` 仅用于非关键标签
- **状态反馈**：hover（边框亮起 + 微位移）、active（scale 0.98）、focus（accent 环）
- **动效降级**：`@media (prefers-reduced-motion: reduce)` 关闭所有过渡/动画
- **错误状态**：行内消息，禁止 `window.alert()`

## 8. 已接受的债务

- **无浅色模式**：内部工具，固定深色（用户无浅色需求）
- **无字体加载**：跟随系统字体，不引入 webfont（内网环境，避免外网依赖）
- **无图标库**：使用内联 SVG/Unicode 符号（内嵌单页，不引 CDN）；状态点用 CSS 圆点
- **服务端渲染**：非 SPA，交互为原生 JS 增强；热刷新为整页手动刷新（无自动轮询，避免运维面板误刷新表单输入）

## 9. 验收标准

- 375px / 768px / 1280px 三档无横向溢出
- 手风琴互斥：展开 A 自动收起 B
- 模型白名单增删保存后 targets.json 更新且热生效
- free/paid 类 token：客户端带 Authorization 优先，未带时用 dashboard 维护的 secrets.json
- `prefers-reduced-motion` 下无动画
- 现有测试（test_dashboard.py 等）全部通过
