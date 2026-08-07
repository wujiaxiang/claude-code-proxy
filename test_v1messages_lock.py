"""
8081 /v1/messages（server.create_message）回归锁定测试。

目的：在「流式帧重组 + 重试 helper 抽取」重构之前，把当前行为钉死，
重构后再跑一遍，行为若变化立即 FAIL。

覆盖分支（server.py create_message）：
  分支 A  models[] 映射命中 → anthropic→openai 翻译 → 转发本地端口 /v1/chat/completions
  分支 B  PREFERRED_PROVIDER == "copilot" → Copilot /v1/messages 原生透传 + 3 次重试

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


class _StreamCtx:
    """client.stream(...) 返回的 async context manager。"""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        await self._resp.aclose()
        return False


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

        # 分支 B 非流式用 post
        async def post(self, url, **kw):
            calls.append(("post", url, kw))
            return _next()

        # 分支 B 流式用 stream
        def stream(self, method, url, **kw):
            calls.append(("stream", url, kw))
            return _StreamCtx(_next())

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


def run_case(script, body_obj, models_cfg=None, provider="copilot", headers=None):
    """
    在受控环境跑一次 /v1/messages：
      - 替换 server.httpx.AsyncClient 为假客户端（按 script 回放）
      - 注入 models[] 配置（None 表示空，用于强制走分支 B）
      - 注入 PREFERRED_PROVIDER
      - asyncio.sleep 打桩为记录耗时（避免真等 0.5s 且可断言 backoff）
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
    old_provider = _srv.PREFERRED_PROVIDER
    try:
        _srv._MODELS_CFG = models_cfg if models_cfg is not None else {"models": [], "modelDefaults": {}}
        _srv.PREFERRED_PROVIDER = provider
        with patch.object(_srv.httpx, "AsyncClient", fake_cls), \
             patch.object(_srv.asyncio, "sleep", fake_sleep):
            status, hdrs, raw = asyncio.run(_asgi_post("/v1/messages", body_obj, headers))
    finally:
        _srv._MODELS_CFG = old_models
        _srv.PREFERRED_PROVIDER = old_provider
    return status, hdrs, raw, calls, sleeps


def run_case_events(script, body_obj, models_cfg=None, provider="copilot", headers=None):
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
    old_provider = _srv.PREFERRED_PROVIDER
    try:
        _srv._MODELS_CFG = models_cfg if models_cfg is not None else {"models": [], "modelDefaults": {}}
        _srv.PREFERRED_PROVIDER = provider
        with patch.object(_srv.httpx, "AsyncClient", fake_cls), \
             patch.object(_srv.asyncio, "sleep", fake_sleep):
            status, hdrs, events = asyncio.run(_asgi_post_events("/v1/messages", body_obj, headers))
    finally:
        _srv._MODELS_CFG = old_models
        _srv.PREFERRED_PROVIDER = old_provider
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
    锁定当前行为 —— 代理原样透传每个 chunk（不做跨 chunk 帧重组），
    末尾追加 data: [DONE]。重构引入 _SseLineBuffer 后此断言应被有意识地更新。
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
    # 关键锁定：chunk 原样拼接 + [DONE]，中间没有被重组/改写
    assert raw == frame + b"data: [DONE]\n\n", f"透传字节不符: {raw!r}"
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
    # 1) 合并字节与改造前完全一致（原样透传 + [DONE]）
    assert b"".join(events) == frame1 + frame2 + b"\n" + b"data: [DONE]\n\n", events
    # 2) 渐进送达：每个 body 事件都是完整行（以 \n 结尾），半截 chunk 不允许直接下发；
    #    同一 chunk 内的多行可能分属相邻事件（逐行 yield），但绝不出现半行事件
    data_events = [e for e in events if e not in (b"data: [DONE]\n\n", b"")]
    assert all(e.endswith(b"\n") for e in data_events), f"存在非完整行事件: {data_events!r}"
    assert data_events[0] == frame1, f"首个下发应为重组后的完整帧，实到 {data_events[0]!r}"
    assert b"".join(data_events[1:]) == frame2 + b"\n", f"后续下发拼接应为 frame2+空行，实到 {data_events[1:]!r}"
    assert [e for e in events if e][-1] == b"data: [DONE]\n\n", f"末尾应追加 [DONE]，实到 {events!r}"
    # 3) 重组正确性：拼接后可逐帧解析为合法 JSON（半截直发会切坏 JSON）
    data_frames = [l[5:].strip() for l in b"".join(events).split(b"\n")
                   if l.startswith(b"data: ") and l[5:].strip() != b"[DONE]"]
    assert len(data_frames) == 2, data_frames
    assert json.loads(data_frames[0])["choices"][0]["delta"]["content"] == "hello world"
    assert json.loads(data_frames[1])["choices"][0]["finish_reason"] == "stop"
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
    assert raw == full + tail + b"data: [DONE]\n\n", f"残留行应原样吐出: {raw!r}"


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


# ============================================================
# 分支 B —— Copilot /v1/messages 原生透传 + 3 次重试
# ============================================================

def test_branch_b_non_stream_retry_then_success():
    """
    分支 B 非流式：第 1 次 500 → 第 2 次 200。
    锁定：共 2 次请求、第 2 次前 sleep 0.5s、响应 model 还原为原始请求名。
    """
    ok = {"id": "msg_1", "type": "message", "model": "copilot-internal",
          "content": [{"type": "text", "text": "ok"}]}
    script = [FakeResponse(500, body=b'{"error":"boom"}'),
              FakeResponse(200, body=json.dumps(ok).encode())]

    status, _, raw, calls, sleeps = run_case(script, _req("claude-3-5-sonnet"), provider="copilot")

    assert status == 200, f"期望 200，实到 {status}"
    assert len(calls) == 2, f"期望重试 1 次共 2 请求，实到 {len(calls)}"
    assert sleeps == [0.5], f"期望重试前 backoff 0.5s，实到 {sleeps}"
    data = json.loads(raw)
    assert data["model"] == "claude-3-5-sonnet", f"model 未还原: {data['model']}"


def test_branch_b_non_stream_retry_exhausted_502():
    """
    分支 B 非流式：连续 500。
    锁定当前行为 —— attempt 0/1 命中 `status>=500 and attempt<2` 重试；
    attempt 2（第 3 次）**不再重试而是直接回传上游 500**，
    因此最终返回 500 而非 502。502 只在连接异常耗尽时出现。
    """
    script = [FakeResponse(500, body=b'{"error":"e1"}'),
              FakeResponse(500, body=b'{"error":"e2"}'),
              FakeResponse(500, body=b'{"error":"e3"}')]

    status, _, raw, calls, sleeps = run_case(script, _req("claude-3-5-sonnet"), provider="copilot")

    assert len(calls) == 3, f"期望最多 3 次请求，实到 {len(calls)}"
    assert sleeps == [0.5, 0.5], f"期望两次 backoff，实到 {sleeps}"
    assert status == 500, f"锁定：第 3 次 500 直接回传上游状态码，实到 {status}"
    assert json.loads(raw) == {"error": "e3"}, raw


def test_branch_b_non_stream_connect_error_exhausted_502():
    """分支 B 非流式：3 次均连接失败 → HTTPException 502（重试真正耗尽的路径）。"""
    err = _srv.httpx.ConnectError("conn refused")
    script = [err, err, err]

    status, _, raw, calls, sleeps = run_case(script, _req("claude-3-5-sonnet"), provider="copilot")

    assert len(calls) == 3, f"期望 3 次尝试，实到 {len(calls)}"
    assert sleeps == [0.5, 0.5], f"期望两次 backoff，实到 {sleeps}"
    assert status == 502, f"期望 502，实到 {status}"
    assert "upstream unavailable" in raw.decode(), raw


def test_branch_b_stream_retry_then_success():
    """
    分支 B 流式：第 1 次 500（读干后重试）→ 第 2 次 200 正常透传。
    锁定：2 次 stream 调用、1 次 0.5s backoff、chunk 原样透传（无 [DONE] 追加）。
    """
    body = b'event: message_start\ndata: {"type":"message_start"}\n\n'
    script = [FakeResponse(500, body=b'{"error":"boom"}'),
              FakeResponse(200, body=body, chunks=[body[:20], body[20:]])]

    status, hdrs, raw, calls, sleeps = run_case(
        script, _req("claude-3-5-sonnet", stream=True), provider="copilot"
    )

    assert status == 200, f"期望 200，实到 {status}"
    assert "text/event-stream" in hdrs.get("content-type", ""), hdrs
    assert len(calls) == 2 and all(c[0] == "stream" for c in calls), calls
    assert sleeps == [0.5], f"期望一次 backoff，实到 {sleeps}"
    # 分支 B 流式与分支 A 不同：不追加 data: [DONE]
    assert raw == body, f"透传字节不符: {raw!r}"


def test_branch_b_stream_retry_exhausted_error_frame():
    """
    分支 B 流式：连续 3 次 500 → 3 次 stream 调用、2 次 backoff，
    锁定当前行为：HTTP 状态仍是 200（StreamingResponse 头已发出），
    body 是一个**裸 JSON 错误对象**（没有 SSE `data: ` 前缀，也没有 event 行）。
    """
    script = [FakeResponse(500, body=b'{"error":"e1"}'),
              FakeResponse(500, body=b'{"error":"e2"}'),
              FakeResponse(500, body=b'{"error":"e3"}')]

    status, hdrs, raw, calls, sleeps = run_case(
        script, _req("claude-3-5-sonnet", stream=True), provider="copilot"
    )

    assert status == 200, f"流式错误当前仍为 200，实到 {status}"
    assert len(calls) == 3, f"期望 3 次尝试，实到 {len(calls)}"
    assert sleeps == [0.5, 0.5], f"期望两次 backoff，实到 {sleeps}"
    assert not raw.startswith(b"data: "), f"锁定：错误帧无 SSE 前缀，实到 {raw!r}"
    data = json.loads(raw)
    assert data["error"]["type"] == "proxy_error", data
    assert "upstream 500" in data["error"]["message"], data


def test_branch_b_stream_connect_error_exhausted_error_frame():
    """分支 B 流式：3 次连接异常 → 同样输出裸 JSON 错误帧，携带异常字符串。"""
    err = _srv.httpx.ReadError("read failed")
    status, _, raw, calls, sleeps = run_case(
        [err, err, err], _req("claude-3-5-sonnet", stream=True), provider="copilot"
    )
    assert status == 200, status
    assert sleeps == [0.5, 0.5], sleeps
    data = json.loads(raw)
    assert "read failed" in data["error"]["message"], data


def test_branch_b_body_cleanup_blank_content_and_tool_choice():
    """
    分支 B 请求体清洗锁定：
      - content 为空白字符串 → 替换为 "."
      - 有 tool_choice 但无 tools → 移除 tool_choice
    """
    ok = {"id": "m", "type": "message", "model": "x", "content": []}
    script = [FakeResponse(200, body=json.dumps(ok).encode())]
    body = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 16,
        "tool_choice": {"type": "auto"},
        "messages": [
            {"role": "user", "content": "   "},
            {"role": "user", "content": "real"},
        ],
    }
    status, _, _, calls, _ = run_case(script, body, provider="copilot")

    assert status == 200, status
    sent = calls[0][2]["json"]
    contents = [m["content"] for m in sent["messages"]]
    assert contents == [".", "real"], f"空白 content 未被替换: {contents}"
    assert "tool_choice" not in sent, "无 tools 时 tool_choice 应被移除"


def test_branch_b_null_content_rejected_by_pydantic_422():
    """
    锁定一处现状缺陷：create_message 里 `if c is None or ...: msg["content"] = "."`
    的 **None 分支经 HTTP 永远走不到** —— MessagesRequest.content 的类型是
    Union[str, List[...]]，content=null 在进入 handler 前就被 Pydantic 判 422。
    即上游根本收不到该请求，清洗代码是死分支。

    本测试锁定「返回 422 且未发出任何上游请求」。若重构时放宽了模型校验
    （让 None 真正走到清洗逻辑），此断言会 FAIL —— 那是预期的行为变更信号。
    """
    script = [FakeResponse(200, body=b'{"id":"m","type":"message","content":[]}')]
    body = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 16,
        "messages": [{"role": "assistant", "content": None}],
    }
    status, _, _, calls, _ = run_case(script, body, provider="copilot")

    assert status == 422, f"锁定：content=null 被 Pydantic 拒绝，实到 {status}"
    assert calls == [], f"422 时不应触达上游，实到 {calls}"


def test_branch_a_wins_over_branch_b():
    """
    分支优先级锁定：models[] 命中时即便 PREFERRED_PROVIDER==copilot，
    也走分支 A（转发本地端口），不进 Copilot 透传。
    """
    up = {"id": "c", "object": "chat.completion", "model": MAPPED_MODEL,
          "choices": [{"index": 0, "message": {"role": "assistant", "content": "A"}, "finish_reason": "stop"}]}
    resp = FakeResponse(200, body=json.dumps(up).encode())
    status, _, _, calls, _ = run_case(
        [resp], _req("lock-test-model"), models_cfg=_MODELS_CFG_FIXTURE, provider="copilot"
    )
    assert status == 200, status
    assert calls[0][0] == "send", f"应走分支 A 的 build_request/send，实到 {calls[0][0]}"
    assert f":{MAPPED_PORT}" in calls[0][1], calls[0][1]


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
