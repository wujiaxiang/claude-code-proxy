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

import gateways.trae_work as trae

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
out = trae._openai_to_trae_body(body)
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
msgs2 = trae._openai_to_trae_body(body2)["messages"]
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
msgs3 = trae._openai_to_trae_body(body3)["messages"]
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
out4 = trae._openai_to_trae_body(body4)
check("temperature 透传", out4.get("temperature") == 0.7)
check("top_p 透传", out4.get("top_p") == 0.9)
check("presence_penalty 透传", out4.get("presence_penalty") == 0.1)
check("frequency_penalty 透传", out4.get("frequency_penalty") == 0.2)
check("stop 数组透传", out4.get("stop") == ["END"])
check("seed 透传", out4.get("seed") == 42)
check("n<=1 不传", "n" not in out4)
check("max_tokens 透传", out4.get("max_tokens") == 32000)
check("max_tokens 超限截断 128000",
      trae._openai_to_trae_body({"model": "glm-5.2", "messages": [],
                                   "max_tokens": 200000}).get("max_tokens") == 128000)

# ─── 3. _parse_dsml_tool_calls：[Tool Call:] + DSML ───
print("[3] 工具调用文本解析")
tc_text = '[Tool Call: bash]\nArguments: {"command":"grep -n xxx /a/b.py"}'
check("[Tool Call:] 识别", trae._looks_like_dsml(tc_text))
tcs = trae._parse_dsml_tool_calls(tc_text)
check("[Tool Call:] 解析出 1 个", len(tcs) == 1)
check("[Tool Call:] name 正确", tcs[0]["function"]["name"] == "bash")
check("[Tool Call:] arguments 为 JSON 字符串",
      json.loads(tcs[0]["function"]["arguments"])["command"] == "grep -n xxx /a/b.py")

dsml_text = ('<｜DSML｜><｜function｜><｜function name｜>get_weather</｜function｜>'
             '<｜parameter｜>{"city":"北京"}</｜parameter｜></｜function｜></｜DSML｜>')
check("DSML 识别", trae._looks_like_dsml(dsml_text))

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
check("DSML invoke 变体识别", trae._looks_like_dsml(dsml_invoke_text))
tcs_invoke = trae._parse_dsml_tool_calls(dsml_invoke_text)
check("DSML invoke 解析出 1 个", len(tcs_invoke) == 1)
check("DSML invoke name 正确", tcs_invoke[0]["function"]["name"] == "bash")
check("DSML invoke arguments 含 command 参数",
      json.loads(tcs_invoke[0]["function"]["arguments"])["command"] == "ls -la")
tcs_i, r_i, content_i = trae._resolve_trae_text(dsml_invoke_text)
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
tcs_multi = trae._parse_dsml_tool_calls(dsml_invoke_multi)
check("DSML invoke 多参数解析出 1 个", len(tcs_multi) == 1)
multi_args = json.loads(tcs_multi[0]["function"]["arguments"])
check("DSML invoke 多参数含 3 个 key", len(multi_args) == 3)
check("DSML invoke 多参数嵌套花括号未截断",
      multi_args["oldString"] == "function foo() { return 1; }")

plain = "这是一个普通回复"
check("普通文本不误识别", not trae._looks_like_dsml(plain))
check("普通文本解析为空", trae._parse_dsml_tool_calls(plain) == [])

# 兜底告警场景（2026-08-03 新增）：模拟未知的"第 5 种变体"——含 DSML 标记特征
# 但不匹配任何已知解析器格式，_resolve_trae_text 应记录 WARNING 但不抛异常，
# 且仍按普通文本处理（不吞掉，不中断），保证"未知新变体"能从日志被发现。
unknown_variant = '<｜DSML｜unknown_wrapper>some content the parser has never seen</｜DSML｜unknown_wrapper>'
check("未知变体仍判定为疑似 DSML", trae._looks_like_dsml(unknown_variant))
check("未知变体已知解析器解析为空", trae._parse_dsml_tool_calls(unknown_variant) == [])
tcs_u, r_u, content_u = trae._resolve_trae_text(unknown_variant)
check("未知变体 resolve 不抛异常且 tool_calls 为空", tcs_u == [])
check("未知变体 resolve 原样保留在 content（不吞掉）", "unknown_wrapper" in content_u)

# ─── 4. _trae_chunk_to_openai：新旧格式 ───
print("[4] output 新旧格式映射")
oai_new = trae._trae_chunk_to_openai({"type": "text", "content": "你好", "reasoning": "思考"}, "glm-5.2")
delta_new = oai_new["choices"][0]["delta"]
check("新格式 content", delta_new.get("content") == "你好")
check("新格式 reasoning→reasoning_content", delta_new.get("reasoning_content") == "思考")
oai_old = trae._trae_chunk_to_openai({"response": "旧", "reasoning_content": "旧思"}, "glm-5.2")
delta_old = oai_old["choices"][0]["delta"]
check("旧格式 content", delta_old.get("content") == "旧")
check("旧格式 reasoning_content", delta_old.get("reasoning_content") == "旧思")

# ─── 5. _trae_tool_calls_to_openai：两种输入形态 + id/index 补齐 ───
print("[5] tool_calls 转换")
# DSML/XML 解析格式（function 键，无 id/index）→ 必须补 id+index（否则客户端不执行）
tc_in = [{"type": "function", "function": {"name": "bash", "arguments": '{"command":"ls"}'}}]
tc_out = trae._trae_tool_calls_to_openai(tc_in)
check("function 键转换 name", bool(tc_out) and tc_out[0]["function"]["name"] == "bash")
check("function 键转换 arguments", tc_out[0]["function"]["arguments"] == '{"command":"ls"}')
check("缺 id 自动补", tc_out[0].get("id", "").startswith("call_"))
check("缺 index 自动补", tc_out[0].get("index") == 0)
# Trae 原生格式（function_call 键，带 id/index）→ 原样保留
native = [{"index": 3, "id": "call_x9", "type": "function",
           "function_call": {"name": "read", "arguments": '{"path":"a"}'}}]
native_out = trae._trae_tool_calls_to_openai(native)
check("function_call 键转换", native_out[0]["function"]["name"] == "read")
check("原生 id 保留", native_out[0]["id"] == "call_x9")
check("原生 index 保留", native_out[0]["index"] == 3)
# 多工具 index 递增
multi = trae._trae_tool_calls_to_openai([
    {"type": "function", "function": {"name": "a", "arguments": "{}"}},
    {"type": "function", "function": {"name": "b", "arguments": "{}"}},
])
check("多工具 index 递增", [x["index"] for x in multi] == [0, 1])
check("空输入返回空", trae._trae_tool_calls_to_openai([]) == [])

# ─── 6. 完整流式 chunk 转换（DSML 解析 → OpenAI chunk 含 tool_calls）───
print("[6] 流式 tool_calls chunk 完整性")
from gateways.trae_work import _trae_chunk_to_openai
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
tcs, rtext, content = trae._resolve_trae_text("你好，这是一段普通回复。")
check("纯文本 tool_calls 为空", tcs == [])
check("纯文本 reasoning 为空", rtext == "")
check("纯文本 content 原样保留", content == "你好，这是一段普通回复。")

# 7.2 [Tool Call: xxx] 文本格式 —— 曾因开头 "[" 独立成 chunk 被误判丢弃
# （2026-08-02 实测：trailing tool-fragment dropped）
full = '[Tool Call: edit]\nArguments: {"filePath":"a.py","oldString":"x","newString":"y"}'
tcs, rtext, content = trae._resolve_trae_text(full)
check("[Tool Call:] 完整文本解析出 1 个 tool_call", len(tcs) == 1)
check("[Tool Call:] name 正确", tcs and tcs[0]["function"]["name"] == "edit")
check("[Tool Call:] arguments 含 filePath",
      tcs and json.loads(tcs[0]["function"]["arguments"])["filePath"] == "a.py")
check("[Tool Call:] content 已清洗（不含调用文本残留）",
      "Tool Call" not in content and "Arguments:" not in content)

# 7.3 reasoning JSON 字面量 + 后续普通正文 —— 曾因 reasoning JSON 未闭合导致
# 整轮卡到流结束才吐出（2026-08-02 实测：trailing leftover 未闭合 JSON）
full_r = '{"reasoning_content":"I need to check the file first."}继续处理下一步。'
tcs, rtext, content = trae._resolve_trae_text(full_r)
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
tcs, rtext, content = trae._resolve_trae_text(full_multi_r)
check("多段 reasoning 全部提取拼接", rtext == "first thought. continuing thought, still thinking.")
check("多段 reasoning 提取后正文无 JSON 字面量泄漏",
      "reasoning_content" not in content, f"content={content!r}")
check("多段 reasoning 提取后正文保留", content == "现在开始修改代码。")
check("多段 reasoning 场景无 tool_calls", tcs == [])

# 7.4 reasoning JSON 未闭合（模型输出被截断，缺尾部 "}）—— 不应崩溃，
# 且因未闭合无法安全提取，应整体降级为纯文本原样返回（不丢失内容）
full_unclosed = '{"reasoning_content":"The user wants me to continue, so I need to update'
tcs, rtext, content = trae._resolve_trae_text(full_unclosed)
check("reasoning 未闭合不提取", rtext == "")
check("reasoning 未闭合不崩溃且不丢内容", content == full_unclosed)
check("reasoning 未闭合无 tool_calls", tcs == [])

# 7.5 DSML 标记 + 前后夹杂普通文本 —— 完整块一次性清洗
full_dsml = ('前置说明。'
             '<｜DSML｜><｜function｜><｜function name｜>get_weather</｜function｜>'
             '<｜parameter｜>{"city":"北京"}</｜parameter｜></｜function｜></｜DSML｜>'
             '后续说明。')
tcs, rtext, content = trae._resolve_trae_text(full_dsml)
check("DSML 完整文本解析出 1 个 tool_call", len(tcs) == 1)
check("DSML name 正确", tcs and tcs[0]["function"]["name"] == "get_weather")

# 7.6 <tool_call> XML 格式（trae-local-api 提示词注入方式）
full_xml = '好的，我来执行。\n<tool_call>\n{"name": "bash", "arguments": {"command": "ls -la"}}\n</tool_call>'
tcs, rtext, content = trae._resolve_trae_text(full_xml)
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
tcs, rtext, content = trae._resolve_trae_text(full_xml_nested)
check("XML 嵌套花括号解析出 1 个 tool_call", len(tcs) == 1)
check("XML 嵌套花括号 name 正确", tcs and tcs[0]["function"]["name"] == "edit")
if tcs:
    args = json.loads(tcs[0]["function"]["arguments"])
    check("XML 嵌套花括号 oldString 完整", args.get("oldString") == "function f(x) {{\n  return x;\n}}")
    check("XML 嵌套花括号 newString 完整", args.get("newString") == "function f(x) {{\n  return x + 1;\n}}")
check("XML 嵌套花括号场景无原始标记泄漏到正文", "tool_call" not in content, f"content={content!r}")

# 7.6c <tool_call> 未闭合（模型输出被截断）—— 不应崩溃；2026-08-04 起剥离未闭合
# 开标签到文本末尾（命中疑似工具调用标记即剥离，不再原样透传半截 XML/JSON），
# 开标签之前的正文保留。此前"原样保留"会让客户端看到裸露的 <tool_call> 半截。
full_xml_unclosed = '开始执行。<tool_call>\n{"name": "bash", "arguments": {"command": "ls'
tcs, rtext, content = trae._resolve_trae_text(full_xml_unclosed)
check("XML 未闭合不崩溃", True)
check("XML 未闭合剥离标记不泄漏", "<tool_call>" not in content, f"content={content!r}")
check("XML 未闭合正文保留", "开始执行" in content, f"content={content!r}")
check("XML 未闭合无 tool_calls", tcs == [])

# 7.7 Wave 2：<seed_call> 的 invoke/function 两种非对称闭合变体
seed_call_invoke = '''<seed_call>
<invoke name="bash">
<parameter name="command">.venv/bin/python -c "import gateways.trae_work as trae; print('IMPORT OK')"</parameter>
<parameter name="workdir">/root/shared-workspace/claude-code-proxy</parameter>
</invoke>
</tool_call>'''
seed_a_expected = {
    "command": '.venv/bin/python -c "import gateways.trae_work as trae; print(\'IMPORT OK\')"',
    "workdir": "/root/shared-workspace/claude-code-proxy",
}
seed_a_tcs, seed_a_reasoning, seed_a_content = trae._resolve_trae_text(seed_call_invoke)
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
seed_b_tcs, seed_b_reasoning, seed_b_content = trae._resolve_trae_text(seed_call_function)
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
mixed_tcs, mixed_reasoning, mixed_content = trae._resolve_trae_text(mixed_fixture)
check("自由文本混杂多行 tool_call JSON 精确解析",
      len(mixed_tcs) == 1
      and mixed_tcs[0]["function"]["name"] == "bash"
      and json.loads(mixed_tcs[0]["function"]["arguments"]) == {"command": mixed_command}
      and mixed_reasoning == ""
      and mixed_content == f"{mixed_prefix}\n\n{mixed_suffix}"
      and "<tool_call>" not in mixed_content
      and '"arguments"' not in mixed_content)

wave2_plain_text = "This is ordinary prose with braces {not JSON} and no tool marker."
plain_tcs, plain_reasoning, plain_content = trae._resolve_trae_text(wave2_plain_text)
check("Wave 2 纯自由文本不误判工具调用",
      plain_tcs == []
      and plain_reasoning == ""
      and plain_content == wave2_plain_text
      and "<tool_call>" not in plain_content
      and '"arguments"' not in plain_content)

# ─── 8. Wave 3：<tool_call> XML 子标签变体 + <seed:tool_result> 复述块 ───
# （2026-08-04 实测抓包：ses_032871f10ffeUFPADwt7q2qizX 会话末尾泄漏）
# 根因：模型从上下文学到 opencode 客户端工具调用历史格式，<tool_call> 块内
# 输出 XML 子标签（<tool_name>/<parameters>/<parameter name=..>）而非 JSON，
# _TOOLCALL_XML_RE 分支 find("{") 失败 → 整段原样泄漏；<seed:tool_result>
# 复述历史工具结果（无闭合标签）也不在任何检测特征里，静默透传。
print("[8] Wave 3: <tool_call> XML 子标签 + <seed:tool_result>")

# 8.1 <tool_call> 内 XML 子标签（真实泄漏：bash + git status）
wave3_tc = '''我来检查当前的 git 状态，看看有哪些修改需要提交。

<tool_call>
<tool_name>bash</tool_name>
<parameters>
<parameter name="command" string="true">cd /root/shared-workspace/claude-code-proxy && git status</parameter>
<parameter name="timeout" string="false">10000</parameter>
</parameters>
</tool_call>'''
w3_tcs, w3_r, w3_c = trae._resolve_trae_text(wave3_tc)
check("tool_call 子标签识别为疑似标记", trae._looks_like_dsml(wave3_tc))
check("tool_call 子标签解析出 1 个", len(w3_tcs) == 1)
check("tool_call 子标签 name 正确",
      w3_tcs and w3_tcs[0]["function"]["name"] == "bash")
check("tool_call 子标签 arguments 完整",
      w3_tcs and json.loads(w3_tcs[0]["function"]["arguments"])["command"]
      == "cd /root/shared-workspace/claude-code-proxy && git status")
check("tool_call 子标签正文已清洗（无标记泄漏）",
      "<tool_call>" not in w3_c and "<tool_name>" not in w3_c and "parameter" not in w3_c,
      f"content={w3_c!r}")
check("tool_call 子标签正文保留前置说明", "我来检查当前的 git 状态" in w3_c)

# 8.2 <seed:tool_result> 纯复述（真实泄漏：历史 grep 结果整段）
wave3_seed = '''<seed:tool_result>
Found 18 match(es) in 1 file(s)

/root/shared-workspace/claude-code-proxy/server.py
  389: yield f"data: {{\\"error\\":\\"upstream ...\\"}}"
  807: re.compile(r'"ResourceExhausted"'),
'''
w3s_tcs, w3s_r, w3s_c = trae._resolve_trae_text(wave3_seed)
check("seed:tool_result 判定为疑似标记", trae._looks_like_dsml(wave3_seed))
check("seed:tool_result 不解析出 tool_calls", w3s_tcs == [])
check("seed:tool_result 复述块已剥离",
      "seed:tool_result" not in w3s_c and "server.py" not in w3s_c,
      f"content={w3s_c!r}")

# 8.3 混合结构：seed:tool_result 复述 + 正文 + 新 tool_call（真实日志 897 行形态）
wave3_mixed = '''<seed:tool_result>
/root/shared-workspace/claude-code-proxy/targets.json:417:   "label": "openrouter",
/root/shared-workspace/claude-code-proxy/targets.json:418:   "listenPort": 8090,
/root/shared-workspace/claude-code-proxy/targets.json:2659:   "port": 8090,
现在让我检查 server.py 中这两个端口的错误处理逻辑：

<tool_call>
<tool_name>grep</tool_name>
<parameters>
<parameter name="pattern" string="true">error.*rewrite|rewrite.*error</parameter>
<parameter name="path" string="true">/root/shared-workspace/claude-code-proxy/server.py</parameter>
</parameters>
</tool_call>'''
w3m_tcs, w3m_r, w3m_c = trae._resolve_trae_text(wave3_mixed)
check("混合结构新调用解析出 1 个", len(w3m_tcs) == 1)
check("混合结构新调用 name 正确",
      w3m_tcs and w3m_tcs[0]["function"]["name"] == "grep")
check("混合结构无标记泄漏",
      "seed:tool_result" not in w3m_c
      and "<tool_call>" not in w3m_c
      and "<tool_name>" not in w3m_c,
      f"content={w3m_c!r}")
check("混合结构正文保留", "现在让我检查" in w3m_c, f"content={w3m_c!r}")

# 8.4 纯文本不受影响
w3_plain = "完全普通的回复，没有工具调用。"
w3p_tcs, w3p_r, w3p_c = trae._resolve_trae_text(w3_plain)
check("Wave 3 纯文本不误判", w3p_tcs == [] and w3p_c == w3_plain)

# 8.5 未闭合 <tool_call>（截断）剥离但保留正文
w3_unclosed = '开始执行。<tool_call>\n{"name": "bash", "arguments": {"command": "ls'
w3u_tcs, w3u_r, w3u_c = trae._resolve_trae_text(w3_unclosed)
check("未闭合 tool_call 不崩溃且剥离标记",
      "<tool_call>" not in w3u_c and "开始执行" in w3u_c,
      f"content={w3u_c!r}")

# ─── 8.2 Wave 8.2：<tool_call> + <tool_name> + <arguments> JSON 变体（2026-08-05 实测）───
# 形态：opencode 客户端工具调用历史格式，模型从上下文学到输出。
# 变体7（2026-08-05 实测，proxy.log 23475 行）：<tool_name> 标签 + <arguments>
# {"..":".."} JSON 包裹参数（而非 <parameter name=".."> 子标签）。此前 _parse_dsml_tool_calls
# 优先检测块内第一个 { → 识别为 JSON 块提取 name → name 为空被 continue 跳过 → subtags
# 返回 [] → 整段 <tool_call> 落入兜底剥离，工具调用意图丢失。
# 修复：_parse_dsml_tool_calls 先检测 <tool_name> 标签，有则走 _parse_toolcall_subtags
#（增强支持 <arguments> JSON 格式）；_parse_toolcall_subtags 新增 <arguments> 分支优先。
print("[8.2] Wave 8.2: <tool_name> + <arguments> JSON 参数")

# 8.2.1 真实泄漏样本（proxy.log 23475 行）
wave82_tc = '''<tool_call>
<tool_name>bash</tool_name>
<arguments>{"command": "cd /root/shared-workspace/claude-code-proxy && grep -i 'content_filter' proxy.log | tail -30"}</arguments>
</tool_call>'''
w82_tcs, w82_r, w82_c = trae._resolve_trae_text(wave82_tc)
check("Wave8.2 <arguments> 解析出 1 个", len(w82_tcs) == 1)
check("Wave8.2 name=bash", w82_tcs and w82_tcs[0]["function"]["name"] == "bash")
check("Wave8.2 arguments 含 command key",
      w82_tcs and "command" in json.loads(w82_tcs[0]["function"]["arguments"]))
check("Wave8.2 正文已清洗（无 XML 泄漏）",
      "<tool_call>" not in w82_c and "<tool_name>" not in w82_c and "<arguments>" not in w82_c,
      f"content={w82_c!r}")

# 8.2.2 多参数 JSON（嵌套花括号）
wave82_multi = '''<tool_call>
<tool_name>edit</tool_name>
<arguments>{"filePath": "/a/b.py", "oldString": "if (x) { return 1; }", "newString": "if (y) { return 2; }"}</arguments>
</tool_call>'''
w82m_tcs, w82m_r, w82m_c = trae._resolve_trae_text(wave82_multi)
check("Wave8.2 多参数解析出 1 个", len(w82m_tcs) == 1)
w82m_args = json.loads(w82m_tcs[0]["function"]["arguments"]) if w82m_tcs else {}
check("Wave8.2 嵌套花括号未截断", w82m_args.get("oldString") == "if (x) { return 1; }")
check("Wave8.2 多参数正文已清洗", "<tool_call>" not in w82m_c, f"content={w82m_c!r}")

# 8.2.3 连续多个 <tool_name>+<arguments>（proxy.log 23475 行真实形态）
wave82_chain = '''<tool_call>
<tool_name>bash</tool_name>
<arguments>{"command": "git status"}</arguments>
</tool_call>
<tool_call>
<tool_name>grep</tool_name>
<arguments>{"pattern": "_clean_codebuddy_body", "path": "/x/server.py"}</arguments>
</tool_call>
<tool_call>
<tool_name>bash</tool_name>
<arguments>{"command": "systemctl status claude-code-proxy"}</arguments>
</tool_call>'''
w82c_tcs, w82c_r, w82c_c = trae._resolve_trae_text(wave82_chain)
check("Wave8.2 连续调用解析出 3 个", len(w82c_tcs) == 3)
check("Wave8.2 3 个 name 正确",
      w82c_tcs and [tc["function"]["name"] for tc in w82c_tcs] == ["bash", "grep", "bash"])
check("Wave8.2 连续调用正文已清洗",
      "<tool_call>" not in w82c_c and "<arguments>" not in w82c_c,
      f"content={w82c_c!r}")

# ─── 9. Wave 4：官方 seed-oss / Qwen3 XML 语法（vllm qwen3.py + seed_oss.py）───
# 官方原生格式：<seed:tool_call><function=bash><parameter=command>ls</parameter></function></seed:tool_call>
# 关键差异：function/parameter 用"无空格无引号"的 <tag=name> 属性形式；
# seed-oss 外层 <seed:tool_call>（带冒号）；推理 <seed:think>（Qwen3 用 <think>）。
# 此前解析器只覆盖 <function name="..">（带空格引号）与 <seed_call>（无冒号），
# 官方语法整体漏解析 → 原样泄漏。vLLM 官方 parser 是"全量 case"权威来源。
print("[9] Wave 4: 官方 seed-oss/Qwen3 XML 语法")

# 9.1 seed:think + seed:tool_call + function= + parameter=
w4_1 = ('<seed:think>I need to check the file.</seed:think>'
        '<seed:tool_call><function=bash><parameter=command>ls -la</parameter></function></seed:tool_call>')
w41_t, w41_r, w41_c = trae._resolve_trae_text(w4_1)
check("官方 seed:tool_call 解析出 1 个", len(w41_t) == 1)
check("官方 seed:tool_call name 正确",
      w41_t and w41_t[0]["function"]["name"] == "bash")
check("官方 seed:tool_call arguments 完整",
      w41_t and json.loads(w41_t[0]["function"]["arguments"])["command"] == "ls -la")
check("官方 seed:tool_call think 标签剥离", "seed:think" not in w41_c, f"content={w41_c!r}")
check("官方 seed:tool_call 调用块剥离", "seed:tool_call" not in w41_c, f"content={w41_c!r}")

# 9.2 tool_call + function= 多参数（嵌套花括号不截断）
w4_2 = ('<tool_call><function=edit><parameter=filePath>/a/b.py</parameter>'
        '<parameter=oldString>function foo() { return 1; }</parameter>'
        '<parameter=newString>function foo() { return 2; }</parameter></function></tool_call>')
w42_t, w42_r, w42_c = trae._resolve_trae_text(w4_2)
check("官方多参数解析出 1 个", len(w42_t) == 1)
w42_args = json.loads(w42_t[0]["function"]["arguments"]) if w42_t else {}
check("官方多参数含 3 个 key", len(w42_args) == 3)
check("官方多参数嵌套花括号未截断",
      w42_args.get("oldString") == "function foo() { return 1; }")

# 9.3 裸 function= 无 tool_call 包裹（官方 parser 的 fallback）
w4_3 = '<function=bash><parameter=command>ls</parameter></function>'
w43_t, w43_r, w43_c = trae._resolve_trae_text(w4_3)
check("官方裸 function= fallback 解析出 1 个", len(w43_t) == 1)
check("官方裸 function= name 正确",
      w43_t and w43_t[0]["function"]["name"] == "bash")
check("官方裸 function= 标记剥离", "function" not in w43_c, f"content={w43_c!r}")

# 9.4 连续两个 seed:tool_call（官方 parser 容忍连续调用）
w4_4 = ('<seed:tool_call><function=bash><parameter=command>git add</parameter></function></seed:tool_call>'
        '<seed:tool_call><function=bash><parameter=command>git status</parameter></function></seed:tool_call>')
w44_t, w44_r, w44_c = trae._resolve_trae_text(w4_4)
check("官方连续调用解析出 2 个", len(w44_t) == 2)

# 9.5 Qwen3 原生 <think>/<tool_call>（无 seed 前缀）
w4_5 = '<think>Let me check</think><tool_call><function=bash><parameter=command>ls</parameter></function></tool_call>'
w45_t, w45_r, w45_c = trae._resolve_trae_text(w4_5)
check("Qwen3 原生格式解析出 1 个", len(w45_t) == 1)
check("Qwen3 think 标签剥离", "think" not in w45_c, f"content={w45_c!r}")

# 9.6 未闭合 <seed:think>（截断）剥离
w4_6 = '<seed:think>thinking...'
w46_t, w46_r, w46_c = trae._resolve_trae_text(w4_6)
check("未闭合 seed:think 剥离", "seed:think" not in w46_c, f"content={w46_c!r}")

# 9.7 正文 + 官方调用混合
w4_7 = '我来执行。\n<seed:tool_call><function=bash><parameter=command>ls</parameter></function></seed:tool_call>'
w47_t, w47_r, w47_c = trae._resolve_trae_text(w4_7)
check("官方调用混合正文保留", len(w47_t) == 1 and "我来执行" in w47_c)
check("官方调用混合无标记泄漏", "seed:tool_call" not in w47_c, f"content={w47_c!r}")

# ─── 10. Wave 5：通用意图根治（2026-08-04，不再打地鼠）───
# 根治设计：检测层(_looks_like_dsml)与剥离层(_strip_generic_tool_blocks)都用
# 通用意图正则 _TOOL_INTENT_TAG_RE——任何 XML 标签只要含工具语义关键词
# （tool/function/param/invoke/args/call 等任意排列）即进剥离路径。模型发明
# 从未见过的新标签也不泄漏（解析不出 tool_calls 但标记被剥离，正文保留）。
print("[10] Wave 5: 通用意图根治（未知变体不泄漏）")

# 10.1 任意自定义工具标签（模型可能发明的任何排列）
w5_cases = [
    ("自定义工具标签", '<my_custom_tool><cmd>ls</cmd><args>{"a":1}</args></my_custom_tool>', ""),
    ("命名空间标签", '<tool:call><function>bash</function><params><p name="cmd">ls</p></params></tool:call>', ""),
    ("简写 func/param", '<func><param>ls</param></func>', ""),
    ("execute 容器", '<execute><tool>bash</tool><cmd>ls</cmd></execute>', ""),
    ("自闭合 call", "<call name='bash' args='{\"command\":\"ls\"}'/>", ""),
    ("command 标签", '<command>ls</command>', ""),
    ("深层嵌套", '<tool_call><func><param>x</param></func><cmd>y</cmd></tool_call>', ""),
    ("正文+未知调用", '好的我来做。\n<any_tool><args>{"cmd":"ls"}</args></any_tool>', "好的我来做。"),
    ("未闭合未知标签", '<any_tool><args>{"cmd":', ""),
]
for w5_name, w5_text, w5_exp in w5_cases:
    w5_t, w5_r, w5_c = trae._resolve_trae_text(w5_text)
    check(f"Wave5 [{w5_name}] 无 XML 标记泄漏",
          "<" not in w5_c, f"content={w5_c!r}")
    if w5_exp is not None:
        check(f"Wave5 [{w5_name}] 正文保留", w5_c == w5_exp, f"content={w5_c!r}")

# 10.2 普通文本/正文含工具单词不误伤（关键词限定在 XML 标签形态内）
w5_normals = [
    "这是一个普通回复，没有任何工具调用。",
    "The function foo() returns 1. The tool was useful.",
    "parameter 这个单词出现在正文里，不是标签。",
    "please call me tomorrow",
    "调用 function 这个词的普通句子。",
]
for w5_n in w5_normals:
    w5_d = trae._looks_like_dsml(w5_n)
    w5_t, w5_r, w5_c = trae._resolve_trae_text(w5_n)
    check(f"Wave5 普通文本不误伤: {w5_n[:20]}...",
          not w5_d and w5_t == [] and w5_c == w5_n,
          f"detected={w5_d} content={w5_c!r}")

# 7.7 空文本 → 全部返回空
tcs, rtext, content = trae._resolve_trae_text("")
check("空文本 tool_calls 为空", tcs == [])
check("空文本 reasoning 为空", rtext == "")
check("空文本 content 为空", content == "")

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
