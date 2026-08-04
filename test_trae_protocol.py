"""
trae-work 协议转换单元测试（无网络依赖）

直接测 server.py 的转换函数，不请求上游：
  - _openai_to_trae_body：工具历史文本化 / 采样参数透传 / max_tokens 截断
  - _parse_dsml_tool_calls：[Tool Call:] 文本格式 + DSML 标记格式
  - _trae_chunk_to_openai：output 新旧格式映射

用法:
  .venv/bin/python test_trae_protocol.py
"""
import json
import sys

import server

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


# ─── 1. _openai_to_trae_body：工具历史文本化 ───
print("[1] 工具历史文本化")
body = {
    "model": "Doubao-Seed-Code",
    "messages": [
        {"role": "user", "content": "帮我看看当前目录"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_x1", "type": "function",
             "function": {"name": "bash", "arguments": '{"command":"ls -la"}'}}]},
        {"role": "tool", "tool_call_id": "call_x1", "content": "server.py targets.json"},
        {"role": "user", "content": "总结"},
    ],
}
out = server._openai_to_trae_body(body)
msgs = out["messages"]
check("assistant.tool_calls → content 文本化",
      "[Tool Call: bash]" in msgs[1]["content"][0]["text"])
check("tool 消息 → user 角色",
      msgs[2]["role"] == "user")
check("tool 消息 → [Tool Call Result: name] 前缀",
      msgs[2]["content"][0]["text"].startswith("[Tool Call Result: bash]"))
check("无孤立 role=tool 消息",
      all(m["role"] != "tool" for m in msgs))

# assistant content + tool_calls 并存
body2 = {
    "model": "glm-5.2",
    "messages": [
        {"role": "assistant", "content": "我先查一下", "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "file content"},
    ],
}
msgs2 = server._openai_to_trae_body(body2)["messages"]
check("content+tool_calls 拼接保留原文",
      msgs2[0]["content"][0]["text"].startswith("我先查一下"))
check("content+tool_calls 拼接含调用文本",
      "[Tool Call: read_file]" in msgs2[0]["content"][0]["text"])

# tool_call_id 无匹配
body3 = {
    "model": "glm-5.2",
    "messages": [
        {"role": "assistant", "content": "x", "tool_calls": [
            {"id": "c3", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "unknown", "content": ""},
    ],
}
msgs3 = server._openai_to_trae_body(body3)["messages"]
check("无匹配 tool_call_id → [Tool Call Result] 无后缀",
      msgs3[1]["content"][0]["text"] == "[Tool Call Result]")

# ─── 2. 采样参数透传 + max_tokens 截断 ───
print("[2] 采样参数透传")
body4 = {
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 32000, "temperature": 0.7, "top_p": 0.9,
    "presence_penalty": 0.1, "frequency_penalty": 0.2,
    "stop": ["END"], "seed": 42, "n": 1,
}
out4 = server._openai_to_trae_body(body4)
check("temperature 透传", out4.get("temperature") == 0.7)
check("top_p 透传", out4.get("top_p") == 0.9)
check("presence_penalty 透传", out4.get("presence_penalty") == 0.1)
check("frequency_penalty 透传", out4.get("frequency_penalty") == 0.2)
check("stop 数组透传", out4.get("stop") == ["END"])
check("seed 透传", out4.get("seed") == 42)
check("n<=1 不传", "n" not in out4)
check("max_tokens 透传", out4.get("max_tokens") == 32000)
check("max_tokens 超限截断 128000",
      server._openai_to_trae_body({"model": "glm-5.2", "messages": [],
                                   "max_tokens": 200000}).get("max_tokens") == 128000)

# ─── 3. _parse_dsml_tool_calls：[Tool Call:] + DSML ───
print("[3] 工具调用文本解析")
tc_text = '[Tool Call: bash]\nArguments: {"command":"grep -n xxx /a/b.py"}'
check("[Tool Call:] 识别", server._looks_like_dsml(tc_text))
tcs = server._parse_dsml_tool_calls(tc_text)
check("[Tool Call:] 解析出 1 个", len(tcs) == 1)
check("[Tool Call:] name 正确", tcs[0]["function"]["name"] == "bash")
check("[Tool Call:] arguments 为 JSON 字符串",
      json.loads(tcs[0]["function"]["arguments"])["command"] == "grep -n xxx /a/b.py")

dsml_text = ('<｜DSML｜><｜function｜><｜function name｜>get_weather</｜function｜>'
             '<｜parameter｜>{"city":"北京"}</｜parameter｜></｜function｜></｜DSML｜>')
check("DSML 识别", server._looks_like_dsml(dsml_text))

# DSML 变体 4（2026-08-03 实测，Doubao-Seed-Code）：<｜DSML｜invoke name=".."> +
# <｜DSML｜parameter name="..">，标签属性形态而非独立标签体。曾完全绕过前 3 种解析器
# （_DSML_LIKE_RE/_DSML_PAIR_RE/_TOOLCALL_XML_RE 全部不匹配），原样透传给客户端。
dsml_invoke_text = (
    '<｜DSML｜tool_calls>\n'
    '<｜DSML｜invoke name="bash">\n'
    '<｜DSML｜parameter name="command" string="true">ls -la</｜DSML｜parameter>\n'
    '</｜DSML｜invoke>\n'
    '</｜DSML｜tool_calls>'
)
check("DSML invoke 变体识别", server._looks_like_dsml(dsml_invoke_text))
tcs_invoke = server._parse_dsml_tool_calls(dsml_invoke_text)
check("DSML invoke 解析出 1 个", len(tcs_invoke) == 1)
check("DSML invoke name 正确", tcs_invoke[0]["function"]["name"] == "bash")
check("DSML invoke arguments 含 command 参数",
      json.loads(tcs_invoke[0]["function"]["arguments"])["command"] == "ls -la")
tcs_i, r_i, content_i = server._resolve_trae_text(dsml_invoke_text)
check("DSML invoke resolve 解析出 tool_calls", len(tcs_i) == 1)
check("DSML invoke resolve 正文已清洗", content_i == "")

# 多参数场景：一个 invoke 内含多个 parameter（如 edit 工具 oldString/newString）
dsml_invoke_multi = (
    '<｜DSML｜tool_calls>\n'
    '<｜DSML｜invoke name="edit">\n'
    '<｜DSML｜parameter name="filePath">/a/b.py</｜DSML｜parameter>\n'
    '<｜DSML｜parameter name="oldString">function foo() { return 1; }</｜DSML｜parameter>\n'
    '<｜DSML｜parameter name="newString">function foo() { return 2; }</｜DSML｜parameter>\n'
    '</｜DSML｜invoke>\n'
    '</｜DSML｜tool_calls>'
)
tcs_multi = server._parse_dsml_tool_calls(dsml_invoke_multi)
check("DSML invoke 多参数解析出 1 个", len(tcs_multi) == 1)
multi_args = json.loads(tcs_multi[0]["function"]["arguments"])
check("DSML invoke 多参数含 3 个 key", len(multi_args) == 3)
check("DSML invoke 多参数嵌套花括号未截断",
      multi_args["oldString"] == "function foo() { return 1; }")

plain = "这是一个普通回复"
check("普通文本不误识别", not server._looks_like_dsml(plain))
check("普通文本解析为空", server._parse_dsml_tool_calls(plain) == [])

# 兜底告警场景（2026-08-03 新增）：模拟未知的"第 5 种变体"——含 DSML 标记特征
# 但不匹配任何已知解析器格式，_resolve_trae_text 应记录 WARNING 但不抛异常，
# 且仍按普通文本处理（不吞掉，不中断），保证"未知新变体"能从日志被发现。
unknown_variant = '<｜DSML｜unknown_wrapper>some content the parser has never seen</｜DSML｜unknown_wrapper>'
check("未知变体仍判定为疑似 DSML", server._looks_like_dsml(unknown_variant))
check("未知变体已知解析器解析为空", server._parse_dsml_tool_calls(unknown_variant) == [])
tcs_u, r_u, content_u = server._resolve_trae_text(unknown_variant)
check("未知变体 resolve 不抛异常且 tool_calls 为空", tcs_u == [])
check("未知变体 resolve 原样保留在 content（不吞掉）", "unknown_wrapper" in content_u)

# ─── 4. _trae_chunk_to_openai：新旧格式 ───
print("[4] output 新旧格式映射")
oai_new = server._trae_chunk_to_openai({"type": "text", "content": "你好", "reasoning": "思考"}, "glm-5.2")
delta_new = oai_new["choices"][0]["delta"]
check("新格式 content", delta_new.get("content") == "你好")
check("新格式 reasoning→reasoning_content", delta_new.get("reasoning_content") == "思考")
oai_old = server._trae_chunk_to_openai({"response": "旧", "reasoning_content": "旧思"}, "glm-5.2")
delta_old = oai_old["choices"][0]["delta"]
check("旧格式 content", delta_old.get("content") == "旧")
check("旧格式 reasoning_content", delta_old.get("reasoning_content") == "旧思")

# ─── 5. _trae_tool_calls_to_openai：两种输入形态 + id/index 补齐 ───
print("[5] tool_calls 转换")
# DSML/XML 解析格式（function 键，无 id/index）→ 必须补 id+index（否则客户端不执行）
tc_in = [{"type": "function", "function": {"name": "bash", "arguments": '{"command":"ls"}'}}]
tc_out = server._trae_tool_calls_to_openai(tc_in)
check("function 键转换 name", bool(tc_out) and tc_out[0]["function"]["name"] == "bash")
check("function 键转换 arguments", tc_out[0]["function"]["arguments"] == '{"command":"ls"}')
check("缺 id 自动补", tc_out[0].get("id", "").startswith("call_"))
check("缺 index 自动补", tc_out[0].get("index") == 0)
# Trae 原生格式（function_call 键，带 id/index）→ 原样保留
native = [{"index": 3, "id": "call_x9", "type": "function",
           "function_call": {"name": "read", "arguments": '{"path":"a"}'}}]
native_out = server._trae_tool_calls_to_openai(native)
check("function_call 键转换", native_out[0]["function"]["name"] == "read")
check("原生 id 保留", native_out[0]["id"] == "call_x9")
check("原生 index 保留", native_out[0]["index"] == 3)
# 多工具 index 递增
multi = server._trae_tool_calls_to_openai([
    {"type": "function", "function": {"name": "a", "arguments": "{}"}},
    {"type": "function", "function": {"name": "b", "arguments": "{}"}},
])
check("多工具 index 递增", [x["index"] for x in multi] == [0, 1])
check("空输入返回空", server._trae_tool_calls_to_openai([]) == [])

# ─── 6. 完整流式 chunk 转换（DSML 解析 → OpenAI chunk 含 tool_calls）───
print("[6] 流式 tool_calls chunk 完整性")
from server import _trae_chunk_to_openai
oai_chunk = _trae_chunk_to_openai(
    {"response": "", "tool_calls": [{"type": "function", "function": tc["function"]} for tc in tc_in]},
    "glm-5.2",
)
delta_tc = oai_chunk["choices"][0]["delta"].get("tool_calls") or []
check("chunk 含 tool_calls", len(delta_tc) == 1)
check("chunk tool_calls 带 id", delta_tc[0].get("id", "").startswith("call_"))
check("chunk tool_calls 带 index", delta_tc[0].get("index") == 0)
check("chunk tool_calls arguments 完整", delta_tc[0]["function"]["arguments"] == '{"command":"ls"}')

# ─── 7. _resolve_trae_text：完整正文一次性解析（2026-08-02 架构重构）───
# 背景：曾在流式接收阶段边收边猜"这个 chunk 像不像标记"，导致以下真实抓包
# 案例反复出现卡顿/丢弃/泄漏。重构后 resp_text 只做纯累积，上游流结束后
# 才对完整文本一次性解析，因此这里直接喂"完整文本"验证解析正确性——
# 不再需要模拟"半截 chunk"，因为半截 chunk 已经不会被单独判断。
print("[7] _resolve_trae_text 完整正文解析")

# 7.1 纯文本，无任何标记 → 原样返回，无 tool_calls/reasoning
tcs, rtext, content = server._resolve_trae_text("你好，这是一段普通回复。")
check("纯文本 tool_calls 为空", tcs == [])
check("纯文本 reasoning 为空", rtext == "")
check("纯文本 content 原样保留", content == "你好，这是一段普通回复。")

# 7.2 [Tool Call: xxx] 文本格式 —— 曾因开头 "[" 独立成 chunk 被误判丢弃
# （2026-08-02 实测：trailing tool-fragment dropped）
full = '[Tool Call: edit]\nArguments: {"filePath":"a.py","oldString":"x","newString":"y"}'
tcs, rtext, content = server._resolve_trae_text(full)
check("[Tool Call:] 完整文本解析出 1 个 tool_call", len(tcs) == 1)
check("[Tool Call:] name 正确", tcs and tcs[0]["function"]["name"] == "edit")
check("[Tool Call:] arguments 含 filePath",
      tcs and json.loads(tcs[0]["function"]["arguments"])["filePath"] == "a.py")
check("[Tool Call:] content 已清洗（不含调用文本残留）",
      "Tool Call" not in content and "Arguments:" not in content)

# 7.3 reasoning JSON 字面量 + 后续普通正文 —— 曾因 reasoning JSON 未闭合导致
# 整轮卡到流结束才吐出（2026-08-02 实测：trailing leftover 未闭合 JSON）
full_r = '{"reasoning_content":"I need to check the file first."}继续处理下一步。'
tcs, rtext, content = server._resolve_trae_text(full_r)
check("reasoning JSON 提取正确", rtext == "I need to check the file first.")
check("reasoning 提取后正文保留", content == "继续处理下一步。")
check("reasoning 场景无 tool_calls", tcs == [])

# 7.3b 多段独立 reasoning JSON 字面量拼接输出 —— 曾因 _extract_reasoning_text
# 用 .search() 只提取第一段、.sub(count=1) 只摘除第一段，导致后续几段
# {"reasoning_content":"..."} JSON 字面量原样泄漏到正文（2026-08-02 实测：
# 用户截图看到裸露的 {"reasoning_content":"..."} 文本混在回复里）
full_multi_r = ('{"reasoning_content":"first thought."}'
                '{"reasoning_content":" continuing thought, still thinking."}'
                '现在开始修改代码。')
tcs, rtext, content = server._resolve_trae_text(full_multi_r)
check("多段 reasoning 全部提取拼接", rtext == "first thought. continuing thought, still thinking.")
check("多段 reasoning 提取后正文无 JSON 字面量泄漏",
      "reasoning_content" not in content, f"content={content!r}")
check("多段 reasoning 提取后正文保留", content == "现在开始修改代码。")
check("多段 reasoning 场景无 tool_calls", tcs == [])

# 7.4 reasoning JSON 未闭合（模型输出被截断，缺尾部 "}）—— 不应崩溃，
# 且因未闭合无法安全提取，应整体降级为纯文本原样返回（不丢失内容）
full_unclosed = '{"reasoning_content":"The user wants me to continue, so I need to update'
tcs, rtext, content = server._resolve_trae_text(full_unclosed)
check("reasoning 未闭合不提取", rtext == "")
check("reasoning 未闭合不崩溃且不丢内容", content == full_unclosed)
check("reasoning 未闭合无 tool_calls", tcs == [])

# 7.5 DSML 标记 + 前后夹杂普通文本 —— 完整块一次性清洗
full_dsml = ('前置说明。'
             '<｜DSML｜><｜function｜><｜function name｜>get_weather</｜function｜>'
             '<｜parameter｜>{"city":"北京"}</｜parameter｜></｜function｜></｜DSML｜>'
             '后续说明。')
tcs, rtext, content = server._resolve_trae_text(full_dsml)
check("DSML 完整文本解析出 1 个 tool_call", len(tcs) == 1)
check("DSML name 正确", tcs and tcs[0]["function"]["name"] == "get_weather")

# 7.6 <tool_call> XML 格式（trae-local-api 提示词注入方式）
full_xml = '好的，我来执行。\n<tool_call>\n{"name": "bash", "arguments": {"command": "ls -la"}}\n</tool_call>'
tcs, rtext, content = server._resolve_trae_text(full_xml)
check("XML tool_call 解析出 1 个", len(tcs) == 1)
check("XML tool_call name 正确", tcs and tcs[0]["function"]["name"] == "bash")

# 7.6b <tool_call> XML 含嵌套花括号 + 转义引号（edit 工具的 oldString/newString
# 常含 JS 代码片段，如 `{{}}` 双重花括号、`\"` 转义引号）—— 曾用正则
# _TOOLCALL_XML_JSON_RE（\{[\s\S]*?\} 非贪婪匹配 arguments）在第一个 "}"
# 处提前截断，导致 arguments JSON 半截、json.loads 校验失败、tcs 为空，
# 整段 <tool_call> 原始文本泄漏到正文（2026-08-02 实测：用户报告 IDE 界面
# 直接显示裸露的 <tool_call>...JSON...</tool_call> 文本，未被解析执行）
full_xml_nested = ('<tool_call>\n{"name": "edit", "arguments": '
                    '{"filePath": "a.js", '
                    '"oldString": "function f(x) {{\\n  return x;\\n}}", '
                    '"newString": "function f(x) {{\\n  return x + 1;\\n}}"}}\n'
                    '</tool_call>')
tcs, rtext, content = server._resolve_trae_text(full_xml_nested)
check("XML 嵌套花括号解析出 1 个 tool_call", len(tcs) == 1)
check("XML 嵌套花括号 name 正确", tcs and tcs[0]["function"]["name"] == "edit")
if tcs:
    args = json.loads(tcs[0]["function"]["arguments"])
    check("XML 嵌套花括号 oldString 完整", args.get("oldString") == "function f(x) {{\n  return x;\n}}")
    check("XML 嵌套花括号 newString 完整", args.get("newString") == "function f(x) {{\n  return x + 1;\n}}")
check("XML 嵌套花括号场景无原始标记泄漏到正文", "tool_call" not in content, f"content={content!r}")

# 7.6c <tool_call> 未闭合（模型输出被截断）—— 不应崩溃，原样保留在 content
# （既然流已结束，这是真实截断，原样展示比静默丢弃更利于排查）
full_xml_unclosed = '开始执行。<tool_call>\n{"name": "bash", "arguments": {"command": "ls'
tcs, rtext, content = server._resolve_trae_text(full_xml_unclosed)
check("XML 未闭合不崩溃且不丢内容", content == full_xml_unclosed)
check("XML 未闭合无 tool_calls", tcs == [])

# 7.7 Wave 2：<seed_call> 的 invoke/function 两种非对称闭合变体
seed_call_invoke = '''<seed_call>
<invoke name="bash">
<parameter name="command">.venv/bin/python -c "import server; print('IMPORT OK')"</parameter>
<parameter name="workdir">/root/shared-workspace/claude-code-proxy</parameter>
</invoke>
</tool_call>'''
seed_a_expected = {
    "command": '.venv/bin/python -c "import server; print(\'IMPORT OK\')"',
    "workdir": "/root/shared-workspace/claude-code-proxy",
}
seed_a_tcs, seed_a_reasoning, seed_a_content = server._resolve_trae_text(seed_call_invoke)
check("seed_call invoke 非对称闭合精确解析",
      len(seed_a_tcs) == 1
      and seed_a_tcs[0]["function"]["name"] == "bash"
      and json.loads(seed_a_tcs[0]["function"]["arguments"]) == seed_a_expected
      and seed_a_reasoning == ""
      and seed_a_content == ""
      and "seed_call" not in seed_a_content
      and "parameter" not in seed_a_content)

seed_call_function = ('<seed_call><function name="bash"><parameter name="command" string="true">'
                      'git add server.py && git status</parameter><parameter name="workdir" string="true">'
                      '/root/shared-workspace/claude-code-proxy</parameter></function></seed:tool_call>')
seed_b_expected = {
    "command": "git add server.py && git status",
    "workdir": "/root/shared-workspace/claude-code-proxy",
}
seed_b_tcs, seed_b_reasoning, seed_b_content = server._resolve_trae_text(seed_call_function)
check("seed_call function string 属性精确解析",
      len(seed_b_tcs) == 1
      and seed_b_tcs[0]["function"]["name"] == "bash"
      and json.loads(seed_b_tcs[0]["function"]["arguments"]) == seed_b_expected
      and seed_b_reasoning == ""
      and seed_b_content == ""
      and "seed_call" not in seed_b_content
      and "string=\"true\"" not in seed_b_content)

# Todo 6 合成 fixture：同一 command 常量既构造 XML JSON，也作为精确期望值，避免
# fixture 与断言分别维护；保留多行、嵌套花括号和转义引号三类历史截断风险。
mixed_prefix = ("Now I'll perform all deletions and insertions using precise multi-line matches.\n"
                "First, let me read the file to understand its structure.")
mixed_command = ("cd /root/shared-workspace/claude-code-proxy && python3 -c '\n"
                 "import re\n"
                 'pattern = re.compile(r"[{\\\\}]")\n'
                 "def process(data):\n"
                 "    result = {key: [item for item in value] for key, value in data.items()}\n"
                 "    return result\n"
                 'print(process({"a": [1, 2, {\\"nested\\": True}]}))\n'
                 "'\n")
mixed_suffix = "Let me verify the change took effect."
mixed_fixture = (mixed_prefix + "\n<tool_call>\n"
                 + json.dumps({"name": "bash", "arguments": {"command": mixed_command}}, ensure_ascii=False)
                 + "\n</tool_call>\n" + mixed_suffix)
mixed_tcs, mixed_reasoning, mixed_content = server._resolve_trae_text(mixed_fixture)
check("自由文本混杂多行 tool_call JSON 精确解析",
      len(mixed_tcs) == 1
      and mixed_tcs[0]["function"]["name"] == "bash"
      and json.loads(mixed_tcs[0]["function"]["arguments"]) == {"command": mixed_command}
      and mixed_reasoning == ""
      and mixed_content == f"{mixed_prefix}\n\n{mixed_suffix}"
      and "<tool_call>" not in mixed_content
      and '"arguments"' not in mixed_content)

wave2_plain_text = "This is ordinary prose with braces {not JSON} and no tool marker."
plain_tcs, plain_reasoning, plain_content = server._resolve_trae_text(wave2_plain_text)
check("Wave 2 纯自由文本不误判工具调用",
      plain_tcs == []
      and plain_reasoning == ""
      and plain_content == wave2_plain_text
      and "<tool_call>" not in plain_content
      and '"arguments"' not in plain_content)

# 7.7 空文本 → 全部返回空
tcs, rtext, content = server._resolve_trae_text("")
check("空文本 tool_calls 为空", tcs == [])
check("空文本 reasoning 为空", rtext == "")
check("空文本 content 为空", content == "")

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
