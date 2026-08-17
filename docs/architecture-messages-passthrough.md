# Architecture — Messages Protocol Passthrough

> **Status:** Converged across Rounds 1–3 of the design discussion.
> **Audience:** plan agent (turn into a work plan).
> **Scope:** the 8081 `/v1/messages` → `protocol=="messages"` passthrough branch in `server.py`.
> **Grounded in:** `server.py`, `gateways/errors.py`, `gateways/messages_contract` (proposed),
> `anthropic_convert.py`, `anthropic_stream_convert.py`, `targets.json`.

---

## 1. Problem & scope

Design a robust **messages-protocol passthrough** for `claude-code-proxy` that handles:

1. **Field filtering** — drop/adapt request fields per endpoint without breaking clients.
2. **Error standardization** — normalize heterogeneous upstream errors into Anthropic's error envelope, consistently across stream and non-stream.
3. **Compatibility with various Anthropic-compatible endpoints** — local loopback targets today; external endpoints tomorrow.

The relevant code path is the **`protocol=="messages"` branch** (`server.py:2045-2134`). It forwards to a
local (or, once auth is fixed, external) Anthropic-compatible target and today:

- filters the request by a **hardcoded allowlist redefined per call**,
- forwards **all inbound headers**,
- echoes the response **byte-for-byte** (no validation).

The OpenAI **translation** branch (`anthropic_convert.py` + `anthropic_stream_convert.py`) is a *separate*
path. It is touched only for the reasoning_content echo fix (§6.3) and is otherwise out of scope.

---

## 2. Current-state facts (evidence)

| # | Fact | Location |
|---|------|----------|
| 1 | Request filtered by a **hardcoded allowlist redefined per call**: `{model, max_tokens, messages, system, temperature, top_p, top_k, stop_sequences, stream, tools, tool_choice, metadata}`. **`thinking` is omitted** → extended thinking silently disabled. | `server.py:2051-2054` |
| 2 | Response passed **raw** (no validation): non-stream `server.py:2132-2134`; stream raw byte-echo `server.py:2101-2105`. | `server.py:2101-2134` |
| 3 | **Error standardization (429-only)** lives in `gateways/errors.py` (`_VENDOR_ERROR_MAPS`, `errors.py:19-26`) via `_map_upstream_error`. Applied to the OpenAI path (`server.py:1615`, `1536-1549` embedded-200) and the messages **non-stream** branch (`server.py:2110`) — **but NOT the messages stream branch** (`server.py:2101-2105`). | `gateways/errors.py`, `server.py` |
| 4 | **Header handling is asymmetric**: OpenAI branch calls `_resolve_auth(headers, target=target)` (`server.py:1503`); messages-passthrough forwards *all* inbound headers except hop-by-hop (`server.py:2061-2067`) and **never injects downstream auth** → only works against loopback targets accepting the dummy key. | `server.py:1503`, `server.py:2061-2067` |
| 5 | **`normalizeSse`** (`server.py:1527-1532`) is **OpenAI-frame-specific** (strips empty `tool_calls`/`content` from `choices[].delta`); applied to OpenAI-path targets only. | `server.py:1527-1532` |
| 6 | **Fail-open is a hard rule**: `anthropic_stream_convert.py:185-186` ("解析失败绝不中断流") and normalizeSse ("解析失败一律原样透传不吞帧"). | `anthropic_stream_convert.py:185-186` |
| 7 | **Tests:** only `test_v1messages_lock.py` covers the translation branch; the passthrough branch has **no regression test**. | `tests/` |

---

## 3. Resolved disagreements (decisions)

**D1 — Passthrough vs Translation → keep passthrough THIN (Option A).**
A canonical envelope / 4-stage pipeline (proposed by Artistry & Unspecified-high) re-serializes both
directions → field loss + latency + bug surface, and **violates the hard fail-open rule**
(`anthropic_stream_convert.py:185-186`). The downstream is already Anthropic-compatible, so no
translation is needed. *Refinement:* add per-endpoint capability profiles as an **allowlist/filter**, not
re-serialization.

**D2 — fail-open vs fail-closed → tri-state by field type.** (Self-correction: an earlier "blanket
fail-open" stance was too blunt.)
- **Security/credential fields** (`authorization`, `x-api-key`, `cookie`, `proxy-authorization`): **fail-closed** — strip client secrets, inject only from `secrets.json`.
- **Unknown OPTIONAL fields:** **fail-open** — pass through (endpoints vary; dropping breaks them).
- **Semantic capability fields** (`thinking`, `top_k`, `tool_choice` modes): **capability-driven** — drop/translate if the endpoint profile says unsupported, pass if supported.

**D3 — Architecture scope → (A) incremental + thin capability-profile extension.**
No rearchitecture, no canonical envelope, no merge of the 3 convert modules (`anthropic_convert.py:1-35`
documents different call shapes; share a constant, don't unify).

---

## 4. Blind spots folded in

- **reasoning_content echo → PRIORITY #1** (translation-path; AGENTS §10.12). Addressed in §6.3.
- **Auth injection gap** → `_resolve_auth` per endpoint (§6.4).
- **Security** → SSRF egress filter + credential-leak scrubbing (§6.5).

---

## 5. Design principles

1. **Thin passthrough:** byte-stream forward/echo; no full re-serialization.
2. **Fail-open default; fail-closed only for secrets.**
3. **Reuse, don't rebuild:** `targets.json`, `secrets.json`, `_resolve_auth`, `gateways/errors.py`, existing convert modules.
4. **Config-driven, hot-reloaded:** all capability flags live in `targets.json` (mtime 2s reload).
5. **Lenient response validation:** backfill required fields; optional unknown-strip — never a full rebuild.

---

## 6. Detailed design

### 6.1 Field filtering (resolves D1, D2, Finding #1)

- **Centralize** the hardcoded allowlist into a shared constant
  `gateways/messages_contract.py::_MESSAGES_ALLOWED_FIELDS`, imported by both passthrough and translation
  paths (kills per-call redefinition + 3-module duplication).
- **Add `thinking`** to the allowlist (or gate by profile) — closes the silent extended-thinking drop.
- **New `messagesProfile` per target in `targets.json`** (optional, config-only, hot-reloaded):

  ```json
  {
    "label": "someEndpoint",
    "handler": "passthrough",
    "messagesProfile": {
      "supportsThinking": true,
      "supportsTopK": false,
      "toolChoiceModes": ["auto", "none", "tool"],
      "maxTokensCap": 32000
    }
  }
  ```

  Request filtering becomes profile-driven: drop fields the profile marks unsupported (e.g., `top_k` when
  `supportsTopK:false`); pass the rest (fail-open for unknown-optional).
- **Tri-state enforcement** at the filter: security fields stripped+injected (§6.4); unknown-optional
  passed; semantic fields gated by `messagesProfile`.

### 6.2 Error standardization (resolves Finding #2, #3)

- **Broaden `gateways/errors.py`** beyond 429 to Anthropic's real `error.type` enum and auth-expiry:

  ```python
  _VENDOR_ERROR_MAPS += [
      ("authentication_error", 401, "authentication_error", "auth failure"),
      ("9002",                  401, "authentication_error", "QClaw upstream auth expired"),
      ("context_length_exceeded", 400, "invalid_request_error", "ctx len"),
      ("invalid_request_error", 400, "invalid_request_error", "OpenAI invalid req"),
      ("not_found",             404, "not_found_error", "model/resource not found"),
      ("overloaded",            503, "overloaded_error", "upstream overloaded"),
  ]
  ```

- **Apply to BOTH messages paths:**
  - Non-stream already calls `_map_upstream_error` (`server.py:2110`) — keep + broaden.
  - **Stream path (`server.py:2101-2105`) currently applies NONE** — add the same mapping + **embedded-200 / in-SSE error detection** (today only OpenAI path, `server.py:1536-1549`).
- **Messages-stream error guard (NOT normalizeSse):** `normalizeSse` is OpenAI-frame-specific; the
  messages-passthrough downstream emits **Anthropic-format** SSE (`event: message_start`…). Applying it is
  structurally wrong. Add a **lightweight, config-gated Anthropic-frame error guard**: intercept
  `event: error` / malformed terminal frames, translate via the extended `errors.py` map, **pass everything
  else through unchanged** (fail-open). *(Self-correction: an earlier "apply normalizeSse to messages
  stream" suggestion was wrong and is withdrawn.)*

### 6.3 reasoning_content echo bug — PRIORITY #1 (Focus 1, Blind Spot #1)

- **Scope:** lives in the **translation path** (OpenAI `reasoning_content` ↔ Anthropic `thinking`), NOT the
  passthrough branch (downstream is Anthropic-compatible, where `reasoning_content` isn't a spec field). The
  passthrough branch must only **preserve `thinking` content blocks** (already inside `messages`, not the
  dropped top-level `thinking` param).
- **Fix:** on the translation path, ensure the buffered/forwarded assistant transcript carries
  `reasoning_content` back to the upstream requiring it (multi-turn tool-call loop). Thin "preserve
  assistant reasoning" rule — **not** a canonical envelope. This is the #1 already-diagnosed production bug
  (AGENTS §10.12) and must be implemented first.

### 6.4 Auth injection (Focus 2, Blind Spot #2)

- The passthrough branch (`server.py:2061-2067`) forwards all inbound headers and never calls
  `_resolve_auth`. Fix (thin, ~3 lines):

  ```python
  # messages-passthrough branch: replace "forward all headers" with
  _fwd_headers = {"content-type": "application/json", "host": f"127.0.0.1:{_fwd_port}"}
  _fwd_headers.update(_resolve_auth(raw_request.headers, target=target))  # same fn as OpenAI branch, server.py:1503
  ```

  Reuses `secrets.json`/`targets.json`; enables real external endpoints without new architecture; enforces
  the D2 security fail-closed policy (client secrets stripped, per-endpoint creds injected).

### 6.5 Security blind spots (Blind Spot #3)

- **SSRF:** today targets `127.0.0.1:{port}` (loopback). If external endpoints enabled via §6.4, add an
  **egress filter** at the transport/`_resolve_auth` layer + block internal ranges (`169.254.169.254`,
  `10/8`, `192.168/16`, `172.16/12`) and `*.internal`.
- **Credential leakage:** fail-closed on auth headers (§6.1/§6.4); never log secret values; **scrub upstream
  error bodies** before reflecting to client (don't echo upstream `api_key`/tokens in error `message`).
- **Prompt-injection:** passthrough must not mutate `messages` content; reflected upstream error text is
  non-executable (displayed only) — acceptable, but keep it out of any request we build.

### 6.6 Tests (resolves Finding #4)

Add `tests/test_messages_passthrough.py` (today only `test_v1messages_lock.py` covers translation):

- `thinking` field preserved (not dropped) when profile allows.
- Error standardization on **both** stream and non-stream (429 + broader enum + embedded-200).
- Unrouted model → 404.
- Auth injection: external target gets `_resolve_auth` creds, client secrets stripped.
- reasoning_content echo preserved across multi-turn on translation path.
- Capability profile drops unsupported field (e.g., `top_k` when `supportsTopK:false`).

---

## 7. Explicitly NOT doing

- No canonical envelope / 4-stage transform pipeline (over-engineering; breaks fail-open).
- No merging the 3 convert modules.
- No applying `normalizeSse` to Anthropic-format messages streams.
- No global `top_k` drop / `system` flatten (would break capable endpoints).

---

## 8. Priority-ordered implementation plan (for the plan agent)

1. **reasoning_content echo fix** (translation path) — #1, already-diagnosed (AGENTS §10.12).
2. **Auth injection** via `_resolve_auth` + **SSRF egress guard**.
3. **`thinking` allowlist fix** + centralize allowlist constant (`gateways/messages_contract.py`).
4. **Error standardization extended to messages stream** + broader enum (reuse `gateways/errors.py`).
5. **Anthropic-frame error guard** on messages stream (config-gated, fail-open).
6. **Per-endpoint `messagesProfile`** in `targets.json` + tri-state filtering.
7. **Passthrough-branch regression tests** (`tests/test_messages_passthrough.py`).

---

## 9. Open risks / notes

- If external endpoints are enabled, the SSRF guard (§6.5) becomes mandatory, not optional.
- `messagesProfile` defaults to "fully capable" for backward compatibility (no behavior change for existing
  local targets until profiles are explicitly set).
- Response-side validation should stay lenient to avoid breaking unknown-but-valid endpoint extensions.

---

---

## 10. Implementation Results (2026-08-17, Hyperplan Session)

### 10.1 完成状态（10/10 tasks）

| 任务 | 描述 | 文件 | 状态 |
|------|------|------|------|
| T1 | reasoning_content echo bug 修复 | `anthropic_convert.py` | ✅ |
| T2 | Auth injection（`_resolve_auth` + fail-closed） | `server.py` | ✅ |
| T3 | SSRF/egress guard（`_is_internal_host` + IPv6） | `server.py` | ✅ |
| T4 | Centralized allowlist + `thinking` 字段 | `gateways/messages_contract.py` | ✅ |
| T5 | `messagesProfile` 能力门控 | `gateways/messages_contract.py`, `config_store.py`, `docs/architecture.md` | ✅ |
| T6 | Broadened error maps（7种 Anthropic error.type） | `gateways/errors.py` | ✅ |
| T7 | Stream path 错误标准化 | `server.py` | ✅ |
| T8 | Config-gated Anthropic 帧错误守护 | `server.py` | ✅ |
| T9 | 完整回归测试套件（48个测试） | `test_messages_passthrough.py` | ✅ |
| T10 | 安全审查（发现并修复 IPv6 SSRF 绕过） | `server.py` | ✅ |

### 10.2 关键架构决策

1. **保持 passthrough "薄"**：字节流 + header 策略 + 最小验证，不引入规范信封或 4 阶段转换管道
2. **三态 fail 策略**：
   - 安全字段：fail-closed（剥离客户端凭据，注入目标凭据）
   - 未知可选字段：fail-open（透传）
   - 语义能力字段：capability-driven（按 messagesProfile 配置丢弃）
3. **不合并 3 个 convert 模块**：调用形状不同，强行合并会引入双向依赖

### 10.3 踩坑记录（Lessons Learned）

#### 坑1：reasoning_content 响应方向缺失
- **问题**：`convert_openai_response_to_anthropic` 没有将 OpenAI 的 `reasoning_content` 转为 Anthropic `thinking` block，导致多轮对话时 reasoning_content 丢失
- **修复**：在响应转换中添加 `reasoning_content` → `thinking` block 转换
- **教训**：请求方向和响应方向的转换必须对称，否则多轮对话会断裂

#### 坑2：passthrough 流式 client 生命周期
- **问题**：passthrough 分支把 `_passthrough_stream()` 在 `async with httpx.AsyncClient` 内 `return`，但 generator 在 `async with` 退出（client 关闭）后才执行 `_resp.aiter_bytes()` → ReadError 断流
- **修复**：在 `async with` 块内先读完所有字节再 yield 内存帧（对齐 openai 分支做法）
- **教训**：StreamingResponse 的 generator 在 return 后才迭代，必须在 async with 内完成数据读取

#### 坑3：SSRF guard IPv6-mapped IPv4 绕过
- **问题**：`_is_internal_host()` 用 `ipaddress.ip_address(host)` 解析后检查 `IPv4Network`，但 `::ffff:169.254.169.254` 等 IPv6-mapped IPv4 字面量解析为 `IPv6Address`，与 `IPv4Network` 的 `in` 检查静默返回 `False` 而非异常
- **修复**：unwrap `IPv6Address.ipv4_mapped` 再检查，并添加 IPv6 link-local (`fe80::/10`) 和 ULA (`fc00::/7`) 范围
- **教训**：网络安全检查必须考虑 IPv4/IPv6 双栈场景

#### 坑4：normalizeSse 不能用于 Anthropic 帧
- **问题**：早期设计建议对 messages-passthrough stream 应用 `normalizeSse`，但这是 OpenAI-frame-specific（strips empty `tool_calls`/`content` from `choices[].delta`），对 Anthropic SSE 格式（`event: message_start`…）会破坏内容
- **修复**：新增独立的 `_guard_anthropic_sse_error_frame`（config-gated，fail-open），专门拦截 `event: error` 帧
- **教训**：不同协议帧格式不能混用，必须设计独立的守护逻辑

#### 坑5：三态 fail 策略 vs 既有扁平白名单
- **问题**：设计 bundle 的三态 fail 策略说"unknown/optional fields remain always-forwarded"，但既有 `test_non_allowlisted_field_is_dropped` 断言未知字段被丢弃
- **解决**：能力门控是**加性**叠加在既有扁平白名单之上，未知字段仍由白名单契约丢弃（既有测试锁定的回归护栏不可破坏）
- **教训**：新设计必须与既有测试契约对齐，不能简单覆盖

### 10.4 测试覆盖

- **新增测试**：48个（`test_messages_passthrough.py`）
- **全量回归**：110个测试通过
- **安全测试**：SSRF guard（11种host组合）、auth injection（fail-closed验证）、消息非变异性（含CRLF/NUL字节注入）

### 10.5 新增文件/模块

| 文件 | 用途 |
|------|------|
| `gateways/messages_contract.py` | 字段白名单常量 + `filter_messages_request` + 能力门控 |
| `test_messages_passthrough.py` | 完整回归测试套件 |
| `messages_test_helpers.py` | ASGI 直调测试基础设施（从 test_v1messages_lock.py 抽取） |

### 10.6 配置扩展

`targets.json` 新增可选 `messagesProfile` 字段：

```json
{
  "label": "someEndpoint",
  "handler": "passthrough",
  "messagesProfile": {
    "supportsThinking": true,
    "supportsTopK": false,
    "supportsToolChoice": true
  }
}
```

- 完全可选，缺失不改变现有行为
- 仅作用于 3 个语义字段：`thinking`、`top_k`、`tool_choice`
- 值必须为布尔或字符串 `"true"/"false"`（config_store 校验）

### 10.7 Commit Strategy

```
1. fix(gateway): preserve reasoning_content across multi-turn translation
2. feat(gateway): inject resolved auth in messages-passthrough branch
3. feat(gateway): add SSRF/egress guard for passthrough targets
4. refactor(gateway): centralize messages field allowlist, add thinking
5. feat(gateway): per-endpoint messagesProfile capability gating
6. feat(gateway): broaden vendor error map to full Anthropic error.type enum
7. feat(gateway): apply error mapping + embedded-error detection to messages stream path
8. feat(gateway): add config-gated Anthropic-frame error guard for messages stream
9. test(gateway): add tests/test_messages_passthrough.py regression suite
10. fix(gateway): SSRF guard IPv6 bypass fix (from security review)
```

---

*Converged design. Implementation complete. Ready for review and merge.*
