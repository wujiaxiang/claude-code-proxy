"""HTTP 转发引擎核心工具（从 server.py 下沉，行为零变化）。

包含：统一 HTTP 请求解析、错误响应回写、SSE 行缓冲重组、流式/非流式响应写出、
状态码覆盖写出、HTTP 状态码原因短语映射。

跨模块约定：
- 对 server 的共享依赖（logger / codebuddy_logger / traework_logger /
  _PROXY_STRIP_RESP_HEADERS / _normalize_codebuddy_sse_line）一律**函数内延迟导入**
  `from server import X`，避免循环依赖（server.py 顶部有 __main__ 别名保护）。
- 本模块被 server.py 顶层 re-export（`from server_http import ...`），保证
  server.py 内部及 gateways/* 的 `from server import _write_response` 继续有效。
"""

import asyncio
import json
from urllib.parse import urlparse


async def _parse_http_request(reader):
    """统一 HTTP 请求解析。
    返回 (method, path, raw_path, headers, body)，请求无效时全返回 None。
    """
    from server import logger  # 延迟导入，避免循环依赖
    try:
        req_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not req_line:
            return None, None, None, None, None
        parts = req_line.decode("utf-8", errors="replace").strip().split(" ", 2)
        method = parts[0] if len(parts) > 0 else "GET"
        raw_path = parts[1] if len(parts) > 1 else "/"

        headers = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                break
            if ":" in line_str:
                k, v = line_str.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        content_len = int(headers.get("content-length", 0))
        body = b""
        if content_len > 0:
            # reader.read(n) 可能返回少于 n 字节，必须循环读满
            while len(body) < content_len:
                remaining = content_len - len(body)
                chunk = await asyncio.wait_for(reader.read(remaining), timeout=30)
                if not chunk:
                    break
                body += chunk
        elif headers.get("transfer-encoding", "").lower() == "chunked":
            # 处理分块编码（OpenCode 等客户端可能使用）
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=30)
                chunk_size_str = line.decode("utf-8", errors="replace").strip()
                if not chunk_size_str:
                    continue
                try:
                    chunk_size = int(chunk_size_str, 16)
                except ValueError:
                    break
                if chunk_size == 0:
                    break
                # reader.read(n) 同上的问题，必须循环读满
                chunk_data = b""
                while len(chunk_data) < chunk_size:
                    remaining = chunk_size - len(chunk_data)
                    part = await asyncio.wait_for(reader.read(remaining), timeout=30)
                    if not part:
                        break
                    chunk_data += part
                body += chunk_data
                await asyncio.wait_for(reader.readline(), timeout=10)  # 吃掉 \r\n

        parsed = urlparse(raw_path)
        return method, parsed.path, raw_path, headers, body
    except asyncio.TimeoutError:
        logger.warning("_parse_http_request timeout reading request")
        return None, None, None, None, None


async def _write_error_response(writer, status, message, *, content_type="application/json", retry_after=None):
    """统一错误响应回写，带日志。"""
    from server import logger  # 延迟导入，避免循环依赖
    body = json.dumps({"error": {"type": "proxy_error", "message": message}}, ensure_ascii=False)
    status_text = {429: "Too Many Requests", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}.get(status, "Error")
    header_lines = f"HTTP/1.1 {status} {status_text}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body.encode())}\r\n"
    if retry_after is not None:
        header_lines += f"Retry-After: {retry_after}\r\n"
    header_lines += "\r\n"
    logger.warning(f"_write_error_response: {status} — {message}")
    try:
        writer.write(header_lines.encode() + body.encode())
        await writer.drain()
    except Exception:
        pass
    try:
        writer.close()
    except Exception:
        pass


class _SseLineBuffer:
    """SSE 行缓冲：按 \\n 切完整行，处理跨 TCP chunk 粘包。

    背景：SSE 帧可能被 TCP 任意切断（一个 data: {...} JSON 跨两个 chunk）。
    纯字节透传时无所谓，但一旦要逐帧改写就必须先重组成完整行，否则会切坏 JSON。
    """
    __slots__ = ("_buf",)

    def __init__(self):
        self._buf = b""

    def feed(self, chunk: bytes) -> list:
        """喂入原始字节，返回本次能切出的完整行（每行含末尾 \\n）。不完整的尾部留在缓冲区。"""
        self._buf += chunk
        lines = []
        while True:
            idx = self._buf.find(b"\n")
            if idx == -1:
                break
            lines.append(self._buf[:idx + 1])
            self._buf = self._buf[idx + 1:]
        return lines

    def flush(self) -> bytes:
        """流结束时吐出残留（无末尾 \\n 的最后一行）。正常 SSE 不应有残留，防御性处理。"""
        rest, self._buf = self._buf, b""
        return rest


async def _write_response(writer, resp, *, stats=None, write_state=None, log_sse=False, _label="", normalize_sse=False, normalize_finish_reason=True):
    """统一从 httpx 响应回写到 writer。
    自动区分流式/非流式，非 200 自动记录日志。
    返回 (status_code, body_bytes) — body_bytes=None 表示流式已写完。
    write_state: 可选的可变字典，用于跟踪 headers_sent 状态（流式场景下避免二次写状态行）
    log_sse: 可选，流式透传时解析 SSE 记录 finish_reason 诊断日志（用于排查上游
      content_filter 拦截等"200 但内容异常"场景）。开启后走行缓冲逐帧处理。
    normalize_sse: 可选，规范化上游不合规 SSE 帧（需 log_sse=True 才生效）。
      由 targets.json 的 normalizeSse 驱动，当前用于 codebuddy——修复上游思考帧
      夹带空 content 导致客户端思考链逐 token 换行的问题。
    normalize_finish_reason: normalize_sse 的子选项，把 finish_reason:"" 归一成 null。
    """
    from server import (
        logger,
        codebuddy_logger,
        traework_logger,
        _PROXY_STRIP_RESP_HEADERS,
        _normalize_codebuddy_sse_line,
    )
    status, body_bytes, is_stream = None, None, False
    try:
        status = resp.status_code
        reason = resp.reason_phrase or "OK"
        content_type = resp.headers.get("content-type", "")
        is_stream = "text/event-stream" in content_type

        # ── 日志：非 200 记录响应前 300 字符 ──
        if status >= 400:
            logger.warning(f"[{resp.url.host if hasattr(resp, 'url') else 'upstream'}] "
                           f"HTTP {status} {reason} | content-type: {content_type}")

        if is_stream:
            writer.write(f"HTTP/1.1 {status} {reason}\r\n".encode())
            for k, v in resp.headers.items():
                if k.lower() not in _PROXY_STRIP_RESP_HEADERS:
                    writer.write(f"{k}: {v}\r\n".encode())
            writer.write(b"\r\n")
            # 标记 headers 已写入（流式场景：状态行+headers 已发送到 writer 缓冲区）
            if write_state is not None:
                write_state["headers_sent"] = True
            if log_sse:
                # codebuddy SSE 诊断日志（2026-08-05）：定位上游 content_filter 拦截
                # （透传下客户端收到 200 空 SSE 无法感知原因）。
                # normalize_sse=True 时额外做帧规范化（修上游夹带空 content 导致的
                # 思考链逐 token 换行，见 _normalize_codebuddy_sse_line）。
                # 用行缓冲重组跨 chunk 的半截帧——改写模式下必须，否则会切坏 JSON。
                saw_filter = False
                saw_finish = set()
                data_lines = 0
                normalized_lines = 0
                line_buf = _SseLineBuffer()

                def _diagnose(text_line: str):
                    """诊断统计——必须基于改写【前】的原始行，否则规范化自身的 bug
                    会掩盖上游真实异常。返回是否为有效 data 行。"""
                    nonlocal saw_filter, data_lines
                    if not text_line.startswith("data:"):
                        return
                    data_str = text_line[5:].strip()
                    if not data_str or data_str == "[DONE]":
                        return
                    data_lines += 1
                    try:
                        obj = json.loads(data_str)
                        for choice in obj.get("choices", []) or []:
                            fr = choice.get("finish_reason")
                            if fr:
                                saw_finish.add(fr)
                                if fr == "content_filter":
                                    saw_filter = True
                    except (json.JSONDecodeError, AttributeError):
                        pass

                def _process(raw_line: bytes) -> bytes:
                    """先诊断原始行，再按需规范化。任何异常都退回原样透传，绝不吞帧。"""
                    nonlocal normalized_lines
                    try:
                        _diagnose(raw_line.decode("utf-8", errors="replace"))
                    except Exception:
                        pass
                    if not normalize_sse:
                        return raw_line
                    try:
                        out_line = _normalize_codebuddy_sse_line(
                            raw_line, finish_reason_to_null=normalize_finish_reason
                        )
                        if out_line is not raw_line:
                            normalized_lines += 1
                        return out_line
                    except Exception:
                        return raw_line  # 双保险：规范化不应抛，再兜一层

                async for chunk in resp.aiter_bytes():
                    out = bytearray()
                    for raw_line in line_buf.feed(chunk):
                        out += _process(raw_line)
                    if out:
                        writer.write(bytes(out))
                        await writer.drain()
                # 流结束：吐残留（无末尾 \n 的最后一行，正常 SSE 不应出现）
                tail = line_buf.flush()
                if tail:
                    writer.write(_process(tail))
                    await writer.drain()

                _gw_logger = codebuddy_logger if _label == "codebuddy" else (traework_logger if _label == "trae-work" else logger)
                _norm_note = f" normalized={normalized_lines}" if normalize_sse else ""
                if saw_filter:
                    _gw_logger.warning(f"[{_label}] SSE content_filter 透传: "
                                       f"data_lines={data_lines} finish_reasons={sorted(saw_finish)}{_norm_note}")
                else:
                    _gw_logger.debug(f"[{_label}] SSE 透传完成: data_lines={data_lines} "
                                     f"finish_reasons={sorted(saw_finish) or '无'}{_norm_note}")
            else:
                async for chunk in resp.aiter_bytes():
                    writer.write(chunk)
                    await writer.drain()
            if stats:
                stats["passthroughOk"] += 1
            return status, None

        body_bytes = await resp.aread()
        body_text = body_bytes.decode("utf-8", errors="replace")
        if status >= 400:
            logger.warning(f"[{resp.url.host if hasattr(resp, 'url') else 'upstream'}] "
                           f"HTTP {status} body: {body_text[:300]}")

        resp_headers = "".join(
            f"{k}: {v}\r\n" for k, v in resp.headers.items()
            if k.lower() not in _PROXY_STRIP_RESP_HEADERS
        )
        writer.write(f"HTTP/1.1 {status} {reason}\r\n{resp_headers}Content-Length: {len(body_bytes)}\r\n\r\n".encode())
        writer.write(body_bytes)
        await writer.drain()
        if stats:
            stats["passthroughOk"] += 1
        return status, body_bytes
    except Exception:
        if status is not None and status >= 400 and body_bytes:
            logger.exception(f"Error writing {status} response to client")
        raise
    finally:
        try:
            writer.close()
        except Exception:
            pass


# HTTP 标准状态码 → 原因短语映射（用于状态行改写，覆盖 400-599 常见码）
_HTTP_STATUS_REASON = {
    400: "Bad Request",
    401: "Unauthorized",
    402: "Payment Required",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    407: "Proxy Authentication Required",
    408: "Request Timeout",
    409: "Conflict",
    410: "Gone",
    411: "Length Required",
    412: "Precondition Failed",
    413: "Payload Too Large",
    414: "URI Too Long",
    415: "Unsupported Media Type",
    416: "Range Not Satisfiable",
    417: "Expectation Failed",
    418: "I'm a teapot",
    421: "Misdirected Request",
    422: "Unprocessable Entity",
    423: "Locked",
    424: "Failed Dependency",
    425: "Too Early",
    426: "Upgrade Required",
    428: "Precondition Required",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    451: "Unavailable For Legal Reasons",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
    505: "HTTP Version Not Supported",
    506: "Variant Also Negotiates",
    507: "Insufficient Storage",
    508: "Loop Detected",
    510: "Not Extended",
    511: "Network Authentication Required",
}


def _get_status_reason(status: int) -> str:
    """获取 HTTP 状态码对应的标准原因短语，未知码返回 'Unknown Status'。"""
    return _HTTP_STATUS_REASON.get(status, "Unknown Status")


async def _write_response_with_status_override(writer, resp, effective_status: int, *, stats=None):
    """
    非流式响应状态码改写：保持上游原始 body 字节完全一致，仅改写状态行。
    用于检测到"上游 200 但 body 嵌错误码"的场景。
    """
    from server import (
        logger,
        _PROXY_STRIP_RESP_HEADERS,
    )
    try:
        # 复用 _write_response 的头部剥离逻辑
        resp_headers = "".join(
            f"{k}: {v}\r\n" for k, v in resp.headers.items()
            if k.lower() not in _PROXY_STRIP_RESP_HEADERS
        )
        # 读取原始 body 字节（resp 已在调用方 aread() 过，这里直接用 resp.content 或重新 aread）
        # 注意：调用方已执行 await resp.aread()，所以 resp.content 可用
        body_bytes = resp.content if hasattr(resp, "content") and resp.content is not None else await resp.aread()

        reason = _get_status_reason(effective_status)
        # 写状态行 + 头部 + Content-Length + body（body 字节级保持原样）
        writer.write(f"HTTP/1.1 {effective_status} {reason}\r\n{resp_headers}Content-Length: {len(body_bytes)}\r\n\r\n".encode())
        writer.write(body_bytes)
        await writer.drain()
        if stats:
            stats["passthroughError"] += 1
        return effective_status, body_bytes
    except Exception:
        logger.exception(f"Error writing status-overridden response ({effective_status}) to client")
        raise
    finally:
        try:
            writer.close()
        except Exception:
            pass


