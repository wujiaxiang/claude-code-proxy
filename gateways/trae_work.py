"""Trae Work 网关专属代码（从 server.py 原样拆分）。

- 主体为 Trae(trae-api-cn.mchost.guru) 上游的 OpenAI <-> llm_utils_chat
  协议转换、模型列表同步，以及 DSML / seed_call / qwen 等多套工具调用
  文本标记的解析与清洗（逆向成果，正则与解析逻辑原样保留）。
- traework_logger 按 name 全局单例，与 server.py 顶部
  `traework_logger = _setup_gateway_logger("trae-work")` 拿到同一已配置实例
  （写入 traework.log），不从此处重新配置。
- 主 logger（proxy.log）与 get_http_client 通过延迟导入 `from server import X`
  取得，保证与拆分前行为一致，无循环依赖（server 主模块别名已在 L27-30 注册）。
"""

import logging
import os
import re
import time
import httpx
import json

traework_logger = logging.getLogger("gateway.trae-work")


# ── Trae Work 协议转换（handler=trae-work）──
# 客户端走 OpenAI 协议（/v1/chat/completions），代理内部转换为
# Trae 的 llm_utils_chat（SSE，content 数组格式）。认证用 Cloud-IDE-JWT。
_TRAE_API_HOST = "https://trae-api-cn.mchost.guru"
_TRAE_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
_TRAE_IDE_VERSION = "0.1.51"
_TRAE_IDE_VERSION_CODE = "20260814"
_TRAE_DEVICE_ID = "199444637423849"
_TRAE_MACHINE_ID = "d2115a713ee587fea5d340ceb8ef1fda3ad808431c24e7fed3085693f52f4428"
# trae 上游模型列表缓存（get_detail_param，TTL 5 分钟）
_TRAE_MODELS_CACHE: list = []
_TRAE_MODELS_CACHE_TIME: float = 0.0
_TRAE_MODELS_TTL: float = 300.0

# ── 排队处理（简化策略，2026-08-02）──
# 上游 request_wait_in_queue 事件 → 模型繁忙，直接终止并返回繁忙提示，不做降级重发。


def _trae_build_headers(token: str) -> dict:
    """构造 Trae Work API 请求头（Cloud-IDE-JWT + 设备指纹）。"""
    return {
        "Authorization": f"Cloud-IDE-JWT {token}",
        "Content-Type": "application/json",
        "x-app-id": _TRAE_APP_ID,
        "x-app-version": "default",
        "x-app-version-code": _TRAE_IDE_VERSION_CODE,
        "x-ide-version-code": _TRAE_IDE_VERSION_CODE,
        "x-ide-version": _TRAE_IDE_VERSION,
        "x-ide-version-type": "stable",
        "x-device-id": _TRAE_DEVICE_ID,
        "x-machine-id": _TRAE_MACHINE_ID,
        "x-device-type": "windows",
        "x-os-version": "Windows 10",
        "x-device-brand": "Standard PC (Q35 + ICH9, 2009)",
        "x-device-cpu": "KVM",
        "x-trae-authorized-services": "feishu",
        "request-traffic-type": "prod",
        "X-Trae-Client-Type": "lite",
    }


async def _trae_fetch_models(token: str) -> list:
    """从 trae 上游 get_detail_param 拉取最新模型列表（TTL 缓存 5 分钟）。

    解析 config_info_list：过滤 __dev 开发变体、不可用(config_switch=false)、
    用户不可见(is_invisible_to_user)的配置；失败时返回缓存兜底。
    """
    from server import logger, get_http_client
    global _TRAE_MODELS_CACHE, _TRAE_MODELS_CACHE_TIME
    now = time.time()
    if _TRAE_MODELS_CACHE and (now - _TRAE_MODELS_CACHE_TIME) < _TRAE_MODELS_TTL:
        return _TRAE_MODELS_CACHE
    if not token:
        return []
    try:
        client = await get_http_client()
        resp = await client.post(
            f"{_TRAE_API_HOST}/api/ide/v1/get_detail_param",
            json={
                "function": "chat_v3",
                "config_names": None,
                "need_prompt": False,
                "current_config_info": None,
                "poly_prompt": True,
                "mode_type": None,
                "agent_type": None,
            },
            headers=_trae_build_headers(token),
            timeout=httpx.Timeout(15.0),
        )
        if resp.status_code != 200:
            logger.warning(f"[trae-work] get_detail_param HTTP {resp.status_code}")
            return _TRAE_MODELS_CACHE or []
        data = resp.json()
        models = []
        seen = set()
        for cfg in data.get("config_info_list", []):
            cname = cfg.get("config_name", "")
            if not cname or cname.endswith("__dev"):
                continue
            if not cfg.get("config_switch", True):
                continue
            if cfg.get("is_invisible_to_user", False):
                continue
            if cname not in seen:
                seen.add(cname)
                models.append(cname)
        if models:
            _TRAE_MODELS_CACHE = models
            _TRAE_MODELS_CACHE_TIME = now
            logger.info(f"[trae-work] 上游模型列表已同步: {len(models)} 个")
        return models
    except Exception as e:
        logger.warning(f"[trae-work] 模型列表拉取失败: {e}")
        return _TRAE_MODELS_CACHE or []


def _openai_to_trae_body(body: dict) -> dict:
    """OpenAI chat.completions 请求体 → Trae llm_utils_chat 请求体。

    工具调用历史文本化（关键）：
    Trae 上游 messages 只有 role+content，无 OpenAI 式 tool_calls / role=tool 概念。
    实测 Doubao-Seed-Code 对"孤立 tool 消息"（assistant.tool_calls 被丢弃后）
    返回 HTTP 200 + 空 SSE 流（0 output 事件），glm-5.2 等可容忍。参考
    trae-local-api 逆向编码（agent.js runAgentLoop）：
      assistant: "[Tool Call: {name}]\nArguments: {args}\n\nResult: ..."
      tool 消息 → user: "[Tool Call Result: {name}]\n{output}"
    """
    trae_messages = []
    tool_refs = {}  # tool_call_id -> 工具名（供后续 role=tool 消息匹配）
    for m in body.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content")

        # assistant 消息自带的 tool_calls → 文本化拼入 content（Trae 无 tool_calls 字段）
        calls_text = ""
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            args = fn.get("arguments") or ""
            calls_text += f"[Tool Call: {name}]\nArguments: {args}\n\n"
            tid = tc.get("id")
            if tid:
                tool_refs[tid] = name
        if calls_text:
            if content and isinstance(content, str):
                content = content.rstrip() + "\n\n" + calls_text.rstrip()
            else:
                content = calls_text.rstrip()

        # role=tool 消息：Trae 无此 role，转 user + 文本化，避免上游收到孤立 tool 消息
        if role == "tool":
            name = tool_refs.get(m.get("tool_call_id"), "")
            suffix = f": {name}" if name else ""
            tool_content = str(content or "").strip()
            content = f"[Tool Call Result{suffix}]\n{tool_content}" if tool_content \
                else f"[Tool Call Result{suffix}]"
            role = "user"

        if isinstance(content, list):
            # 已数组化（OpenAI 多模态），转成 Trae 的 {type,text} 列表
            parts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") in ("text", "input_text"):
                        parts.append({"type": "text", "text": c.get("text", "")})
                    elif c.get("type") == "image_url":
                        # Trae 的 content.image_url 字段类型为对象（LLMRawMessageImageUrl），
                        # 原样透传 OpenAI 格式 {"image_url": {"url": ...}}——不要转成 image 字段（Trae 4001）。
                        # 注意：Trae 图片能力只对内置多模态模型开放（Doubao_1_6/qwen-3.7-plus 等），
                        # 非白名单模型（Doubao-Seed-2.1-Pro/glm-5.2）返回 3003/1005，属上游限制。
                        parts.append({"type": "image_url", "image_url": c.get("image_url", {})})
                    else:
                        parts.append({"type": "text", "text": str(c)})
                else:
                    parts.append({"type": "text", "text": str(c)})
            trae_messages.append({"role": role, "content": parts, "role_type": 0})
        else:
            trae_messages.append({"role": role,
                                  "content": [{"type": "text", "text": str(content or "")}],
                                  "role_type": 0})
    out = {
        "messages": trae_messages,
        "function": "chat_v3",
        "stream": bool(body.get("stream", False)),
    }
    model = body.get("model", "glm-5.2")
    if model and model not in ("auto", "trae-work"):
        # 去掉可能的 "trae/" 前缀
        clean = model.split("/")[-1]
        out["model"] = clean
        out["config_name"] = clean
    # ── tools 翻译：透传 + 提示词注入 ──
    # OpenAI: tools[].function.parameters = object
    # Trae:   tools[].function.parameters = JSON 字符串（实测 object 直接 4001）
    # 注：Trae 上游对标准 tools 字段支持不可靠（seed-code 实测不识别 → 输出乱格式），
    #     按 trae-local-api 方式额外注入提示词（XML <tool_call> 格式），响应侧解析。
    tools = body.get("tools")
    if tools:
        trae_tools = []
        for t in tools:
            fn = t.get("function") or {}
            params = fn.get("parameters")
            fn2 = dict(fn)
            if isinstance(params, (dict, list)):
                fn2["parameters"] = json.dumps(params, ensure_ascii=False)
            trae_tools.append({**t, "function": fn2})
        out["tools"] = trae_tools
        # 提示词注入到最后一条 user 消息（trae-local-api buildToolPrompt 方式）
        tool_prompt = _build_trae_tool_prompt(tools)
        if tool_prompt and out["messages"]:
            last = out["messages"][-1]
            if last.get("role") == "user":
                last["content"] = last["content"] + [{"type": "text", "text": "\n\n" + tool_prompt}]
            else:
                out["messages"].append({"role": "user",
                                        "content": [{"type": "text", "text": tool_prompt}],
                                        "role_type": 0})
    # ── 采样参数尽力透传（参考 trae-local-api trae-client.js llmUtilsChat）──
    # 上游 best-effort 支持，不保证全部生效；max_tokens 截断到 128000
    if isinstance(body.get("max_tokens"), (int, float)) and body["max_tokens"]:
        out["max_tokens"] = min(int(body["max_tokens"]), 128000)
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        val = body.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = val
    if body.get("stop"):
        out["stop"] = body["stop"] if isinstance(body["stop"], list) else [str(body["stop"])]
    if isinstance(body.get("seed"), (int, float)) and body["seed"]:
        out["seed"] = body["seed"]
    if isinstance(body.get("n"), int) and body["n"] > 1:
        out["n"] = body["n"]
    return out


def _trae_chunk_to_openai(chunk: dict, model: str) -> dict:
    """Trae output 事件 → OpenAI chat.completion.chunk。

    兼容上游两种 output 形态（trae-local-api 逆向结论）：
      旧格式: {"response": "...", "reasoning_content": "...", "tool_calls": [...]}
      新格式(2026-05): {"type": "text", "content": "...", "reasoning": "..."}
    """
    content = chunk.get("response", "") or chunk.get("content", "") or ""
    reasoning = chunk.get("reasoning_content") or chunk.get("reasoning") or ""
    delta = {}
    if content:
        delta["content"] = content
    if reasoning:
        delta["reasoning_content"] = reasoning
    trae_tc = chunk.get("tool_calls")
    if trae_tc:
        oai_tc = _trae_tool_calls_to_openai(trae_tc)
        if oai_tc:
            delta["tool_calls"] = oai_tc
    return {
        "id": f"chatcmpl-{abs(hash(str(chunk.get('session_id', ''))))}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


def _trae_tool_calls_to_openai(trae_tc: list) -> list:
    """tool_calls → OpenAI tool_calls（兼容两种输入形态）。

    Trae 原生: {"index":0,"id":"call_x","type":"function",
                "function_call":{"name":"get_weather","arguments":"..."}}
    DSML/XML 解析: {"type":"function","function":{"name":"get_weather","arguments":"..."}}（无 id/index）

    输出统一 OpenAI 格式：{"id","type","function","index"}；缺 id/index 时补生成
    （OpenAI 协议要求，客户端按 id 关联工具结果；缺 index 时流式无法分片累积）。
    """
    oai = []
    for i, tc in enumerate(trae_tc):
        if not isinstance(tc, dict):
            continue
        # 兼容 function_call（Trae 原生）与 function（DSML/XML 解析）两种键
        fc = tc.get("function_call") or tc.get("function") or {}
        fn = {}
        if fc.get("name"):
            fn["name"] = fc["name"]
        if fc.get("arguments") is not None:
            fn["arguments"] = fc["arguments"]
        if not fn:
            continue
        item = {"type": tc.get("type", "function"), "function": fn}
        item["index"] = tc.get("index") if tc.get("index") is not None else i
        if tc.get("id"):
            item["id"] = tc["id"]
        else:
            item["id"] = f"call_{int(time.time() * 1000)}_{i}"
        oai.append(item)
    return oai


# ── DSML 标记解析（seed-code 系模型：工具调用以文本标记输出在 response 字段）──
# 实测形态（Doubao-Seed-Code，2026-08-02）：
#   <｜DSML｜>
#   <｜function｜>
#   <｜function name｜>get_weather</｜function｜>
#   <｜parameter｜>{"city":"北京"}</｜parameter｜>
#   </｜function｜>
#   </｜DSML｜>
# 注意：<｜function name｜>...</｜function｜> 和外层 <｜function｜>...</｜function｜> 共用
# 同一个闭合标记 "</｜function｜>"（而非 "</｜function name｜>"），曾用非贪婪
# "<｜function｜>(.*?)</｜function｜>" 提取整段块，结果非贪婪匹配在第一个
# </｜function｜>（其实是 name 标签的闭合）处就停止，导致 <｜parameter｜> 从未
# 被捕获到块内（_DSML_FN_RE 从未真正工作过，2026-08-02 补测试时发现）。
# 改用一次性配对正则，同时捕获 name + parameter，避免闭合标记歧义。
_DSML_PAIR_RE = re.compile(
    r"<[｜|]function[｜|]>\s*<[｜|]function name[｜|]>(.*?)</[｜|]function[｜|]>"
    r"\s*<[｜|]parameter[｜|]>(.*?)</[｜|]parameter[｜|]>\s*</[｜|]function[｜|]>", re.S)
_DSML_LIKE_RE = re.compile(r"<[｜|](?:DSML|function|function name|parameter)[｜|]>")
# seed-code 从历史文本化格式学到的输出形态（2026-08-02 实测）：response 字段输出
# "[Tool Call: bash]\nArguments: {\"command\":\"...\"}" 纯文本而非 DSML/tool_calls 事件。
# 识别并解析回 tool_calls，避免把该文本原样透传给客户端（IDE 无法识别）。
# Arguments 用平衡括号提取（贪婪 \{.*\} 会吞掉后面的 reasoning JSON 文本）。
_TOOLCALL_TEXT_RE = re.compile(r"\[Tool Call: ([A-Za-z0-9_\-\.:/]+)\]\s*\n?\s*Arguments:\s*(\{)", re.S)


def _extract_balanced_json(text: str, start: int) -> str | None:
    """从 text[start]（'{'）起做平衡括号提取，返回完整 JSON 对象字符串；无匹配返回 None。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


# 模型以 {"reasoning_content":"..."} JSON 字面量输出思考（2026-08-02 实测），
# 需提取为 reasoning 而非作为 content 透传（客户端会把 JSON 字面量当正文显示）。
_DSML_REASONING_RE = re.compile(r'\{"reasoning_content":"((?:[^"\\]|\\.)*)"\}', re.S)
# trae-local-api 方式：提示词注入后模型用 <tool_call> XML 输出工具调用（2026-08-02）
_TOOLCALL_XML_RE = re.compile(r"<tool_call\b[^>]*>([\s\S]*?)</tool_call\s*>", re.I)
# 注：内部 JSON 对象（{"name":...,"arguments":{...}}）不用正则提取，改用
# _extract_balanced_json 平衡括号扫描（见 _parse_dsml_tool_calls），避免嵌套
# 花括号/转义引号导致提取截断（2026-08-02 实测：edit 工具的 oldString 含 JS
# 代码花括号，曾用正则提取导致 JSON 截断校验失败、整段泄漏到正文）
# DSML 外层完整包裹（含 <｜DSML｜>...</｜DSML｜>），一次性移除时用
_DSML_BLOCK_RE = re.compile(r"<[｜|]DSML[｜|]>[\s\S]*?</[｜|]DSML[｜|]>", re.S)

# ── DSML 第 4 种变体：<｜DSML｜invoke name="..."> / <｜DSML｜parameter name="...">
# （2026-08-03 实测，Doubao-Seed-Code）：
#   <｜DSML｜tool_calls>
#   <｜DSML｜invoke name="bash">
#   <｜DSML｜parameter name="command" string="true">...</｜DSML｜parameter>
#   </｜DSML｜invoke>
#   </｜DSML｜tool_calls>
# 与前 3 种（<｜function｜>/<｜function name｜>/<｜parameter｜> 独立标签、
# [Tool Call: name] 纯文本、<tool_call>JSON</tool_call>）完全不同的第 4 种
# 标签语法：tool 名和 param 名作为**标签属性**（name="xxx"）而非独立标签体。
# 教训：与其继续为每个新样本量身定制一条正则（治标），这里改写一个通用
# "<｜DSML｜TAGNAME attr="val" ...>...</｜DSML｜TAGNAME>" 家族扫描器，只认
# 标签语法结构本身（TAGNAME 任意、属性任意），不绑定具体 tool/param 名，
# 这样才能覆盖同一标签家族里模型可能继续变换出的其他排列，而不是每次追新样本。
_DSML_INVOKE_RE = re.compile(
    r'<[｜|]DSML[｜|]invoke\s+name="([^"]*)"[^>]*>([\s\S]*?)</[｜|]DSML[｜|]invoke\s*>', re.S)
_DSML_PARAM_RE = re.compile(
    r'<[｜|]DSML[｜|]parameter\s+name="([^"]*)"[^>]*>([\s\S]*?)</[｜|]DSML[｜|]parameter\s*>', re.S)
# 整个 <｜DSML｜tool_calls>...</｜DSML｜tool_calls> 外层包裹，清洗正文时一次性移除
_DSML_TOOLCALLS_BLOCK_RE = re.compile(
    r"<[｜|]DSML[｜|]tool_calls[｜|]?>[\s\S]*?</[｜|]DSML[｜|]tool_calls\s*>", re.S)
# 检测用：任意 "<｜DSML｜任意标签" 前缀（含 invoke/parameter/tool_calls 等家族全部成员）
_DSML_ANY_TAG_RE = re.compile(r"<[｜|]DSML[｜|][A-Za-z_]+")
# seed-code 实测变体：<seed_call> 外层可能用 </seed_call>、</tool_call> 或
# </seed:tool_call> 闭合。外层不能锚定开头，避免吞掉其前正常正文。
_SEED_CALL_RE = re.compile(
    r"<seed_call\b[^>]*>([\s\S]*?)(?:</seed_call\s*>|</tool_call\s*>|</seed:tool_call\s*>)", re.I)
_SEED_CALL_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]*)"[^>]*>([\s\S]*?)</invoke\s*>', re.I)
_SEED_CALL_FUNCTION_RE = re.compile(r'<function\s+name="([^"]*)"[^>]*>([\s\S]*?)</function\s*>', re.I)
_SEED_CALL_PARAM_OPEN_RE = re.compile(r'<parameter\s+name="([^"]*)"[^>]*>', re.I)
# 变体 6（2026-08-04 实测，Doubao-Seed-Code）：<tool_call> 内部不是 JSON 而是
# XML 子标签：<tool_name>bash</tool_name><parameters><parameter name="command"
# string="true">...</parameter>...</parameters>。曾因 _TOOLCALL_XML_RE 分支要求
# 块内 find("{") 而解析失败，整段 <tool_call> 原样泄漏到正文（IDE 显示裸露 XML，
# 工具未执行）。
_TOOLCALL_NAME_RE = re.compile(r"<tool_name\b[^>]*>([\s\S]*?)</tool_name\s*>", re.I)
_TOOLCALL_PARAM_RE = re.compile(
    r'<parameter\s+name="([^"]*)"[^>]*>([\s\S]*?)</parameter\s*>', re.I)
# 变体 7（2026-08-05 实测）：<tool_call> 内 <tool_name> 标签 + <arguments>{"..": ".."}</arguments>
# JSON 包裹形式（opencode 客户端历史工具调用格式的同源形态，模型从上下文学到）。与
# 变体 6 的 <parameter name=".."> 子标签并列，解析时优先取 <arguments> 整块 JSON。
_TOOLCALL_ARGS_RE = re.compile(r"<arguments\b[^>]*>([\s\S]*?)</arguments\s*>", re.I)
# ── 官方 seed-oss / Qwen3 XML 语法（vLLM 官方 parser，2026-08-04 补全）──
# Qwen3/seed-oss 模型原生工具调用格式（vllm/parser/qwen3.py + seed_oss.py）：
#   <think>...</think>（seed-oss 用 <seed:think>）推理
#   <seed:tool_call><function=bash><parameter=command>ls -la</parameter></function></seed:tool_call>
# 关键差异：function/parameter 用"无空格无引号"的 <tag=name> 属性形式，且
# seed-oss 外层是 <seed:tool_call>（带冒号），与已支持的 <seed_call>（无冒号）
# 和 <function name="..">（带空格引号）是两套不同语法——官方 parser 三者都认。
# 此外官方 parser 还容忍：无 <tool_call> 前缀直接 <function=>（fallback）、
# </function> 后直接下一个 <tool_call>（连续调用未闭合外层）。
_SEED_TOOL_CALL_RE = re.compile(
    r"<seed:tool_call\b[^>]*>([\s\S]*?)(?:</seed:tool_call\s*>|</seed_call\s*>|</tool_call\s*>)", re.I)
_QWEN_FUNC_RE = re.compile(r"<function\s*=\s*([^>\s/]+)\s*>([\s\S]*?)(?:</function\s*>|(?=<tool_call\b|<seed:tool_call\b|<seed_call\b))", re.I)
_QWEN_PARAM_RE = re.compile(
    r"<parameter\s*=\s*([^>\s/]+)\s*>([\s\S]*?)(?:</parameter\s*>|(?=<parameter\s*=|<function\s*=</tool_call\b|<seed:tool_call\b))", re.I)
# 变体 7（2026-08-04 实测）：模型把历史工具结果用 <seed:tool_result> 包裹复述
# （无闭合标签，直接透传给客户端显示重复的历史 grep 结果）。识别用于剥离。
_SEED_TOOL_RESULT_OPEN_RE = re.compile(r"<seed:tool_result\b[^>]*>", re.I)
# 通用工具调用意图正则（2026-08-04 根治层）：匹配任意 XML 标签中出现的工具
# 语义关键词。模型自由生成时无论发明什么标签排列（<any_tool_xxx>、<tool:xxx>、
# <func>、<param> 等），只要标签名含这些关键词就命中——这是"不再打地鼠"的
# 关键：识别层从"已知标签白名单"升级为"语义关键词通用匹配"，新变体自动落入
# 剥离路径，不会静默透传。仅匹配 XML 标签形态（<...>），正文里出现 function/
# tool 等单词不误伤。
_TOOL_INTENT_TAG_RE = re.compile(
    r"<(?:[a-zA-Z_:][\w:.-]*)?\s*[a-zA-Z_:]*"
    r"(?:tool(?:[_:\-](?:call|name|result|usage))?|func(?:tion)?|parameter|param(?:s)?|"
    r"invoke|argument|args|arg|tool_call|seed_call|execute|cmd|command|call)\b[^>]*>",
    re.I)


def _build_trae_tool_prompt(tools: list) -> str:
    """OpenAI tools 定义 → Trae 提示词文本（trae-local-api buildToolPrompt 方式）。

    Trae 上游 llm_utils_chat 不识别标准 tools 字段（seed-code 实测输出乱格式），
    把工具定义注入提示词并指示 XML 调用格式，响应侧解析 <tool_call>。
    """
    descs = []
    for t in tools or []:
        fn = t.get("function") or {}
        name = fn.get("name") or ""
        desc = fn.get("description") or ""
        params = fn.get("parameters") or {}
        props = (params or {}).get("properties") or {}
        param_str = ", ".join(
            f"{k}: {v.get('description') or v.get('type')}" for k, v in props.items())
        descs.append(f"- {name}({param_str}): {desc}")
    return ("[Available tools - 使用 XML 格式调用工具]:\n" + "\n".join(descs) +
            "\n\n调用工具时，在回复中包含以下格式:\n"
            '<tool_call>\n{"name": "工具名", "arguments": {"参数": "值"}}\n</tool_call>\n'
            "可以一次回复多个工具调用。收到工具结果后分析并回复用户。仅在需要时调用工具。")


def _extract_reasoning_text(text: str) -> str:
    """从累积文本提取所有 {"reasoning_content":"..."} JSON 字面量的内容并拼接（JSON unescape）。

    seed-code 实测会把多段思考分别包成多个独立的 {"reasoning_content":"..."} JSON
    （而非一个整体 JSON 装完整思考），曾用 .search() 只提取第一段，导致后续几段
    原样以 JSON 字面量泄漏到正文（2026-08-02 实测：客户端看到裸露的
    {"reasoning_content":"..."} 文本）。改用 .finditer() 提取全部并拼接。
    无匹配返回 ""。
    """
    parts = []
    for m in _DSML_REASONING_RE.finditer(text or ""):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except Exception:
            parts.append(m.group(1))
    return "".join(parts)


def _looks_like_dsml(text: str) -> bool:
    """文本是否含工具调用标记特征（seed-code 系模型输出）。

    兼容形态：DSML 标记（<｜DSML｜>...）、[Tool Call: name] 文本格式、
    {"reasoning_content":"..."} JSON 字面量（思考封装）、<tool_call> XML、
    <seed_call>/<seed:tool_result> 等 seed 系标签，以及各形态的分片半截
    （尽早进入缓冲累积，避免原始标记/JSON 字面量透传给客户端）。

    2026-08-04 根治：在"具体特征白名单"之上叠加**通用意图正则**
    （_TOOL_INTENT_TAG_RE）——模型发明新标签时，只要标签名含工具语义关键词
    （tool_call/function/parameter/invoke/tool_name/tool_result/arguments 等），
    一律判定为疑似工具调用进入剥离路径，不再逐个打地鼠。关键词限定在 XML
    标签形态内（<...>），避免误伤普通正文里出现"function"等单词。
    """
    t = text or ""
    return bool(_DSML_LIKE_RE.search(t) or _TOOLCALL_TEXT_RE.search(t)
                or _DSML_REASONING_RE.search(t) or _TOOLCALL_XML_RE.search(t)
                or _DSML_ANY_TAG_RE.search(t)
                or _SEED_CALL_RE.search(t) or _SEED_TOOL_RESULT_OPEN_RE.search(t)
                or _SEED_TOOL_CALL_RE.search(t) or _QWEN_FUNC_RE.search(t)
                or _TOOLCALL_NAME_RE.search(t) or _TOOLCALL_PARAM_RE.search(t)
                or "<seed:think" in t or "<think" in t
                or "<function=" in t
                or "<seed:" in t or "<tool_" in t
                or "[Tool Call" in t or "Arguments:" in t
                or '{"reasoning_content"' in t or "reasoning_content" in t
                or "tool_call" in t or t.startswith('{"')
                or bool(_TOOL_INTENT_TAG_RE.search(t)))


# ── 架构说明（2026-08-02 重构）──────────────────────────────────────────
# 曾尝试在流式接收过程中"边收边猜这个 chunk 是不是工具调用标记的开头/半截"
# （_is_potential_toolcall_prefix 等启发式），结果每堵住一种半截标记（如
# "[" 单独成 chunk、reasoning JSON 未闭合）就会冒出下一种变种——因为任意
# 长度的文本前缀理论上都可能是"某个标记的未完成前缀"，这是不可判定问题。
#
# 参考 trae-local-api（官方逆向实现，/root/trae-local-api/src/agent.js
# runAgentStream + extractToolCalls）的架构：从不在流式接收阶段做标记判断，
# 而是先把整轮的 response/content 原始累积成 fullContent，等上游 SSE 流
# 完全结束（收到 'end'）后，才对完整文本一次性跑正则解析 tool_calls，
# 解析完再统一 flush 给客户端。标记必然是完整的，不存在"半截"问题。
#
# 本实现采用同样策略：resp_text 流式阶段只做纯累积（不做任何 content 提前
# 转发），reasoning_content/tool_calls 等结构化字段（上游明确给出、非文本
# 猜测）仍然逐 chunk 立即转发，因为它们不存在"文本标记未闭合"的歧义。
def _strip_seed_tool_result_blocks(text: str) -> str:
    """剥离 <seed:tool_result> 复述块（2026-08-04 实测，Doubao-Seed-Code）。

    模型会把历史工具结果用 <seed:tool_result> 开标签包裹复述（无闭合标签），
    原样透传给客户端 = 重复显示历史 grep 结果。处理策略（按开标签后是否有
    新的强工具标记区分）：
      - 无后续强标记（纯复述）：整块丢弃，避免历史结果整段泄漏
      - 有后续强标记（混合结构：复述 + 正文 + 新调用）：开标签后内容与正文
        无闭合标签无法可靠分界，保守只剥开标签本身、保留后续全部——正文不丢，
        且新工具调用块由 _TOOLCALL_XML_RE 等后续清洗删除。
    """
    if "<seed:tool_result" not in text:
        return text
    parts = []
    i = 0
    while True:
        m = _SEED_TOOL_RESULT_OPEN_RE.search(text, i)
        if not m:
            parts.append(text[i:])
            break
        parts.append(text[i:m.start()])  # 开标签之前的正文保留
        nxt = re.search(r"<tool_call\b|<seed_call\b|<invoke\b|<function\b", text[m.end():], re.I)
        if nxt:
            # 混合结构：剥掉开标签，从开标签后继续（正文与新调用保留）
            i = m.end()
        else:
            break  # 纯复述：后续内容全部丢弃
    return "".join(parts)


def _resolve_trae_text(full_text: str) -> tuple[list, str, str]:
    """对一整轮已完整接收的 response/content 文本做工具调用/reasoning 解析。

    Returns: (tool_calls, reasoning_text, content_text)
      - tool_calls: 解析出的工具调用列表（OpenAI tool_calls 格式，可能为空）
      - reasoning_text: 提取出的 reasoning（{"reasoning_content":"..."} JSON 字面量）
      - content_text: 清洗掉工具调用/reasoning 标记后的正文（tool_calls 非空时省略，
        避免把 "[Tool Call: xxx]\nArguments: {...}" 之类的调用文本也当正文回显）
    """
    from server import logger
    text = full_text or ""
    reasoning_text = _extract_reasoning_text(text)
    if reasoning_text:
        text = _DSML_REASONING_RE.sub("", text)  # 摘除全部（可能有多段），不只第一段
    tool_calls = _parse_dsml_tool_calls(text)
    if not tool_calls and _looks_like_dsml(text):
        # 兜底告警（2026-08-03 新增）：文本命中"疑似工具调用标记"特征，但所有已知
        # 解析器均未能解析出 tool_calls——大概率是模型又输出了一种尚未支持的新变体。
        # 记录 WARNING + 原始文本，便于从日志第一时间发现新变体。
        # 2026-08-04 修复：不再"按普通文本原样透传"——命中疑似标记的文本直接透传
        # 会把未解析的 <tool_call>/<seed:tool_result> 等原始标记泄漏给客户端（IDE
        # 显示裸露 XML、工具不执行）。改为剥离已知强标记块（tool_call/seed_call/
        # seed:tool_result 复述块等），只保留剩余正文；剥离后若为空则正文为空，
        # 宁可丢内容也不泄漏未解析的调用标记（调用意图已丢失，正文保留也无意义）。
        logger.warning(f"[trae-work] _looks_like_dsml=True 但未解析出 tool_calls，"
                        f"疑似新的工具调用标记变体，已剥离强标记。原始文本: {text[:16384]!r}")
        content_text = _strip_strong_tool_markers(text).strip()
    elif tool_calls:
        # 工具调用文本本身不作为正文回显（DSML/[Tool Call:]/<tool_call> 全部清洗掉）
        # 顺序关键：先剥 <seed:tool_result> 复述块（依赖 <tool_call>/<invoke> 等
        # 强标记界定块边界），再删 <tool_call> 等调用块——反序会因调用块已删
        # 找不到边界而吞掉复述块之后的正文（2026-08-04 实测）。
        content_text = _SEED_CALL_RE.sub("", text)
        content_text = _SEED_TOOL_CALL_RE.sub("", content_text)
        # 官方 seed-oss 推理标签 <seed:think>...</seed:think>（Qwen3 用 <think>）：
        # 推理内容已由上游 reasoning_content 结构化字段单独转发，文本形态的
        # think 标签不透明传给客户端（与 reasoning JSON 字面量同一处理原则）
        content_text = re.sub(r"<seed:think\b[^>]*>[\s\S]*?</seed:think\s*>", "", content_text, flags=re.I)
        content_text = re.sub(r"<think\b[^>]*>[\s\S]*?</think\s*>", "", content_text, flags=re.I)
        content_text = _strip_seed_tool_result_blocks(content_text)
        content_text = _DSML_BLOCK_RE.sub("", content_text)
        content_text = _DSML_TOOLCALLS_BLOCK_RE.sub("", content_text)
        content_text = _TOOLCALL_TEXT_RE.sub("", content_text)
        for m in re.finditer(_TOOLCALL_TEXT_RE, text):
            args = _extract_balanced_json(text, m.start(2))
            if args is not None:
                content_text = content_text.replace(args, "", 1)
        content_text = _TOOLCALL_XML_RE.sub("", content_text)
        # 官方 seed-oss 语法（<seed:tool_call><function=..>）已由 _SEED_TOOL_CALL_RE
        # 剥离；裸 <function=..>（无外层）由 _QWEN_FUNC_RE 兜底剥离
        content_text = _QWEN_FUNC_RE.sub("", content_text)
        content_text = content_text.strip()
    else:
        content_text = text.strip()
    return tool_calls, reasoning_text, content_text


def _strip_generic_tool_blocks(text: str) -> str:
    """通用剥离：移除任意"工具语义" XML 标签块（2026-08-04 根治层）。

    配合 _looks_like_dsml 的通用意图正则 _TOOL_INTENT_TAG_RE——检测层已从
    "已知标签白名单"升级为"语义关键词通用匹配"，剥离层必须同样通用，否则
    检测到新变体却剥不掉（白名单剥离 = 检测白搭，依旧泄漏）。

    做法：扫描文本中所有含工具语义关键词的标签（<tool_xxx>/<function>/
    <parameter>/<invoke>/<args> 等任意排列），对每个开标签用**平衡标签扫描**
    找到对应闭标签（支持嵌套，如 <tool_call><function>..</function></tool_call>），
    整块删除；未闭合的开标签从开标签剥离到文本末尾（调用输出被截断时，
    其后都是调用内容，原样透传只会泄漏半截 XML）。
    """
    out = text
    while True:
        m = _TOOL_INTENT_TAG_RE.search(out)
        if not m:
            break
        start = m.start()
        open_tag = m.group(0)
        # 自闭合标签 <call name=".." /> → 直接删
        if open_tag.rstrip().endswith("/>"):
            out = out[:start] + out[m.end():]
            continue
        # 提取开标签名（含命名空间前缀，如 <seed:tool_call> → seed:tool_call）
        name_m = re.match(r"<([a-zA-Z_:][\w:.-]*)", open_tag)
        if not name_m:
            out = out[:start] + out[m.end():]
            continue
        open_name = name_m.group(1)
        # 平衡扫描：找到与该开标签配对的闭标签（容忍嵌套同名标签）
        depth = 1
        pos = m.end()
        close_re = re.compile(rf"</{re.escape(open_name)}\s*>", re.I)
        open_re = re.compile(rf"<{re.escape(open_name)}\b[^>]*>", re.I)
        end = None
        while pos < len(out):
            nxt_open = open_re.search(out, pos)
            nxt_close = close_re.search(out, pos)
            if nxt_close and (not nxt_open or nxt_close.start() < nxt_open.start()):
                depth -= 1
                if depth == 0:
                    end = nxt_close.end()
                    break
                pos = nxt_close.end()
            elif nxt_open:
                depth += 1
                pos = nxt_open.end()
            else:
                break
        if end is not None:
            out = out[:start] + out[end:]
        else:
            # 未闭合：开标签之后全部视为调用内容，剥离到末尾
            out = out[:start]
    return out


def _strip_strong_tool_markers(text: str) -> str:
    """剥离所有已知"强工具调用标记"块，保留剩余文本（2026-08-04 兜底用）。

    覆盖：<tool_call>...</tool_call>（含 XML 子标签变体）、<seed_call>...</...>、
    DSML 独立块、[Tool Call: xxx]\nArguments: {...} 文本、<seed:tool_result> 复述块。
    另处理未闭合的 <tool_call> 开标签（模型输出被截断）：从开标签剥离到文本
    末尾——既然模型已进入调用输出模式，其后内容都是调用参数，原样透传只会把
    半截 JSON/XML 泄漏给客户端（2026-08-04 实测：IDE 显示裸露 <tool_call>）。
    供 _resolve_trae_text 解析失败兜底时调用——把未识别变体的标记外壳剥掉，
    防止原始 XML/文本标记原样泄漏给客户端；剩余正文继续展示。
    """
    out = text
    out = _SEED_CALL_RE.sub("", out)
    out = _SEED_TOOL_CALL_RE.sub("", out)
    # 官方 seed-oss/Qwen3 推理标签（<seed:think>/<think>）不透明给客户端
    out = re.sub(r"<seed:think\b[^>]*>[\s\S]*?</seed:think\s*>", "", out, flags=re.I)
    out = re.sub(r"<think\b[^>]*>[\s\S]*?</think\s*>", "", out, flags=re.I)
    # 先剥 <seed:tool_result>（依赖 <tool_call>/<invoke> 等强标记界定边界），再删调用块
    out = _strip_seed_tool_result_blocks(out)
    out = _TOOLCALL_XML_RE.sub("", out)
    out = _DSML_BLOCK_RE.sub("", out)
    out = _DSML_TOOLCALLS_BLOCK_RE.sub("", out)
    out = _TOOLCALL_TEXT_RE.sub("", out)
    for m in re.finditer(_TOOLCALL_TEXT_RE, text):
        args = _extract_balanced_json(text, m.start(2))
        if args is not None:
            out = out.replace(args, "", 1)
    # 未闭合 <tool_call> 开标签（无闭合标签，_TOOLCALL_XML_RE 匹配不到）→ 剥到末尾
    unclosed = re.search(r"<tool_call\b[^>]*>", out, re.I)
    if unclosed:
        out = out[:unclosed.start()]
    # 未闭合 <seed:tool_call> 开标签（官方 seed-oss 外层，截断）→ 剥到末尾
    unclosed_seed = re.search(r"<seed:tool_call\b[^>]*>", out, re.I)
    if unclosed_seed:
        out = out[:unclosed_seed.start()]
    # 未闭合 <function=..> 开标签（官方语法，无闭合 </function>）→ 剥到末尾
    unclosed_func = re.search(r"<function\s*=\s*[^>]*>", out, re.I)
    if unclosed_func:
        out = out[:unclosed_func.start()]
    # 未闭合 <seed:think>/<think> 开标签（截断）→ 剥到末尾
    unclosed_think = re.search(r"<seed:think\b[^>]*>|<think\b[^>]*>", out, re.I)
    if unclosed_think:
        out = out[:unclosed_think.start()]
    # 根治兜底：通用剥离任意"工具语义" XML 标签块（覆盖所有未识别新变体）
    # 已知格式已被上方逐条剥离，此处处理剩余的任何工具语义标签排列。
    out = _strip_generic_tool_blocks(out)
    return out


def _parse_toolcall_subtags(block: str) -> list:
    """变体 6（2026-08-04 实测，Doubao-Seed-Code）：<tool_call> 块内 XML 子标签。

    形态（与 opencode 客户端工具调用的历史格式同源，模型从上下文学到）：
      <tool_call>
      <tool_name>bash</tool_name>
      <parameters>
      <parameter name="command" string="true">cd /x && git status</parameter>
      <parameter name="timeout" string="false">10000</parameter>
      </parameters>
      </tool_call>

    变体 7（2026-08-05 实测）：同外层 <tool_name> 标签，但参数不是 <parameter>
    而是 <arguments>{"command": ".."}</arguments> JSON 包裹形式：
      <tool_call>
      <tool_name>bash</tool_name>
      <arguments>{"command": "cd /x && git status"}</arguments>
      </tool_call>
    曾因只认 <parameter> 子标签，<arguments> 块被跳过 → subtags 返回 [] → 整段
    <tool_call> 落入兜底剥离路径，工具调用意图丢失（2026-08-05 实测）。

    解析 <tool_name> 为工具名；<arguments> 整块 JSON 直接作为 arguments 字符串；
    <parameter name=".."> 收集为 arguments 的 {param_name: value}。JSON 值仍走
    _extract_balanced_json（防嵌套花括号截断），普通字符串值按标签位置切片。
    无 tool_name 或参数异常返回 []（交由上层兜底）。
    """
    tcs = []
    for name_match in _TOOLCALL_NAME_RE.finditer(block):
        tool_name = name_match.group(1).strip()
        if not tool_name:
            continue
        # 变体 7 优先：<arguments> JSON 包裹整块参数
        args_match = _TOOLCALL_ARGS_RE.search(block)
        if args_match:
            args_raw = args_match.group(1).strip()
            json_start = args_raw.find("{")
            if json_start >= 0:
                balanced = _extract_balanced_json(args_raw, json_start)
                if balanced is not None and not args_raw[:json_start].strip():
                    args_raw = balanced
            item = {"type": "function", "function": {"name": tool_name, "arguments": args_raw}}
            tcs.append(item)
            continue
        params = {}
        for pm in _TOOLCALL_PARAM_RE.finditer(block):
            param_name = pm.group(1).strip()
            if not param_name:
                continue
            param_value = pm.group(2)
            json_start = param_value.find("{")
            if json_start >= 0:
                balanced = _extract_balanced_json(param_value, json_start)
                if balanced is not None and not param_value[:json_start].strip():
                    param_value = balanced
            params[param_name] = param_value
        item = {"type": "function", "function": {"name": tool_name}}
        if params:
            item["function"]["arguments"] = json.dumps(params, ensure_ascii=False)
        tcs.append(item)
    return tcs


def _parse_dsml_tool_calls(text: str) -> list:
    """把工具调用标记文本解析为 OpenAI tool_calls 列表；无匹配返回 []。

    依次尝试：<｜DSML｜invoke name=".."><｜DSML｜parameter name="..">..</｜DSML｜parameter></｜DSML｜invoke>
    标签属性变体 → DSML 标记（<｜function｜> 块）→ [Tool Call: name]\nArguments: {...} 文本
    → <tool_call>JSON</tool_call> → <seed_call> 异步闭合标签变体。
    """
    tcs = []
    # 变体 4（2026-08-03）：<｜DSML｜invoke name="bash"> + 内部多个
    # <｜DSML｜parameter name="command" ...>value</｜DSML｜parameter>。
    # 一个 invoke 可能含多个 parameter，需要收集成 {param_name: value} 再序列化 arguments。
    for tool_name, invoke_body in _DSML_INVOKE_RE.findall(text):
        tool_name = tool_name.strip()
        if not tool_name:
            continue
        params = {}
        for param_name, param_val in _DSML_PARAM_RE.findall(invoke_body):
            param_name = param_name.strip()
            if param_name:
                params[param_name] = param_val
        item = {"type": "function", "function": {"name": tool_name}}
        if params:
            item["function"]["arguments"] = json.dumps(params, ensure_ascii=False)
        tcs.append(item)
    if tcs:
        return tcs
    for name_raw, args_raw in _DSML_PAIR_RE.findall(text):
        name = name_raw.strip()
        if not name:
            continue
        args = args_raw.strip()
        item = {"type": "function", "function": {"name": name}}
        if args:
            item["function"]["arguments"] = args
        tcs.append(item)
    if not tcs:
        # [Tool Call: name]\nArguments: {...} 文本格式（seed-code 从历史学到的输出形态）
        for m in _TOOLCALL_TEXT_RE.finditer(text):
            name = m.group(1).strip()
            args = _extract_balanced_json(text, m.start(2))
            if args is None:
                break  # arguments 分片未完整（{ 后还没闭合），等待更多内容累积
            item = {"type": "function", "function": {"name": name}}
            if args:
                item["function"]["arguments"] = args
            tcs.append(item)
    if not tcs:
        # <tool_call> XML 格式（trae-local-api 提示词注入方式，模型遵循提示词输出）：
        # <tool_call> 内部本应是一个完整 JSON 对象 {"name":...,"arguments":{...}}。
        # 曾用正则 _TOOLCALL_XML_JSON_RE（\{[\s\S]*?\} 非贪婪匹配 arguments 对象）
        # 提取 name/arguments，但正则无法正确处理"对象内嵌套花括号"（如 JS 代码片段
        # 里的 `{{}}`）或"转义引号"——遇到第一个 `}` 就提前截断，导致 arguments 提取
        # 出半截 JSON、json.loads 校验失败、tcs 为空，整段 <tool_call> 文本原样
        # 泄漏到正文给客户端（2026-08-02 实测：edit 工具调用的 oldString/newString
        # 含 JS 花括号代码，被截断泄漏）。
        # 教训：任何"提取 JSON 对象子串"的场景，一律用平衡括号扫描
        # （_extract_balanced_json），不能用正则模拟花括号配对——这是本文件里
        # 反复踩坑的同一类错误（DSML 配对正则、reasoning 多段提取也是类似教训）。
        for block in _TOOLCALL_XML_RE.findall(text):
            block = block.strip()
            # 变体 6/7（2026-08-04/05 实测）：块内含 <tool_name> XML 子标签时，
            # 优先走子标签解析（<parameter name=".."> 与 <arguments>{"..":".."} 两形态）。
            # 注意：变体 7 的 <arguments> 里含 {（JSON），若按下方平衡括号扫描会把
            # {"command":...} 当"块内完整 JSON"提取，但该 JSON 无 name 字段 →
            # json.loads 后 name 为空被 continue 跳过 → 整段逃逸（2026-08-05 实测）。
            # 故必须先用 _TOOLCALL_NAME_RE 判断 XML 子标签形态，再决定走哪条解析。
            if _TOOLCALL_NAME_RE.search(block):
                subtags = _parse_toolcall_subtags(block)
                if subtags:
                    tcs.extend(subtags)
                continue
            obj_start = block.find("{")
            if obj_start < 0:
                continue  # 无 tool_name 也无 JSON，非工具调用块，跳过
            obj_str = _extract_balanced_json(block, obj_start)
            if obj_str is None:
                continue  # 未闭合（半截标记），交由上层判定是否继续等待
            try:
                # 模型常在 command JSON 字符串中直接输出多行脚本；这违反严格 JSON
                # 的控制字符约束，但仍是可恢复的工具调用文本。strict=False 保留原值。
                obj = json.loads(obj_str, strict=False)
            except Exception:
                continue
            name = (obj.get("name") or obj.get("tool_name") or "").strip()
            if not name:
                continue
            args_val = obj.get("arguments", {})
            args_str = json.dumps(args_val, ensure_ascii=False) if not isinstance(args_val, str) else args_val
            tcs.append({"type": "function", "function": {"name": name, "arguments": args_str}})
    if not tcs:
        # 变体 5（2026-08-04）：<seed_call> 内的 invoke/function + parameter。
        # 参数标签只用正则定位开标签，值通过闭合标签位置切片，避免非贪婪正则在
        # JSON/JS 花括号内容上截断；JSON 值仍交给平衡括号扫描提取。
        for seed_call in _SEED_CALL_RE.findall(text):
            for variant_re in (_SEED_CALL_INVOKE_RE, _SEED_CALL_FUNCTION_RE):
                for tool_name, invoke_body in variant_re.findall(seed_call):
                    tool_name = tool_name.strip()
                    if not tool_name:
                        continue
                    params = {}
                    for param_match in _SEED_CALL_PARAM_OPEN_RE.finditer(invoke_body):
                        param_name = param_match.group(1).strip()
                        value_end = invoke_body.find("</parameter>", param_match.end())
                        if not param_name or value_end < 0:
                            continue
                        param_value = invoke_body[param_match.end():value_end]
                        json_start = param_value.find("{")
                        if json_start >= 0:
                            balanced_value = _extract_balanced_json(param_value, json_start)
                            if balanced_value is not None and not param_value[:json_start].strip():
                                param_value = balanced_value
                        params[param_name] = param_value
                    item = {"type": "function", "function": {"name": tool_name}}
                    if params:
                        item["function"]["arguments"] = json.dumps(params, ensure_ascii=False)
                    tcs.append(item)
    if not tcs:
        # 变体 8（2026-08-04，官方 seed-oss/Qwen3 XML 语法，vllm qwen3.py）：
        #   <seed:tool_call><function=bash><parameter=command>ls</parameter></function></seed:tool_call>
        # 官方语法用"无空格无引号"的 <tag=name> 属性形式（与 <function name="..">
        # 带空格引号形式是两套不同语法），seed-oss 外层为 <seed:tool_call>（带冒号）。
        # 官方 parser 还容忍：无外层直接 <function=>（fallback）、</function> 后
        # 连续下一个 <tool_call>。先匹配 <seed:tool_call> 外层，再匹配裸 <function=>。
        for block in _SEED_TOOL_CALL_RE.findall(text):
            tcs.extend(_parse_qwen_func_params(block))
        if not tcs:
            # 无 <seed:tool_call> 外层：官方 parser 的 fallback——裸 <function=> 直接解析
            for tool_name, func_body in _QWEN_FUNC_RE.findall(text):
                tool_name = tool_name.strip()
                if not tool_name:
                    continue
                params = {}
                for param_name, param_val in _QWEN_PARAM_RE.findall(func_body):
                    param_name = param_name.strip()
                    param_val = param_val.strip()
                    if param_name:
                        json_start = param_val.find("{")
                        if json_start >= 0:
                            balanced_value = _extract_balanced_json(param_val, json_start)
                            if balanced_value is not None and not param_val[:json_start].strip():
                                param_val = balanced_value
                        params[param_name] = param_val
                item = {"type": "function", "function": {"name": tool_name}}
                if params:
                    item["function"]["arguments"] = json.dumps(params, ensure_ascii=False)
                tcs.append(item)
    return tcs


def _parse_qwen_func_params(func_body: str) -> list:
    """解析官方 Qwen3/seed-oss <function=..> 块内的 <parameter=..> 参数为 tool_calls。

    与 _QWEN_PARAM_RE 配合：一个 function 块内可能有多个 <parameter=key>value</parameter>，
    全部收集成 {param_name: value} 再序列化 arguments。JSON 值走平衡括号提取，
    防止嵌套花括号截断（与 _parse_toolcall_subtags 同一教训）。
    """
    tcs = []
    func_re = re.compile(r"<function\s*=\s*([^>\s/]+)\s*>([\s\S]*?)(?:</function\s*>|$)", re.I)
    for fm in func_re.finditer(func_body):
        tool_name = fm.group(1).strip()
        if not tool_name:
            continue
        inner = fm.group(2)
        params = {}
        for pm in _QWEN_PARAM_RE.finditer(inner):
            param_name = pm.group(1).strip()
            param_val = pm.group(2).strip()
            if not param_name:
                continue
            json_start = param_val.find("{")
            if json_start >= 0:
                balanced_value = _extract_balanced_json(param_val, json_start)
                if balanced_value is not None and not param_val[:json_start].strip():
                    param_val = balanced_value
            params[param_name] = param_val
        item = {"type": "function", "function": {"name": tool_name}}
        if params:
            item["function"]["arguments"] = json.dumps(params, ensure_ascii=False)
        tcs.append(item)
    return tcs


def _trae_final_to_openai(model: str, finish_reason: str = "stop") -> dict:
    """流结束标记（OpenAI 兼容 finish）。

    finish_reason 必须与实际输出匹配：转出过 tool_calls 时为 "tool_calls"
    （客户端据此执行工具），否则 "stop"（默认）。
    """
    return {
        "id": "chatcmpl-final",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }


def _trae_nonstream_to_openai(model: str, content_parts: list, reasoning_parts: list, tool_calls: list | None = None) -> dict:
    """非流式：把累积的 response/reasoning 拼成 OpenAI 完成响应；含 tool_calls 时附上。"""
    message: dict = {
        "role": "assistant",
        "content": "".join(content_parts),
        "reasoning_content": "".join(reasoning_parts) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-trae",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _handle_traework(writer, target, method, path, headers, body, stats, label):
    """Trae Work 协议代理：OpenAI 请求 → llm_utils_chat → OpenAI 响应。

    覆盖 /v1/chat/completions（含流式）。
    认证：Cloud-IDE-JWT（secrets.json 的 trae_work_token）。
    """
    import json as _json
    # 延迟导入：_TARGETS / _SECRETS 在配置热重载时会被重新绑定，
    # 每次调用重新读取才能拿到最新配置（与拆分前同模块访问行为一致）
    from server import (
        _cfg, _SECRETS, _TARGETS,
        _write_error_response, _write_response, _bump_model_stats,
    )
    token = _cfg.resolve_secret(target, _SECRETS) or os.environ.get("TRAE_WORK_TOKEN", "")
    if not token:
        await _write_error_response(writer, 401, "Trae Work token 缺失，请到 dashboard 填写 trae_work_token")
        return
    # 函数内所有日志切到 trae-work 独立文件（traework.log），不污染 proxy.log
    logger = traework_logger
    api_headers = _trae_build_headers(token)

    try:
        # ── /v1/models：优先上游 get_detail_param 实时同步（TTL 缓存），
        #    再按 targets.json enabled=true 白名单过滤（屏蔽空响应/收费模型，如
        #    Doubao-Seed-2.1-Pro / kimi-k3 / DeepSeek-V4-Flash-Official），
        #    失败回退配置白名单，再兜底静态列表 ──
        if path == "/v1/models" and method == "GET":
            models = []
            _tw_tgt = next((t for t in _TARGETS if t.get("label") == "trae-work"), None)
            # 配置白名单：仅 enabled=true 的模型（dashboard 可编辑）
            _cfg_whitelist = []
            for m in (_tw_tgt or {}).get("models", []):
                mid = m.get("id") if isinstance(m, dict) else str(m)
                if mid and (m.get("enabled", False) if isinstance(m, dict) else True):
                    _cfg_whitelist.append(mid)
            upstream = await _trae_fetch_models(token)
            if upstream:
                models = [{"id": mid, "object": "model", "created": 1700000000, "owned_by": "trae"}
                          for mid in upstream if mid in _cfg_whitelist]
            if not models:
                for mid in _cfg_whitelist:
                    models.append({"id": mid, "object": "model", "created": 1700000000, "owned_by": "trae"})
            if not models:
                # 兜底：配置缺失时的静态列表（仅可用模型）
                for mid in ("glm-5.2", "DeepSeek-V4-Pro", "DeepSeek-V4-Flash", "Doubao-Seed-Code"):
                    models.append({"id": mid, "object": "model", "created": 1700000000, "owned_by": "trae"})
            payload = _json.dumps({"data": models, "object": "list", "has_more": False}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload), payload))
            await writer.drain()
            writer.close(); return

        # ── /v1/chat/completions：转换 + 转发 ──
        if path == "/v1/chat/completions" and method == "POST":
            try:
                body_json = _json.loads(body.decode("utf-8"))
            except Exception:
                await _write_error_response(writer, 400, "invalid json"); return
            model = (body_json.get("model") or "glm-5.2").split("/")[-1]
            is_stream = bool(body_json.get("stream", False))
            # 注：totalRequests 由外层 _handle_target_request 统一计数，这里不再 +1（避免双重计数）
            _bump_model_stats(label, model, "ok")
            logger.debug(f"[{label}] trae-work {method} {path} model={model} stream={is_stream} "
                         f"req_body={_json.dumps(body_json, ensure_ascii=False)[:2000]}")

            trae_body = _openai_to_trae_body(body_json)
            endpoint = f"{_TRAE_API_HOST}/api/agent/v3/llm_utils_chat"
            logger.debug(f"[{label}] trae-work upstream POST {endpoint} "
                         f"body={_json.dumps(trae_body, ensure_ascii=False)[:3000]}")
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as c:
                req = c.build_request("POST", endpoint, headers=api_headers,
                                      content=_json.dumps(trae_body).encode())
                resp = await c.send(req, stream=True)
                logger.debug(f"[{label}] trae-work upstream HTTP {resp.status_code}")

                if resp.status_code >= 400:
                    resp_body = await resp.aread()
                    logger.debug(f"[{label}] trae-work upstream error body="
                                 f"{resp_body.decode('utf-8', errors='replace')[:4000]}")
                    await _write_error_response(writer, resp.status_code,
                                                f"Trae upstream HTTP {resp.status_code}: {resp_body.decode('utf-8', errors='replace')[:300]}")
                    return

                # ── 流式：Trae SSE → OpenAI SSE ──
                # 架构（2026-08-02 重构，对齐 trae-local-api 官方实现）：response/content
                # 正文只做纯累积（text_buf），不逐 chunk 转发；reasoning_content/tool_calls
                # 等结构化字段（上游明确给出，非文本猜测）逐 chunk 立即转发。上游流结束后，
                # 对累积的完整正文一次性解析工具调用/reasoning，再统一 flush（_resolve_trae_text）。
                # 好处：不存在"标记半截"的判定难题（标记此时必然完整或确定不存在）。
                # 代价：牺牲逐字打字机效果，换取正确性（不会再出现半截标记导致卡顿/丢弃/泄漏）。
                if is_stream:
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
                    await writer.drain()
                    text_buf = ""  # 本轮累积的 response/content 正文（流结束后统一解析）
                    n_chunks = 0
                    busy_aborted = False
                    emitted_tool_calls = False  # 转出过 tool_calls → 流结束 finish_reason="tool_calls"
                    async for chunk in resp.aiter_bytes():
                        line = chunk.decode("utf-8", errors="replace")
                        for raw in line.split("\n"):
                            raw = raw.strip()
                            if not raw.startswith("data:"):
                                continue
                            data_str = raw[5:].strip()
                            if not data_str:
                                continue
                            try:
                                trae_chunk = _json.loads(data_str)
                            except Exception:
                                continue
                            # 上游 SSE 有非对象 data 行（如 "Processing_xxx" 字符串），跳过
                            if not isinstance(trae_chunk, dict):
                                continue
                            # 排队事件 request_wait_in_queue（字节原生）：模型繁忙，直接终止返回提示
                            _pos = trae_chunk.get("position")
                            if _pos is None and isinstance(trae_chunk.get("data"), dict):
                                _pos = trae_chunk["data"].get("position")
                            if isinstance(_pos, (int, float)) and _pos > 0:
                                logger.warning(f"[{label}] trae-work busy: queue position #{int(_pos)} (model={model}), aborting")
                                oai = _trae_chunk_to_openai({"response": f"[模型繁忙，排队位置 #{int(_pos)}，请稍后重试]"}, model)
                                n_chunks += 1
                                writer.write(("data: " + _json.dumps(oai, ensure_ascii=False) + "\n\n").encode())
                                await writer.drain()
                                busy_aborted = True
                                break
                            # SSE error 事件（上游流式错误 {code,message}）：不再静默吞掉
                            if trae_chunk.get("error") or (trae_chunk.get("code") and "message" in trae_chunk):
                                err_code = trae_chunk.get("code") or ""
                                err_msg = trae_chunk.get("message") or trae_chunk.get("error") or ""
                                logger.warning(f"[{label}] trae-work SSE error: code={err_code} msg={str(err_msg)[:300]}")
                                oai = _trae_chunk_to_openai({"response": f"[Trae error {err_code}: {err_msg}]"}, model)
                                n_chunks += 1
                                writer.write(("data: " + _json.dumps(oai, ensure_ascii=False) + "\n\n").encode())
                                await writer.drain()
                                continue
                            # 只转换 output 事件（旧格式 response/reasoning_content + 新格式 type=text/content/reasoning）
                            if ("response" in trae_chunk or "reasoning_content" in trae_chunk
                                    or "content" in trae_chunk or "reasoning" in trae_chunk
                                    or trae_chunk.get("type") == "text"):
                                resp_text = trae_chunk.get("response") or trae_chunk.get("content") or ""
                                # 上游 progress 提示（旧格式）过滤，避免当正文输出
                                if resp_text.startswith("Building prompt:") or resp_text.startswith("Completed building prompt"):
                                    continue
                                if resp_text:
                                    text_buf += resp_text
                                # reasoning_content/reasoning 是上游明确给出的结构化字段（非文本猜测），
                                # 不存在"标记未闭合"的歧义，可以立即转发
                                reasoning = trae_chunk.get("reasoning_content") or trae_chunk.get("reasoning") or ""
                                if reasoning:
                                    oai_r = _trae_chunk_to_openai({"reasoning_content": reasoning}, model)
                                    n_chunks += 1
                                    writer.write(("data: " + _json.dumps(oai_r, ensure_ascii=False) + "\n\n").encode())
                                    await writer.drain()
                                # 上游原生 tool_calls 字段（非文本解析出来的，结构明确）→ 立即转发
                                if trae_chunk.get("tool_calls"):
                                    oai_tc = _trae_chunk_to_openai({"tool_calls": trae_chunk["tool_calls"]}, model)
                                    emitted_tool_calls = True
                                    n_chunks += 1
                                    logger.debug(f"[{label}] trae-work chunk tool_calls: "
                                                 f"{_json.dumps(trae_chunk.get('tool_calls'), ensure_ascii=False)[:800]}")
                                    writer.write(("data: " + _json.dumps(oai_tc, ensure_ascii=False) + "\n\n").encode())
                                    await writer.drain()
                        if busy_aborted:
                            break
                    # 上游流结束：对累积的完整正文一次性解析（此时标记必然完整或确定不存在）
                    if text_buf:
                        tcs, rtext, content_text = _resolve_trae_text(text_buf)
                        logger.debug(f"[{label}] trae-work resolved: tool_calls={len(tcs)} "
                                     f"reasoning={bool(rtext)} content_len={len(content_text)}")
                        if rtext:
                            oai_r = _trae_chunk_to_openai({"reasoning_content": rtext}, model)
                            n_chunks += 1
                            writer.write(("data: " + _json.dumps(oai_r, ensure_ascii=False) + "\n\n").encode())
                            await writer.drain()
                        if content_text:
                            oai_c = _trae_chunk_to_openai({"response": content_text}, model)
                            n_chunks += 1
                            writer.write(("data: " + _json.dumps(oai_c, ensure_ascii=False) + "\n\n").encode())
                            await writer.drain()
                        if tcs:
                            emitted_tool_calls = True
                            oai_tc = _trae_chunk_to_openai(
                                {"response": "", "tool_calls": [{"type": "function", "function": tc["function"]} for tc in tcs]},
                                model,
                            )
                            n_chunks += 1
                            writer.write(("data: " + _json.dumps(oai_tc, ensure_ascii=False) + "\n\n").encode())
                            await writer.drain()
                    logger.debug(f"[{label}] trae-work stream done, {n_chunks} chunks → client"
                                 + (" (busy abort)" if busy_aborted else ""))
                    writer.write(("data: " + _json.dumps(
                        _trae_final_to_openai(model, "tool_calls" if emitted_tool_calls else "stop"),
                        ensure_ascii=False) + "\n\n").encode())
                    writer.write(b"data: [DONE]\n\n")
                    await writer.drain()
                    stats["passthroughOk"] += 1
                    writer.close(); return

                # ── 非流式：累积 output 事件（架构对齐流式：正文纯累积，流结束后统一解析）──
                resp_body = await resp.aread()
                content_parts, reasoning_parts, tool_calls = [], [], []
                text_buf = ""
                for raw in resp_body.decode("utf-8", errors="replace").split("\n"):
                    raw = raw.strip()
                    if not raw.startswith("data:"):
                        continue
                    data_str = raw[5:].strip()
                    if not data_str:
                        continue
                    try:
                        trae_chunk = _json.loads(data_str)
                    except Exception:
                        continue
                    # 上游 SSE 有非对象 data 行（如 "Processing_xxx" 字符串），跳过
                    if not isinstance(trae_chunk, dict):
                        continue
                    # SSE error 事件（上游流式错误 {code,message}）：不再静默吞掉
                    if trae_chunk.get("error") or (trae_chunk.get("code") and "message" in trae_chunk):
                        err_code = trae_chunk.get("code") or ""
                        err_msg = trae_chunk.get("message") or trae_chunk.get("error") or ""
                        logger.warning(f"[{label}] trae-work SSE error: code={err_code} msg={str(err_msg)[:300]}")
                        content_parts.append(f"[Trae error {err_code}: {err_msg}]")
                        continue
                    resp_text = trae_chunk.get("response") or trae_chunk.get("content") or ""
                    if resp_text:
                        # 上游 progress 提示（旧格式）过滤，避免当正文输出
                        if resp_text.startswith("Building prompt:") or resp_text.startswith("Completed building prompt"):
                            continue
                        text_buf += resp_text
                    reasoning = trae_chunk.get("reasoning_content") or trae_chunk.get("reasoning") or ""
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    tc = trae_chunk.get("tool_calls")
                    if tc:
                        # 上游非流式可能把同一工具调用分片输出（如 glm-5.2 的 arguments
                        # 拆成多个 tool_calls 事件），按 index 合并 name/arguments
                        for oai_tc in _trae_tool_calls_to_openai(tc):
                            idx = oai_tc.get("index", len(tool_calls))
                            if idx < len(tool_calls):
                                prev = tool_calls[idx]
                                fn = prev.get("function", {})
                                cur = oai_tc.get("function", {})
                                if cur.get("name") and not fn.get("name"):
                                    fn["name"] = cur["name"]
                                if cur.get("arguments"):
                                    fn["arguments"] = (fn.get("arguments") or "") + cur["arguments"]
                                prev["function"] = fn
                                if oai_tc.get("id") and not prev.get("id"):
                                    prev["id"] = oai_tc["id"]
                            else:
                                tool_calls.append(oai_tc)
                if text_buf:
                    resolved_tcs, resolved_r, resolved_content = _resolve_trae_text(text_buf)
                    if resolved_r:
                        reasoning_parts.append(resolved_r)
                    if resolved_tcs:
                        tool_calls = resolved_tcs
                    elif resolved_content:
                        content_parts.append(resolved_content)
                out = _trae_nonstream_to_openai(model, content_parts, reasoning_parts, tool_calls or None)
                payload = _json.dumps(out, ensure_ascii=False).encode()
                logger.debug(f"[{label}] trae-work nonstream done: content_parts={len(content_parts)} "
                             f"reasoning={len(reasoning_parts)} tool_calls={len(tool_calls)} "
                             f"out_len={len(payload)}")
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n".encode())
                writer.write(payload)
                await writer.drain()
                stats["passthroughOk"] += 1
                writer.close(); return

        # ── 其他路径：透传原生端点 ──
        upstream_url = f"{_TRAE_API_HOST}{path}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), trust_env=False) as c:
            req = c.build_request(method, upstream_url, headers=api_headers, content=body if body else None)
            resp = await c.send(req, stream=True)
            status, _ = await _write_response(writer, resp, stats=stats)
            logger.debug(f"[{label}] trae-work passthrough {method} {path} → HTTP {status}")
            if status and status >= 400:
                logger.warning(f"[{label}] trae-work {path} HTTP {status}")
            return
    except Exception as e:
        stats["passthroughError"] += 1
        logger.exception(f"[{label}] trae-work proxy exception")
        try:
            await _write_error_response(writer, 503, f"Trae Work proxy error: {e}")
        except Exception:
            pass
