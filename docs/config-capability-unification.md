# 配置能力统一架构设计（P1/P2 待实施）

> **状态**：P0 ✅ (588e83b) · **P1 ✅ 已实施**（见下方实施记录）· P2 待实施。
> **日期**：2026-08-05 · **设计**：Oracle 架构评审 · **记录**：主会话
> 本文档是 P1/P2 阶段的实施蓝图，接手的会话以此为准。

---

## 1. 背景与问题

claude-code-proxy（FastAPI 多端口 LLM 代理，server.py 单文件 ~10500 行内嵌 dashboard）
有三个配置编辑入口，表面是三个 UI，实质是**三种数据抽象**：

| 入口 | 读写数据 | 数据位置 | 来源约束 |
|---|---|---|---|
| agg-modal | `virtualModels`（dict） | 聚合 target 内部字段 | 池成员 port+model 引用其他 target 的 models |
| models-modal | `models[]`（list） | targets.json 顶层 | target.port+model 引用任意端口（含 agg:xxx） |
| model-modal | `target.models[]`（白名单） | 每个 target 自己 | 纯本地开关，不引用他者 |

三者**没有公共模型实体**。一个"模型"在系统里是三个不同东西：
- 聚合视角：虚拟模型 id（`agg:sonnet`）= **路由规则的 key**
- 8081 视角：全局模型定义（`name/aliases → target`）= **别名 → 路由映射**
- target 视角：白名单条目（id+enabled）= **该端口能透传什么的 ACL**

### 根本症结（按重要度）

1. **数据模型割裂（首要）**：三处用三种结构描述"可被请求的模型"，改动一处不反映另一处。
2. **渲染管线割裂（次要但代价高）**：三套独立 HTML 拼装函数（`buildModelsEditorHtml` / `buildAggConfigHtml` / `_model_details_html`），交互词汇不统一（`mm-add-btn` / `mrow-add-btn` / `agg-add-row` 并存），**直接导致 Bug 1**（`querySelector('.mm-add-btn')` 撞名取错锚点，已修复于 588e83b）。
3. **校验双写（潜伏债）**：前端 JS 手写校验 + 后端 `validate_targets` 再写一遍，规则漂移。
4. **数据源不统一（监控混乱之源）**：8081 卡片 = `_anthropic_port_models()` 动态生成；聚合卡片 = `AggregatorEngine.get_stats()`；target 卡片 = `t["models"]`。同一屏三个"模型"区块语义不同。
5. **模型列表来源碎片化**：copilot/free/paid 有标准 `/models`；qclaw 本地返回；trae-work 走私有 `get_detail_param`；codebuddy 无通道——"模型列表"四种实现。

---

## 2. 目标架构（设计原则：只整合能整合的，承认不能整合的）

三个视图本质管理不同维度（路由/别名/ACL），**不合并为一个超级编辑器**。正确方向：
**一个数据源（targets.json）+ 多个专门视图 + 共享渲染与校验管线**。

### 2.1 数据层：保留 targets.json 单一事实源，引入"模型引用图"概念

不引入新存储，targets.json 结构不变。改动在**读取侧**——内存内统一视图 `ModelRegistry`
（server.py 启动 + 热重载时重建）：

```
ModelRegistry
├── targets:     { label → { port, handler, models[], enabled } }        原样
├── globalModels: [ { name, aliases[], target:{port,model} } ]           顶层 models[]
├── virtualModels:{ vmId → { defaultPool[], fallbackPool[], ... } }       聚合 target.virtualModels
└── 派生索引（只读，不持久化）
    ├── byPort:    { port → 该端口可被请求的真实模型名集合 }
    ├── dangling:  [ 引用了不存在目标的条目（改名后悬空） ]
    └── capabilities: { label → { canListModels, canPrune, modelsSource } }
```

- `capabilities` 收敛"模型列表来源碎片化"：抽象为**单一接口**
  `modelsSource ∈ upstream | local | private | none`，从 targets.json 的 `hasModels` + `handler` 派生，
  经 `/api/targets` 一并返回，前端不再各自判断。

**统一模型获取接口**（服务端）：
```
async def _get_target_models(label) -> { models, source, error }
   source = upstream  → GET /models（copilot/free/paid，httpx 直取）
   source = local     → 返回 t.models[] 配置（qclaw）
   source = private   → 走 trae-work 的 get_detail_param（TTL 缓存）
   source = none      → 返回空 + 前端降级提示（codebuddy）
```
`/api/targets/{label}/models` 与 `can_prune` 判定都从这个接口取，删掉各处独立获取逻辑。

### 2.2 渲染层：不合并 modal，但共享"行渲染 + 校验 + 消息"基础设施

不引入前端框架。抽出 4 个共享 JS 函数，三个 modal 复用：

| 共享函数 | 职责 | 替代现状 |
|---|---|---|
| `mmRow({fields, onDelete})` | 渲染一行可编辑记录 | 三处各自 HTML 拼接 |
| `mmMsg(el, kind, text)` | 统一成功/警告/错误提示 | 各自 `msg.textContent=...; msg.className=...` |
| `mmValidate(rules, values)` | 同步执行规则集合并返回首个错误 | 三处内联 if 检查 |
| `mmInsertRow(section, rowHtml)` | **明确语义的插入**：永远插在 section 末尾"添加按钮行"之前 | Bug 1 的 querySelector 撞名 |

约定：每个 modal 的 section 容器结构固定为 `...rows + <div class="mm-add-row"><button class="mm-add-btn">`，
`mmInsertRow` 只找 `section > .mm-add-row`，**按钮类名与插入锚点解耦**（P0 已为 agg-vm-add 示范）。

### 2.3 校验层：后端 Pydantic 是唯一事实源，前端只做"镜像提示"

- 规则集中在 `config_store.validate_targets`（已是），新增规则一律加在这里
- 前端校验改"声明式规则描述"：后端 `/api/config/schema` 返回 JSON 校验规则，前端 `mmValidate` 通用执行
- **错误位置回显**：后端 422 返回 `{"errors": [{"path": "virtualModels.agg:x.defaultPool[0].port", "msg": "..."}]}`，
  前端按 path 高亮对应行（当前 detail 是字符串数组，用户看不到哪一行错）
- 代价可控：schema 是静态 JSON 描述，不需要 JSON Schema 库

### 2.4 交互层：减混淆靠"明示视图边界" + 改动反馈，不靠合并入口

1. **每个 modal 顶部加固定"作用域提示"**（部分已存在，强化）：
   - agg-modal：「本页配置仅影响 8080 聚合路由，不改变 8081 模型列表」
   - models-modal：「本页定义 8081 模型别名 → 下游，新增项保存后立即出现在 8081 卡片」
   - model-modal：「本页仅控制 {label} 端口透传白名单，不影响其他端口」
2. **保存成功后的"生效位置提示"**：保存 models[] 成功时 msg 追加「→ 已在 8081 卡片显示 N 个模型」；
   保存聚合配置成功时追加「→ 聚合路由已热重载」。
3. **改动后自动局部刷新**：保存成功后不整页刷新，局部重拉受影响卡片数据替换 DOM
   （当前保存后只关 modal，用户要手动刷新才看到——"我改了但看不到"的主因）。
4. **悬空引用可视化**：`ModelRegistry.dangling` 在 dashboard 顶部以全局警示条展示
   （如「models[2].target 指向 agg:sonnet，但该虚拟模型不存在」）。

---

## 3. Bug 2 排查清单（"8081 保存后监控不显示"——未复现，待用户补充信息）

实测 API 保存 4 个模型 8081 卡片正常显示，代码主链路无问题。可能根因按概率：

1. **浏览器缓存**（最常见）：dashboard 是无缓存头服务端渲染，但用户可能开了强缓存插件。让用户硬刷 Ctrl+F5。
2. **热重载时序**：mtime 轮询 2s，保存后 `tail -f proxy.log` 看 `config reloaded`；`curl /api/models` 确认后端已更新。
3. **`saveModelsEditor` 静默跳过**：`if (!n && !a && !p && !m) return;` 只跳全空行；
   corner case：用户点了「+ 添加模型」新增空行没填就保存 → 空行被跳过，其余正常保存，
   用户以为"新增那行没保存"（UX 问题：应在保存前提示"有 N 行未填写"）。
4. **多 tab/多窗口**：tab A/B 同时打开同一 modal 编辑，后者保存覆盖前者。无版本/etag 保护。
5. **看错卡片**：models[] 只影响 8081 卡片；看其他 target 卡片模型区是白名单，预期不同。

**无条件建议做的修复**（无论根因）：
- 保存成功 msg 显示实际保存条目数：`✅ 已保存 4 个模型定义`
- 8081 卡片 KV 已有「模型数量」显示，用户能立刻对数字

---

## 4. 分阶段实施建议

### P0 ✅ 已完成（commit 588e83b）
Bug 1 修复：`agg-vm-add` 专属类名 + 锚点。核查 addAggPoolMember/addModelsRow/addModelRow 无同类隐患。

### P1：能力统一（Short，1-4h）✅ 已实施
**目标**：减混淆 + "改动=生效"可感知，不动数据结构。

实际交付：
1. 三个 modal 顶部作用域提示文案：`buildModelsEditorHtml` / `buildAggConfigHtml` / `_model_details_html(edit_mode)` 各加 `mm-scope` 条
2. 保存成功 msg 追加条目数 + 生效位置：三个 save 函数统一改 `mmMsg(msg, 'ok', ...)` 并写明「已保存 N 个 X → Y 卡片已更新」
3. 保存成功后局部刷新：新 `refreshCardDom(port)` 重拉 dashboard HTML、替换目标卡片 DOM、保留展开态，**不再 `location.reload()`**（仅 prune/recrack/doReload/credentials 仍用全页）
4. 悬空引用警示条：后端 `GET /api/config/dangling` 只读扫描 `models[].target` 与 `virtualModels.{vm}.{defaultPool|fallbackPool}[]` 的端口存在性与模型白名单匹配；dashboard 顶部 `<div id="dangling-bar">` 由 `loadDanglingBar()` 在初始化与每次保存后拉取渲染
5. 共享 `mmMsg` / `mmInsertRow`（+ 嵌套归属守卫 `mmOwnsNode` / `mmScope`）四个 JS 函数：`addModelsRow` / `addAggPoolMember` / `addAggVm` / `addModelRow` 全部走 `mmInsertRow`，三个 save 全部走 `mmMsg`
6. 顺手重构：手风琴 IIFE → 具名 `bindCardAccordion`（可重绑，支撑局部刷新）、卡片 `data-port` 属性（refreshCardDom 锚点）

**实测验证**（Playwright）：
- 打开三个 modal → 顶部均显示作用域文案（包含 + 不影响双段）
- `addModelsRow` / `addAggPoolMember` / `addAggVm` / `addModelRow` 各自行均插入到正确锚点之前，**Bug 1 嵌套撞名不再发生**（`mmOwnsNode` 双重保险）
- 在 `models[]` 新增 `__p1_probe` 并保存 → msg 显示「✅ 已保存 4 个模型定义 → 已在 8081 卡片显示」、卡片 KV「模型数量 3 → 4」、表格 4 行、`body.dataset.p1probe` 标记存活（**证明无整页刷新**）
- 改 `__p1_probe.target.port = 9999` 后 `loadDanglingBar()` 立即渲染黄色警示条，`<code>` 标签内显示路径 `models[3].target`
- 还原配置后警示条自动隐藏

**测试**：`test_targets_schema.py` 30/30 ✅；`test_dashboard.py` 27/30 ✅（3 项失败 `test_edit_modal_fallback_no_key` / `test_gemini_native_badge` / `test_model_whitelist_editor` 为 main 基线预存在，与 P1 无关）

**风险**：低。改动集中 dashboard 渲染与 JS，不改配置读写路径。

### P2：架构收敛（Medium，1-2d）
**目标**：校验与模型来源的单一事实源。**做之前先确认 P1 已稳定**。
1. **ModelRegistry 内存索引**：热重载时构建 byPort/dangling/capabilities，dashboard 渲染改读 Registry（targets.json 结构不变）
2. **`_get_target_models(label)` 统一接口**：四种 modelsSource 收敛，can_prune 与 `/api/targets/{label}/models` 共用
3. **校验规则 JSON 描述**：`/api/config/schema` 声明式规则 + 前端 `mmValidate` 通用执行（可选，单人维护项目收益需重新评估）
4. **错误 path 回显**：validate_targets 返回结构化 `{"path","msg"}`，前端按 path 高亮

**风险**：ModelRegistry 涉及 dashboard 渲染主路径，需 test_dashboard.py 全覆盖后再动。
**验证**：全部现有测试 + 新增 `test_model_registry.py`（悬空检测/capabilities 派生/统一接口四 source 行为）。

---

## 5. 明确不做的事（过度设计，排除）

1. 不引入 React/Vue/任何前端框架（内网单文件约束）
2. 不拆分 targets.json 成多文件（单一事实源价值 > 按关注点分文件）
3. 不合并三个 modal 成超级编辑器（管理不同维度，合并更糟）
4. 不做自动改名联动（用户已明确：引用方留手动，悬空警示条 + 手动处理更诚实）
5. 不引入 JSON Schema 库（validate_targets 手写校验已足够）
6. 不实现撤销/版本历史（targets.json 用 git 管）
7. 不动 AggregatorEngine（引擎与配置编辑 UI 正交，零改动）
8. P2.3（校验规则 JSON 描述）标记可选：除非 P1 后发现规则漂移事故，否则不做

---

## 附：关键文件与行号速查

| 主题 | 位置 |
|---|---|
| Bug 1 现场（已修复） | `server.py` addAggVm（锚点已改 `.agg-vm-add`） |
| addAggPoolMember 正确锚点示范 | `server.py`（用 `.agg-add-row`） |
| 模型定义保存 | `server.py` saveModelsEditor；后端 `api_update_models` |
| 聚合配置保存 | `server.py` saveAggConfig；后端 `api_update_aggregate_config` |
| 8081 卡片模型数据源 | `server.py` `_anthropic_port_models()`（读 `_MODELS_CFG`） |
| 聚合可用端口 | `server.py` `api_get_aggregate_config` |
| can_prune 判定 | `server.py` dashboard 卡片循环（`hasModels` 字段 + `handler=="copilot"` 兜底） |
| 校验 | `config_store.py` validate_targets 家族 |
| 热重载 | `server.py` `_config_watcher`（mtime 轮询 2s） |

## 附：本会话相关提交

- `588e83b` fix(dashboard): 8080 聚合编辑器「+ 新增虚拟模型」按钮锚点撞名修复（P0）
- `9d5ae15` feat(dashboard): 清理过期模型按钮扩展到直连网关 + 直连 token 保存修复
- `5441ddc` fix(dashboard): 聚合网关 8080 纳入 availablePorts
- `fd2c0bd` refactor(dashboard): 移除 8081 硬编码模型死名单
- `cd1c9f4` feat(dashboard): 三个编辑 modal 统一升级 agg-* 视觉体系 + crack x-api-key 修复
- **P1（本次）** feat(dashboard): 配置能力统一 P1 — 作用域提示 / mmMsg+mmInsertRow 共享函数 / 保存后局部刷新(无整页刷新) / 悬空引用警示条(/api/config/dangling)
