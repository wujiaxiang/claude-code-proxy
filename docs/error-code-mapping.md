# 上游错误码映射与限流转换

> 本文档说明 claude-code-proxy 如何把上游（qclaw / nvidia / openrouter / copilot 等）返回的错误
> 标准化为下游客户端（opencode / Claude Code 等）能识别并重试的 **HTTP 429 + `Retry-After`**
> 或流式 **SSE `error` 事件**。排查「请求中断 / `UnknownError` / 不重试」类问题时，先读本文。

---

## 1. 为什么需要这层转换

不同上游的错误返回格式**不统一**，而下游 OpenAI SDK 只认标准错误：

| 上游 | 真实错误返回 | 下游若不转换的结果 |
|------|-------------|-------------------|
| **nvidia**（8091, passthrough） | 裸字符串 `ResourceExhausted: Worker local total request limit reached (32/32)`，HTTP 状态可能是 200（伪成功）或 429 | 200+裸串 → SDK 解析失败 → `UnknownError`；429 无 `Retry-After` → 无退避 |
| **openrouter**（8090, passthrough） | `{"code":502,"message":"Upstream error from Nvidia: ResourceExhausted: ...","metadata":{...}}`，HTTP 502 | 502 透传 → SDK 当作 5xx，多数不重试（或盲目重试稳定错误） |
| **qclaw**（8085, handler=qclaw→LiteLLM） | litellm 抛 `RateLimitError` / `ResourceExhausted` 异常 | 异常被兜成 HTTP 500 + `UnknownError` → 不重试 |
| 标准 OpenAI 兼容 | `{"error":{"type":"rate_limit_error",...}}` | 已标准，无需转换（仍识别） |

**核心目标**：把所有「限流 / 资源耗尽」类错误统一转成 **HTTP 429 + `Retry-After`**（非流式）
或 **SSE `error` 事件**（流式），让 opencode 自动指数退避重试。

---

## 2. 配置表（数据驱动，单点维护）

所有错误识别都基于一张映射表，新增网关只需追加一行，**无需改逻辑**：

```python
# server.py
_VENDOR_ERROR_MAPS = [
    # 字段特征(子串),      目标HTTP状态, SSE error type,     说明
    ("ResourceExhausted", 429, "rate_limit_error", "qclaw/nvidia/openrouter 资源耗尽（并发限制）"),
    ("Worker local total request limit reached", 429, "rate_limit_error", "nvidia/openrouter 本地并发已满"),
    ("rate_limit_exceeded", 429, "rate_limit_error", "OpenAI 标准限流码"),
    ("too_many_requests", 429, "rate_limit_error", "OpenAI 标准限流码"),
    ("RateLimitError", 429, "rate_limit_error", "litellm 限流异常类名"),
]
```

匹配规则（`_map_upstream_error(body_text)`）：
1. 先按 `_VENDOR_ERROR_MAPS` 做**子串匹配**（大小写敏感）——覆盖无标准 `error` 信封的格式
   （openrouter 的 `message` 字段、nvidia 裸字符串）；
2. 回退到标准 OpenAI 信封 `{"error":{...}}` 含 `_VENDOR_ERROR_PATTERNS` 特征。

返回 `(http_status, sse_error_type)` 或 `None`（不可识别）。

> **扩展方法**：遇到新网关的限流错误体，先抓一段原始 body，挑一个稳定的子串，往
> `_VENDOR_ERROR_MAPS` 加一行即可。若目标状态不是 429（如某些网关用 503 表示过载），
> 改对应行的第二列。

---

## 3. 两条处理路径

代理有两条错误转换路径，对应不同上游接入方式：

### 路径 A：透传路径（`_handle_target_request`，覆盖 nvidia/openrouter/codebuddy 等 passthrough 端口）

- **非流式**（约 3857 行）：上游响应读 body 后调用 `_map_upstream_error`；
  命中则写 `HTTP/1.1 <target_status> ... Retry-After: <_VENDOR_RETRY_AFTER>`。
- **流式**（`passthrough_stream`，约 5321 行）：`status>=400` 时读 body 调 `_map_upstream_error`；
  命中则 yield SSE `{"error":{"type":<err_type>,"message":...}}` + `data: [DONE]`，**不重试**；
  非限流 5xx 才重试 3 次；其他 4xx 直接透传原始流。

### 路径 B：LiteLLM 翻译路径（`openai_chat_completions`，覆盖 qclaw/copilot/anthropic/gemini）

- litellm 抛异常被 `except Exception` 捕获（约 5302 行对应位置）；
- `_is_rate_limit_error(exc)` 先按 `litellm.RateLimitError`/`RouterRateLimitError` 类型判断，
  再按 `_VENDOR_ERROR_MAPS` 的 keyword 兜底；
- 命中则 `raise HTTPException(status_code=429, headers={"Retry-After": ...})`；
  非限流异常保持原 500。
- 流式（`_litellm_oai_stream`）：迭代时抛限流异常 → yield SSE `error` 事件 + `[DONE]`；
  非限流异常 `raise`（保持原行为：流中断）。

---

## 4. 排查指南（遇到「不重试 / UnknownError」时）

1. **确认端口与 handler**：看 `targets.json`，该端口 `handler` 是 `passthrough`（走路径 A）
   还是 `qclaw`/`copilot`/`gemini`（走路径 B via LiteLLM）。
2. **抓原始上游响应**：在 `proxy.log` 搜该端口的 `HTTP <status>: <body>` 行（非流式）
   或流式路径的 `upstream <status>`；确认上游到底返回了什么（状态码 + body 形态）。
3. **对照配置表**：原始 body 里是否含 `_VENDOR_ERROR_MAPS` 中某个子串？
   - 含 → 应该已被转 429/SSE error；若没转，检查是否走了预期路径（见下）。
   - 不含 → 在 `_VENDOR_ERROR_MAPS` 加一行新子串，重启生效（热重载不支持此常量，需 `systemctl restart`）。
4. **常见漏网情况**：
   - 上游返回 **HTTP 200 + 错误 body**（伪成功，如 nvidia 裸串 200）：路径 A 非流式
     `status>=400` 分支**不会**进，但 `_map_upstream_error` 仍能在 3857 行命中并转 429——已覆盖。
   - **流式 200 + 错误**：流式路径只在 `status>=400` 时读 body，若上游 200 伪成功则无法拦截
     （需在 `_handle_target_request` 流式分支另加 body 嗅探，当前未做；如遇此场景再补）。
   - **路径 B 的 500 而非 429**：确认异常是否被 `_is_rate_limit_error` 识别；litellm 包装的
     异常 message 是否含映射表 keyword。
5. **重启**：改 `_VENDOR_ERROR_MAPS` 属代码常量，`sudo systemctl restart claude-code-proxy`
   （不要手动 kill + nohup）。

---

## 5. 测试

`test_error_code_mapping.py` 覆盖：
- `_vendor_body_retryable` / `_map_upstream_error` 识别 openrouter（ResourceExhausted / 免费池 rate-limited 文案）/ nvidia / 标准 OAI 等限流格式；
- LiteLLM 路径限流 → 429 + `Retry-After`；非限流 → 500 不变。

运行：`.venv/bin/python test_error_code_mapping.py`

---

## 6. 设计约束（勿违反）

- 只用子串匹配，不引入正则复杂度；新增规则优先加表项而非改函数。
- 非限流错误（5xx 真实故障、4xx 鉴权/参数）**不**误判为 429。
- 流式下 headers 已发送无法改 HTTP 状态，限流只能靠 SSE `error` 事件传达。
- 不改 `targets.json` / `secrets.json`；错误映射表当前硬编码在 `server.py`（未来若需按端口定制，
  可下沉到 `targets.json` 每 target 的 `errorMaps` 字段，再在 `_map_upstream_error` 里合并）。
