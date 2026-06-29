#!/usr/bin/env python3
"""Debug tool conversion by directly testing QClaw with different formats."""
import json, httpx

QCLAW_URL = "http://127.0.0.1:19000/proxy/llm/chat/completions"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer __QCLAW_AUTH_GATEWAY_MANAGED__",
    "User-Agent": "OpenAI/JS 6.39.1",
    "x-agent-id": "main",
}
MODEL = "pool-deepseek-v4-pro"

def test(title, body):
    print(f"\n{'='*60}")
    print(f"TEST: {title}")
    print(f"{'='*60}")
    print("BODY:", json.dumps(body, ensure_ascii=False, indent=2)[:800])
    resp = httpx.post(QCLAW_URL, json=body, headers=HEADERS, timeout=30)
    d = resp.json()
    msg = d.get("choices", [{}])[0].get("message", {})
    content = msg.get("content", "")
    tool_calls = msg.get("tool_calls", [])
    print(f"HTTP {resp.status_code}")
    print(f"content: {content[:200] if content else '(EMPTY)'}")
    print(f"tool_calls: {json.dumps(tool_calls, ensure_ascii=False)[:200] if tool_calls else '(NONE)'}")
    print(f"finish_reason: {d['choices'][0].get('finish_reason')}")

# Test 1: tool role format (standard OpenAI)
test("Standard OpenAI tool role format", {
    "model": MODEL,
    "max_tokens": 200,
    "messages": [
        {"role": "system", "content": "You are Claude. Use tools."},
        {"role": "user", "content": "ls"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "README.md\nserver.py"},
    ],
    "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}],
})

# Test 2: assistant with content + tool_calls
test("Assistant with content + tool_calls", {
    "model": MODEL,
    "max_tokens": 200,
    "messages": [
        {"role": "system", "content": "You are Claude. Use tools."},
        {"role": "user", "content": "ls"},
        {"role": "assistant", "content": "Let me check.", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "README.md\nserver.py"},
    ],
    "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}],
})

# Test 3: empty string content instead of null
test("Assistant with empty content string", {
    "model": MODEL,
    "max_tokens": 200,
    "messages": [
        {"role": "system", "content": "You are Claude."},
        {"role": "user", "content": "ls"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "README.md\nserver.py"},
    ],
    "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}],
})

# Test 4: no content field at all
test("Assistant without content field", {
    "model": MODEL,
    "max_tokens": 200,
    "messages": [
        {"role": "system", "content": "You are Claude."},
        {"role": "user", "content": "ls"},
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "README.md\nserver.py"},
    ],
    "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}],
})

# Test 5: tool_calls with no tools definition
test("No tools in request (should not use tools)", {
    "model": MODEL,
    "max_tokens": 200,
    "messages": [
        {"role": "system", "content": "You are Claude."},
        {"role": "user", "content": "ls"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "README.md\nserver.py"},
    ],
})
