# claude-code-proxy 横向扩展多端口架构设计

- 日期：2026-07-31
- 状态：已批准（用户确认方案 A，并批准整体设计）
- 涉及文件：`server.py`（~3100 行单文件）、`targets.json`、`.env`、新增 `secrets.json`、新增 `crack_*.py` 工具

---

## 1. 背景与目标

当前代理是"单主端口 + 配置文件切换"模式：

- `8081`：Anthropic 协议入口（Claude Code 连接） + dashboard 管理页
- `8082`：OpenAI 协议 TCP，通过 `PREFERRED_PROVIDER` 环境变量在 qclaw/copilot/openai 之间动态切换
- `8090`/`8091`：vendor 透明代理（openrouter / nvidia），由 `targets.json` 驱动
- `8084`：codebuddy（已列入 targets.json，带 `apikeyEnv: CODEBUDDY_TOKEN`）

**目标**：改为**横向扩展模式**——每个端口固定对应一个供应商，按"是否需要破解"分类，管理界面可维护 key/token/isFree 标记。

### 核心设计决策（用户已确认）

| 决策点 | 结论 |
|---|---|
| 方案选型 | **方案 A**：除 8081 外所有端口本质都是 OpenAI 协议透传，仅 header 补充不同；破解逻辑从 server.py 拆出为独立工具 |
| 8085 claw | claw = QClaw，qclaw 配置（模型映射/reasoning）迁到 8085 target 条目 |
| 端口规划 | 破解类用 808x 段，免费代理用 809x 段 |
| 管理界面 | dashboard 加表单，配置落 JSON 文件，**热生效**无需重启 |
| isFree 字段 | 本次只存储 + 接口暴露，重试策略后续再定（预留） |
| 破解 token 存储 | 私密 key/token 落 JSON（热更新），非私密静态配置留 .env/targets.json |
| 破解工具形态 | 每供应商一个独立脚本（可单独 CLI 运行），server.py 启动时自动尝试调用，失败不阻塞 |
| 8081 角色 | **本次不动**；未来做聚合接口（类似 openrouter/free），8081 只负责协议翻译 |

---

## 2. 端口与分类规划

| 端口 | 供应商 | 分类 | 状态 |
|---|---|---|---|
| 8081 | dashboard + Anthropic 入口 | — | 保留，本次不改逻辑（仅增加转发目标配置字段） |
| 8082 | copilot | `crack`（破解·质量高） | 改为固定 copilot，不再读 PREFERRED_PROVIDER |
| 8084 | codebuddy | `crack` | 保留 |
| 8085 | claw（QClaw） | `crack` | 新增，迁入 qclaw 配置 |
| 8086 | trae-work | `crack` | 预留（`enabled: false`） |
| 8090 | openrouter | `free` | 保留 |
| 8091 | nvidia | `free` | 保留 |
| 8092 | gemini-openai | `free` | 新增 |
| 8093 | opencode-zen | `free` | 新增（`opencode.ai/zen/v1`） |
| 8094 | open-go | `paid` | 新增（`opencode.ai/zen/go/v1`） |
| 8095-8099 | — | — | 预留 |

分类标签语义：
- `crack`：需要破解/提取 token 才能用的高质量供应商
- `free`：不需要破解、免费（质量较低，限流频繁）——`isFree` 默认 true
- `paid`：不需要破解、收费（key 由用户购买）——`isFree` 默认 false

---

## 3. 架构：统一透传引擎

```
Claude Code / OpenAI 客户端
        │
        ▼
┌─────────────────────────────────────────────────┐
│  8081 Anthropic 入口 + dashboard（保持不变）      │
│  └─ 协议翻译 ──► 转发到可配置目标端口（默认 8082） │
└─────────────────────────────────────────────────┘
        ▲
        │ 内网调用（未来聚合接口从这里扩展）
        │
┌───────┴─────────────────────────────────────────┐
│  统一透传引擎（单 handler，targets.json 驱动）     │
│                                                  │
│  8082 copilot   (crack)   ──► GHE/官方 Copilot   │
│  8084 codebuddy (crack)   ──► copilot.tencent.com│
│  8085 qclaw     (crack)   ──► mmgrcalltoken.3g.qq.com
│  8086 trae-work (crack)   ──► (预留)              │
│  8090 openrouter(free)    ──► openrouter.ai      │
│  8091 nvidia    (free)    ──► integrate.api.nvidia.com
│  8092 gemini-openai (free)──► generativelanguage.googleapis.com
│  8093 opencode-zen  (free)──► opencode.ai/zen/v1
│  8094 open-go     (paid)  ──► opencode.ai/zen/go/v1
│                                                  │
│  每个端口 = 一个 target 条目，共享：              │
│  ├─ HTTP 解析 / 响应回写（现有 _parse_http_request 等）│
│  ├─ 鉴权：crack 注入 secrets.json 里的 token；     │
│  │         free/paid 透传客户端 Authorization     │
│  ├─ 429 翻译 + 5xx 重试 3 次（现有逻辑保留）       │
│  └─ 统计（现有 _VENDOR_STATS 结构）               │
└─────────────────────────────────────────────────┘
```

### 核心原则

1. **8081 之外所有端口 = OpenAI 透传**。差异只在 header 注入和可选的 body 清理，由 target 的 `handler` 字段选择（`passthrough` / `copilot` / `qclaw`）。破解逻辑本身不在 handler 里——由独立工具预先提取 token 写入 secrets.json。
2. **移除 `PREFERRED_PROVIDER` 机制**。8082 不再动态切换；端口与供应商一一绑定。qclaw 的 BIG/MEDIUM/SMALL_MODEL + reasoning 配置迁移为 target 条目字段。
3. **启动流程**：加载 targets.json → 对 crack 类 target 调用对应破解工具（自动提取 token 写 secrets.json；失败则等用户手工填）→ 为每个 enabled target 启动一个 asyncio TCP server。
4. **热生效**：targets.json / secrets.json 变更（dashboard 表单或文件修改）→ mtime 轮询（2s）→ 重载配置；端口新增/移除动态生效，无需重启。

---

## 4. 配置模型

### 4.1 `targets.json`（非私密配置，现有文件扩展字段）

```json
{
  "anthropicForwardPort": 8082,
  "targets": [
    {
      "label": "copilot",
      "listenPort": 8082,
      "category": "crack",
      "handler": "copilot",
      "targetHost": "copilot-proxy.githubusercontent.com",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "",
      "crackTool": "crack_copilot.py",
      "secretRef": "copilot_token",
      "models": ["claude-opus-4.8", "claude-sonnet-5", "claude-haiku-4.5", "gpt-5.5"],
      "extraHeaders": { "Copilot-Integration-Id": "copilot-developer-cli" }
    },
    {
      "label": "codebuddy",
      "listenPort": 8084,
      "category": "crack",
      "handler": "passthrough",
      "targetHost": "copilot.tencent.com",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/v2",
      "crackTool": "crack_codebuddy.py",
      "secretRef": "codebuddy_token",
      "models": ["glm-5.2", "kimi-k2.7", "deepseek-v4-pro"]
    },
    {
      "label": "qclaw",
      "listenPort": 8085,
      "category": "crack",
      "handler": "qclaw",
      "targetHost": "mmgrcalltoken.3g.qq.com",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/aizone/v1",
      "crackTool": "crack_qclaw.py",
      "secretRef": "qclaw_api_key",
      "models": ["pool-deepseek-v4-pro", "pool-deepseek-v4-flash", "pool-glm-5.2", "pool-kimi-k2.7-code-highspeed"],
      "modelMapping": { "opus": "pool-deepseek-v4-pro", "sonnet": "pool-deepseek-v4-pro", "haiku": "pool-deepseek-v4-flash" },
      "reasoning": { "big": "high", "medium": "low", "small": "low" }
    },
    {
      "label": "trae-work",
      "listenPort": 8086,
      "category": "crack",
      "handler": "passthrough",
      "targetHost": "",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "",
      "crackTool": "",
      "secretRef": "trae_work_token",
      "models": [],
      "enabled": false
    },
    {
      "label": "openrouter",
      "listenPort": 8090,
      "category": "free",
      "handler": "passthrough",
      "isFree": true,
      "targetHost": "openrouter.ai",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/api/v1",
      "models": ["nvidia/nemotron-3-ultra-550b-a55b:free", "openai/gpt-oss-20b:free"]
    },
    {
      "label": "nvidia",
      "listenPort": 8091,
      "category": "free",
      "handler": "passthrough",
      "isFree": true,
      "targetHost": "integrate.api.nvidia.com",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/v1",
      "models": ["deepseek-ai/deepseek-v4-flash", "qwen/qwen3-coder-480b-a35b-instruct"]
    },
    {
      "label": "gemini-openai",
      "listenPort": 8092,
      "category": "free",
      "handler": "passthrough",
      "isFree": true,
      "targetHost": "generativelanguage.googleapis.com",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/v1beta/openai",
      "models": ["gemini-2.5-pro", "gemini-2.5-flash"]
    },
    {
      "label": "opencode-zen",
      "listenPort": 8093,
      "category": "free",
      "handler": "passthrough",
      "isFree": true,
      "targetHost": "opencode.ai",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/zen/v1",
      "models": ["deepseek-v4-flash-free", "mimo-v2.5-free", "big-pickle"]
    },
    {
      "label": "open-go",
      "listenPort": 8094,
      "category": "paid",
      "handler": "passthrough",
      "isFree": false,
      "targetHost": "opencode.ai",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/zen/go/v1",
      "models": ["deepseek-v4-pro", "kimi-k2.7"]
    }
  ]
}
```

**schema 变更对比（现有 → 新）**：

| 字段 | 现有 | 新增/变更 |
|---|---|---|
| 顶层结构 | 数组 | **对象**（`anthropicForwardPort` + `targets` 数组） |
| `category` | — | 新增：`crack` / `free` / `paid` |
| `isFree` | — | 新增：仅 free/paid 使用，用户可维护，重试策略预留 |
| `handler` | — | 新增：`passthrough`（默认）/ `copilot` / `qclaw` |
| `crackTool` | — | 新增：破解工具脚本名 |
| `secretRef` | — | 新增：secrets.json 中的键名 |
| `enabled` | — | 新增：false 表示端口暂不监听（trae-work 预留） |
| `modelMapping` | — | 新增：qclaw 特有，haiku/sonnet/opus → 模型 |
| `reasoning` | — | 新增：qclaw 特有，big/medium/small → high/low |
| `apikeyEnv` | 有 | **保留为 fallback**：优先 secrets.json，其次环境变量 |
| `extraHeaders` | — | 新增：额外 header（copilot 的 Copilot-Integration-Id） |

### 4.2 `secrets.json`（新增，gitignore，私密 key/token）

```json
{
  "copilot_token": "github_pat_xxx",
  "codebuddy_token": "xxx",
  "qclaw_api_key": "sk-xxx",
  "trae_work_token": ""
}
```

- dashboard 表单写这里，mtime 轮询 → 热生效，无需重启
- **优先级**：`secrets.json` > 环境变量（`apikeyEnv` fallback）> 客户端透传
- 需加入 `.gitignore`

### 4.3 `.env` 保留项

非私密静态配置继续放 .env（`QCLAW_BASE_URL`、`DEBUG`、日志相关、`VENDOR_RETRY_AFTER_SECONDS` 等）。PREFERRED_PROVIDER/BIG_MODEL/MEDIUM_MODEL/SMALL_MODEL/COPILOT_*_TOKEN 等**废弃或降级为 fallback**。

---

## 5. 破解工具（独立脚本）

每供应商一个独立脚本，职责单一：**提取 token → 写入 secrets.json**。server.py 启动时自动调用，也可 CLI 独立运行（在别的机器跑完把 token 填回来）。

| 脚本 | 职责 | 提取来源 |
|---|---|---|
| `crack_copilot.py` | 提取 Copilot GHE token | 本机 GitHub CLI（`gh auth token`）/ Copilot 客户端安装目录 |
| `crack_codebuddy.py` | 提取 CodeBuddy token | 本机 CodeBuddy 客户端安装目录/配置 |
| `crack_qclaw.py` | 提取 QClaw API Key | 复用现有 DPAPI 解密逻辑（`%APPDATA%\QClaw\app-store.json` + `Local State`） |
| `crack_traework.py` | 提取 Trae Work token | 预留（未来实现） |

### 统一 CLI 接口

```
用法: python crack_<vendor>.py [--secrets secrets.json] [--force]

行为:
  1. 尝试本地提取（找安装目录 / gh CLI / DPAPI）
  2. 成功 → 写入 secrets.json 对应键（secretRef），打印 ✅ token 已更新，退出码 0
  3. 失败 → 打印 ❌ 无法本地提取 + 引导文案（提示用户在其他机器运行本脚本，
            或手工获取 token 后到 dashboard 填写），退出码非 0
  4. --force 时即使已有 key 也重新提取
```

### server.py 启动流程改造

```
启动
 ├─ 加载 targets.json
 ├─ 加载 secrets.json
 ├─ 对每个 category=crack 的 target：
 │    ├─ secrets.json 已有对应 key？→ 跳过破解工具，直接用
 │    └─ 无 key → 调用 crackTool 脚本尝试提取（超时 30s）
 │         ├─ 成功 → 写入 secrets.json
 │         └─ 失败 → 不阻塞启动，日志警告 + dashboard 卡片标红提示"缺 token"
 ├─ 对每个 enabled target → 启动 asyncio TCP server
 └─ 启动 mtime 轮询（targets.json / secrets.json → 热重载）
```

**关键约束**：
- 破解工具不阻塞启动——提取失败只警告，端口照常监听；请求时缺 token 返回 401 提示去 dashboard 补
- 已有 key 时不重复调破解工具（避免启动慢）；dashboard 提供"重新提取"按钮手动触发
- 破解工具是纯独立脚本，不 import server.py（避免循环依赖），只依赖标准库 + json + os

---

## 6. 管理界面（dashboard 增强）

### 6.1 REST API（8081 FastAPI 新增）

| 方法/路径 | 功能 |
|---|---|
| `GET /api/targets` | 返回 targets 列表 + secrets 元信息（key 打码 `sk-***`） |
| `PUT /api/targets/{label}` | 更新非私密字段（category/isFree/models/targetHost/routePrefix/handler 等） |
| `PUT /api/secrets/{label}` | 更新私密 key/token（写 secrets.json） |
| `POST /api/targets/{label}/recrack` | 触发破解工具重新提取 |
| `POST /api/reload` | 手动触发热重载 |

### 6.2 dashboard 页面

现有只读页面升级为可写管理界面：

| 能力 | 说明 |
|---|---|
| **端口卡片** | 每个 target 一张卡：端口/供应商/category 彩色徽章（破解/免费/收费）/isFree 开关/流量统计/缺 token 告警 |
| **编辑表单** | 卡片内可编辑：key/token（secretRef，密码框打码）、isFree 开关、models、targetHost/routePrefix（高级，折叠） |
| **重新提取** | 破解类卡片有"重新运行破解工具"按钮，调用 crackTool 并刷新 |
| **保存** | 表单提交 → 写 targets.json（非私密）+ secrets.json（私密）→ 触发热重载 |

### 6.3 热生效机制

```
配置变更（表单保存 / 文件手动编辑）
  └─► mtime 轮询检测（每 2s）
        ├─ targets.json 变更 → 重载：diff 端口列表，新增端口起新 server，
        │                      移除端口关旧 server，保留的更新配置
        └─ secrets.json 变更 → 重载 key（不重启 server）
```

---

## 7. 失败重试策略（本次范围）

- **本次**：保留现有逻辑（5xx 重试 3 次 + 429 翻译 + `Retry-After`），`isFree` 字段只存储和暴露，不驱动行为差异。
- **未来**：基于 `isFree` 实现差异化重试（免费供应商限流更频繁 → 指数退避 / 更长 Retry-After），字段已预留。

---

## 8. 数据流示例

### 8.1 Crack 链路（qclaw 8085）

```
Claude Code → 8081 /v1/messages（Anthropic 翻译，LiteLLM）
  → 转发 8085 /v1/chat/completions（OpenAI）
    → handler=qclaw：注入 Bearer (secrets.json qclaw_api_key) + UA + body 清理
    → httpx → mmgrcalltoken.3g.qq.com/aizone/v1/chat/completions
    → 响应（usage 缺失 → tiktoken 估算注入）
```

### 8.2 Free 链路（openrouter 8090）

```
OpenAI 客户端 → 8090 /v1/chat/completions
  → handler=passthrough：透传客户端 Authorization（key 由客户端自己带）
  → httpx → openrouter.ai/api/v1/chat/completions（path 重写）
  → 429 翻译 / 5xx 重试 3 次
```

### 8.3 缺 token 场景

```
crack 端口收到请求但 secrets.json 无 key 且无环境变量 fallback
  → 返回 401 {"error": {"type": "missing_token",
      "message": "请到 dashboard (http://127.0.0.1:8081/dashboard) 填写 {secretRef}"}}
```

---

## 9. 兼容性与迁移

| 项目 | 处理 |
|---|---|
| `PREFERRED_PROVIDER` | 移除；8082 固定 copilot。若环境变量仍存在，启动时警告忽略 |
| `targets.json` 旧格式（数组） | 启动时检测，自动迁移为 `{anthropicForwardPort, targets: [...]}`，保留原文件备份 |
| `apikeyEnv`（如 CODEBUDDY_TOKEN） | 保留为 fallback（secrets.json 优先） |
| 8081 端口 | 逻辑不动，仅 `anthropicForwardPort` 可配置（默认 8082） |
| dashboard 旧页面 | 保留模型表格/流量统计，追加编辑能力 |
| `test_dashboard.py` | 需更新（targets 新结构 + 新 API 测试） |
| `test_suite.py` | 需核对（8082 固定 copilot 后行为断言更新） |

---

## 10. 测试策略

1. **单元测试**（新增 `test_targets_schema.py`）：
   - targets.json schema 校验（必需字段、端口唯一性、label 唯一性）
   - secrets.json 读写 + 打码展示逻辑
   - 配置迁移逻辑（旧数组格式 → 新对象格式）
2. **集成测试**（扩展 `test_dashboard.py`）：
   - GET /api/targets 返回全部端口与分类
   - PUT /api/secrets/{label} 更新后热生效
   - POST /api/targets/{label}/recrack 触发
3. **破解工具测试**：
   - crack_qclaw.py 在无 QClaw 环境时优雅失败（退出码非 0 + 引导文案）
4. **回归**：test_suite.py 中 8082 copilot 行为断言更新

---

## 11. 风险与回滚

| 风险 | 缓解 |
|---|---|
| targets.json 新格式破坏现有启动 | 兼容旧格式自动迁移 + 备份 |
| 移除 PREFERRED_PROVIDER 影响现有用户 | 启动警告 + 文档说明；.env 保留读取但忽略 |
| 破解工具路径/依赖问题 | 纯标准库实现；失败不阻塞；日志明确 |
| 热重载竞态（轮询中改文件） | 原子写（写临时文件 + rename）；重载串行化 |
| dashboard 误操作写坏配置 | PUT 前 schema 校验；写前备份 `*.bak.<ts>` |

---

## 12. 实施顺序（建议）

1. targets.json 新 schema + 迁移逻辑 + 校验
2. secrets.json 读写模块 + gitignore
3. 统一透传引擎重构（handler 分发 + 鉴权注入 + 401 缺 token）
4. 8082 固定 copilot / 8085 qclaw 挂接
5. 破解工具 crack_qclaw / crack_codebuddy / crack_copilot
6. 热重载机制（mtime 轮询 + 端口 diff）
7. dashboard REST API + 表单
8. 测试更新 + 回归
9. 文档更新（README/AGENTS.md/CHANGELOG）
