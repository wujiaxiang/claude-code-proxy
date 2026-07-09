"""
Claude Code 代理统一测试套件

整合自 test_claude_api.py / test_messages_endpoint.py / tests.py 三个历史文件。
覆盖翻译链路（/v1/messages）和透传链路（/v1/chat/completions），
模拟 Claude Code 真实请求场景，包括 thinking、tools、流式 SSE 事件序列验证。

用法:
  python test_suite.py                # 跑全部测试
  python test_suite.py --simple       # 只跑基础场景（1-5）
  python test_suite.py --tools        # 只跑工具 / thinking 相关（9-12）
  python test_suite.py --oai          # 只跑 OpenAI 透传端点（15）
  python test_suite.py --no-streaming # 跳过流式测试

环境变量:
  PREFERRED_PROVIDER=qclaw            # qclaw（默认）或其他
  BIG_MODEL / MEDIUM_MODEL / SMALL_MODEL  # 覆盖默认 pool-* 模型名
  PROXY_HOST / PROXY_PORT             # 覆盖默认 127.0.0.1:8082
"""
import argparse
import http.client
import json
import os
import sys
import time

# ─── 配置 ───
PROXY = os.environ.get("PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("PROXY_PORT", "8082"))
TIMEOUT = 60

# ─── 根据 provider 选择测试模型名 ───
# 翻译链路（/v1/messages）用别名（sonnet/opus/haiku），代理自动映射
# 透传链路（/v1/chat/completions）用真实模型名，直接透传给上游
_PROVIDER = os.environ.get("PREFERRED_PROVIDER", "qclaw").lower()
if _PROVIDER == "qclaw":
    OAI_MODEL_BIG = os.environ.get("BIG_MODEL", "pool-glm-5.2")
    OAI_MODEL_MED = os.environ.get("MEDIUM_MODEL", "pool-deepseek-v4-pro")
    OAI_MODEL_SMALL = os.environ.get("SMALL_MODEL", "pool-deepseek-v4-flash")
else:
    OAI_MODEL_BIG = "claude-opus-4-20250514"
    OAI_MODEL_MED = "claude-sonnet-4.6"
    OAI_MODEL_SMALL = "claude-haiku-4.5"

# ─── 统计 ───
passed = 0
failed = 0
warn = 0


# ============================================================
# 基础请求工具
# ============================================================

def request(path, body=None, method="POST"):
    """发送 HTTP 请求，返回 (status, raw_bytes, headers_dict)。"""
    conn = http.client.HTTPConnection(PROXY, PORT, timeout=TIMEOUT)
    headers = {"Content-Type": "application/json"} if body else {}
    body_bytes = json.dumps(body).encode() if body else None
    conn.request(method, path, body=body_bytes, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    headers_dict = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, raw, headers_dict


def request_stream(path, body):
    """发送流式请求，读取完整 SSE 流并返回 (status, decoded_text, headers_dict)。"""
    conn = http.client.HTTPConnection(PROXY, PORT, timeout=TIMEOUT)
    conn.request("POST", path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    raw = b""
    is_anthropic = "/v1/messages" in path
    terminator = b"message_stop" if is_anthropic else b"[DONE]"
    for _ in range(2000):
        chunk = resp.read(65536)
        if not chunk:
            break
        raw += chunk
        if terminator in raw:
            break
    headers_dict = {k.lower(): v for k, v in resp.getheaders()}
    status = resp.status
    conn.close()
    return status, raw.decode(errors="replace"), headers_dict


def test(name, path, body=None, method="POST", checks=None, allow_non_200=False):
    """测试单个非流式请求，支持自定义校验函数。"""
    global passed, failed, warn
    try:
        status, raw, _ = request(path, body, method)
        try:
            data = json.loads(raw)
        except Exception:
            data = {}

        if status != 200 and not allow_non_200:
            try:
                detail = data.get("detail", raw.decode(errors="replace")[:200])
            except Exception:
                detail = raw.decode(errors="replace")[:200]
            print(f"❌ {name}: HTTP {status}")
            print(f"   {detail}")
            failed += 1
            return

        if checks:
            for ck, fn in checks.items():
                try:
                    ok, msg = fn(data, status)
                    if not ok:
                        print(f"⚠️  {name}: {ck} failed — {msg}")
                        warn += 1
                        return
                except Exception as e:
                    print(f"⚠️  {name}: {ck} check error — {e}")
                    warn += 1
                    return

        print(f"✅ {name}")
        passed += 1
    except Exception as e:
        print(f"❌ {name}: {e}")
        failed += 1


# ============================================================
# 校验函数
# ============================================================

def has_text(data, _):
    texts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return bool("".join(texts).strip()), "no text in response"


def model_equals(expected):
    def check(data, _):
        return data.get("model", "") == expected, f"expected model={expected}, got={data.get('model','')}"
    return check


def has_stop_reason(data, _):
    return data.get("stop_reason") is not None, "missing stop_reason"


def expect_status(code):
    return lambda d, s: (s == code, f"expected {code}, got {s}")


def check_sse_events(events_required):
    """校验 SSE 流中是否包含所有必需事件类型（针对 decoded text）。"""
    def check(decoded_text):
        missing = [e for e in events_required if e not in decoded_text]
        return (not missing, f"missing events: {missing}")
    return check


def extract_sse_text_delta(decoded_text):
    """从 SSE 流中提取 text_delta 的文本内容。"""
    texts = []
    for line in decoded_text.split("\n"):
        if "content_block_delta" not in line or "text_delta" not in line:
            continue
        if "data: " not in line:
            continue
        try:
            j = json.loads(line.split("data: ", 1)[1])
            t = j.get("delta", {}).get("text", "")
            if t:
                texts.append(t)
        except Exception:
            pass
    return "".join(texts)


def parse_sse_events(decoded_text):
    """解析 SSE 事件序列，返回 [(event_type, data_dict)] 列表。"""
    events = []
    current_event = None
    for line in decoded_text.split("\n"):
        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: "):
            data_str = line[6:]
            if data_str == "[DONE]":
                events.append(("done", None))
                break
            try:
                events.append((current_event or "unknown", json.loads(data_str)))
            except json.JSONDecodeError:
                pass
    return events


def print_sse_sequence(events, limit=30):
    """打印 SSE 事件序列摘要。"""
    for i, (evt_type, evt_data) in enumerate(events[:limit]):
        if evt_type == "done":
            print(f"    [{i}] DONE")
        elif evt_type == "content_block_start":
            idx = evt_data.get("index")
            block_type = evt_data.get("content_block", {}).get("type")
            print(f"    [{i}] content_block_start: index={idx}, type={block_type}")
        elif evt_type == "content_block_stop":
            print(f"    [{i}] content_block_stop: index={evt_data.get('index')}")
        elif evt_type == "content_block_delta":
            idx = evt_data.get("index")
            delta_type = evt_data.get("delta", {}).get("type")
            print(f"    [{i}] content_block_delta: index={idx}, delta_type={delta_type}")
        elif evt_type == "message_start":
            print(f"    [{i}] message_start")
        elif evt_type == "message_delta":
            stop_reason = evt_data.get("delta", {}).get("stop_reason")
            print(f"    [{i}] message_delta: stop_reason={stop_reason}")
        elif evt_type == "message_stop":
            print(f"    [{i}] message_stop")
        elif evt_type == "ping":
            print(f"    [{i}] ping")
        else:
            print(f"    [{i}] {evt_type}: {str(evt_data)[:80]}")


# ============================================================
# OpenAI 兼容端点测试工具
# ============================================================

def oai_test(name, body, checks=None):
    """测试 /v1/chat/completions 端点。"""
    global passed, failed, warn
    try:
        status, raw, _ = request("/v1/chat/completions", body)
        if status != 200:
            try:
                detail = json.loads(raw).get("detail", raw.decode(errors="replace")[:200])
            except Exception:
                detail = raw.decode(errors="replace")[:200]
            print(f"❌ {name}: HTTP {status} — {detail}")
            failed += 1
            return None
        data = json.loads(raw)
        if checks:
            for ck, fn in checks.items():
                try:
                    ok, msg = fn(data, status)
                    if not ok:
                        print(f"⚠️  {name}: {ck} — {msg}")
                        warn += 1
                        return data
                except Exception as e:
                    print(f"⚠️  {name}: {ck} check error — {e}")
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


def oai_has_usage(data, _):
    return data.get("usage", {}).get("completion_tokens", 0) > 0, f"usage={data.get('usage')}"


def oai_has_id(data, _):
    return bool(data.get("id")), "missing id field"


def oai_has_choices(data, _):
    return bool(data.get("choices")), "missing choices"


# ============================================================
# 工具定义
# ============================================================

CALCULATOR_TOOL = {
    "name": "calculator",
    "description": "Evaluate mathematical expressions",
    "input_schema": {
        "type": "object",
        "properties": {"expression": {"type": "string", "description": "数学表达式"}},
        "required": ["expression"],
    },
}

WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get weather information for a location",
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "城市或地点"},
            "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        },
        "required": ["location"],
    },
}

BASH_TOOL = {
    "name": "Bash",
    "description": "执行命令",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "要执行的命令"}},
        "required": ["command"],
    },
}

READ_TOOL = {
    "name": "Read",
    "description": "读取文件",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
}


# ============================================================
# 测试场景
# ============================================================

def section(title):
    print("\n" + "=" * 55)
    print(title)
    print("=" * 55)


# ─── 1. 基础连通性 ───
def test_basic_connectivity():
    section("1. 基础连通性")
    test("GET /", "/", method="GET",
         checks={"json": lambda d, s: (d.get("message") is not None, "no message")})


# ─── 2. 模型名还原 ───
def test_model_name_restore():
    section("2. 模型名还原（Claude Code 校验）")
    for model_name in ["sonnet", "claude-sonnet-4-20250514", "claude-haiku-3-5-20241022", "claude-opus-4-20250514"]:
        test(f"模型名: {model_name}", "/v1/messages", body={
            "model": model_name, "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }, checks={"model": model_equals(model_name), "has_text": has_text})


# ─── 3. System Prompt ───
def test_system_prompt():
    section("3. System Prompt（Claude Code 发送多种格式）")
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
        "response_ok": lambda d, s: (s in (200, 400, 500), f"unexpected status {s}"),
    }, allow_non_200=True)


# ─── 4. 流式响应 ───
def test_streaming():
    global passed, failed
    section("4. 流式响应（Anthropic SSE 格式）")
    required = ["message_start", "content_block_start", "content_block_delta",
                "content_block_stop", "message_delta", "message_stop"]

    for model_name in ["sonnet", "claude-haiku-3-5-20241022"]:
        try:
            status, decoded, headers = request_stream("/v1/messages", {
                "model": model_name, "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
                "system": "You are Claude Code.",
                "stream": True,
            })
            missing = [e for e in required if e not in decoded]
            text = extract_sse_text_delta(decoded)
            if status == 200 and not missing and text.strip():
                print(f"✅ 流式 {model_name}: {text[:80]}...")
                passed += 1
            else:
                print(f"❌ 流式 {model_name}: HTTP {status}  missing={missing}  text={text[:50]}")
                failed += 1
        except Exception as e:
            print(f"❌ 流式 {model_name}: {e}")
            failed += 1


# ─── 5. 消息格式 ───
def test_message_format():
    section("5. 消息格式")
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


# ─── 6. 参数传递 ───
def test_params():
    section("6. 参数传递")
    for param_name, param_val in [("temperature", 0.7), ("top_p", 0.9), ("top_k", 40), ("max_tokens", 256)]:
        test(f"参数 {param_name}={param_val}", "/v1/messages", body={
            "model": "sonnet", "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}],
            param_name: param_val,
            "stream": False,
        }, checks={"has_text": has_text})


# ─── 7. Stop Sequences / Metadata ───
def test_stop_and_metadata():
    section("7. Stop Sequences / Metadata")
    test("stop_sequences", "/v1/messages", body={
        "model": "sonnet", "max_tokens": 200,
        "messages": [{"role": "user", "content": "count 1 2 3 4 5"}],
        "stop_sequences": ["3"],
        "stream": False,
    }, checks={"has_text": has_text})

    test("metadata", "/v1/messages", body={
        "model": "sonnet", "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"user_id": "test-123"},
        "stream": False,
    }, checks={"has_text": has_text})


# ─── 8. Token 计数 ───
def test_count_tokens():
    section("8. Token 计数")
    test("count_tokens", "/v1/messages/count_tokens", body={
        "model": "sonnet",
        "messages": [{"role": "user", "content": "hello world"}],
    }, checks={
        "has_input_tokens": lambda d, s: (d.get("input_tokens", 0) > 0, "input_tokens=0"),
    })


# ─── 9. Tools 定义 ───
def test_tools():
    section("9. Tools（Claude Code 会发工具定义）")
    test("简单工具", "/v1/messages", body={
        "model": "sonnet", "max_tokens": 200,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [BASH_TOOL],
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


# ─── 10. Tool Choice ───
def test_tool_choice():
    section("10. Tool Choice")
    test("tool_choice=auto", "/v1/messages", body={
        "model": "sonnet", "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
        "tool_choice": {"type": "auto"},
        "stream": False,
    }, checks={"has_text": has_text})


# ─── 11. Thinking（来自 test_messages_endpoint.py） ───
def test_thinking():
    section("11. Thinking（adaptive / enabled / budget / 历史 / 工具组合）")

    test("thinking type=adaptive", "/v1/messages", body={
        "model": "sonnet", "max_tokens": 1024,
        "thinking": {"type": "adaptive"},
        "messages": [{"role": "user", "content": "1+1=? Reply with just the number."}],
        "stream": False,
    }, checks={"has_text": has_text})

    test("thinking type=enabled with budget_tokens", "/v1/messages", body={
        "model": "sonnet", "max_tokens": 1024,
        "thinking": {"type": "enabled", "budget_tokens": 5000},
        "messages": [{"role": "user", "content": "1+1=? Reply with just the number."}],
        "stream": False,
    }, checks={"has_text": has_text})

    # 历史 422 bug：assistant 消息含 thinking block
    test("thinking in history（历史 422 bug）", "/v1/messages", body={
        "model": "sonnet", "max_tokens": 1024,
        "thinking": {"type": "adaptive"},
        "messages": [
            {"role": "user", "content": "1+1=?"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "The user asked 1+1. The answer is 2.", "signature": ""},
                {"type": "text", "text": "2"},
            ]},
            {"role": "user", "content": "2+2=?"},
        ],
        "stream": False,
    }, checks={"has_text": has_text})

    test("thinking + tools", "/v1/messages", body={
        "model": "sonnet", "max_tokens": 1024,
        "thinking": {"type": "adaptive"},
        "tools": [WEATHER_TOOL],
        "messages": [{"role": "user", "content": "What's the weather in Beijing?"}],
        "stream": False,
    }, checks={"has_text": has_text})

    # 完整 Claude Code 风格：system + tools + thinking history
    test("full Claude Code style", "/v1/messages", body={
        "model": "sonnet", "max_tokens": 4096,
        "thinking": {"type": "adaptive"},
        "system": [{"type": "text", "text": "You are a helpful assistant."}],
        "tools": [BASH_TOOL, READ_TOOL],
        "messages": [
            {"role": "user", "content": "What is 1+1?"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Simple math: 1+1=2", "signature": ""},
                {"type": "text", "text": "1+1=2"},
            ]},
            {"role": "user", "content": "What is 2+2?"},
        ],
        "stream": False,
    }, checks={"has_text": has_text})

    # output_config 额外字段（真实请求中见过）
    test("output_config 额外字段", "/v1/messages", body={
        "model": "sonnet", "max_tokens": 100,
        "thinking": {"type": "adaptive"},
        "output_config": {"format": "text"},
        "messages": [{"role": "user", "content": "1+1=?"}],
        "stream": False,
    }, checks={"has_text": has_text})


# ─── 12. 错误处理 ───
def test_error_handling():
    section("12. 错误处理")
    test("缺少 model 参数返回 422", "/v1/messages", body={
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }, checks={"is_error": expect_status(422)}, allow_non_200=True)


# ─── 13. 流式 Thinking + SSE 事件序列验证（来自 test_messages_endpoint.py） ───
def test_streaming_thinking():
    global passed, failed
    section("13. 流式 Thinking + SSE 事件序列验证")
    required = ["message_start", "content_block_start", "content_block_delta",
                "content_block_stop", "message_delta", "message_stop"]

    cases = [
        ("stream_thinking", {
            "model": "sonnet", "max_tokens": 1024,
            "stream": True,
            "thinking": {"type": "adaptive"},
            "messages": [{"role": "user", "content": "1+1=? Reply with just the number."}],
        }),
        ("stream_thinking_history", {
            "model": "sonnet", "max_tokens": 1024,
            "stream": True,
            "thinking": {"type": "adaptive"},
            "messages": [
                {"role": "user", "content": "1+1=?"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "1+1=2", "signature": ""},
                    {"type": "text", "text": "2"},
                ]},
                {"role": "user", "content": "2+2=?"},
            ],
        }),
    ]

    for name, payload in cases:
        try:
            status, decoded, _ = request_stream("/v1/messages", payload)
            if status != 200:
                print(f"❌ {name}: HTTP {status}")
                print(f"   {decoded[:500]}")
                failed += 1
                continue
            events = parse_sse_events(decoded)
            missing = [e for e in required if e not in decoded]
            text = extract_sse_text_delta(decoded)
            if missing:
                print(f"❌ {name}: missing events {missing}")
                failed += 1
                continue
            print(f"✅ {name}: {len(events)} events, text={text[:60]!r}")
            print_sse_sequence(events, limit=15)
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")
            failed += 1


# ─── 14. 性能基准 ───
def test_performance():
    section("14. 性能基准")
    start = time.time()
    status, raw, _ = request("/v1/messages", {
        "model": "sonnet", "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    })
    elapsed = time.time() - start
    data = json.loads(raw)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    print(f"⏱️  首次调用: {elapsed:.1f}s  回复: {text[:80] if text else '(空)'}")
    if elapsed > 15:
        print("⚠️  首次调用 > 15s，可能有问题")


# ─── 15. OpenAI 兼容端点 /v1/chat/completions（透传链路） ───
def test_openai_endpoint():
    global passed, failed
    section("15. OpenAI 兼容端点 /v1/chat/completions（透传链路）")

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

    oai_test(f"big 模型 {OAI_MODEL_BIG}", {
        "model": OAI_MODEL_BIG,
        "messages": [{"role": "user", "content": "reply: OK"}],
        "stream": False,
    }, checks={"has_content": oai_has_content})

    oai_test(f"small 模型 {OAI_MODEL_SMALL}", {
        "model": OAI_MODEL_SMALL,
        "messages": [{"role": "user", "content": "reply: OK"}],
        "stream": False,
    }, checks={"has_content": oai_has_content})

    oai_test("多轮对话", {
        "model": OAI_MODEL_MED,
        "messages": [
            {"role": "user", "content": "my secret number is 42"},
            {"role": "assistant", "content": "Got it, your secret number is 42."},
            {"role": "user", "content": "what is my secret number?"},
        ],
        "stream": False,
    }, checks={"has_content": oai_has_content})

    oai_test("system prompt", {
        "model": OAI_MODEL_MED,
        "messages": [
            {"role": "system", "content": "You are a concise assistant."},
            {"role": "user", "content": "reply: OK"},
        ],
        "stream": False,
    }, checks={"has_content": oai_has_content})

    oai_test("max_tokens 参数", {
        "model": OAI_MODEL_MED,
        "messages": [{"role": "user", "content": "count to 100"}],
        "max_tokens": 20,
        "stream": False,
    }, checks={
        "has_choices": oai_has_choices,
        "finish_reason_length": lambda d, _: (
            (d.get("choices") or [{}])[0].get("finish_reason") in ("length", "stop"),
            f"finish_reason={((d.get('choices') or [{}])[0].get('finish_reason'))}",
        ),
    })

    oai_test("temperature=0.5, top_p=0.9", {
        "model": OAI_MODEL_MED,
        "messages": [{"role": "user", "content": "reply: OK"}],
        "temperature": 0.5, "top_p": 0.9,
        "stream": False,
    }, checks={"has_content": oai_has_content})

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

    # OpenAI 流式
    try:
        status, decoded, _ = request_stream("/v1/chat/completions", {
            "model": OAI_MODEL_SMALL,
            "messages": [{"role": "user", "content": "reply with exactly: streaming works"}],
            "stream": True,
        })
        content_parts = []
        for line in decoded.split("\n"):
            if not line.startswith("data:") or "[DONE]" in line:
                continue
            try:
                d = json.loads(line[5:].strip())
                delta = (d.get("choices") or [{}])[0].get("delta", {})
                t = delta.get("content")
                if t and isinstance(t, str) and t.strip():
                    content_parts.append(t)
            except Exception:
                pass
        full = "".join(content_parts)
        if status == 200 and full.strip():
            print(f"✅ 流式响应 {OAI_MODEL_SMALL}: {full[:80]!r}")
            passed += 1
        else:
            print(f"❌ 流式响应: HTTP {status}  text={full[:80]!r}")
            failed += 1
    except Exception as e:
        print(f"❌ 流式响应: {e}")
        failed += 1

    # usage 字段完整性
    oai_test("usage 字段完整性", {
        "model": OAI_MODEL_SMALL,
        "messages": [{"role": "user", "content": "say hi"}],
        "stream": False,
    }, checks={
        "prompt_tokens": lambda d, _: (d.get("usage", {}).get("prompt_tokens", 0) > 0,
                                        f"prompt_tokens={d.get('usage', {}).get('prompt_tokens')}"),
        "completion_tokens": lambda d, _: (d.get("usage", {}).get("completion_tokens", 0) > 0,
                                            f"completion_tokens={d.get('usage', {}).get('completion_tokens')}"),
    })

    # 性能基准
    start = time.time()
    status, raw, _ = request("/v1/chat/completions", {
        "model": OAI_MODEL_MED,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    })
    elapsed = time.time() - start
    data = json.loads(raw)
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    print(f"⏱️  OpenAI 端点延迟: {elapsed:.1f}s  回复: {text[:60]!r}")
    if elapsed > 15:
        print("⚠️  延迟 > 15s，可能有网络问题")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Claude Code 代理统一测试套件")
    parser.add_argument("--simple", action="store_true", help="只跑基础场景（1-5）")
    parser.add_argument("--tools", action="store_true", help="只跑工具 / thinking 相关（9-12）")
    parser.add_argument("--oai", action="store_true", help="只跑 OpenAI 透传端点（15）")
    parser.add_argument("--no-streaming", action="store_true", help="跳过流式测试")
    args = parser.parse_args()

    print(f"代理: http://{PROXY}:{PORT}  provider={_PROVIDER}")
    print(f"模型: big={OAI_MODEL_BIG}  med={OAI_MODEL_MED}  small={OAI_MODEL_SMALL}")

    # 选择运行场景
    if args.oai:
        test_openai_endpoint()
    elif args.simple:
        test_basic_connectivity()
        test_model_name_restore()
        test_system_prompt()
        if not args.no_streaming:
            test_streaming()
        test_message_format()
    elif args.tools:
        test_tools()
        test_tool_choice()
        test_thinking()
        if not args.no_streaming:
            test_streaming_thinking()
        test_error_handling()
    else:
        # 全部
        test_basic_connectivity()
        test_model_name_restore()
        test_system_prompt()
        if not args.no_streaming:
            test_streaming()
        test_message_format()
        test_params()
        test_stop_and_metadata()
        test_count_tokens()
        test_tools()
        test_tool_choice()
        test_thinking()
        test_error_handling()
        if not args.no_streaming:
            test_streaming_thinking()
        test_performance()
        test_openai_endpoint()

    # 总结
    print("\n" + "=" * 55)
    total = passed + failed + warn
    print(f"✅ 通过: {passed}  ⚠️  警告: {warn}  ❌ 失败: {failed}  (共 {total})")
    if failed == 0 and warn == 0:
        print("🎉 全部通过！Claude Code 可以正常使用")
    elif failed == 0:
        print("✅ 核心功能正常（有一些可忽略的警告）")
    else:
        print("❌ 有测试失败，需要修复")
        sys.exit(1)


if __name__ == "__main__":
    main()
