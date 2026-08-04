# Trae Work 破解与 API 逆向文档

> 本文档固化了字节跳动 Trae Work（TRAE SOLO CN）的 LLM API 认证破解成果：
> 认证数据存储、tc 加密格式、全部 API 接口规范、设备指纹 header、token 生命周期，
> 以及配套工具 `crack_traework.py` / `crack_common.py` 的用法与架构。
> 所有接口与参数均来自实际逆向与实测验证（2026-08），非官方文档。
>
> 本文档是 [`crack_traework.py`](../crack_traework.py)、[`crack_common.py`](../crack_common.py)
> 与代理 8086 端口（handler=`trae-work`）的实现参考。

---

## 1. 产品背景

**Trae Work = TRAE SOLO CN**，是字节跳动的一款独立 AI 工作产品，区别于 **Trae CN（Trae IDE）**。

| 维度 | Trae CN（Trae IDE） | Trae Work（TRAE SOLO CN） |
|------|--------------------|--------------------------|
| 角色 | 集成开发环境 | 独立 AI 工作产品（本代理接入目标） |
| 产品码 | — | `SOLO_Lite` |
| 版本 | — | `0.1.43`（`x-ide-version: 0.1.43`） |
| 安装目录 | `%USERPROFILE%\Trae CN\` | `%APPDATA%\TRAE SOLO CN\` |

> ⚠️ **易踩坑**：两个产品的安装目录非常相似，破解时务必确认操作的是
> `TRAE SOLO CN`（Trae Work），不是 `Trae CN`（Trae IDE）。详见[排查备忘](#10-排查备忘)。

在 `claude-code-proxy` 中，Trae Work 作为 crack 类网关接入：

- **端口**：8086（targets.json 中 `listenPort=8086`，handler=`trae-work`，默认未启用，需在 targets.json 中启用）
- **上游**：`trae-api-cn.mchost.guru`（LLM 网关）与 `api.trae.cn`（业务 API）
- **分类**：`crack`（本地客户端提取 token，代理注入认证）
- **客户端接入**：`base_url = http://<host>:8086/v1`，`api_key = "dummy"`（crack 类代理不校验）

涉及两台机器：

- **CT105（Linux）**：本机，跑代理
- **192.168.2.177（Windows）**：SSH 免密（`Administrator`），装有 Trae Work 客户端，是 token 提取源

---

## 2. 认证数据存储与 tc 解密

### 2.1 存储位置

```
路径: %APPDATA%\TRAE SOLO CN\User\globalStorage\storage.json
key:  iCubeAuthInfo://icube.cloudide
```

- 明文 JSON 字段：`token`（access，即 Cloud-IDE-JWT）、`refreshToken`、`expiredAt`、
  `refreshExpiredAt`、`userId`、`host`、`account`
- 值是 base64 编码的加密 blob，以 `dGMFEAAA...` 开头（`dGMFEAAA` 是 `t\x05\x10\x00\x00` 的 base64）

### 2.2 tc 加密格式

解码后整体布局：

```
[6B header][32B random][AES-128-CBC 密文]
```

| 段 | 长度 | 说明 |
|----|------|------|
| header | 6B | 加密类型标记。`74 63 05 10 00 00`（"tc" + 类型）= AES；另有 AES_PRIVATE 类型 |
| random | 32B | 随机数，参与密钥派生 |
| 密文 | 可变 | AES-128-CBC，padding 后为明文的密文 |

解密结果：

```
[64B SHA-512 hash][明文 JSON]
```

- 前 64 字节是明文 JSON 的 SHA-512 哈希，用于完整性校验
- 校验通过后的剩余部分才是真实明文

### 2.3 密钥派生（SHA-512）

```
salt = SALT_A XOR SALT_B   # AES 类型（每字节异或，共 64B）
       SALT_C XOR SALT_D   # AES_PRIVATE 类型
key  = SHA-512(SHA-512(random) + salt)[0:16]
iv   = SHA-512(SHA-512(random) + salt)[16:32]
```

- key/iv 来自 `SHA-512(SHA-512(random) + salt)` 输出，前 16 字节为 key，16~32 字节为 iv
- 4 组盐值硬编码在 `crack_common.py` / `crack_traework.py` 中，来源是开源项目
  [ZedeX/trae-local-api](#9-开源项目参考) 的逆向成果

> **crack_traework.py 中的实现**：`_detect_enc_type()` 识别 header 类型，
> `_decrypt_tc()` 完成派生 + 解密 + SHA-512 校验。

---

## 3. API 端点总表

### 3.1 LLM 与 IDE 业务端点（`trae-api-cn.mchost.guru`）

| 用途 | 端点 | 认证 |
|------|------|------|
| LLM 对话 | `POST https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat` | Cloud-IDE-JWT + 设备指纹 |
| 创建代理任务 | `POST https://trae-api-cn.mchost.guru/api/agent/v3/create_agent_task` | 同上 |
| 模型列表 | `POST https://trae-api-cn.mchost.guru/api/ide/v1/get_detail_param`（body: `function=chat_v3`） | 同上 |

### 3.2 账户业务端点（`api.trae.cn`）

| 用途 | 端点 | 认证 |
|------|------|------|
| 额度查询 | `POST https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage` | Cloud-IDE-JWT + 设备指纹 |
| 签到状态 | `POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/status` | 同上 |
| 签到领取 | `POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/claim` | 同上 |
| 刷新 token | `POST https://api.trae.cn/cloudide/api/v3/trae/oauth/ExchangeToken` | **无**（裸调即可，设备已 BOUND） |

---

## 4. 认证 header 清单（设备指纹，全部固定值）

所有 LLM / 业务请求（除 ExchangeToken）都需携带：

```
Authorization: Cloud-IDE-JWT <token>
x-app-id: 6eefa01c-1036-4c7e-9ca5-d891f63bfcd8
x-ide-version: 0.1.43
x-ide-version-code: 20260730
x-device-id: 199444637423849
x-machine-id: d2115a713ee587fea5d340ceb8ef1fda3ad808431c24e7fed3085693f52f4428
x-device-type: windows
X-Trae-Client-Type: lite
x-trae-authorized-services: feishu
```

- `Authorization` 的 `token` 即 storage.json 解密后的 access token
- `x-*` 各字段均为设备指纹固定值，实测不会随时间变化，可直接硬编码
- `X-Trae-Client-Type: lite` 对应 SOLO_Lite 产品码

---

## 5. LLM 请求/响应协议

### 5.1 OpenAI → Trae 请求体转换

代理 8086 把 OpenAI `/v1/chat/completions` 请求转换为 Trae `llm_utils_chat` 格式：

```json
POST /api/agent/v3/llm_utils_chat
{
  "messages": [
    {
      "role": "user",
      "content": [{"type": "text", "text": "hi"}],
      "role_type": 0
    }
  ],
  "function": "chat_v3",
  "stream": true,
  "model": "glm-5.2",
  "config_name": "glm-5.2"
}
```

- `content` 是**多模态数组**，`{"type": "text", "text": "..."}` 为文本块
- `role_type: 0` 表示普通用户消息
- `model` 与 `config_name` 相同（如 `glm-5.2`）

**角色支持（2026-08-02 逆向结论）**：上游 messages 只有 `role + content`，**无 OpenAI 式 `tool_calls` / `role=tool` 概念**。`system/user/assistant` 直接透传；`role=tool` 必须**转 `user` 并文本化**（见 §5.4 请求侧），否则 `Doubao-Seed-Code` 返回 200 + 空 SSE 流（0 output 事件），`glm-5.2` 等可容忍。

### 5.2 SSE 响应事件

响应为 SSE 流，关键事件：

```
event: metadata        # 元数据（模型、会话等）
event: timing_cost     # 耗时统计
event: output          # 正文输出，data 含 response + reasoning_content
event: error           # 错误 {code, message, extra}
event: progress_notice # 进度提示（"Processing_xxx" 字符串，跳过）
event: queue_*         # 排队事件（request_wait_in_queue 含 position，不转发）
```

- `event:output` 的 data 是 JSON，兼容**两种形态**（trae-local-api 逆向结论）：
  - 旧格式：`{"response": ..., "reasoning_content": ..., "tool_calls": [...]}`
  - 新格式（2026-05）：`{"type": "text", "content": ..., "reasoning": ...}`
- 代理需把两种形态都还原为 OpenAI 流式分块（`_trae_chunk_to_openai`：`response|content` → `content`，`reasoning_content|reasoning` → `reasoning_content`）
- **`event:error` 不再静默**：`{code, message}` → WARNING 日志 + 转成 `[Trae error {code}: {message}]` 文本 chunk 透给客户端（2026-08-02 修复）
- **progress 过滤**：旧格式 response 以 `Building prompt:` / `Completed building prompt` 开头 → 丢弃（上游进度提示，不当正文输出）
- 代理需要把 SSE 事件还原为 OpenAI 流式分块格式，并把 `reasoning_content` 映射到
  OpenAI 的 `reasoning` 字段（若客户端支持）

### 5.3 图片/多模态（实测结论，2026-08-02 修订）

**Trae 上游支持图片**，格式为标准 OpenAI 格式（错误消息暴露 Go struct：`LLMRawMessageImageUrl`）：

```json
{"type": "image_url", "image_url": {"url": "https://公网图片或data URI"}}
```

**关键：图片能力只对 Trae 内置多模态模型开放**（官方 FAQ + 实测确认）：

| 模型 | 传图实测 |
|---|---|
| `Doubao_1_6` | ✅ 成功（"图中呈现出湛蓝的天空…"） |
| `qwen-3.7-plus` / `minimax-m3` / `Doubao-Seed-2.0-Code` | ✅ 成功 |
| `Doubao-Seed-2.1-Pro` / `glm-5.2`（非多模态名单） | ❌ 3003 `all models failed` / 1005 |
| `kimi-k3` | ❌ 1005（不在多模态名单） |

- 传图失败与图片 URL 域名关系不大（gstatic 公网 URL 实测可用）；**根因是模型不在 Trae 多模态白名单**，Trae 层直接拒绝其图片请求
- 错误格式排查记录：`image_url` 传字符串 → HTTP 400；`image` 字段 / `type:base64` / `data` → 4001 `param is invalid`；`image_url:{url}`（标准格式）→ 通过解析
- IDE 客户端内部有 `multimodal/report_image_content` 图片上报 RPC（先传 Trae 存储），但**直接传公网 URL 即可绕过**（对内置多模态模型）

**代理使用要求**：8086 端口传图可用，但客户端必须用**内置多模态模型**（如 `Doubao_1_6`），`_openai_to_trae_body` 已按标准格式原样透传 `image_url`。

---

## 5.4 工具调用（Function Calling）翻译层（2026-08-02 实现）

**问题**：客户端传标准 OpenAI `tools`，Trae 上游要求 `tools[].function.parameters` 是 **JSON 字符串**（object 直接 4001）；且响应侧不同模型输出形态完全不同，代理需要统一翻译成 OpenAI `tool_calls`。

### 请求侧（`_openai_to_trae_body`）

- 透传 `tools`，仅转换：`parameters` object/list → `json.dumps()` 字符串（实测不转 → `4001 bad request: cannot unmarshal object into ... of type string`）
- 其余字段（type/name/description）原样
- **tools 提示词注入（2026-08-02，trae-local-api 方式）**：Trae 上游对标准 tools 字段支持不可靠（seed-code 实测不识别 → 输出乱格式），额外把工具定义注入提示词（`_build_trae_tool_prompt`，附加到最后一条 user 消息），指示模型用 `<tool_call>{"name":"...","arguments":{...}}</tool_call>` XML 格式输出工具调用，响应侧解析（`_TOOLCALL_XML_RE`）
- **工具调用历史文本化（2026-08-02 修复，seed-code 空响应根因）**：assistant 消息的 `tool_calls` 字段上游不识别，直接丢弃会让后续 `role=tool` 消息变成"孤立 tool 消息"，`Doubao-Seed-Code` 因此返回 200 + 空 SSE 流（`stream done, 0 chunks`，客户端收不到任何内容）。转换时：
  - assistant 带 `tool_calls` → 序列化拼入 content：`"[Tool Call: {name}]\nArguments: {args}"`（有 content 则换行拼接）
  - `role=tool` 消息 → 转 `role=user`，content 加前缀：`"[Tool Call Result: {name}]\n{输出}"`（按 `tool_call_id` 匹配工具名，无匹配省略后缀）
  - 编码参考 trae-local-api（逆向 Trae 客户端）`agent.js runAgentLoop` 的文本化回填方式
- 采样参数（`temperature/top_p/presence_penalty/frequency_penalty/stop/seed/n`）**尽力透传**（2026-08-02），`max_tokens` 截断到 128000（参考 trae-client.js）

### 排队处理（简化策略，2026-08-02）

- 上游 `request_wait_in_queue` 事件（字节原生排队能力，data 含 position）→ **模型繁忙，直接终止**返回 `[模型繁忙，排队位置 #N，请稍后重试]`，**不做降级重发**
- 曾实现排队感知降级（参考 trae-local-api 分档重发），用户评估后撤回——排队即繁忙，保持简单

### DeepSeek-V4-Flash 实测结论（2026-08-04）

**测试环境**：CT105（Linux 代理）+ 192.168.2.177（Windows Trae Work 客户端），targets.json 启用 8086 端口，模型 `DeepSeek-V4-Flash`（targets.json 中 `models[]` 启用白名单，而非 `modelMapping`）。

**Seed 基线**：`test_trae_work_e2e.py --only seed-simple --port 8086`，**4/4 通过**（流式/非流式 × 简单/多轮）。

**DeepSeek-V4-Flash 专项用例**（来自 `scripts/test-cases/trae-work/`）：

| 用例文件 | 场景 | 结果 |
|----------|------|------|
| `scripts/test-cases/trae-work/deepseek-flash-simple.json` | 简单对话（流式） | HTTP/SSE 基础断言通过，**但每次专项运行被判定 busy（上游 queue position #341）** |
| `scripts/test-cases/trae-work/deepseek-flash-tool-history.json` | 工具历史（流式） | HTTP/SSE 基础断言通过，**但每次专项运行被判定 busy（上游 queue position #340）** |

**每次专项运行结果**：**7 passed / 2 failed**，失败原因均为上游 `queue position #341` / `#340` 的 busy。

**代理当前行为**（`_handle_traework`）：

- 检测到上游 `request_wait_in_queue` 事件且 `position > 0` 时，写入文本 chunk `[模型繁忙，排队位置 #N，请稍后重试]`
- 仍发送 **HTTP 200**、**finish_reason=stop**、**SSE `[DONE]`**
- **不做降级重发**，不抛出异常，不返回非 2xx 状态码

**对客户端（OMO/opencode）的影响**：

- OMO/opencode 看到 HTTP 200 + 正常 SSE 流 + finish stop + [DONE]，**通常不会按失败触发重试/换模型**
- 这是典型的**“业务失败被 HTTP 200 伪装成功”**的已知问题：客户端层面看起来请求成功了，实际内容是排队提示

**后续改进方向（未实现，仅记录方向）**：

- 若要让客户端识别并重试，应将 busy 映射为可识别的非 2xx 错误（如 503/429），或由聚合层（8080）专门识别该错误文本并触发降级
- 需评估流式协议兼容性：中途切换状态码会破坏 SSE 流，可能需在流式开始前预检或改为非流式预检
- 保留现有“不做降级重发”的历史决策（2026-08-02），本次事实不与之冲突——当前策略是“检测到排队即终止并返回提示”，未来可演进为“检测到排队 → 返回可识别错误码 → 触发客户端/聚合层重试”

### 响应侧（按模型分两类）

上游实测（2026-08-02，9 模型全测）：

| 类别 | 模型 | 上游形态 | 代理翻译 |
|------|------|----------|----------|
| **A 原生 tool_calls** | `glm-5.2` `glm-5.1` `qwen-3.7-plus` `minimax-m3` `DeepSeek-V4-Pro` `DeepSeek-V4-Flash` | `tool_calls[]` JSON 字段（`function_call{name, arguments}`，流式也是全量无分片） | 结构化字段，逐 chunk 立即转发：字段映射 `function_call` → `function`，透出 `id`/`index`（`_trae_tool_calls_to_openai`） |
| **B 文本形态标记**（DSML / `[Tool Call:]` / `{"reasoning_content":...}` JSON 字面量 / `<tool_call>` XML） | `Doubao-Seed-Code`（seed-code） | `response`/`content` 字段里混杂输出各种文本标记，形态不稳定、且可能被截断/分片 | **正文纯累积，流结束后一次性解析**（见下方架构说明），避免半截标记误判 |
| **C 空响应（不可用）** | `Doubao-Seed-2.1-Pro` `kimi-k3` `DeepSeek-V4-Flash-Official` | 无 response 无 tool_calls（普通对话也空，疑似收费/渠道过滤） | 已从 `/v1/models` 白名单剔除 |

### 流式架构：正文纯累积 + 流结束统一解析（2026-08-02 重构）

**背景**：B 类模型（seed-code）的工具调用/思考文本以**不稳定的文本标记**混在 `response`/`content` 字段里输出，且经常被 SSE 分片、甚至被截断（未闭合）。曾尝试在流式接收过程中"边收边猜这个 chunk 是不是标记的开头/半截"（启发式函数 `_is_potential_toolcall_prefix` 等），结果每堵住一种半截标记就会冒出下一种变种——因为任意长度的文本前缀理论上都可能是"某个标记的未完成前缀"，这是不可判定问题。实测抓包命中过三种变种：

1. `[Tool Call: xxx]` 的开头 `"["` 独立成一个 SSE chunk，被误判为普通正文提前透传，导致后续标记文本缺了开头 `[` 无法匹配，整段工具调用在流结束时被当残段丢弃
2. `{"reasoning_content":"..."}` JSON 字面量：只要子串 `"reasoning_content"` 曾经出现且未被完整闭合摘除，`_looks_like_dsml` 判断函数就会对缓冲区永远返回 `True`，导致后续所有正文被无限期拖入缓冲区，直到流结束才一次性吐出（表现为卡顿数秒）
3. reasoning JSON 被模型截断（缺尾部 `"}`），永远无法闭合，同样导致无限期缓冲

**参考 `trae-local-api`（官方逆向实现，`/root/trae-local-api/src/agent.js` `runAgentStream`/`extractToolCalls`）的架构**：该实现从不在流式接收阶段做标记判断，而是把整轮 `response`/`content` 原始累积成 `fullContent`，等上游 SSE 流完全结束（收到 `'end'`）后，才对完整文本一次性跑正则解析 `tool_calls`，此时标记必然完整（或确定不存在），不存在"半截"问题。

**本实现采用同样策略**（`_resolve_trae_text`，`_handle_traework` 流式/非流式两条路径共用）：

- `response`/`content` 正文：流式阶段只做纯字符串累积（`text_buf`），**不逐 chunk 转发给客户端**
- `reasoning_content`/`reasoning`、原生 `tool_calls` 字段：上游明确给出的结构化字段（非文本猜测），不存在"标记未闭合"的歧义，**逐 chunk 立即转发**
- 上游 SSE 流结束后，对累积的完整 `text_buf` 调用 `_resolve_trae_text()` 一次性解析：
  1. 先提取 `{"reasoning_content":"..."}` JSON 字面量（若存在且已闭合）
  2. 再解析工具调用标记（DSML `<｜function｜>` 块 / `[Tool Call: name]\nArguments: {...}` 文本 / `<tool_call>` XML），三种格式都会被完整清洗出正文
  3. 清洗后的 `content_text` 与解析出的 `tool_calls`/`reasoning` 分别 flush 给客户端

**代价**：牺牲逐字打字机效果（seed-code 系模型的回复不再是实时流式，而是整轮结束后一次性吐出），换取 100% 正确性——不会再出现半截标记导致的卡顿/丢弃/泄漏。原生 tool_calls 模型（A 类）不受影响，仍然逐 chunk 实时流式。

### 后续两个衍生 bug 及根治（架构重构后仍暴露，2026-08-02 同日修复）

架构重构消除了"半截标记误判"，但暴露出两个更底层的问题——本质都是**用正则去处理本应严谨解析的结构**：

1. **reasoning 多段拼接未完整提取**：`_extract_reasoning_text` 曾用 `.search()` 只找第一个 `{"reasoning_content":"..."}` JSON 字面量、`.sub(count=1)` 只摘除第一个。而 seed-code 会把多段思考拆成**多个独立的** `{"reasoning_content":"..."}` JSON 拼接输出（不是一个整体 JSON 装完），第二段及之后的原样残留在正文里，客户端看到裸露的 JSON 字面量泄漏。修复：改用 `.finditer()`/`.sub()`（不限 count）处理全部片段。
2. **`<tool_call>` XML 参数提取被嵌套结构截断**：`_TOOLCALL_XML_JSON_RE` 用正则 `\{[\s\S]*?\}` 非贪婪匹配 `arguments` JSON 对象，遇到嵌套花括号（如 `edit` 工具 `oldString`/`newString` 里的 JS 代码 `{{}}`）或转义引号，在第一个 `}` 处提前截断，`json.loads` 校验失败、`tool_calls` 解析为空，导致整段 `<tool_call>...</tool_call>` 原始文本泄漏到正文（表现为客户端 IDE 界面直接显示裸露的工具调用 JSON，未被解析执行）。

**教训（已做代码级审查确认根治）**：任何"提取 JSON 对象子串"的场景一律禁止用正则模拟花括号配对——这是同一类错误第三次出现（DSML 配对正则、reasoning 多段提取、这次的 XML JSON 提取）。统一改用平衡括号扫描 `_extract_balanced_json`（原仅用于 `[Tool Call:]` 文本格式，现 DSML/XML 两条路径同步收编），全代码库 grep 确认无残留同类脆弱正则。

### Wave 2 新发现并已修复的文本变体（2026-08-03 抓包）

下列文本来自 `proxy.log` 实际抓取后脱敏；其中大段文本日志曾被 `text[:2000]`
截断，示例不声称保留完整原文。2026-08-04 的实现和回归用例覆盖了以下三个具体形态：

1. **`<seed_call>` + `invoke`，闭合标签不对称**（Todo 5）：脱敏示例：
   ```xml
   <seed_call><invoke name="bash"><parameter name="command">…</parameter></invoke></tool_call>
   ```
   根因：已有解析器只覆盖 DSML、`[Tool Call:]` 与标准 `<tool_call>` 形态，未识别
   `<seed_call>` 外层；实际样本用 `</tool_call>` 而非 `</seed_call>` 闭合。修复：新增
   `_SEED_CALL_RE`，以 `re.search` 语义匹配不受前置正文影响，并容忍 `</seed_call>`、
   `</tool_call>`、`</seed:tool_call>` 三种外层闭合；内部 `<invoke name="X">` 参数按
   标签位置切片，JSON 参数仍交给 `_extract_balanced_json`，不用非贪婪正则截取参数值。

2. **`<seed_call>` + `function`，带 `string="true"` 属性**（Todo 5）：脱敏示例：
   ```xml
   <seed_call><function name="bash"><parameter name="command" string="true">…</parameter></function></seed:tool_call>
   ```
   根因：同属未覆盖的 `<seed_call>` 外层变体，且 `<parameter>` 额外的 `string="true"`
   属性不能影响参数名和值的提取。修复：同一 `_SEED_CALL_RE` 接入第 5 种解析尝试，
   加入 `<function name="X">` 子变体；参数开标签只读取 `name`，忽略其余属性。解析出
   工具调用后，`_resolve_trae_text` 的正文清洗链先执行 `_SEED_CALL_RE.sub()`，再执行
   `_DSML_BLOCK_RE.sub()`，保证原始调用块不会残留到 content。

3. **自由文本前缀混杂标准 `<tool_call>` JSON 的多行 command**（Todo 6）：脱敏示例：
   ```xml
   说明正文…<tool_call>{"name":"bash","arguments":{"command":"python3 -c '\n多行脚本，含 {…} 与 \"…\"'"}}</tool_call>后续正文…
   ```
   根因经合成完整 fixture 调试确认：`_TOOLCALL_XML_RE.search()` 已能在自由文本后找到
   `<tool_call>`，`_extract_balanced_json` 也能提取完整对象；失败点是默认
   `json.loads()` 拒绝 command 字符串中的原始多行控制字符。修复：该 XML 路径改为
   `json.loads(..., strict=False)`，保留原始多行 command；未知标记警告的文本上限由
   `text[:2000]` 提升为 `text[:16384]`，使后续抓包保留足够的诊断上下文。

**当前已知局限性**：本轮仅覆盖上述两个 `<seed_call>` 子变体和完整闭合、但 command
含原始多行控制字符的标准 `<tool_call>` JSON。未闭合标记仍按原文保留以便排查；模型仍
可能产生未覆盖的第 6+ 种文本变体。命中疑似标记但未解析时会记录 warning，后续须以日志
和 fixture 验证为准，不能宣称已穷尽所有变体。

### 判定逻辑

不按模型名硬编码：响应含 `tool_calls` 字段 → A 路径立即转发；否则 `response`/`content` 一律走纯累积 + 流结束统一解析（B 路径，见上）。新模型自动归队。

### /v1/models 白名单过滤

`GET /v1/models` 上游列表按 targets.json `enabled=true` 过滤（C 类模型不再出现在客户端模型列表）。

### 分片合并（glm-5.2 特殊性）

glm-5.2 **非流式**时上游把同一工具调用的 `arguments` 分片输出（多个 `tool_calls` 事件，第二个只有 `{"arguments":"}"}`）。代理非流式累积时按 `index` 合并 name/arguments（`tool_calls` 数组原地拼接）。流式下上游输出全量，无需合并。

---

## 6. token 生命周期

### 6.1 有效期

| token | 有效期 | 说明 |
|-------|--------|------|
| access（Cloud-IDE-JWT） | **14 天**（JWT，`exp - iat = 1210000s`） | 用于全部 API 调用的 Authorization |
| refresh | **半年**（`refreshExpiredAt`） | 每次刷新轮换新值，**可无限续期** |

### 6.2 刷新协议

- 端点：`POST https://api.trae.cn/cloudide/api/v3/trae/oauth/ExchangeToken`
- **当前设备已 BOUND**（`BoundDeviceID=qwy86n6rolp7r5`），裸调 ExchangeToken 即可刷新，**无需任何签名**
- 完整协议（逆向 main.js）实际带 RSA DeviceProof 签名 + DeviceInfo，但设备已绑定时可省略
- ⚠️ **风险**：若换设备，BOUND 失效，则需要完整 RSA 签名协议才能续期

### 6.3 自动续期

- `crack_traework.py --refresh` 用 secrets.json 中的 `refreshToken` 刷新 access token 并回写
- cron 脚本 `trae_work_daily.sh`（`0 3 * * *`）每日执行：签到 + 当 token 剩余有效期 < 3 天才触发刷新

### 6.4 额度结构示例（`ide_user_ent_usage` 返回）

用户"阿软259"（userId=`2328093701182980`）：**积分计费**（`is_credits_billing=true`），权益包：

- 老用户福利 2000×2（到期 2026-08-31）
- 免费额度 / 每月登录赠送 500 / 签到奖励 200×2

---

## 7. crack_traework.py 用法

工具位于仓库根目录，Linux / Windows 均可运行（Windows 端提取 storage.json，Linux 端做解密与刷新）。

```
python crack_traework.py [--secrets secrets.json] [--force]
python crack_traework.py --checkin        # 查询今日签到状态
python crack_traework.py --claim          # 执行每日签到
python crack_traework.py --quota          # 查询剩余额度
python crack_traework.py --refresh        # 用 refreshToken 刷新 access token
python crack_traework.py --export         # 导出私密数据 JSON（供 dashboard 粘贴）
python crack_traework.py --import-json FILE   # 从 JSON 文件/粘贴内容导入私密数据
```

| 参数 | 作用 |
|------|------|
| （无） | 默认：从本机 storage.json 解密并写入 secrets.json |
| `--secrets <path>` | 指定 secrets.json 路径（默认仓库根目录） |
| `--force` | 即使已有 key 也重新提取 |
| `--checkin` | 查询今日签到状态 |
| `--claim` | 执行每日签到领取 |
| `--quota` | 查询剩余额度 |
| `--refresh` | 刷新 access token（**只需 secrets.json 里的 refreshToken，无需本地 storage.json**） |
| `--export` | 导出私密数据 JSON |
| `--import-json <FILE>` | 从 JSON 导入私密数据 |

写入 secrets.json 的字段：`trae_work_token` / `trae_work_refresh_token` / `trae_work_user_id`
（secrets.json 已 gitignore，不入库）。

---

## 8. crack_common.py 架构（CRACK_STATUS_HANDLERS 注册表）

`crack_common.py` 提供破解网关的公共能力：

- **tc 解密**：`_detect_enc_type()` / `_decrypt_tc()`，供 crack_traework 复用
- **Trae 状态查询**：额度、签到、token 有效期
- **`CRACK_STATUS_HANDLERS` 注册表**：破解网关状态查询的统一入口

```python
CRACK_STATUS_HANDLERS = {
    "trae-work": trae_status,
    # "codebuddy": codebuddy_status,   # TODO: 待实现
    # "qclaw":     qclaw_status,       # TODO: 待实现
}
```

`get_crack_status(label, secrets)` 是 dashboard 的统一入口：

1. 按 `label` 查注册表，未注册则返回 `supported: false`
2. 从 secrets 读取 `<label 的 _ 形式>_token` / `<...>_refresh_token`，缺失则返回 `configured: false`
3. 调用对应 handler，返回额度 / 签到 / token 有效期，并附带 `supported: true, configured: true`

**扩展点**：未来 codebuddy / qclaw 的签到 / 额度 / 状态查询，只需实现一个
`xxx_status(token, refresh) -> dict` 函数并在注册表登记即可，dashboard 自动展示。
CodeBuddy Pro 用户每日 +100 credits 的签到可仿照 trae-work 模式接入。

server.py 对应 API：

- `GET /api/crack/{label}/status` — 额度 / 签到 / token 状态（调 `crack_common.get_crack_status`）
- `PUT /api/secrets/{label}/bulk` — 批量导入私密 JSON（body: `{"data": {"token":..., "refreshToken":..., "userId":...}}`）

---

## 9. 开源项目参考

以下开源项目为本任务的破解提供了关键算法与思路（**仅作参考，非官方**）：

| 项目 | 贡献 |
|------|------|
| [ZedeX/trae-local-api](https://github.com/ZedeX/trae-local-api) | **tc 解密算法来源**：AES-128-CBC + SHA-512 盐值派生，本文档 2.3 节的密钥派生即出自此项目逆向 |
| [laojichao/trae-local-api](https://github.com/laojichao/trae-local-api) | 上述项目的 fork，**支持 solo 版**（TRAE SOLO） |
| [Oh-My-Trae/trae-db-decrypt](https://github.com/Oh-My-Trae/trae-db-decrypt) | **SQLCipher 数据库解密**（Trae 本地数据库） |
| [linqiu919/trae2api](https://github.com/linqiu919/trae2api) | Trae API 封装（转 OpenAI 兼容 API） |
| [likecu/trae-minimax-client](https://github.com/likecu/trae-minimax-client) | Trae API 封装（minimax 客户端） |
| [guoq1/check1](https://github.com/guoq1/check1) | 第三方签到脚本（非 Trae 官方） |
| [vibe-coding-labs/trae-reverse-engineering](https://github.com/vibe-coding-labs/trae-reverse-engineering) | Trae 逆向分析 |

---

## 10. 排查备忘

### 10.1 关键路径

| 对象 | 路径 |
|------|------|
| CT105（Linux）工作目录 | `/root/shared-workspace/claude-code-proxy` |
| CT105 Python | `.venv/bin/python`（含 cryptography / httpx） |
| Windows 机器 | SSH 免密：`ssh Administrator@192.168.2.177` |
| Windows pwsh | `"C:\Program Files\PowerShell\7\pwsh.exe" -NoProfile -ExecutionPolicy Bypass -File x.ps1` |
| Windows Python | `C:\Users\Administrator\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -X utf8` |
| Trae Work 数据目录 | `C:\Users\Administrator\AppData\Roaming\TRAE SOLO CN\` |

### 10.2 代理日常管理（CT105）

```bash
# 重启代理（8081 冲突时先确认端口）
pkill -9 -f server.py
setsid .venv/bin/python server.py > /tmp/proxy.log 2>&1 < /dev/null &

# 端口对照
# 8081=dashboard/FastAPI  8082=copilot  8084=codebuddy
# 8085=qclaw  8086=trae-work  8090-8094=free/paid
```

### 10.3 CDP 抓包方法（曾用）

- 代理曾通过 CDP（9224 端口）抓包 trae-work renderer 进程
- 请求头里可见 `Authorization: Cloud-IDE-JWT`，是确认认证 header 来源的实证手段

### 10.4 常见坑

1. **trae-work vs trae-ide 混淆**：`C:\Users\Administrator\Trae CN\` 是 **Trae IDE**（不是目标）；
   `C:\Users\Administrator\TRAE SOLO CN\` 才是 **Trae Work**（目标）。目录只差一个词，极易搞混。
2. **token 泄露面**：refreshToken 半年有效，勿外传；secrets.json 已 gitignore，别 `git add .`。
3. **ExchangeToken 设备绑定**：当前裸调成功（BOUND）；换设备后需完整 RSA 签名协议（逆向 main.js 可得）。
4. **dashboard 是 Python f-string**：内嵌 JS 的 `{` / `}` 必须写成 `{{` / `}}`，且不能有反斜杠。

---

## 附：与本文档相关的代码位置

| 文件 | 说明 |
|------|------|
| `crack_traework.py` | tc 解密、提取、签到、额度、刷新、导出/导入 |
| `crack_common.py` | tc 解密公共函数 + `CRACK_STATUS_HANDLERS` 注册表 |
| `server.py` | `_handle_traework` handler（OpenAI ↔ llm_utils_chat 转换）+ `_crack_env_check` trae 分支 + 状态/批量导入 API |
| `config_store.py` | `VALID_HANDLERS` 含 `trae-work` |
| `targets.json` | trae-work target definition（8086、19 个模型 enabled 白名单配置） |
| `trae_work_daily.sh` | cron 每日签到 + token 剩 <3 天刷新 |

---


## 附：协议转换问题排查定位

seed-code 等模型协议转换出问题（空响应/工具调用异常/格式不兼容）时，对照以下文件定位：

- **字节真实协议参考**：`/root/trae-local-api/`（git 源 `ZedeX/trae-local-api`）
  - `src/trae-client.js` → 入参格式（llm_utils_chat 请求构造）
  - `src/openai-format.js` → 出参格式（SSE 事件解析，output 新旧两种形态）
  - `src/agent.js` → 工具调用历史编码（文本化回填，上游无 tool_calls/tool 概念）
- **server.py 转换实现**：`_openai_to_trae_body`（入参）/ `_trae_chunk_to_openai`（出参）/ `_parse_dsml_tool_calls`（seed-code 专有）
- 排查步骤：开 DEBUG（`systemctl edit` 加 `Environment=DEBUG=true`）→ 看 proxy.log 里 `trae-work upstream POST body=` 转换产物 → 对照上面文件找差异 → 查完恢复 INFO

---

## 附：trae-work 回归测试（2026-08-02 建立）

> trae-work 协议复杂（模型形态各异/工具历史/新旧格式），改完相关代码必须完整跑一遍。

| 脚本 | 覆盖 | 运行 |
|------|------|------|
| `test_trae_protocol.py` | 单元：`_openai_to_trae_body` 工具历史文本化/采样透传、`_parse_dsml_tool_calls`（DSML + [Tool Call:] + 平衡括号）、`_trae_chunk_to_openai` 新旧格式。**无网络**，快 | `.venv/bin/python test_trae_protocol.py` |
| `test_trae_work_e2e.py` | 端到端：14 个用例打 8086（模型×协议×历史矩阵），验证 HTTP 200/非空/tool_calls/无标记泄漏/arguments 合法 JSON。**消耗真实 crack 额度** | `.venv/bin/python test_trae_work_e2e.py`（`--only 前缀` 选跑） |

- 用例集合：`scripts/test-cases/trae-work/*.json`（14 个：glm-5.2 / Doubao-Seed-Code × 流式/非流式 × 简单/工具历史/content+tool_calls/多轮/长 system）
- 历史关键回归点：`seed-tool-history`（曾 0 chunks 空响应）、`seed-nonstream-toolhistory`（曾 arguments 混入 reasoning）、`seed-*-tool*`（曾 [Tool Call:] 文本泄漏）
- 排查时临时加流式 `chunk raw=` DEBUG 日志（已内置，`_handle_traework`），看上游原始事件与转换输出
