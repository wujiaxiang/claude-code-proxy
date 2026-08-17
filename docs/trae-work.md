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

### Wave 3/4：打地鼠根因修复 + 官方格式补全（2026-08-04）

**为什么此前堵了又漏（根因）**：
1. `_looks_like_dsml` 是**白名单特征判定**——只认已知标签（DSML/`[Tool Call`/`<tool_call>`），
   模型发明新标签（`<seed:tool_result>`、`<tool_name>`）就不在特征里 → 静默透传。
2. 兜底逻辑**只 WARNING 不拦截**——命中疑似标记但解析失败时 `content_text = text.strip()`
   原样透传，泄漏是必然的，WARNING 只负责事后记录。

**Wave 3（实测抓包，ses_032871f10ffeUFPADwt7q2qizX 会话泄漏）**：
1. **`<tool_call>` XML 子标签变体**（模型从 opencode 历史学到）：
   ```xml
   <tool_call><tool_name>bash</tool_name><parameters>
   <parameter name="command" string="true">cd /x && git status</parameter></parameters></tool_call>
   ```
   `_TOOLCALL_XML_RE` 分支要求块内 `find("{")`（JSON），XML 子标签无 `{` → 解析失败泄漏。
   修复：`_parse_toolcall_subtags()` 解析 `<tool_name>`/`<parameter name=..>` 子标签。
2. **`<seed:tool_result>` 复述块**（模型复述历史工具结果，无闭合标签）：
   ```xml
   <seed:tool_result>
   /root/...targets.json:417: "label": "openrouter",...
   ```
   不在任何检测特征里，静默透传。修复：`_strip_seed_tool_result_blocks()` 剥离——
   纯复述（无后续强标记）整块丢弃；混合结构（复述+正文+新调用）剥开标签保留正文。

**Wave 4（官方格式，vllm 权威来源）**：查 vLLM 官方 parser（`vllm/parser/qwen3.py` +
`vllm/parser/seed_oss.py` + `vllm/tool_parsers/seed_oss_tool_parser.py`）确认
Doubao-Seed-Code 家族（seed-oss）的**原生 XML 语法**是 Qwen3 语法 + seed 包装：
```
<seed:think>...</seed:think>  ← 推理（Qwen3 用 <think>）
<seed:tool_call><function=bash><parameter=command>ls -la</parameter></function></seed:tool_call>
```
关键差异：`<function=name>`/`<parameter=key>` 是**无空格无引号**属性形式，与已支持的
`<function name="..">`（带空格引号）是两套不同语法；外层 `<seed:tool_call>`（带冒号）
与已支持的 `<seed_call>`（无冒号）不同。官方 parser 还容忍：无外层直接 `<function=>`
（fallback）、`</function>` 后连续下一个 `<tool_call>`（未闭合外层）。本轮全部补上
（`_SEED_TOOL_CALL_RE`/`_QWEN_FUNC_RE`/`_QWEN_PARAM_RE` + `_parse_qwen_func_params`）。

**根治设计（不再打地鼠）**：
- `_looks_like_dsml` 补全特征：`<seed:`/`<tool_`/`<parameters>`/`<parameter name=`/
  `<function=`/`<seed:think`/`<think` 等前缀，命中标记不再静默透传
- 兜底改为 **`_strip_strong_tool_markers()`**：命中强工具标签但解析失败时剥离标记块，
  不原样透传；未闭合开标签（`<tool_call>`/`<seed:tool_call>`/`<function=`/think）也截断剥离
- `_strip_seed_tool_result_blocks()` 处理 `<seed:tool_result>` 复述
- 已知局限：混合结构（复述+正文+新调用）无闭合标签无法精确分界，保守只剥开标签
  保留正文（工具调用已正确解析执行，残留的是模型自吐的复述文本非标记泄漏）

**通用意图根治（Wave 5，2026-08-04）——为什么官方格式救不了我们**：
vLLM 官方 parser（`vllm/parser/qwen3.py` + `seed_oss.py`）是**服务端 grammar 约束
解码**——官方推理时强制模型输出 `<seed:tool_call><function=..>`，所以官方"全量 case"
是约束下的唯一格式。而 Trae 的 `llm_utils_chat` 服务端**没有 grammar 引导**，模型
自由生成，从上下文历史学格式（代理文本化的 `[Tool Call:]`、opencode 的 seed 上下文
等），输出空间无限——**枚举官方格式 + 补丁式修变体永远堵不完**。

根治 = 三层防线，把"能不能解析"与"会不会泄漏"解耦：
1. **检测层**：`_TOOL_INTENT_TAG_RE` 通用意图正则——任何 XML 标签只要含工具语义
   关键词（`tool/function/param/invoke/args/call/cmd` 等任意排列、任意前缀/命名空间）
   即判定"疑似工具调用"，不再依赖已知标签白名单。关键词限定在 XML 标签形态内
   （`<...>`），正文里出现 function/tool 等单词不误伤。
2. **解析层**：已知格式（DSML/`[Tool Call:]`/JSON/seed_call/官方 Qwen3/XML 子标签）
   正常解析为 tool_calls 执行。
3. **拦截层**：`_strip_generic_tool_blocks()` 通用剥离——用平衡标签扫描删除任意
   含工具语义关键词的 XML 块（支持嵌套、自闭合、未闭合截断），解析失败的新变体
   标记被剥掉、正文保留，**绝不原样透传**。

保证：模型发明从未见过的新标签也不泄漏（只是解析不出 tool_calls，标记被剥离），
彻底消除"新变体 → 泄漏必现"的因果链。回归测试 Wave 5 覆盖未知变体 + 误伤防护。

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
- 每日任务由**统一调度器** `crack_daily.py` 的 `daily_traework()` 承接（唯一 cron 入口 `scripts/cron/crack_daily.sh`，`0 3 * * *`）：先查 `checkin_status()` 判断今日是否已签到再决定 claim（幂等），并在 access token 剩余 < 2 天时触发刷新。**勿新增独立 cron**——扩展方式见 `crack_daily.py` docstring
  > 历史：旧脚本 `trae_work_daily.sh` 已于 2026-08-05 删除（功能被 `daily_traework()` 完全覆盖且更优，旧脚本无脑 claim 不做幂等检查）

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
| `crack_daily.py` | `daily_traework()` 每日签到（幂等）+ token 剩 <2 天刷新；统一调度入口 |

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

---

## 11. 重大发现：真实客户端已迁移到 `workflow/start` 新协议（2026-08-17）

### 11.1 现象

8086 端口（`llm_utils_chat` HTTP+SSE 接口）自 2026-08-13 起持续返回 `code=4011`
（`We're sorry, your requests have exceeded the rate limit.`），且**与请求频率无关**——
单次、间隔数分钟的孤立请求同样必现。同期 177 上真实 Trae Work 客户端（VSCode 插件）
对话功能完全正常。

### 11.2 排查过程（按时间顺序，均已证伪，仅供参考勿重复）

1. **IDE 版本号过旧**（`0.1.43`/`20260730` → 从 177 真实 `product.json` 取得 `appVersion=0.1.51`，
   构建日期 `2026-08-14`）：更新 `gateways/trae_work.py`（实际生效路径）、`crack_common.py`、
   `crack_traework.py` 三处常量后**问题依旧**，直连上游裸测（绕开代理层）`extra:null`，
   无更多诊断信息。
2. **账号/额度问题**：`--quota` 显示多档权益未耗尽（仅一档 2000/2000 用尽，其余充足）；
   `--checkin` 正常；`trae_work_token` JWT 未过期（有效至 2026-08-28）。**排除**。
3. **设备指纹伪造**：`x-device-id`/`x-machine-id` 硬编码值与 177 上
   `telemetry.machineId` 实测**完全一致**（非伪造）。**排除**。
4. **Header 缺失**：对照文档 §4 清单逐项核对，`_trae_build_headers` 字段齐全。**排除**。

### 11.3 抓包定位真因（四层递进，前三层均失败）

| 方法 | 结果 | 原因（事后判断） |
|------|------|------|
| CDP 渲染进程 Network 域（`--remote-debugging-port`） | 只抓到 `get_session_usage`（账户信息接口），从未见聊天请求 | 聊天流量不在这个渲染进程发起（或者不在这条 fetch/XHR 路径） |
| CDP Node 主进程 hook（`--inspect` + `Runtime.evaluate` 劫持 `http`/`https`/`fetch`） | 抓到 `icube-api.bytedance.net/trae/ping` 心跳（证明 hook 本身有效），但从未见聊天流量 | 聊天流量也不在 Node 主进程的 `http`/`https`/`fetch` 层 |
| Electron `--proxy-server=host:port` 系统级重定向 + mitmproxy | 63 个请求全是遥测域名（`mcs.zijieapi.com` 等），**无一条 `trae-api-cn.mchost.guru`** | `--proxy-server` 只影响 Chromium 网络栈，聊天请求绕过了它 |
| **`mitmdump --mode "local:TRAE SOLO CN.exe"`（WinDivert 驱动，OS 层按进程名重定向，见 §12）** | **✅ 成功**：抓到完整真实流量，含 `POST https://api5-normal.mchost.guru/api/agent/v3/workflow/start` | 唯一能在 OS socket 层无差别拦截、不受应用自身路由逻辑影响的方法 |

**结论（几经修正，以此为准）**：

1. 真实客户端对话走的是全新接口 **`POST https://api5-normal.mchost.guru/api/agent/v3/workflow/start`**（host 也变了，不再是 `trae-api-cn.mchost.guru`），**完全不是**本文档 §3.1、`crack_traework.py`/`gateways/trae_work.py` 一直在用的 `api/agent/v3/llm_utils_chat`。
2. 认证头也变了：真实请求用 **`x-ide-token: <JWT>`**（裸 token，无前缀），不是 `Authorization: Cloud-IDE-JWT <token>`。
3. 新增大量头：`x-bridge-transport: aha`、`x-lgw-req-sdk-type: 3`、`package-type: stable_cn`、`x-ahanet-timeout: 86400`、`x-request-pin`、`x-requested-at`、`x-request-id`、`x-trae-request-id`、`x-lscbd-aid`、`x-lscbd-platform`、`x-ss-dp`、`x-tt-trace-id`、以及三个疑似加密/签名材料头 **`x-helios`/`x-medusa`/`x-neptune`**。
4. **`workflow/start` 的请求体和响应体本身是密文**（不是 gzip，是随机字节流），必须先搞清楚 `x-helios`/`x-medusa`/`x-neptune` 这套加密方案才能构造合法请求——这不是"改几个 header"能解决的，而是要逆向一套专有加密协议。
5. 抓包过程中一度误判"聊天走 `wss://.../ws/v2` 持久 WebSocket"——后来用 Trae **IDE**（非 Trae Work）做对照实验证伪：IDE 在**完全不聊天**、仅空跑的情况下同样会建立这个 `ws/v2` 连接（`aid=711126` 与遥测 SDK 共用的 aid 一致）。**结论修正为**：`ws/v2` 是字节通用的长连接推送通道（类似 Frontier 基建，用于通知/在线状态），**不是**专属聊天传输层——这是本次排查唯一一个先下结论、后被推翻的判断，记录下来避免下次重复踩坑。
6. 尝试寻找"本地明文网关"（怀疑聊天内容先发到本地 sidecar 明文，再由 sidecar 加密转发）：确认 Trae Work 主进程确实监听了额外本地端口 `127.0.0.1:17788`（HTTP，CORS 头 `Access-Control-Allow-Headers: x-jwt-token`），但**用渲染进程 Network 域和 Node 主进程 hook 两条路径都没能捕获到对 17788 的实际调用**——要么调用来自尚未定位的第三个进程，要么 17788 根本跟对话链路无关（更像是扩展宿主/本地 IPC 用途）。此方向未能证实或证伪，如后续要继续深挖，起点是先确认 17788 到底被谁调用（可用 `Sysinternals Handle`/`Process Monitor` 这类工具查看 socket 归属，本次未安装此类工具）。

### 11.4 8086 网关现状与建议

- 现有实现（`llm_utils_chat` + `Authorization: Cloud-IDE-JWT`）**仍然"活着"**：能连通、能鉴权、返回 200，只是被一个持续性的 `code=4011` 限流卡死——大概率是因为它已不是主路径，后端对这条旧路径做了限流处理。
- 改 header/版本号**不会修复**这个问题（已实测：`x-ide-version`/`x-ide-version-code` 改成真实值后 4011 依旧）。
- 若要让 8086 真正可用，需要重写整个上游对接层去适配 `workflow/start`，前提是先破解 `x-helios`/`x-medusa`/`x-neptune` 这套加密方案——工作量远大于当前实现，建议作为独立任务立项，而非在现有代码上小修小补。

> 通用抓包方法论（`mitmproxy local` 模式的具体用法、CDP 自动打字、常见坑）已整理到
> 独立的 §12，本节聚焦本次排查的具体结论。

---

## 12. 通用抓包方法论（适用于 Trae Work / Trae IDE / VSCode Trae 插件等所有 Electron/VSCode 系客户端）

> 本节与具体产品无关，是这次排查沉淀下来的通用步骤，下次抓任意 Electron/VSCode 系
> 客户端的真实网络协议时直接照抄，不用重新摸索。

### 12.1 环境前提

- 目标客户端装在 Windows 机器上，本例是 `192.168.2.177`（`Administrator` 免密 SSH，见
  §10.1），Python venv 用的是 `C:\Users\Administrator\AppData\Local\hermes\hermes-agent\venv`
- 需要该 venv 装有 `mitmproxy`（`pip install mitmproxy`，自带 `mitmproxy-windows`/
  `pydivert`/`mitmproxy_rs` 依赖，装一次即可，7 月 8 号就装过，确认存在再决定要不要重装）
- SSH 是非交互会话（无桌面），**GUI 应用不能直接 `start`/`cmd /c` 启动**——非交互 session
  里启动的 GUI 进程要么静默失败要么无法渲染，见 12.2

### 12.2 关键坑：非交互 SSH 会话启动 GUI 应用

`ssh user@host "cmd /c start ..."` 或 `Start-Process` 在纯 SSH 会话里启动 GUI 应用
**大概率静默失败**（无报错、进程不存在），因为 SSH 会话默认落在 session 0（services），
GUI 应用需要真实的交互式桌面 session。

**解法**：用 `schtasks` 创建一次性任务，加 `/it`（interactive token）参数，让任务
以当前登录用户的交互式 token 运行，这样启动的进程会出现在真实桌面 session
（`query session` 能看到的 `rdp-tcp#N` 这一行）：

```
schtasks /create /tn <任务名> /tr "<完整命令行，含参数>" /sc once /st 23:59 /ru <用户名> /it /f
schtasks /run /tn <任务名>
:: 等几秒确认进程起来后
schtasks /delete /tn <任务名> /f
```

- `/tr` 里的可执行文件路径含空格时，整个 `/tr` 值要用**外层双引号**包住，路径本身
  再套一层转义引号（`\"...\"`）；嵌套引号超过 2-3 层容易解析出错，**优先把命令写成
  一个 `.bat` 文件用 `Write` 工具在本地写好、`scp` 传过去，再 `schtasks /tr` 直接指向
  这个 `.bat` 文件**，比在命令行里堆转义可靠得多（本次踩过 `cmd /c "..."` 嵌套转义
  失败的坑，改用 `.bat` 文件后一次成功）
- 每次改动完记得清理任务（`schtasks /delete`）避免残留

### 12.3 三种抓包手段的优先级（按成功率排序）

| 优先级 | 方法 | 命令 | 覆盖范围 | 何时用 |
|---|---|---|---|---|
| 1（首选） | **mitmproxy `local` 模式**（WinDivert，按进程名重定向） | `mitmdump --mode "local:进程名.exe" -w out.flow` | OS socket 层，**不管请求从 Node/Rust/Chromium 哪一层发出都能截获**，唯一在本次排查里成功抓到真实聊天请求的方法 | 默认首选，不需要额外装 Npcap/Wireshark/Proxifier（这三个都尝试过，均因需要交互式会话/内核驱动装不上或卡住） |
| 2 | CDP 渲染进程 Network 域 | `--remote-debugging-port=9222` + `chrome://inspect` 或直接连 `ws://127.0.0.1:9222/devtools/page/<id>` 用 `Network.enable` | 只能看到**这个渲染进程**自己发起的 fetch/XHR，看不到其他进程、看不到 Node 主进程、看不到 Rust 核心 | 快速验证某个 UI 交互对应的接口调用时够用，但**看不到就不代表没有**——只能证明"这层没发"，不能证明"整个应用没发" |
| 3 | CDP Node 主进程 hook | `--inspect=9223` + `Runtime.evaluate` 注入代码劫持 `http`/`https`/`fetch` | 只能看到 **Node 主进程**（Electron browser process）里经这三个 API 发出的请求 | 同上，局限明显，且本次两次尝试聊天请求都没抓到（抓到了心跳/ping，证明 hook 有效，但主链路不在这层） |

**核心教训**：方法 2、3 都是"在某一层挂钩子"，只能证明"这一层有没有"，**证伪能力弱**——
只要应用有多进程/多语言运行时（Electron 主进程 + 渲染进程 + Rust sidecar，是这类
"AI IDE"的常见架构），关键流量完全可能绕过任意一层的钩子。方法 1（OS 层按进程名
拦截）没有这个盲区，才是应该优先尝试的手段，能省下大量在方法 2/3 上反复摸索的时间。

### 12.4 用 CDP 自动打字发消息（不用等真人手动点）

如果需要在抓包窗口内主动触发一次对话（而不是等人工发消息、屡屡错过窗口），
可以用 CDP 直接操作 UI：

1. 先枚举候选输入框：
   ```js
   Array.from(document.querySelectorAll('textarea, [contenteditable="true"], input[type="text"]'))
     .map(el => ({tag: el.tagName, cls: el.className, visible: !!(el.offsetWidth||el.offsetHeight)}))
   ```
2. **不要用 `document.execCommand('insertText', ...)`**——现代富文本编辑器（Lexical/Slate
   类框架）不认这套，`insertText` 会静默失败（`innerText` 检查出来是空的），只是没报错
   看起来像成功了，容易误判
3. 改用 CDP **`Input.insertText`**（原生输入事件，走 Chromium 真实输入管线，兼容性好
   得多）+ `Input.dispatchKeyEvent`（Enter 键）：
   ```python
   await send(ws, N, "Input.insertText", {"text": "要发的内容"})
   await send(ws, N+1, "Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
   await send(ws, N+2, "Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
   ```
4. 发送前先 `el.focus()`（Runtime.evaluate 里对目标元素调用），否则 `Input.insertText`
   可能落到错误的（或没有）焦点元素上

### 12.5 常见反复踩的坑（复查清单）

1. `mitmdump --mode local:进程名` 的进程名传错（比如手滑传了 `help` 想看帮助），会
   **静默"成功启动"并挂起等待匹配进程**，不报错、不打印用法——先用确认能匹配到的
   真实进程名验证有输出，再排除"参数写错"这个可能
2. `ssh -f -N -L` 端口转发命令本身会立刻返回（正常），但如果因为端口冲突或权限问题
   没转发成功，本地 `curl` 会显示 connection refused——每次建隧道后**必须** `curl` 验证
   一次再往下走，不要假设转发一定成功
3. 同时启动两个重量级 Electron 应用（比如 Trae IDE + Trae Work）来做对照实验，
   叠加抓包工具的资源/驱动占用，**可能导致其中一个意外崩溃退出**——如果要开第二个
   应用做对比，做完立刻关掉，并检查原来那个还活着，不活着立刻拉起来，不要等用户
   发现"应用消失了"才处理
4. 一旦从渲染进程或 Node 主进程都没抓到目标流量，**不要无限重试同一层**——换成
   方法 1（OS 层 `mitmdump local` 模式）大概率能解决，重试次数控制在 2-3 次内没
   进展就换手段
5. 抓到的响应体如果是"看起来像乱码但不是明显的 gzip/deflate 头"，大概率是**真正的
   加密密文**，不是编码问题——不要浪费时间试各种字符集解码，直接确认协议已加密，
   记录相关的自定义 header（本例是 `x-helios`/`x-medusa`/`x-neptune`），作为后续
   加密方案逆向的输入

### 12.6 多目标横向验证结果（2026-08-17，同一台机器装了 Trae IDE / Trae Work / VSCode+MarsCode 插件三个客户端）

| 目标 | 结果 |
|---|---|
| **Trae Work**（TRAE SOLO CN.exe） | ✅ 成功抓到真实协议（见 §11.3）：`POST https://api5-normal.mchost.guru/api/agent/v3/workflow/start` |
| **Trae IDE**（Trae CN.exe） | ✅ 成功抓到：**同样是** `POST https://60.5.20.200/api/agent/v3/workflow/start`（host 是负载均衡的另一个 IP，路径完全一致），配套 `create_agent_task`/`sync_history_state`/`query_history_state`/`chat_mode`/`batch_get_detail_param` |
| **VSCode + MarsCode 插件**（Code.exe，扩展 ID `marscode.marscode-extension`，官方名 **TraeCode**） | ⚠️ 定位到真实域名 **`a0ai.marscode.cn`**，但**证书锁定（cert pinning）**——该域名在 TLS 握手阶段就拒绝 mitmproxy 的（已加入 Windows 信任库的）CA 证书，`http.proxy` 系统代理设置也绕不过，无法解密内容。三个客户端里唯一真正拦不到明文的 |

**结论**：`workflow/start` 是**字节 Trae 全系产品线通用的新协议**（IDE 和 Work 共用同一套
`v3/agent` API 族），不是 Trae Work 专属的变化——这进一步说明旧的 `llm_utils_chat`
在整条产品线里都已经过时，8086 网关要跟上必须整体迁移到新协议（并解决 §11.3 提到的
请求体加密问题），不存在"换个产品线接口就没这问题"的取巧空间。

**VSCode 侧的关键突破与瓶颈（2026-08-17，第二轮补充排查）**：

1. **突破**：VSCode/Electron 有独立于系统代理的 `http.proxy`/`http.proxyStrictSSL`/
   `http.proxySupport` 设置（`%APPDATA%\Code\User\settings.json`），专门用于让扩展宿主
   进程（Extension Host，独立 Node.js 进程）的网络请求走指定代理——这比 `mitmdump local`
   模式更精确命中扩展这一层，且**不需要驱动**。设置示例：
   ```json
   {
     "http.proxy": "http://127.0.0.1:8888",
     "http.proxyStrictSSL": false,
     "http.proxySupport": "override"
   }
   ```
   改完必须**彻底 `taskkill /F /IM Code.exe /T` 后重新启动**（不是重载窗口）才生效，
   同时要保证 `mitmdump -p 8888`（**普通正向代理模式，不是 `local` 模式**）在整个
   VSCode 重启过程中不中断——本次踩过的坑：中途 `mitmdump` 掉线过几次导致 VSCode
   内部判定"代理不可用"进而退化直连，即使之后代理恢复也不会重连，必须让代理先
   稳定运行、再重启 VSCode，顺序不能反
2. **瓶颈**：即使拿到明文代理链路，`a0ai.marscode.cn`（高度疑似 MarsCode 真正的 AI
   接口域名）在 TLS 握手阶段就报 `Client TLS handshake failed ... does not trust
   the proxy's certificate`——说明这个域名的请求走的是**证书锁定的独立 TLS 客户端**
   （不受 VS Code 全局 `http.proxyStrictSSL: false` 影响，这个设置只对标准 Node.js
   `https` 模块生效，证书锁定客户端绕过了这层配置），比 Trae Work/IDE 的防护更强

**Trae IDE 抓包补充方法**：VSCode 系客户端的 AI 聊天面板是**双层 iframe 的 webview**
（`document.querySelectorAll('iframe.webview')`能找到外层 iframe，但内层内容因跨域
无法用 `Runtime.evaluate` 直接读取/操作）。本次没有解出精确坐标去自动点击输入框，
改为请人工在窗口内手动发送——这个针对 VSCode webview 的自动化输入问题本次**未解决**，
如果下次需要在 VSCode 系客户端里自动触发聊天，需要用 CDP `Page.createIsolatedWorld`
或类似手段拿到 webview 内层 frame 的 `executionContextId`，直接在那个 context 里
跑 `Runtime.evaluate`，而不是在最外层 workbench 页面的 context 里执行。

### 12.6.1 排查中验证过的开源项目（均非可用捷径，记录以免重复调研）

| 项目 | 描述 | 验证结论 |
|---|---|---|
| `Ttungx/trae-solo-local-api`（ZedeX 分支，7 星，2026-08-11 更新） | 专门声称适配 TRAE SOLO/Work 的本地 OpenAI 兼容网关 | ❌ 实测 `src/trae-client.js` 第 474 行调用的仍是**旧接口** `api/agent/v3/llm_utils_chat`，跟我们现有 8086 网关用的是同一个已被限流的接口，**不解决问题**。`src/crypto.js` 也只是它自己本地存储用的通用 AES-GCM 工具（随机自生成 key），与 `x-helios`/`x-medusa`/`x-neptune` 这套线上协议加密**完全无关**，容易被名字误导 |
| `DASungta/trae-proxy`（Go 项目，49 星，2026-07-31 更新） | "爆锤 Trae 不支持自定义模型接入" | ❌ 方向完全相反——这是让 Trae **反过来调用你自己的模型后端**（自定义模型接入 Trae 内部使用），不是把 Trae 自己的对话能力代理给外部消费，跟我们的目标南辕北辙 |

**教训**：网络上（含 AI 生成的建议）推荐的"现成开源方案"必须先验证再采信——
方法是直接读目标仓库 `src/` 下实际发请求的那个文件（本例是 `trae-client.js`），
grep 真实调用的端点路径，而不是只看 README 的功能描述和更新时间。

### 12.6.2 下一步方向（未实施，仅记录，供后续继续时参考）

鉴于新协议（`workflow/start` + `x-helios`/`x-medusa`/`x-neptune` 签名/加密）没有
现成开源实现，且证书锁定（VSCode 侧的 `a0ai.marscode.cn`）挡住了纯抓包路线，
**更可行的方向是"寄生注入"而非"逆向加密算法"**：

- 已验证可行的基础能力：`--inspect=<port>` + CDP `Runtime.evaluate` 可以在**真实
  登录态的 Trae 主进程内**执行任意 JS（本次用它 hook 过 `http`/`https`/`fetch`，
  也验证过能捕获到真实的 `icube-api.bytedance.net/trae/ping` 心跳请求）
- 思路：不逆向加密算法，而是在运行时找到 `app.asar` 解包后源码里**真正构建
  `workflow/start` 请求体（含签名）的那个函数**，直接在已认证的进程上下文里调用它、
  传入自己的消息内容——因为签名/加密是该函数自己算的，我们不需要知道算法细节
- 关键前置步骤（未做）：`app.asar` 解包（`npx asar extract app.asar out/` 之类），
  在解包出的 JS 里搜索 `workflow/start`、`x-helios` 等关键字符串定位目标函数，
  再决定怎么在运行时调用它（可能需要拿到闭包内的 auth/session 对象作为参数）
- 工作量评估：比"改 header/版本号"大得多，但比"逆向签名算法从零实现"小得多，
  是相对最优的性价比路径，只是本次会话未执行，留待下次专项处理

### 12.7 收尾清理（每次抓包后必做）

- `taskkill /F /IM mitmdump.exe`（停止抓包，flush 输出文件）
- 目标客户端如果被临时加过 `--remote-debugging-port`/`--inspect`/`--proxy-server`
  等调试参数重启过，**验证完立刻按无参数重新拉起**，恢复原状
- 删除临时下载的安装包/日志/`.flow` 抓包文件/`.bat` 脚本（`del /Q ...`）
- 删除所有临时 `schtasks` 任务（`schtasks /delete /tn ... /f`）——一次性任务如果中途
  被打断可能忘删，每次收尾前 `schtasks /query` 过一遍确认
- 关闭本地建立的 SSH 端口转发进程（`pkill -f "L <本地端口>:127.0.0.1:<远程端口>"`）
