"""
messages_test_helpers.py —— 8081 /v1/messages 相关测试的共享 ASGI 直调基础设施。

从 `test_v1messages_lock.py` 抽取而来（Task 9  consolidation）：原 lock 测试与
各 task 测试各自复制了一份 FakeResponse / 假 AsyncClient / _asgi_post / run_case
样板，本模块把它们收敛为单一事实源，供 `test_v1messages_lock.py`（继续做 SSE
帧重组回归锁）与 `test_messages_passthrough.py`（Task 1-8 合并回归套件）共同 import。

设计要点（与既有行为逐字节兼容）：
  * 假客户端 send 时记录的 call 元组固定为 5 元组：
        (method, url, {"stream": bool}, headers, content)
    其中 index 2 仍是 {"stream": ...} 字典 —— 保证 lock 测试里
    `calls[0][2]["stream"]` 这类既有断言无需改动即可复用。
  * headers / content 放在 index 3 / 4，新套件通过 `_call_headers` / `_call_content`
    读取，互不干扰。
  * 全程不起真实端口：ASGI 直调 server.app + 替换 server.httpx.AsyncClient。

仅包含纯测试基础设施，不含任何 `test_*` 函数 —— 不会被 pytest 误收集。
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import server as _srv  # noqa: E402


# ============================================================
# 假 httpx.AsyncClient —— 记录调用 + 按脚本回放响应
# ============================================================

class FakeResponse:
    """最小 httpx.Response 替身，支持 aread / aiter_bytes / aclose。

    - body:       完整响应体字节（aread 返回）。
    - chunks:     流式分块序列（aiter_bytes 逐块 yield）；为 None 时退化为 [body]。
    """

    def __init__(self, status_code, body=b"", chunks=None):
        self.status_code = status_code
        self._body = body
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
    """构造假 AsyncClient 类。

    script: 列表，每项是 FakeResponse 或 Exception（抛出以模拟连接错误）；
            按调用顺序依次消费；耗尽后复用最后一项。
    calls:  外部传入的 list，用于记录每次调用。每条记录为 5 元组：
            (method, url, {"stream": bool}, headers, content)
            其中 headers / content 来自 build_request 的 kwargs（转发 header 与 body）。
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

        def build_request(self, method, url, **kw):
            return {"method": method, "url": url, **kw}

        async def send(self, req, stream=False):
            # 与既有 lock 测试兼容：index 2 仍是 {"stream": ...} 字典
            calls.append(
                ("send", req["url"], {"stream": stream}, req.get("headers"), req.get("content"))
            )
            return _next()

    return FakeAsyncClient


# ============================================================
# call 元组解包便利函数
# ============================================================

def _call_url(call):
    return call[1]


def _call_stream(call):
    return call[2]["stream"]


def _call_headers(call):
    return call[3]


def _call_content(call):
    return call[4]


def _get_ci(d, name):
    """大小写不敏感取 header 值（Starlette headers.items() 保留原始大小写）。"""
    name = name.lower()
    for k, v in (d or {}).items():
        if k.lower() == name:
            return v
    return None


# ============================================================
# ASGI 直调 —— 不起端口，直接把 request 喂给 app
# ============================================================

async def _asgi_post(path, body_obj, headers=None):
    """通过 ASGI 协议直接调用 server.app，返回 (status, headers_dict, body_bytes)。

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
        # 流式中不能返回 http.disconnect —— StreamingResponse 会监听它并提前中断流，
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
    用于断言流式输出的渐进分块粒度。"""
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


# ============================================================
# run_case —— 受控环境跑一次 /v1/messages
# ============================================================

def run_case(script, body_obj, models_cfg=None, models_targets=None,
             headers=None, guard_enabled=None, patch_sleep=False):
    """在受控环境跑一次 /v1/messages：
      - 替换 server.httpx.AsyncClient 为假客户端（按 script 回放）
      - 注入 models[] 配置（_MODELS_CFG，分支开关）
      - 可选注入 _TARGETS（profile 能力门控查询源）
      - 可选切换 ANTHROPIC_SSE_ERROR_GUARD_ENABLED
      - 可选打桩 asyncio.sleep（记录耗时，可断言 backoff）
    返回 (status, headers, body_bytes, calls, sleeps)。
    """
    calls = []
    sleeps = []
    fake_cls = make_fake_client_cls(script, calls)

    old_models = _srv._MODELS_CFG
    old_targets = _srv._TARGETS if models_targets is not None else None
    old_guard = getattr(_srv, "ANTHROPIC_SSE_ERROR_GUARD_ENABLED", False)
    try:
        _srv._MODELS_CFG = models_cfg if models_cfg is not None else {"models": [], "modelDefaults": {}}
        if models_targets is not None:
            _srv._TARGETS = models_targets
        if guard_enabled is not None:
            _srv.ANTHROPIC_SSE_ERROR_GUARD_ENABLED = guard_enabled

        ctx = patch.object(_srv.httpx, "AsyncClient", fake_cls)
        ctx.__enter__()
        try:
            if patch_sleep:
                real_sleep = asyncio.sleep

                async def fake_sleep(sec, *a, **kw):
                    sleeps.append(sec)
                    return await real_sleep(0)

                sp = patch.object(_srv.asyncio, "sleep", fake_sleep)
                sp.__enter__()
                try:
                    status, hdrs, raw = asyncio.run(_asgi_post("/v1/messages", body_obj, headers))
                finally:
                    sp.__exit__(None, None, None)
            else:
                status, hdrs, raw = asyncio.run(_asgi_post("/v1/messages", body_obj, headers))
        finally:
            ctx.__exit__(None, None, None)
    finally:
        _srv._MODELS_CFG = old_models
        if old_targets is not None:
            _srv._TARGETS = old_targets
        if guard_enabled is not None:
            _srv.ANTHROPIC_SSE_ERROR_GUARD_ENABLED = old_guard
    return status, hdrs, raw, calls, sleeps


def run_case_events(script, body_obj, models_cfg=None, models_targets=None,
                    headers=None, guard_enabled=None):
    """与 run_case 相同，但返回 (status, headers, body_events, calls, sleeps)，
    body_events 为 http.response.body 事件的原始列表（未拼接）。"""
    calls = []
    sleeps = []
    fake_cls = make_fake_client_cls(script, calls)

    old_models = _srv._MODELS_CFG
    old_targets = _srv._TARGETS if models_targets is not None else None
    old_guard = getattr(_srv, "ANTHROPIC_SSE_ERROR_GUARD_ENABLED", False)
    try:
        _srv._MODELS_CFG = models_cfg if models_cfg is not None else {"models": [], "modelDefaults": {}}
        if models_targets is not None:
            _srv._TARGETS = models_targets
        if guard_enabled is not None:
            _srv.ANTHROPIC_SSE_ERROR_GUARD_ENABLED = guard_enabled

        ctx = patch.object(_srv.httpx, "AsyncClient", fake_cls)
        ctx.__enter__()
        try:
            status, hdrs, events = asyncio.run(_asgi_post_events("/v1/messages", body_obj, headers))
        finally:
            ctx.__exit__(None, None, None)
    finally:
        _srv._MODELS_CFG = old_models
        if old_targets is not None:
            _srv._TARGETS = old_targets
        if guard_enabled is not None:
            _srv.ANTHROPIC_SSE_ERROR_GUARD_ENABLED = old_guard
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
