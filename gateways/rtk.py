# RTK token-saver（9Router open-sse/rtk 的 Python 移植，MIT，纯标准库）。
#
# 干什么：在请求发往上游前，就地压缩 Anthropic `tool_result` 块里超长的工具输出
# （文件全文 / 长日志 / 目录树等），把中段砍掉只留头尾，省 prompt token。
#
# 移植范围（首版刻意最小）：只有一个过滤器 smart_truncate（头 120 行 + 尾 60 行）。
# 不做 gitLog/grep/buildOutput 等类型特化（本期 Must-NOT-Have），也不做 LLM 总结式压缩。
#
# 只碰 tool_result：`text` / `thinking` / `tool_use` 等块一律零改动——那是模型的思考与
# 指令，动了就是改语义；tool_result 是机器产出的原始噪音，砍中段无损意图。
# `is_error=True` 的 tool_result 同样跳过：错误栈的中段往往正是根因所在。
#
# 降级原则：本模块是「旁路省钱」，任何异常都不得影响主请求链路——
# 所有对外函数捕获异常后返回原文 / 空 stats，绝不 raise，也绝不半途改坏 body。
#
# 四重护栏（compress_text）：太小不压 / 太大不压 / 过滤器异常回退原文 / 压完变空或
# 没变短则回退原文。任何一条不满足都当作「没发生过」，stats 也不记账。
#
# 字节 vs 字符：9Router 的 MIN_COMPRESS_SIZE / RAW_CAP 语义是 bytes，因此这里统一用
# len(text.encode("utf-8")) 计量；而 smart_truncate 的门槛是「行数」，与字节无关。
#
# 跨模块约定（AGENTS.md §7）：对 server 的共享依赖用函数内延迟导入 + fallback，
# 保证无 server 运行时（纯单测 test_rtk.py）也能独立 `import gateways.rtk`。
import logging
from typing import Any, Callable, Dict, List, Optional

# ─── 常量（取自 9Router constants.js，精确值，勿随意调） ───
RAW_CAP = 10 * 1024 * 1024        # 单块原文上限 10 MiB：超过多半是二进制/巨型 dump，不值得处理
MIN_COMPRESS_SIZE = 500           # 小于 500 字节不压：省下的还不够标记行占的
SMART_TRUNCATE_HEAD = 120         # 保留头部行数
SMART_TRUNCATE_TAIL = 60          # 保留尾部行数
SMART_TRUNCATE_MIN_LINES = 250    # 行数少于此值不截断（头+尾+余量）
FILTER_NAME = "smart-truncate"

_FALLBACK_LOGGER = logging.getLogger("rtk")


def _log():
    """取 server 的 logger；无 server 运行时（单测）回落到本地 logger。

    只在 server 已被加载时复用它的 logger——绝不由本模块「首次」触发 server 导入。
    本模块是旁路优化，一条降级 warning 不该拉起整个 server（配置加载 / 凭据解密 /
    网关日志 handler 等副作用），那在单测里是污染，在运行时是无谓开销。
    """
    try:
        import sys
        srv = sys.modules.get("server")
        if srv is not None and getattr(srv, "logger", None) is not None:
            return srv.logger
    except Exception:
        pass
    return _FALLBACK_LOGGER


def smart_truncate(text: str) -> str:
    """保留头 120 行 + 尾 60 行，中段替换为一行计数标记。

    对齐 JS：按 "\\n" 切分，不做 \\r\\n 预处理（保持与上游实现一致的行为，
    CRLF 文本的 \\r 会留在行尾，与 JS 版结果逐字节相同）。
    行数不足 SMART_TRUNCATE_MIN_LINES 时原样返回——头尾加起来 180 行，
    250 行以下砍不出值得的收益，还平白丢上下文。
    """
    lines = text.split("\n")
    if len(lines) < SMART_TRUNCATE_MIN_LINES:
        return text
    head = lines[:SMART_TRUNCATE_HEAD]
    tail = lines[-SMART_TRUNCATE_TAIL:]
    cut = len(lines) - SMART_TRUNCATE_HEAD - SMART_TRUNCATE_TAIL
    # 精确字符串：三个 ASCII 点 + 空格 + "+N lines truncated"（不用省略号字符 …）
    marker = "... +%d lines truncated" % cut
    return "\n".join(head + [marker] + tail)


def safe_apply(fn: Any, text: str) -> str:
    """调用过滤器，任何异常 / 非法返回都退回原文。

    只保证「类型安全 + 不抛」；是否值得采用（变空 / 变长）由 compress_text 判定，
    两层职责分开，过滤器实现者不必自己重复护栏。
    """
    if not callable(fn):
        return text
    try:
        out = fn(text)
    except Exception as e:
        _log().warning(f"[RTK] filter panicked, passthrough: {e}")
        return text
    if not isinstance(out, str):
        return text
    return out


def compress_text(text: str, stats: Dict[str, Any], shape: str) -> str:
    """压缩单段文本；四重护栏任一不满足即原样返回且不记 stats。

    stats 结构：{"bytesBefore": int, "bytesAfter": int,
                "hits": [{"shape": str, "filter": str, "saved": int}]}
    只有真正产生收益时才累加，因此 format_rtk_log 里的百分比天然非负。
    """
    if not isinstance(text, str) or not text:
        return text
    bytes_in = len(text.encode("utf-8"))
    # 护栏 1/2：太小（收益 < 噪音）或太大（多半非文本，处理成本不划算）
    if bytes_in < MIN_COMPRESS_SIZE or bytes_in > RAW_CAP:
        return text

    # 护栏 3：过滤器异常/返回非 str → safe_apply 已退回原文
    # 走模块全局查表（而非直接引用），便于注入替换过滤器且不改调用点
    out = safe_apply(globals().get("smart_truncate"), text)

    # 护栏 4：变空或没变短 → 当作没发生（宁可不省，也不能把内容弄丢或弄大）
    if not out or len(out) == 0:
        return text
    bytes_out = len(out.encode("utf-8"))
    if bytes_out >= bytes_in:
        return text

    stats["bytesBefore"] += bytes_in
    stats["bytesAfter"] += bytes_out
    stats["hits"].append({
        "shape": shape,
        "filter": FILTER_NAME,
        "saved": bytes_in - bytes_out,
    })
    return out


def compress_tool_results(messages: Any) -> Dict[str, Any]:
    """就地压缩 Anthropic messages 中的 tool_result 块。

    只处理两种形态：content 为 str（claude-string）、content 为 block 数组中的
    text part（claude-array）。is_error=True 整块跳过。其余块（text/thinking/
    tool_use/image…）绝不触碰。

    整体包 try/except：宁可一点都不省，也不能因为省钱把请求搞挂——
    异常时返回空 stats + compressed=False，此时 body 可能已被部分压缩，
    但每次改写本身都是「原文 → 更短的合法文本」，不会产生非法结构。
    """
    stats: Dict[str, Any] = {"bytesBefore": 0, "bytesAfter": 0, "hits": []}
    try:
        for msg in (messages or []):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                if block.get("is_error") is True:
                    continue  # 错误栈的中段常是根因，绝不砍
                inner = block.get("content")
                if isinstance(inner, str):
                    block["content"] = compress_text(inner, stats, "claude-string")
                elif isinstance(inner, list):
                    for part in inner:
                        if not isinstance(part, dict) or part.get("type") != "text":
                            continue
                        ptext = part.get("text")
                        if isinstance(ptext, str):
                            part["text"] = compress_text(ptext, stats, "claude-array")
    except Exception as e:
        _log().warning(f"[RTK] compress_tool_results failed, passthrough: {e}")
        return {"stats": {"bytesBefore": 0, "bytesAfter": 0, "hits": []},
                "compressed": False}
    return {"stats": stats, "compressed": bool(stats["hits"])}


def format_rtk_log(stats: Dict[str, Any]) -> Optional[str]:
    """把 stats 渲染成单行日志；无命中返回 None（调用方据此决定是否打日志）。"""
    try:
        hits: List[Dict[str, Any]] = stats.get("hits") or []
        if not hits:
            return None
        before = stats.get("bytesBefore", 0) or 0
        after = stats.get("bytesAfter", 0) or 0
        saved = before - after
        pct = round(saved / before * 100, 1) if before else "0"
        filters = ",".join(sorted({str(h.get("filter")) for h in hits}))
        return f"[RTK] saved {saved}B / {before}B ({pct}%) via [{filters}] hits={len(hits)}"
    except Exception as e:
        _log().warning(f"[RTK] format_rtk_log failed: {e}")
        return None
