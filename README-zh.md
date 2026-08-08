# Claude Code Proxy

让 Claude Code 通过第三方 API 运行，无需 Anthropic 官方账号。
**多端口架构：每个上游供应商一个独立端口，由 `targets.json` 配置驱动。**

---

## 多端口架构 🏗️

| 端口 | 供应商 | 分类 | handler | 协议 |
|------|--------|------|---------|------|
| 8080 | aggregator | aggregate | aggregator | OpenAI（聚合网关：虚拟模型路由 / 会话粘性 / 熔断降级） |
| 8079 | dashboard | — | FastAPI | 管理面（dashboard + 全部 /api/* 管理 API，独立端口） |
| 8081 | anthropic-compatible | — | FastAPI | Anthropic 翻译入口（/v1/messages 等，对应 targets[] 中 handler="anthropic" 的 target） |
| 8082 | copilot-enterprise | crack | copilot | OpenAI（GHE 企业版，收费） |
| 8083 | copilot | crack | copilot | OpenAI（个人版） |
| 8084 | codebuddy | crack | passthrough | OpenAI |
| 8085 | qclaw | crack | qclaw | OpenAI |
| 8086 | trae-work | crack | trae-work | OpenAI（签到/额度/续期） |
| 8090 | openrouter | free | passthrough | OpenAI |
| 8091 | nvidia | free | passthrough | OpenAI |
| 8092 | gemini | free | **gemini-native** | OpenAI↔Gemini 原生转换 |
| 8093 | opencode-zen | free | passthrough | OpenAI |
| 8094 | open-go | paid | passthrough | OpenAI |

> 权威端口表（targets.json 驱动）见 [docs/architecture.md](docs/architecture.md)。

- **统一透传引擎**：所有端口共享 HTTP 解析/转发/429 翻译/重试逻辑，由 `targets.json` 驱动
- **分类**：`crack`（破解，注入 secrets.json token）/ `free`（免费，透传客户端 key）/ `paid`（收费，透传客户端 key）
- **isFree**：管理界面维护，标记供应商 key 是否免费（重试策略预留字段）
- **热重载**：`targets.json` / `secrets.json` mtime 轮询（2s），修改后自动生效
- **base_url 规范**：crack 类与 gemini-native 统一 `/v1`（代理内部映射下游）；free/paid 透传用 `routePrefix`（如 `/api/v1`）
- **codebuddy 非流式兼容**：上游只接受流式（11101），代理自动把非流式请求转流式聚合为完整 JSON，非流式客户端也可用
- **SSE 帧规范化（`normalizeSse`）**：部分上游返回的 SSE 帧不合规（如 codebuddy 每帧都塞满 `content:""` / `tool_calls:[]` 等空字段，导致客户端 SDK 按"键是否出现"分段，把思考链切成数百个独立块）。开启后代理逐帧剔除空值字段还原标准 OpenAI 格式，有内容的 `tool_calls` 等严格保留；解析失败一律原样透传不吞帧
- **管理界面**：`http://127.0.0.1:8079/dashboard`（也可经任意端口 `/dashboard` 反向代理访问，实际由 8079 独立 app 承载，`/api/*` 同理）

## 快速启动

```bash
cd claude-code-proxy
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # 或 uv sync

# 配置 targets.json（端口/供应商/分类/handler/模型）与 secrets.json（token）
# 运行配置（可选）写进 targets.json 顶层 server 段，只剩四个键：listenPort / dashboardPort / log / cache
# .env 已废弃删除（备份 .env.bak），旧部署用 scripts/migrate_env_to_targets.py 迁移进 server 段

.venv/bin/python server.py   # 启动 8081 翻译入口 + 8079 dashboard + targets.json 定义的全部端口
```

> 端口-供应商绑定由 `targets.json` 决定。8081 的模型路由由 `targets[]` 中 `handler="anthropic"` 的 8081 target 嵌套的 `models[]` 决定（模型 → 端口 + 下游模型名），没配的模型直接 404。
>
> legacy 单端口模式（按环境变量选 provider + LiteLLM 翻译）已彻底下线，相关的四个 server 段旧键一并删除；残留在旧配置里也只会被静默忽略，不报错。

## 客户端接入

### Claude Code（Anthropic 协议）

```json
// ~/.claude/settings.json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8081",
    "ANTHROPIC_API_KEY": "dummy",
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "API_TIMEOUT_MS": "3000000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": 1,
    "CLAUDE_CODE_EFFORT_LEVEL": "low",
    "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"
  }
}
```

### OpenAI 兼容客户端

```
base_url = http://<局域网IP>:<端口>/v1     （crack 类 / gemini-native）
base_url = http://<局域网IP>:<端口>/api/v1  （openrouter 等 free/paid 透传）
api_key  = dummy（crack 类）；真实 key（free/paid 透传）
```

> 局域网 IP 与后缀直接显示在 dashboard 卡片详情的 `base_url` 属性，可复制即用。

## Dashboard 管理界面

`http://127.0.0.1:8079/dashboard`（也可经任意端口 `/dashboard` 反向代理访问，实际由 8079 独立 app 承载）：

- **分类栏**：聚合网关（8081）/ 破解网关 / 直连网关 三组，带数量徽标
- **卡片**：端口强调条 + 请求数 + 流量统计块（成功率/运行时长/进度条）+ 可粘贴 `base_url` + token 编辑
- **模型编辑弹框**（✏️ 编辑模型）：iOS 滑动开关 + **总开关**（全开/全关/部分开 indeterminate）+ 搜索框 + **编辑态自动拉取下游真实模型列表**（失败降级配置）
- **8081 自身统计**：`/v1/messages` 请求数与模型级统计

## 模型映射（3 级梯度）

| Claude Code 请求模型 | 映射到 | 配置 |
|---------------------|--------|------|
| Opus 系列 | `BIG_MODEL` | `targets.json` 的 `modelMapping.opus` |
| Sonnet 系列 | `MEDIUM_MODEL` | `modelMapping.sonnet` |
| Haiku 系列 | `SMALL_MODEL` | `modelMapping.haiku` |

基于**子串包含**匹配，短名 `sonnet` / `haiku` / `opus` 也有效。copilot 模型角色（opus/sonnet/haiku 对应的真实模型名）现从 copilot target（8082/8083）的 `modelRoles` 字段实时推导（见 `gateways/copilot.py` 的 `_copilot_*_model()`），不再使用环境变量。

## 调试

```bash
# 开启详细日志
DEBUG=true python server.py

# 实时跟踪
tail -f proxy.log
```

| 变量 | 默认 | 说明 |
|------|------|------|
| `DEBUG` | `false` | `true` 启用详细日志（请求/响应/模型映射） |
| `LOG_FILE` | 空 | 文件日志路径（`DEBUG=false` 也生效，会记录 warning/error） |
| `LOG_RETENTION_DAYS` | `7` | 轮转日志保留天数，过期自动清理 |
| `LOG_ROTATE_WHEN` | `midnight` | 日志轮转周期单位（如 `midnight`、`H`） |
| `LOG_ROTATE_INTERVAL` | `1` | 轮转周期步长 |

---

## 测试

统一测试套件 [test_suite.py](test_suite.py)，整合自历史三个文件（`test_claude_api.py` / `test_messages_endpoint.py` / `tests.py`），覆盖 8081 的翻译链路 `/v1/messages` 和 target 端口的透传链路 `/v1/chat/completions`（注：8081 自身的 legacy `/v1/chat/completions` 端点已删除）。

```bash
# 启动服务后运行全部测试（15 大类，38 个测试点）
python test_suite.py

# 分场景运行
python test_suite.py --simple       # 基础场景：连通性/模型名/system/消息格式
python test_suite.py --tools        # 工具 + thinking + 错误处理
python test_suite.py --oai          # 仅 OpenAI 透传端点
python test_suite.py --no-streaming # 跳过流式测试
```

测试覆盖：连通性、模型名还原、System Prompt、流式 SSE 事件序列、多轮对话、参数透传、Stop Sequences、Tools、Tool Choice、**Thinking**（adaptive/enabled/budget/历史 422 bug/工具组合）、Token 计数、错误处理、性能基准、**OpenAI `/v1/chat/completions` 透传端点**（11 个场景）。

---

## 架构

```
Claude Code / Cline
  --Anthropic 格式--> :8081/v1/messages
                         |
                         |  查 models[]：模型 -> 端口 + 下游模型名
                         |    命中   -> Anthropic->OpenAI 翻译 -> httpx 转发本地端口 -> 译回 Anthropic
                         |    未命中 -> 404（无兜底路径）
                         v
                    本地 target 端口（8082~8094）

任意 Agent / 自定义工具
  --OpenAI 格式--> :8082 copilot-enterprise  -> httpx（部分模型走 Responses API 桥接）
                   :8083 copilot             -> httpx（同上，个人版账号）
                   :8084 codebuddy           -> httpx + body 清洗 / SSE 规范化 / 非流式聚合
                   :8085 qclaw               -> httpx + body 白名单清洗 + Key 自动解密
                   :8086 trae-work           -> OpenAI <-> llm_utils_chat 协议转换
                   :8092 gemini              -> OpenAI <-> Gemini generateContent 原生转换
                   :8090/8091/8093/8094      -> httpx 透传（客户端自带 key）

                   :8080 aggregator          -> 虚拟模型路由 / 会话粘性 / 熔断降级 -> 上面各真实端口
```

> **注：** 8081 只有这一条路径。按 provider 分流 + LiteLLM 翻译的 legacy 单端口模式已彻底删除，legacy `/v1/chat/completions` 端点也一并移除。`/v1/messages/count_tokens` 改用 tiktoken 本地估算，不再经任何翻译层。

完整配置参考 `targets.json`（顶层 `server` 段为运行配置，`targets` 数组为 Target 定义）与 `secrets.json`（私密凭据），schema 见 [docs/architecture.md](docs/architecture.md)

---


## QClaw 上游直连方案

🔗 详细 QClaw 方案（三层架构 / API Key 解密链路 / 三条铁律 / 排查指南 / 关键文件）见 [docs/qclaw.md](docs/qclaw.md)；19000 网关逆向结论见 [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md)。
