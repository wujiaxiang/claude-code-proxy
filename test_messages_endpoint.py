#!/usr/bin/env python3
"""Test /v1/messages endpoint with realistic Claude Code client requests."""
import json
import httpx
import sys

BASE_URL = "http://127.0.0.1:8082"
TIMEOUT = 60

def test(name, payload, expect_status=200):
    """Send request and check status code."""
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"{'='*60}")
    try:
        resp = httpx.post(
            f"{BASE_URL}/v1/messages",
            json=payload,
            timeout=TIMEOUT,
        )
        status_emoji = "✅" if resp.status_code == expect_status else "❌"
        print(f"{status_emoji} Status: {resp.status_code} (expected {expect_status})")
        if resp.status_code != expect_status:
            print(f"Response body: {resp.text[:2000]}")
            return False
        # For 200, try to parse and show key fields
        if resp.status_code == 200:
            try:
                data = resp.json()
                if "content" in data:
                    block_types = [b.get("type") for b in data["content"]]
                    print(f"  Content blocks: {block_types}")
                    print(f"  Stop reason: {data.get('stop_reason')}")
                elif resp.headers.get("content-type", "").startswith("text/event-stream"):
                    pass  # streaming, already consumed
                else:
                    print(f"  Response keys: {list(data.keys())[:10]}")
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"❌ Exception: {e}")
        return False

def test_stream(name, payload, expect_status=200):
    """Send streaming request and verify SSE event sequence."""
    print(f"\n{'='*60}")
    print(f"STREAM TEST: {name}")
    print(f"{'='*60}")
    try:
        with httpx.stream(
            "POST",
            f"{BASE_URL}/v1/messages",
            json=payload,
            timeout=TIMEOUT,
        ) as resp:
            status_emoji = "✅" if resp.status_code == expect_status else "❌"
            print(f"{status_emoji} Status: {resp.status_code} (expected {expect_status})")
            if resp.status_code != expect_status:
                print(f"Response body: {resp.read().decode('utf-8', errors='replace')[:2000]}")
                return False
            
            events = []
            current_event = None
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    current_event = line[7:]
                elif line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        events.append(("done", None))
                        break
                    try:
                        data = json.loads(data_str)
                        events.append((current_event or "unknown", data))
                    except json.JSONDecodeError:
                        pass
            
            # Print event sequence
            print(f"  Event sequence ({len(events)} events):")
            for i, (evt_type, evt_data) in enumerate(events):
                if evt_type == "done":
                    print(f"    [{i}] DONE")
                elif evt_type == "content_block_start":
                    idx = evt_data.get("index")
                    block_type = evt_data.get("content_block", {}).get("type")
                    print(f"    [{i}] content_block_start: index={idx}, type={block_type}")
                elif evt_type == "content_block_stop":
                    idx = evt_data.get("index")
                    print(f"    [{i}] content_block_stop: index={idx}")
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
                    print(f"    [{i}] {evt_type}: {str(evt_data)[:100]}")
            
            return True
    except Exception as e:
        print(f"❌ Exception: {e}")
        return False

# ============================================================
# Test cases
# ============================================================

results = []

# --- Test 1: Basic text, no thinking, no tools ---
results.append(test("basic_text", {
    "model": "claude-sonnet-5",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "1+1=? Reply with just the number."}],
}))

# --- Test 2: thinking type=adaptive (Claude Code format) ---
results.append(test("thinking_adaptive", {
    "model": "claude-sonnet-5",
    "max_tokens": 1024,
    "thinking": {"type": "adaptive"},
    "messages": [{"role": "user", "content": "1+1=? Reply with just the number."}],
}))

# --- Test 3: thinking type=enabled with budget_tokens ---
results.append(test("thinking_enabled_budget", {
    "model": "claude-sonnet-5",
    "max_tokens": 1024,
    "thinking": {"type": "enabled", "budget_tokens": 5000},
    "messages": [{"role": "user", "content": "1+1=? Reply with just the number."}],
}))

# --- Test 4: Assistant message with thinking block in history (THE 422 BUG) ---
results.append(test("thinking_in_history", {
    "model": "claude-sonnet-5",
    "max_tokens": 1024,
    "thinking": {"type": "adaptive"},
    "messages": [
        {"role": "user", "content": "1+1=?"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "The user asked 1+1. The answer is 2.", "signature": ""},
            {"type": "text", "text": "2"}
        ]},
        {"role": "user", "content": "2+2=?"},
    ],
}))

# --- Test 5: With tools ---
results.append(test("with_tools", {
    "model": "claude-sonnet-5",
    "max_tokens": 1024,
    "tools": [{
        "name": "get_weather",
        "description": "Get weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    }],
    "messages": [{"role": "user", "content": "What's the weather in Beijing?"}],
}))

# --- Test 6: thinking + tools ---
results.append(test("thinking_plus_tools", {
    "model": "claude-sonnet-5",
    "max_tokens": 1024,
    "thinking": {"type": "adaptive"},
    "tools": [{
        "name": "get_weather",
        "description": "Get weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    }],
    "messages": [{"role": "user", "content": "What's the weather in Beijing?"}],
}))

# --- Test 7: Full Claude Code style request (system, tools, thinking history) ---
results.append(test("full_claude_code_style", {
    "model": "claude-sonnet-5",
    "max_tokens": 4096,
    "thinking": {"type": "adaptive"},
    "system": [{"type": "text", "text": "You are a helpful assistant."}],
    "tools": [
        {
            "name": "Bash",
            "description": "Execute bash commands",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"]
            }
        },
        {
            "name": "Read",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    ],
    "messages": [
        {"role": "user", "content": "What is 1+1?"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Simple math: 1+1=2", "signature": ""},
            {"type": "text", "text": "1+1=2"}
        ]},
        {"role": "user", "content": "What is 2+2?"},
    ],
}))

# --- Test 8: output_config field (seen in real request) ---
results.append(test("output_config_extra_field", {
    "model": "claude-sonnet-5",
    "max_tokens": 100,
    "thinking": {"type": "adaptive"},
    "output_config": {"format": "text"},
    "messages": [{"role": "user", "content": "1+1=?"}],
}))

# --- Stream Test 1: basic streaming ---
test_stream("stream_basic", {
    "model": "claude-sonnet-5",
    "max_tokens": 100,
    "stream": True,
    "messages": [{"role": "user", "content": "1+1=? Reply with just the number."}],
})

# --- Stream Test 2: streaming with thinking ---
test_stream("stream_thinking", {
    "model": "claude-sonnet-5",
    "max_tokens": 1024,
    "stream": True,
    "thinking": {"type": "adaptive"},
    "messages": [{"role": "user", "content": "1+1=? Reply with just the number."}],
})

# --- Stream Test 3: streaming with thinking history in messages ---
test_stream("stream_thinking_history", {
    "model": "claude-sonnet-5",
    "max_tokens": 1024,
    "stream": True,
    "thinking": {"type": "adaptive"},
    "messages": [
        {"role": "user", "content": "1+1=?"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "1+1=2", "signature": ""},
            {"type": "text", "text": "2"}
        ]},
        {"role": "user", "content": "2+2=?"},
    ],
})

# ============================================================
# Summary
# ============================================================
print(f"\n{'='*60}")
print(f"SUMMARY: {sum(results)}/{len(results)} passed")
print(f"{'='*60}")
if not all(results):
    failed = [i+1 for i, r in enumerate(results) if not r]
    print(f"Failed tests: {failed}")
    sys.exit(1)
else:
    print("All tests passed! ✅")
    sys.exit(0)
