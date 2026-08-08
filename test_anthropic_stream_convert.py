"""
OpenAI SSE → Anthropic SSE 流式转换回归测试（移植 LiteLLM 测试思路）

参考 LiteLLM tests/llm_translation/test_anthropic_completion.py 的
`streaming_format_tests` 事件序列断言模式 + 边界用例：
- 文本流式：message_start → content_block_start → delta* → stop → message_delta → message_stop
- thinking 块切换（reasoning_content → thinking，再切回 text）
- 工具调用（tool_calls → tool_use 块 + input_json_delta 分片）
- 并行工具（新 function.name = 新块）
- usage 合并（choices=[] 的 usage 专帧并入 message_delta）
- 块切换重发（触发 chunk 的 delta 不丢）
- finish_reason 映射（stop/length/tool_calls/function_call）

纯脚本式（无 pytest），.venv/bin/python test_anthropic_stream_convert.py 直接跑。
"""

import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from anthropic_stream_convert import convert_openai_sse_to_anthropic  # noqa: E402

passed = 0
failed = 0


def _events(data_lines):
    """转换并返回 [(event_type, payload_dict), ...] 事件列表。"""
    out = []
    for b in convert_openai_sse_to_anthropic(data_lines, "test-model"):
        text = b.decode("utf-8")
        etype = text.split("\n")[0].replace("event: ", "").strip()
        payload = {}
        for line in text.split("\n"):
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    payload = {}
        out.append((etype, payload))
    return out


def _oai_chunks(chunk_dicts):
    """构造 OpenAI SSE data 行（每个 dict 一个 chunk + [DONE]）。"""
    return [json.dumps(c) for c in chunk_dicts] + ["[DONE]"]


def test_text_stream_sequence():
    """S1: 文本流式完整事件序列。"""
    chunks = [
        {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},  # role-only opener，应被吞
        {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": " world"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    ]
    events = _events(_oai_chunks(chunks))
    types = [t for t, _ in events]
    assert types == [
        "message_start", "content_block_start", "content_block_delta",
        "content_block_delta", "content_block_stop", "message_delta", "message_stop",
    ], f"文本事件序列错误: {types}"
    # message_start 结构
    msg = events[0][1]["message"]
    assert msg["role"] == "assistant" and msg["content"] == [] and msg["model"] == "test-model"
    # content_block_start 体为空（LiteLLM 约定）
    assert events[1][1]["content_block"] == {"type": "text", "text": ""}
    # delta 内容
    assert events[2][1]["delta"] == {"type": "text_delta", "text": "Hello"}
    assert events[3][1]["delta"] == {"type": "text_delta", "text": " world"}
    # message_delta：stop → end_turn + usage 合并（cache 扣除）
    md = events[5][1]
    assert md["delta"]["stop_reason"] == "end_turn"
    assert md["usage"]["input_tokens"] == 10
    assert md["usage"]["output_tokens"] == 5
    print("PASS test_text_stream_sequence")


def test_thinking_block_switch():
    """S2: thinking（reasoning_content）→ text 块切换。"""
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "思考中"}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning_content": "继续"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "答案"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events = _events(_oai_chunks(chunks))
    types = [t for t, _ in events]
    # thinking 块 + text 块，各含 start/stop，块切换在中间
    assert types.count("content_block_start") == 2, types
    assert types.count("content_block_stop") == 2, types
    # 第一块是 thinking
    cb1 = events[1][1]["content_block"]
    assert cb1["type"] == "thinking", f"第一块应为 thinking: {cb1}"
    # thinking delta
    assert events[2][1]["delta"]["type"] == "thinking_delta"
    # 第二块是 text（切换后重发触发 chunk 的 delta——首 token 不丢）
    cb2_types = [e[1]["content_block"]["type"] for e in events if e[0] == "content_block_start"]
    assert cb2_types == ["thinking", "text"], cb2_types
    print("PASS test_thinking_block_switch")


def test_tool_use_conversion():
    """S3: 工具调用 → tool_use 块 + input_json_delta 分片。"""
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                                                "function": {"name": "get_weather", "arguments": ""}}]},
                      "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city": "SH"}'}}]},
                      "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = _events(_oai_chunks(chunks))
    types = [t for t, _ in events]
    cb = events[1][1]["content_block"]
    assert cb["type"] == "tool_use", f"应为 tool_use 块: {cb}"
    assert cb["name"] == "get_weather"
    assert cb["id"] == "call_1"
    # 第二个 chunk 的 arguments 走 input_json_delta
    deltas = [e[1]["delta"] for e in events if e[0] == "content_block_delta"]
    assert deltas and deltas[0]["type"] == "input_json_delta", deltas
    assert deltas[0]["partial_json"] == '{"city": "SH"}'
    # finish_reason=tool_calls → stop_reason=tool_use
    md = [e[1] for e in events if e[0] == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "tool_use", md
    print("PASS test_tool_use_conversion")


def test_parallel_tools_new_block():
    """S4: 并行工具——新 function.name 出现即开新块。"""
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                                                "function": {"name": "get_weather", "arguments": ""}}]},
                      "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city": "A"}'}}]},
                      "finish_reason": None}]},
        # 新 tool name → 新块
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_2",
                                                "function": {"name": "get_time", "arguments": ""}}]},
                      "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": '{"tz": "UTC"}'}}]},
                      "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = _events(_oai_chunks(chunks))
    starts = [e[1]["content_block"] for e in events if e[0] == "content_block_start"]
    assert len(starts) == 2, f"应有 2 个 tool_use 块: {[s['name'] for s in starts]}"
    assert [s["name"] for s in starts] == ["get_weather", "get_time"], starts
    # 两个块的 index 递增
    idxs = [e[1]["index"] for e in events if e[0] == "content_block_start"]
    assert idxs == [0, 1], idxs
    print("PASS test_parallel_tools_new_block")


def test_usage_merge():
    """S5: usage 专帧（choices=[]）并入 message_delta（cache 扣除）。"""
    chunks = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                   "prompt_tokens_details": {"cached_tokens": 30}}},
    ]
    events = _events(_oai_chunks(chunks))
    md = [e[1] for e in events if e[0] == "message_delta"][0]
    # input_tokens = prompt_tokens - cache_read = 100 - 30 = 70
    assert md["usage"]["input_tokens"] == 70, md["usage"]
    assert md["usage"]["output_tokens"] == 20
    assert md["usage"]["cache_read_input_tokens"] == 30
    # message_delta 之后只有 message_stop
    types = [t for t, _ in events]
    md_idx = types.index("message_delta")
    assert types[md_idx + 1:] == ["message_stop"], types
    print("PASS test_usage_merge")


def test_block_switch_reemit():
    """S6: 块切换重发触发 chunk 的 delta（首 token 不丢）。"""
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "想"}, "finish_reason": None}]},
        # thinking → text 切换：触发 chunk 带 content，必须重发
        {"choices": [{"delta": {"content": "第一"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "字"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events = _events(_oai_chunks(chunks))
    text_deltas = [e[1]["delta"]["text"] for e in events
                   if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "text_delta"]
    assert text_deltas == ["第一", "字"], f"切换后首 token 丢失: {text_deltas}"
    print("PASS test_block_switch_reemit")


def test_finish_reason_mapping():
    """S7: finish_reason 映射（stop/length/tool_calls/function_call）。"""
    from anthropic_stream_convert import _FINISH_MAP
    assert _FINISH_MAP["stop"] == "end_turn"
    assert _FINISH_MAP["length"] == "max_tokens"
    assert _FINISH_MAP["tool_calls"] == "tool_use"
    assert _FINISH_MAP["function_call"] == "tool_use"  # LiteLLM 漏映射，补上
    print("PASS test_finish_reason_mapping")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
            globals()["passed"] += 1
        except AssertionError as e:
            globals()["failed"] += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            globals()["failed"] += 1
            print(f"ERROR {t.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
