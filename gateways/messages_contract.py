"""
messages_contract.py — /v1/messages passthrough 请求体字段契约。

原本该白名单内联在 server.py 的 create_message 里（server.py:2051-2054），
属于 per-request 局部常量，难以单测且缺失 `thinking` 字段。

这里把它提升为单一事实源（single source of truth），供 server.py 透传路径引用，
并补上 `thinking`，使上游 extended thinking 不再被静默丢弃。

只有 /v1/messages passthrough 这一条路径引用本模块；其它代码路径不受影响。
"""
from typing import Optional

# Anthropic /v1/messages 标准透传字段白名单。
# 原 server.py 内联集合 = 以下 12 个字段；本次唯一新增 = "thinking"。
# 注意：契约刻意保持最小，禁止随意加字段（改前需评估下游协议兼容性）。
_MESSAGES_ALLOWED_FIELDS = frozenset(
    {
        "model",
        "max_tokens",
        "messages",
        "system",
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
        "stream",
        "tools",
        "tool_choice",
        "metadata",
        "thinking",  # 新增：保留扩展思考（extended thinking）配置
    }
)

# 语义能力字段 → 其在 target `messagesProfile` 中的开关键名。
#
# 只有这三类「语义能力字段」会被 profile 门控：当对应开关键显式为 false 时，
# 即便该字段在白名单内，也将在转发前被剥离（capability-driven fail 策略档）。
# 其余白名单字段（含未知/可选字段）一律照常转发——profile 门控绝不外溢。
# profile 中某开关键缺失时，按 fail-open 处理：默认视为「支持」（透传），
# 绝不因「未声明」而静默丢弃（设计约束：禁止全局 top_k 误删）。
_SEMANTIC_CAPABILITY_FIELDS = {
    "thinking": "supportsThinking",
    "top_k": "supportsTopK",
    "tool_choice": "supportsToolChoice",
}


def _profile_disables(profile: Optional[dict], field: str) -> bool:
    """返回 True 表示 profile 显式声明该语义字段不被支持，须剥离。

    - profile 为 None 或未含对应开关键 → 默认支持（不剥离，fail-open）。
    - 开关键显式为 false（含字符串 "false"） → 剥离。
    - 其它任何值（true / 缺失 / 非布尔） → 透传，绝不误删。
    """
    if not isinstance(profile, dict):
        return False
    cap_key = _SEMANTIC_CAPABILITY_FIELDS.get(field)
    if cap_key is None:
        return False
    supported = profile.get(cap_key, True)
    if supported is False:
        return True
    if isinstance(supported, str) and supported.strip().lower() == "false":
        return True
    return False


def filter_messages_request(body: dict, profile: Optional[dict] = None) -> dict:
    """只保留 Anthropic /v1/messages 标准字段，丢弃额外字段（如 context_management）。

    `profile` 为可选的目标 `messagesProfile` 字典（targets.json 中声明的
    每端能力开关）。当 profile 显式声明某语义能力字段（thinking/top_k/tool_choice）
    不被支持时，该字段即便在白名单内也会被剥离；profile 缺失或对应键未声明，
    则完全沿用既往行为（白名单照常，未知字段照常丢弃），保证存量 target 零影响。

    纯函数：不触碰网络/全局状态，便于单测。
    """
    out: dict = {}
    for k, v in body.items():
        if k not in _MESSAGES_ALLOWED_FIELDS:
            continue  # 不在白名单 → 丢弃（既有契约不变）
        # 仅对已知语义能力字段做 profile 门控：显式 false 才剥离，否则透传
        if _profile_disables(profile, k):
            continue
        out[k] = v
    return out
