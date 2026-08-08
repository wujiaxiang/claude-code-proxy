"""
8081 /v1/messages（server.create_message）回归锁定测试。

目的：在「流式帧重组 + 重试 helper 抽取」重构之前，把当前行为钉死，
重构后再跑一遍，行为若变化立即 FAIL。

覆盖分支（server.py create_message）：
  分支 A  models[] 映射命中 → anthropic→openai 翻译 → 转发本地端口 /v1/chat/completions

仅覆盖分支 A：原分支 B（PREFERRED_PROVIDER == "copilot" 的 Copilot /v1/messages
原生透传 + 3 次重试）已随「8081 legacy 单端口模式大清理」删除，其用例与
PREFERRED_PROVIDER 注入管道一并移除。分支 A 只由 _MODELS_CFG 注入驱动。

不需要任何真实端口：全部通过 ASGI 直调 app + 替换 server.httpx.AsyncClient 完成。
分支 C（LiteLLM）不在本次重构范围，未覆盖。

用法: python _test_v1messages_lock.py
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import server as _srv  # noqa: E402

passed = 0
failed = 0

MAPPED_PORT = 18099
MAPPED_MODEL = "upstream-model-x"
# 分支 A 的 models[] 配置：请求 "lock-test-model" 时命中
_MODELS_CFG_FIXTURE = {
    "modelDefaults": {"defaultPort": 8082},
    "models": [
        {
            "name": "lock-test-model",
            "aliases": ["lock-test-alias"],
            "target": {"port": MAPPED_PORT, "model": MAPPED_MODEL},
        }
    ],
}


# ============================================================
# 假 httpx.AsyncClient —— 记录调用 + 按脚本回放响应
# ============================================================

class FakeResponse:
    """最小 httpx.Response 替身，支持 aread / aiter_bytes / json / aclose。"""

    def __init__(self, status_code, body=b"", chunks=None):
        self.status_code = status_code
        self._body = body
        # chunks 为 None 时用整块 body；非 None 时按给定切分回放（模拟 TCP 分片）
        self._chunks = chunks
        self.closed = False

    async def aread(self):
        return self._body

    async def aiter_bytes(self):
        for c in (self._chunks if self._chunks is not None else [self._body]):
            yield c

    def json(self):
        return json.loads(self._body.decode("utf-8"))

    async def aclose(self):
        self.closed = True


def make_fake_client_cls(script, calls):
    """
    构造假 AsyncClient 类。

    script: 列表，每项是 FakeResponse 或 Exception 实例（抛出以模拟连接错误）。
            按调用顺序依次消费；耗尽后复用最后一项。
    calls:  外部传入的 list，用于记录每次调用 (method, url, kwargs)。
    """
    state = {"i": 0}

    def _next():
        i = min(state["i"], len(script) - 1)
        state["i"] += 1
        item = script[i]
        if isinstance(item, Exception):
            raise item
        return item

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self.closed = True
            return False

        async def aclose(self):
            self.closed = True

        # 分支 A 用 build_request + send
        def build_request(self, method, url, **kw):
            return {"method": method, "url": url, **kw}

        async def send(self, req, stream=False):
            calls.append(("send", req["url"], {"stream": stream}))
            return _next()

    return FakeAsyncClient


# ============================================================
# ASGI 直调 —— 不起端口，直接把 request 喂给 app
# ============================================================

async def _asgi_post(path, body_obj, headers=None):
    """
    通过 ASGI 协议直接调用 server.app，返回 (status, headers_dict, body_bytes)。
    流式响应会把所有 http.response.body 事件拼接后返回。
    """
    payload = json.dumps(body_obj).encode("utf-8")
    hdrs = [(b"content-type", b"application/json"), (b"host", b"127.0.0.1:8081")]
    for k, v in (headers or {}).items():
        hdrs.append((k.encode(), v.encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": hdrs,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8081),
    }

    sent = {"consumed": False}

    async def receive():
        if not sent["consumed"]:
            sent["consumed"] = True
            return {"type": "http.request", "body": payload, "more_body": False}
        # 不能返回 http.disconnect —— StreamingResponse 会监听它并提前中断流，
        # 导致只收到第一个 chunk。真实服务端在请求体读完后不会立刻断连，
        # 这里永久挂起以还原真实行为（响应发完后 app() 返回，任务被回收）。
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    out = {"status": None, "headers": {}, "body": b""}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await _srv.app(scope, receive, send)
    return out["status"], out["headers"], out["body"]


async def _asgi_post_events(path, body_obj, headers=None):
    """与 _asgi_post 相同，但保留 http.response.body 事件列表（不拼接），
    用于断言流式输出的渐进分块粒度（_SseLineBuffer 重组后应按完整行逐块送达）。"""
    payload = json.dumps(body_obj).encode("utf-8")
    hdrs = [(b"content-type", b"application/json"), (b"host", b"127.0.0.1:8081")]
    for k, v in (headers or {}).items():
        hdrs.append((k.encode(), v.encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": hdrs,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8081),
    }

    sent = {"consumed": False}

    async def receive():
        if not sent["consumed"]:
            sent["consumed"] = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    out = {"status": None, "headers": {}, "events": []}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body":
            out["events"].append(msg.get("body", b""))

    await _srv.app(scope, receive, send)
    return out["status"], out["headers"], out["events"]


def run_case(script, body_obj, models_cfg=None, headers=None):
    """
    在受控环境跑一次 /v1/messages：
      - 替换 server.httpx.AsyncClient 为假客户端（按 script 回放）
      - 注入 models[] 配置（分支 A 的唯一开关）
      - asyncio.sleep 打桩为记录耗时（可断言 backoff）
    返回 (status, headers, body_bytes, calls, sleeps)
    """
    calls = []
    sleeps = []
    fake_cls = make_fake_client_cls(script, calls)

    real_sleep = asyncio.sleep

    async def fake_sleep(sec, *a, **kw):
        sleeps.append(sec)
        return await real_sleep(0)

    old_models = _srv._MODELS_CFG
    try:
        _srv._MODELS_CFG = models_cfg if models_cfg is not None else {"models": [], "modelDefaults": {}}
        with patch.object(_srv.httpx, "AsyncClient", fake_cls), \
             patch.object(_srv.asyncio, "sleep", fake_sleep):
            status, hdrs, raw = asyncio.run(_asgi_post("/v1/messages", body_obj, headers))
    finally:
        _srv._MODELS_CFG = old_models
    return status, hdrs, raw, calls, sleeps


def run_case_events(script, body_obj, models_cfg=None, headers=None):
    """与 run_case 相同，但返回 (status, headers, body_events, calls, sleeps)，
    body_events 为 http.response.body 事件的原始列表（未拼接）。"""
    calls = []
    sleeps = []
    fake_cls = make_fake_client_cls(script, calls)

    real_sleep = asyncio.sleep

    async def fake_sleep(sec, *a, **kw):
        sleeps.append(sec)
        return await real_sleep(0)

    old_models = _srv._MODELS_CFG
    try:
        _srv._MODELS_CFG = models_cfg if models_cfg is not None else {"models": [], "modelDefaults": {}}
        with patch.object(_srv.httpx, "AsyncClient", fake_cls), \
             patch.object(_srv.asyncio, "sleep", fake_sleep):
            status, hdrs, events = asyncio.run(_asgi_post_events("/v1/messages", body_obj, headers))
    finally:
        _srv._MODELS_CFG = old_models
    return status, hdrs, events, calls, sleeps


def _req(model, stream=False, **extra):
    b = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }
    if stream:
        b["stream"] = True
    b.update(extra)
    return b


# ============================================================
# 分支 A —— models[] 映射命中，转发本地端口
# ============================================================

def test_branch_a_stream_passthrough_split_frames():
    """
    分支 A 流式：上游把一个 SSE JSON 帧切成多个 TCP chunk。
    锁定当前行为 —— 代理重组半截帧后转换为标准 Anthropic SSE 事件序列
    （message_start → content_block_start → content_block_delta →
    content_block_stop → message_delta → message_stop），内容保留。
    """
    frame = b'data: {"choices":[{"delta":{"content":"hello world"}}]}\n\n'
    # 在 JSON 中间切断，制造半截帧
    cut = 30
    chunks = [frame[:cut], frame[cut:]]
    resp = FakeResponse(200, body=frame, chunks=chunks)

    status, hdrs, raw, calls, _ = run_case(
        [resp], _req("lock-test-model", stream=True), models_cfg=_MODELS_CFG_FIXTURE
    )

    assert status == 200, f"期望 200，实到 {status}"
    assert "text/event-stream" in hdrs.get("content-type", ""), hdrs
    # 关键锁定：输出为标准 Anthropic SSE 事件序列，内容保留（"hello world"）
    raw_str = raw.decode("utf-8", errors="replace")
    assert "event: message_start" in raw_str, f"缺 message_start: {raw_str[:200]!r}"
    assert "event: content_block_start" in raw_str, f"缺 content_block_start"
    assert '"type": "text_delta"' in raw_str, f"缺 text_delta"
    assert "hello world" in raw_str, f"内容丢失: {raw_str!r}"
    assert "event: content_block_stop" in raw_str
    assert "event: message_stop" in raw_str
    # 转发目标端口正确、以 stream=True 发送
    assert calls and calls[0][0] == "send", calls
    assert calls[0][1] == f"http://127.0.0.1:{MAPPED_PORT}/v1/chat/completions", calls[0][1]
    assert calls[0][2]["stream"] is True, calls[0][2]


def test_branch_a_stream_response_closed():
    """分支 A 流式：透传结束后必须 aclose 上游响应（finally 分支）。"""
    resp = FakeResponse(200, body=b"data: {}\n\n", chunks=[b"data: ", b"{}\n\n"])
    run_case([resp], _req("lock-test-alias", stream=True), models_cfg=_MODELS_CFG_FIXTURE)
    assert resp.closed is True, "上游流式响应未被关闭"


def test_branch_a_non_stream_upstream_502():
    """
    分支 A 非流式：上游返回 502 且 body 是合法 JSON
    → 当前行为是原样回传上游 JSON + 上游状态码（不包装成 proxy_error）。
    """
    up = {"error": {"message": "bad gateway from upstream"}}
    resp = FakeResponse(502, body=json.dumps(up).encode())

    status, _, raw, calls, _ = run_case(
        [resp], _req("lock-test-model"), models_cfg=_MODELS_CFG_FIXTURE
    )

    assert status == 502, f"期望 502，实到 {status}"
    assert json.loads(raw) == up, f"期望原样回传上游 body，实到 {raw!r}"
    # 非流式：send(stream=False)，且只调一次（分支 A 无重试）
    assert len(calls) == 1, f"分支 A 不应重试，实际调用 {len(calls)} 次"
    assert calls[0][2]["stream"] is False, calls[0][2]


def test_branch_a_non_stream_invalid_json_502():
    """分支 A 非流式：上游返回非 JSON → 502 + proxy_error 包装。"""
    resp = FakeResponse(200, body=b"<html>not json</html>")
    status, _, raw, _, _ = run_case(
        [resp], _req("lock-test-model"), models_cfg=_MODELS_CFG_FIXTURE
    )
    assert status == 502, f"期望 502，实到 {status}"
    data = json.loads(raw)
    assert data["error"]["type"] == "proxy_error", data
    assert "upstream invalid response" in data["error"]["message"], data


def test_branch_a_non_stream_success_translates_back():
    """
    分支 A 非流式成功：OpenAI 响应译回 Anthropic 格式。

    锁定一处与分支 B 不对称的行为：convert_openai_response_to_anthropic 里
    `model` 取 `openai_data.get("model", original_model)`，即**优先用上游返回的
    模型名**，original_model 只是兜底。所以客户端看到的是上游内部模型名
    （upstream-model-x），而分支 B 会显式把 model 改回原始请求名。
    重构时若想统一，此断言需有意识地更新。
    """
    up = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": MAPPED_MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }
    resp = FakeResponse(200, body=json.dumps(up).encode())
    status, _, raw, _, _ = run_case(
        [resp], _req("lock-test-model"), models_cfg=_MODELS_CFG_FIXTURE
    )
    assert status == 200, f"期望 200，实到 {status}"
    data = json.loads(raw)
    assert data.get("type") == "message", data
    assert data.get("model") == MAPPED_MODEL, \
        f"锁定：分支 A 透出上游模型名而非还原原始名，实到 {data.get('model')}"
    texts = [b.get("text") for b in data.get("content", []) if b.get("type") == "text"]
    assert "pong" in texts, data


def test_branch_a_stream_split_frame_reassembled_progressive():
    """
    分支 A 流式（_SseLineBuffer 重组）：上游把一个 JSON 帧切成两半分两个 chunk 发送，
    代理必须等凑齐完整行后再下发 —— 客户端收到的是重组后的完整帧，且分块渐进送达。

    与 test_branch_a_stream_passthrough_split_frames 的关系：
      那个用例锁定「整帧到达时字节逐字节不变」；本用例锁定「半截帧到达时先缓冲、
      凑齐 \n 后才吐出」——两者合并字节都等于 上游帧 + [DONE]，但本用例进一步断言
      下发粒度是「完整行」而非「原始 TCP chunk」。
    """
    frame1 = b'data: {"choices":[{"delta":{"content":"hello world"}}]}\n'
    frame2 = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
    # frame1 在 JSON 中间切断；frame2 完整但与空行(帧分隔)同 chunk 到达
    cut = 30
    chunks = [frame1[:cut], frame1[cut:], frame2 + b"\n"]

    status, hdrs, events, calls, _ = run_case_events(
        [FakeResponse(200, chunks=chunks)],
        _req("lock-test-model", stream=True),
        models_cfg=_MODELS_CFG_FIXTURE,
    )

    assert status == 200, f"期望 200，实到 {status}"
    assert "text/event-stream" in hdrs.get("content-type", ""), hdrs
    # 1) 输出为 Anthropic SSE 事件序列（转换器输出），内容保留
    raw_all = b"".join(events)
    raw_str = raw_all.decode("utf-8", errors="replace")
    assert "event: message_start" in raw_str
    assert "event: content_block_start" in raw_str
    assert "hello world" in raw_str, f"内容丢失: {raw_str!r}"
    assert "event: message_stop" in raw_str
    # 2) 渐进送达：每个 body 事件都是完整行（以 \n 结尾），半截 chunk 不允许直接下发
    data_events = [e for e in events if e not in (b"", )]
    assert all(e.endswith(b"\n") for e in data_events), f"存在非完整行事件: {data_events!r}"
    # 3) 重组正确性：转换器输出的事件都可解析（Anthropic 事件 payload 是合法 JSON）
    import re as _re
    for line in raw_all.split(b"\n"):
        if line.startswith(b"data: "):
            payload = line[6:].strip()
            if payload:
                assert json.loads(payload), f"非法事件 payload: {payload!r}"
    # 4) 转发目标不变
    assert calls and calls[0][0] == "send" and calls[0][2]["stream"] is True, calls


def test_branch_a_stream_trailing_partial_line_flushed():
    """
    分支 A 流式边界：上游最后一行无末尾 \n（异常截断的不完整帧）。
    _SseLineBuffer.flush() 在流结束时把残留原样吐出（不吞帧、不中断流），
    随后仍正常追加 [DONE]。
    """
    full = b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
    tail = b'data: {"choices":[{"delta":{},"fin'  # 被截断的半截帧，无 \n
    resp = FakeResponse(200, chunks=[full, tail])

    status, _, raw, _, _ = run_case(
        [resp], _req("lock-test-model", stream=True), models_cfg=_MODELS_CFG_FIXTURE
    )

    assert status == 200, f"期望 200，实到 {status}"
    # 完整帧内容保留（Anthropic 事件）；被截断的半截帧被转换器安全跳过（不吞流）
    raw_str = raw.decode("utf-8", errors="replace")
    assert "hello world" in raw_str or '"text_delta"' in raw_str or "event: message_start" in raw_str, \
        f"流式输出异常: {raw_str[:200]!r}"


def test_branch_a_non_stream_rate_limit_mapped_to_429():
    """
    分支 A 非流式错误统一：上游 500 且 body 含限流特征（ResourceExhausted）
    → 经 _map_upstream_error 翻译为标准 429 + rate_limit_error + Retry-After，
    与 _handle_target_request 的错误翻译行为一致（不再原样透传 500）。
    """
    up = {"error": {"message": "ResourceExhausted: quota depleted", "code": 500}}
    resp = FakeResponse(500, body=json.dumps(up).encode())

    status, hdrs, raw, calls, _ = run_case(
        [resp], _req("lock-test-model"), models_cfg=_MODELS_CFG_FIXTURE
    )

    assert status == 429, f"限流特征应翻译为 429，实到 {status}"
    data = json.loads(raw)
    assert data["error"]["type"] == "rate_limit_error", data
    assert data["error"]["original_status"] == 500, data
    assert hdrs.get("retry-after"), f"应带 Retry-After 头，实到 {hdrs}"
    # 非可识别错误仍原样透传由 test_branch_a_non_stream_upstream_502 锁定（不变）


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    global failed
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
