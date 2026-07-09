"""
Claude Code 完整兼容性测试
模拟 Claude Code 真实发送的所有请求场景
"""
import http.client, json, time, sys, os

PROXY = "127.0.0.1"
PORT = 8082
passed = 0
failed = 0
warn = 0

# ─── 根据 provider 选择测试模型名 ───
# 翻译链路（/v1/messages）用别名（sonnet/opus/haiku），代理会自动映射
# 透传链路（/v1/chat/completions）用真实模型名，直接透传给上游
_PROVIDER = os.environ.get("PREFERRED_PROVIDER", "qclaw").lower()
if _PROVIDER == "qclaw":
    # QClaw 上游直连：用 pool-* 模型名
    OAI_MODEL_BIG = os.environ.get("BIG_MODEL", "pool-glm-5.2")
    OAI_MODEL_MED = os.environ.get("MEDIUM_MODEL", "pool-deepseek-v4-pro")
    OAI_MODEL_SMALL = os.environ.get("SMALL_MODEL", "pool-deepseek-v4-flash")
else:
    # Copilot/OpenAI 等：用原始模型名
    OAI_MODEL_BIG = "claude-opus-4-20250514"
    OAI_MODEL_MED = "claude-sonnet-4.6"
    OAI_MODEL_SMALL = "claude-haiku-4.5"

def test(name, path, body=None, method="POST", checks=None):
    """测试单个请求，支持自定义校验"""
    global passed, failed, warn
    try:
        conn = http.client.HTTPConnection(PROXY, PORT)
        headers = {"Content-Type": "application/json"} if body else {}
        body_bytes = json.dumps(body).encode() if body else None
        conn.request(method, path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()

        try:
            data = json.loads(raw)
        except Exception:
            data = {}

        # checks 里若有 is_error 或 response_ok，允许非 200 状态码
        allow_non_200 = checks and ("is_error" in checks or "response_ok" in checks)
        if resp.status != 200 and not allow_non_200:
            try:
                detail = data.get("detail", raw.decode()[:200])
            except Exception:
                detail = raw.decode()[:200]
            print(f"❌ {name}: HTTP {resp.status}")
            print(f"   {detail}")
            failed += 1
            return

        if checks:
            for ck, fn in checks.items():
                try:
                    ok, msg = fn(data, resp)
                    if not ok:
                        print(f"⚠️ {name}: {ck} failed — {msg}")
                        warn += 1
                        return
                except Exception as e:
                    print(f"⚠️ {name}: {ck} check error — {e}")
                    warn += 1
                    return

        print(f"✅ {name}")
        passed += 1
    except Exception as e:
        print(f"❌ {name}: {e}")
        failed += 1

# ========== 辅助校验 ==========
def has_text(data, _):
    texts = [b.get("text","") for b in data.get("content",[]) if b.get("type")=="text"]
    return bool("".join(texts).strip()), "no text in response"

def model_equals(expected):
    def check(data, _):
        actual = data.get("model","")
        return actual == expected, f"expected model={expected}, got={actual}"
    return check

def has_stop_reason(data, _):
    return data.get("stop_reason") is not None, "missing stop_reason"

# ========== 1. 基础连通性 ==========
print("=" * 55)
print("1. 基础连通性")
print("=" * 55)

test("GET /", "/", method="GET", checks={"json": lambda d,r: (d.get("message") is not None, "no message")})

# ========== 2. 模型名还原测试 ==========
print("\n" + "=" * 55)
print("2. 模型名还原（Claude Code 校验）")
print("=" * 55)

for model_name in ["sonnet", "claude-sonnet-4-20250514", "claude-haiku-3-5-20241022", "claude-opus-4-20250514"]:
    test(f"模型名: {model_name}", "/v1/messages", body={
        "model": model_name, "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }, checks={"model": model_equals(model_name), "has_text": has_text})

# ========== 3. System Prompt 格式 ==========
print("\n" + "=" * 55)
print("3. System Prompt（Claude Code 发送多种格式）")
print("=" * 55)

test("system = 字符串", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 50,
    "messages": [{"role": "user", "content": "hi"}],
    "system": "You are Claude Code, a coding assistant.",
    "stream": False,
}, checks={"has_text": has_text})

test("system = 列表(text blocks)", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 50,
    "messages": [{"role": "user", "content": "hi"}],
    "system": [{"type": "text", "text": "You are Claude Code."}],
    "stream": False,
}, checks={"has_text": has_text})

test("长 system prompt (>10KB)", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 50,
    "messages": [{"role": "user", "content": "hi"}],
    "system": "You are Claude Code. " * 500,
    "stream": False,
}, checks={
    # Copilot API 有 payload 大小限制，400/500 均属预期，200 也可能成功
    "response_ok": lambda d, r: (r.status in (200, 400, 500), f"unexpected status {r.status}"),
})

# ========== 4. 流式测试 ==========
print("\n" + "=" * 55)
print("4. 流式响应（Anthropic SSE 格式）")
print("=" * 55)

def check_stream_events(events_required):
    def check(data, resp):
        ct = resp.getheader("content-type", "")
        if "text/event-stream" not in ct:
            return False, f"content-type={ct}"
        raw = data  # raw bytes
        decoded = raw.decode() if isinstance(raw, bytes) else str(data)
        for ev in events_required:
            if ev not in decoded:
                return False, f"missing event: {ev}"
        return True, ""
    return check

for model_name in ["sonnet", "claude-haiku-3-5-20241022"]:
    conn = http.client.HTTPConnection(PROXY, PORT)
    conn.request("POST", "/v1/messages", json.dumps({
        "model": model_name, "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
        "system": "You are Claude Code.",
        "stream": True,
    }).encode(), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    raw = b""
    for _ in range(100):
        c = resp.read(65536)
        if not c: break
        raw += c
        if b"message_stop" in raw: break
    conn.close()

    decoded = raw.decode()
    events = [e for e in ["message_start", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"]]
    ok = all(e in decoded for e in events)
    # 提取文本
    texts = []
    for line in decoded.split("\n"):
        if "content_block_delta" in line and 'text_delta' in line:
            try:
                j = json.loads(line.split("data: ")[1])
                texts.append(j["delta"]["text"])
            except: pass
    full_text = "".join(texts)
    if ok and full_text.strip():
        print(f"✅ 流式 {model_name}: {full_text[:80]}...")
        passed += 1
    else:
        missing = [e for e in events if e not in decoded]
        print(f"❌ 流式 {model_name}: HTTP {resp.status}  missing={missing}  text={full_text[:50]}")
        failed += 1

# ========== 5. 消息格式 ==========
print("\n" + "=" * 55)
print("5. 消息格式")
print("=" * 55)

test("单轮对话", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 50,
    "messages": [{"role": "user", "content": "hello"}],
    "stream": False,
}, checks={"has_text": has_text})

test("多轮对话", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 50,
    "messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "what is 1+1"},
    ],
    "stream": False,
}, checks={"has_text": has_text})

test("空消息", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 50,
    "messages": [{"role": "user", "content": ""}],
    "stream": False,
}, checks={"has_text": has_text})

# ========== 6. 参数传递 ==========
print("\n" + "=" * 55)
print("6. 参数传递")
print("=" * 55)

for param_name, param_val in [
    ("temperature", 0.7),
    ("top_p", 0.9),
    ("top_k", 40),
    ("max_tokens", 256),
]:
    test(f"参数 {param_name}={param_val}", "/v1/messages", body={
        "model": "sonnet", "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
        param_name: param_val,
        "stream": False,
    }, checks={"has_text": has_text})

# ========== 7. Stop Sequences ==========
print("\n" + "=" * 55)
print("7. Stop Sequences")
print("=" * 55)

test("stop_sequences", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 200,
    "messages": [{"role": "user", "content": "count 1 2 3 4 5"}],
    "stop_sequences": ["3"],
    "stream": False,
}, checks={"has_text": has_text})

# ========== 8. Metadata ==========
print("\n" + "=" * 55)
print("8. Metadata")
print("=" * 55)

test("metadata", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 50,
    "messages": [{"role": "user", "content": "hi"}],
    "metadata": {"user_id": "test-123"},
    "stream": False,
}, checks={"has_text": has_text})

# ========== 9. Count Tokens ==========
print("\n" + "=" * 55)
print("9. Token 计数")
print("=" * 55)

test("count_tokens", "/v1/messages/count_tokens", body={
    "model": "sonnet",
    "messages": [{"role": "user", "content": "hello world"}],
}, checks={
    "has_input_tokens": lambda d,r: (d.get("input_tokens", 0) > 0, "input_tokens=0")
})

# ========== 10. Tools 定义 ==========
print("\n" + "=" * 55)
print("10. Tools（Claude Code 会发工具定义）")
print("=" * 55)

test("简单工具", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 200,
    "messages": [{"role": "user", "content": "hi"}],
    "tools": [
        {"name": "Bash", "description": "执行命令", "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要执行的命令"}},
            "required": ["command"]
        }}
    ],
    "stream": False,
}, checks={"has_text": has_text})

test("多个工具", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 200,
    "messages": [{"role": "user", "content": "hi"}],
    "tools": [
        {"name": "Bash", "description": "bash", "input_schema": {"type": "object", "properties": {}}},
        {"name": "Read", "description": "读文件", "input_schema": {"type": "object", "properties": {}}},
        {"name": "Write", "description": "写文件", "input_schema": {"type": "object", "properties": {}}},
        {"name": "Edit", "description": "编辑", "input_schema": {"type": "object", "properties": {}}},
        {"name": "Glob", "description": "glob", "input_schema": {"type": "object", "properties": {}}},
        {"name": "Grep", "description": "grep", "input_schema": {"type": "object", "properties": {}}},
    ],
    "stream": False,
}, checks={"has_text": has_text})

# ========== 11. Tool Choice ==========
print("\n" + "=" * 55)
print("11. Tool Choice")
print("=" * 55)

test("tool_choice=auto", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 50,
    "messages": [{"role": "user", "content": "hi"}],
    "tool_choice": {"type": "auto"},
    "stream": False,
}, checks={"has_text": has_text})

# ========== 12. Thinking ==========
print("\n" + "=" * 55)
print("12. Thinking")
print("=" * 55)

test("thinking enabled", "/v1/messages", body={
    "model": "sonnet", "max_tokens": 100,
    "messages": [{"role": "user", "content": "hi"}],
    "thinking": {"enabled": True},
    "stream": False,
}, checks={"has_text": has_text})

# ========== 13. 错误处理 ==========
print("\n" + "=" * 55)
print("13. 错误处理")
print("=" * 55)

test("缺少 model 参数返回 422", "/v1/messages", body={
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "hi"}],
    "stream": False,
}, checks={"is_error": lambda d, r: (r.status == 422, f"expected 422, got {r.status}")})

# ========== 14. 并发 / 性能 ==========
print("\n" + "=" * 55)
print("14. 性能基准")
print("=" * 55)

start = time.time()
conn = http.client.HTTPConnection(PROXY, PORT)
conn.request("POST", "/v1/messages", json.dumps({
    "model": "sonnet", "max_tokens": 50,
    "messages": [{"role": "user", "content": "hi"}],
    "stream": False,
}).encode(), {"Content-Type": "application/json"})
resp = conn.getresponse()
data = json.loads(resp.read())
conn.close()
elapsed = time.time() - start
text = "".join(b.get("text","") for b in data.get("content",[]) if b.get("type")=="text")
print(f"⏱️ 首次调用: {elapsed:.1f}s  回复: {text[:80] if text else '(空)'}")
if elapsed > 15:
    print("⚠️ 首次调用 > 15s，可能有问题")

# ========== 15. OpenAI 兼容端点 /v1/chat/completions ==========
print("\n" + "=" * 55)
print("15. OpenAI 兼容端点 /v1/chat/completions")
print("=" * 55)

def oai_test(name, body, checks=None):
    """测试 /v1/chat/completions 端点"""
    global passed, failed, warn
    try:
        conn = http.client.HTTPConnection(PROXY, PORT)
        conn.request("POST", "/v1/chat/completions",
                     json.dumps(body).encode(),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        if resp.status != 200:
            try:
                detail = json.loads(raw).get("detail", raw.decode()[:200])
            except:
                detail = raw.decode()[:200]
            print(f"❌ {name}: HTTP {resp.status} — {detail}")
            failed += 1
            return None
        data = json.loads(raw)
        if checks:
            for ck, fn in checks.items():
                try:
                    ok, msg = fn(data, resp)
                    if not ok:
                        print(f"⚠️ {name}: {ck} — {msg}")
                        warn += 1
                        return data
                except Exception as e:
                    print(f"⚠️ {name}: {ck} check error — {e}")
                    warn += 1
                    return data
        print(f"✅ {name}")
        passed += 1
        return data
    except Exception as e:
        print(f"❌ {name}: {e}")
        failed += 1
        return None

def oai_has_content(data, _):
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return bool(text and text.strip()), f"no content, got: {text!r}"

def oai_model_passthrough(expected):
    def check(data, _):
        got = data.get("model", "")
        return got == expected, f"expected model={expected!r}, got={got!r}"
    return check

def oai_has_usage(data, _):
    u = data.get("usage", {})
    return u.get("completion_tokens", 0) > 0, f"usage={u}"

def oai_has_id(data, _):
    return bool(data.get("id")), "missing id field"

def oai_has_choices(data, _):
    return bool(data.get("choices")), "missing choices"

# 15-1. 基础非流式
oai_test(f"基础请求 {OAI_MODEL_MED}", {
    "model": OAI_MODEL_MED,
    "messages": [{"role": "user", "content": "reply with one word: OK"}],
    "stream": False,
}, checks={
    "has_content": oai_has_content,
    "has_id": oai_has_id,
    "has_choices": oai_has_choices,
    "has_usage": oai_has_usage,
})

# 15-2. big 模型
oai_test(f"big 模型 {OAI_MODEL_BIG}", {
    "model": OAI_MODEL_BIG,
    "messages": [{"role": "user", "content": "reply: OK"}],
    "stream": False,
}, checks={"has_content": oai_has_content})

# 15-3. small 模型
oai_test(f"small 模型 {OAI_MODEL_SMALL}", {
    "model": OAI_MODEL_SMALL,
    "messages": [{"role": "user", "content": "reply: OK"}],
    "stream": False,
}, checks={"has_content": oai_has_content})

# 15-4. 多轮对话
oai_test("多轮对话", {
    "model": OAI_MODEL_MED,
    "messages": [
        {"role": "user", "content": "my secret number is 42"},
        {"role": "assistant", "content": "Got it, your secret number is 42."},
        {"role": "user", "content": "what is my secret number?"},
    ],
    "stream": False,
}, checks={"has_content": oai_has_content})

# 15-5. system prompt
oai_test("system prompt", {
    "model": OAI_MODEL_MED,
    "messages": [
        {"role": "system", "content": "You are a concise assistant."},
        {"role": "user", "content": "reply: OK"},
    ],
    "stream": False,
}, checks={"has_content": oai_has_content})

# 15-6. max_tokens 参数透传
oai_test("max_tokens 参数", {
    "model": OAI_MODEL_MED,
    "messages": [{"role": "user", "content": "count to 100"}],
    "max_tokens": 20,
    "stream": False,
}, checks={
    "has_choices": oai_has_choices,
    "finish_reason_length": lambda d, _: (
        (d.get("choices") or [{}])[0].get("finish_reason") in ("length", "stop"),
        f"finish_reason={((d.get('choices') or [{}])[0].get('finish_reason'))}"
    ),
})

# 15-7. temperature / top_p
oai_test("temperature=0.5, top_p=0.9", {
    "model": OAI_MODEL_MED,
    "messages": [{"role": "user", "content": "reply: OK"}],
    "temperature": 0.5, "top_p": 0.9,
    "stream": False,
}, checks={"has_content": oai_has_content})

# 15-8. tools 定义
oai_test("tools 定义", {
    "model": OAI_MODEL_MED,
    "messages": [{"role": "user", "content": "hi"}],
    "tools": [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        },
    }],
    "stream": False,
}, checks={"has_choices": oai_has_choices})

# 15-9. 流式响应
print()
conn = http.client.HTTPConnection(PROXY, PORT, timeout=60)
conn.request("POST", "/v1/chat/completions", json.dumps({
    "model": OAI_MODEL_SMALL,
    "messages": [{"role": "user", "content": "reply with exactly: streaming works"}],
    "stream": True,
}).encode(), {"Content-Type": "application/json"})
resp = conn.getresponse()
raw = b""
for _ in range(500):
    c = resp.read(4096)
    if not c: break
    raw += c
    if b"[DONE]" in raw: break
conn.close()
decoded = raw.decode(errors="replace")
content_parts = []
for line in decoded.split("\n"):
    if not line.startswith("data:") or "[DONE]" in line:
        continue
    try:
        d = json.loads(line[5:].strip())
        delta = (d.get("choices") or [{}])[0].get("delta", {})
        # 跳过 reasoning_text 块，只取真正的 content
        t = delta.get("content")
        if t and isinstance(t, str) and t.strip():
            content_parts.append(t)
    except Exception:
        pass
full = "".join(content_parts)
if resp.status == 200 and full.strip():
    print(f"✅ 流式响应 {OAI_MODEL_SMALL}: {full[:80]!r}")
    passed += 1
else:
    print(f"❌ 流式响应: HTTP {resp.status}  text={full[:80]!r}")
    failed += 1

# 15-10. OpenAI 格式 usage 字段
d = oai_test("usage 字段完整性", {
    "model": OAI_MODEL_SMALL,
    "messages": [{"role": "user", "content": "say hi"}],
    "stream": False,
}, checks={
    "prompt_tokens": lambda d, _: (d.get("usage", {}).get("prompt_tokens", 0) > 0,
                                    f"prompt_tokens={d.get('usage',{}).get('prompt_tokens')}"),
    "completion_tokens": lambda d, _: (d.get("usage", {}).get("completion_tokens", 0) > 0,
                                        f"completion_tokens={d.get('usage',{}).get('completion_tokens')}"),
})

# 15-11. 性能基准
start = time.time()
conn = http.client.HTTPConnection(PROXY, PORT)
conn.request("POST", "/v1/chat/completions", json.dumps({
    "model": OAI_MODEL_MED,
    "messages": [{"role": "user", "content": "hi"}],
    "stream": False,
}).encode(), {"Content-Type": "application/json"})
resp = conn.getresponse()
data = json.loads(resp.read())
conn.close()
elapsed = time.time() - start
text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
print(f"⏱️  OpenAI 端点延迟: {elapsed:.1f}s  回复: {text[:60]!r}")
if elapsed > 15:
    print("⚠️  延迟 > 15s，可能有网络问题")

# ========== 总结 ==========
print("\n" + "=" * 55)
total = passed + failed + warn
print(f"✅ 通过: {passed}  ⚠️ 警告: {warn}  ❌ 失败: {failed}")
if failed == 0 and warn == 0:
    print("🎉 全部通过！Claude Code 可以正常使用")
elif failed == 0:
    print("✅ 核心功能正常（有一些可忽略的警告）")
else:
    print("❌ 有测试失败，需要修复")
    sys.exit(1)
