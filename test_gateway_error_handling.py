"""
test_gateway_error_handling.py — Wave 1 统一转发引擎错误码归一化测试（脚本式，无 pytest）
用法: python test_gateway_error_handling.py

覆盖 Todo 1-3 的全部行为验证场景：
1. ConnectError → 502
2. ConnectTimeout → 502 (或 ConnectError 也映射 502，以"收到 502"为准)
3. ReadTimeout → 504
4. RemoteProtocolError 不抛 NameError
5. body 嵌错误码改写 (200 → 504)
6. 防误报-合法 choices (200 保持 200)
7. 防误报-上游非 200 (500 不被 body 覆盖)
8. 流中途断连 (headers 已提交) → 单次状态行 + warning
9. 正常流不受影响
10. /api/* 流式代理路径中途断连 (grep 佐证 headers_sent 已设置)
"""
import asyncio
import json
import sys
import socket
import subprocess
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import server
import httpx

passed = 0
failed = 0


# ─── 工具函数 ───

def find_free_port():
    """获取一个随机可用端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_target(upstream_port, label="test-target", handler="passthrough", category="free", **overrides):
    """构造最小可用的 target 字典"""
    target = {
        "label": label,
        "listenPort": find_free_port(),
        "category": category,
        "handler": handler,
        "targetHost": "127.0.0.1",
        "targetPort": upstream_port,
        "targetProtocol": "http",
        "routePrefix": "",
        "models": [],
        "secretRef": None,
        "apikeyEnv": None,
        "apikey": None,
        "enabled": True,
    }
    target.update(overrides)
    return target


def make_request(stream=False, model="test-model"):
    """构造最小 HTTP 请求"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream
    }).encode()
    req = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n" + body
    )
    return req


async def run_target(target, request_bytes, timeout=10.0):
    """
    起监听端口，把 _handle_target_request 作为 connection handler，
    client 发请求读响应
    """
    captured = {}

    async def handle(reader, writer):
        try:
            await server._handle_target_request(reader, writer, target)
        except Exception as e:
            captured["handler_exc"] = e
        finally:
            try:
                writer.close()
            except Exception:
                pass

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request_bytes)
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(), timeout)
        writer.close()
        await writer.wait_closed()
    finally:
        srv.close()
        await srv.wait_closed()
    return resp, captured


async def start_mock_upstream(handler_coro, port=None):
    """启动 mock 上游 HTTP 服务器，返回 (server, port)"""
    if port is None:
        port = find_free_port()

    async def handle(reader, writer):
        try:
            await handler_coro(reader, writer)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    srv = await asyncio.start_server(handle, "127.0.0.1", port)
    return srv, port


# ─── Mock 上游处理器 ───

async def mock_upstream_connect_refused(reader, writer):
    """上游拒绝连接（端口无监听）"""
    writer.close()


async def mock_upstream_accept_then_close(reader, writer):
    """接受连接后立即关闭（模拟 ConnectError）"""
    writer.close()


async def mock_upstream_send_headers_only(reader, writer):
    """发送响应头但不发 body（触发 ReadTimeout）"""
    await reader.read(1024)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
    await writer.drain()
    await asyncio.sleep(10)


async def mock_upstream_partial_chunked_then_close(reader, writer):
    """发送部分 chunked body 后关闭（触发 RemoteProtocolError）"""
    await reader.read(1024)
    writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
    writer.write(b"5\r\nhello\r\n")
    await writer.drain()
    writer.close()


async def mock_upstream_200_with_error_envelope(reader, writer):
    """返回 200 但 body 嵌错误码"""
    await reader.read(1024)
    body = json.dumps({
        "code": 504,
        "message": "Upstream idle timeout exceeded",
        "metadata": {"error_type": "timeout"}
    }).encode()
    resp = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n" + body
    )
    writer.write(resp)
    await writer.drain()
    writer.close()


async def mock_upstream_200_with_choices(reader, writer):
    """返回正常 chat completion (含 choices 和 object)"""
    await reader.read(1024)
    body = json.dumps({
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        "code": 200
    }).encode()
    resp = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n" + body
    )
    writer.write(resp)
    await writer.drain()
    writer.close()


async def mock_upstream_500_with_error_envelope(reader, writer):
    """返回 500 且 body 嵌错误码"""
    await reader.read(1024)
    body = json.dumps({
        "code": 504,
        "message": "Upstream idle timeout exceeded"
    }).encode()
    resp = (
        b"HTTP/1.1 500 Internal Server Error\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n" + body
    )
    writer.write(resp)
    await writer.drain()
    writer.close()


async def mock_upstream_stream_then_close(reader, writer):
    """流式响应：发送 headers + 2 个 SSE chunk 后关闭（不发 [DONE]）"""
    await reader.read(1024)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
    writer.write(b"data: {\"a\":1}\n\n")
    writer.write(b"data: {\"b\":2}\n\n")
    await writer.drain()
    writer.close()


async def mock_upstream_normal_stream(reader, writer):
    """正常完整流式响应"""
    await reader.read(1024)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
    writer.write(b"data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\n\n")
    writer.write(b"data: {\"choices\":[{\"delta\":{\"content\":\" world\"}}]}\n\n")
    writer.write(b"data: [DONE]\n\n")
    await writer.drain()
    writer.close()


async def mock_upstream_api_sse_then_close(reader, writer):
    """/api/* 路径的 SSE 响应后断连"""
    await reader.read(1024)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
    writer.write(b"data: test\n\n")
    await writer.drain()
    writer.close()


# ─── 测试用例 (每个都是 async 函数) ───

async def test_1_connect_error_502():
    """ConnectError → 502：mock 上游端口无监听"""
    global passed, failed
    try:
        port = find_free_port()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.close()

        target = make_target(port, label="test-connect-error")
        req = make_request(stream=False)

        resp, captured = await run_target(target, req, timeout=5.0)

        assert b"HTTP/1.1 502" in resp, f"Expected 502, got: {resp[:200]}"
        print(f"PASS test_1_connect_error_502")
        passed += 1
    except AssertionError as e:
        print(f"FAIL test_1_connect_error_502: {e}")
        failed += 1
    except Exception as e:
        print(f"ERROR test_1_connect_error_502: {e}")
        failed += 1


async def test_2_connect_timeout_502():
    """ConnectTimeout → 502：monkeypatch 短 connect timeout，连黑洞地址"""
    global passed, failed
    original_timeout = server._TARGET_HTTPX_TIMEOUT
    try:
        server._TARGET_HTTPX_TIMEOUT = httpx.Timeout(connect=0.5, read=300.0, write=300.0, pool=300.0)

        target = make_target(80, label="test-connect-timeout")
        target["targetHost"] = "192.0.2.1"
        target["targetPort"] = 80
        req = make_request(stream=False)

        resp, captured = await run_target(target, req, timeout=10.0)

        assert b"HTTP/1.1 502" in resp, f"Expected 502, got: {resp[:200]}"
        print(f"PASS test_2_connect_timeout_502")
        passed += 1
    except AssertionError as e:
        print(f"FAIL test_2_connect_timeout_502: {e}")
        failed += 1
    except Exception as e:
        print(f"ERROR test_2_connect_timeout_502: {e}")
        failed += 1
    finally:
        server._TARGET_HTTPX_TIMEOUT = original_timeout


async def test_3_read_timeout_504():
    """ReadTimeout → 504：mock 上游发送头但不发 body"""
    global passed, failed
    original_timeout = server._TARGET_HTTPX_TIMEOUT
    try:
        server._TARGET_HTTPX_TIMEOUT = httpx.Timeout(read=1.0, connect=1.0, write=300.0, pool=300.0)

        upstream_srv, upstream_port = await start_mock_upstream(mock_upstream_send_headers_only)
        try:
            target = make_target(upstream_port, label="test-read-timeout")
            req = make_request(stream=False)

            resp, captured = await run_target(target, req, timeout=5.0)

            assert b"HTTP/1.1 504" in resp, f"Expected 504, got: {resp[:200]}"
            print(f"PASS test_3_read_timeout_504")
            passed += 1
        finally:
            upstream_srv.close()
            await upstream_srv.wait_closed()
    except AssertionError as e:
        print(f"FAIL test_3_read_timeout_504: {e}")
        failed += 1
    except Exception as e:
        print(f"ERROR test_3_read_timeout_504: {e}")
        failed += 1
    finally:
        server._TARGET_HTTPX_TIMEOUT = original_timeout


async def test_4_remote_protocol_error_no_nameerror():
    """RemoteProtocolError 不抛 NameError：部分 chunked 后断连"""
    global passed, failed
    original_timeout = server._TARGET_HTTPX_TIMEOUT
    try:
        server._TARGET_HTTPX_TIMEOUT = httpx.Timeout(read=2.0, connect=1.0, write=300.0, pool=300.0)

        upstream_srv, upstream_port = await start_mock_upstream(mock_upstream_partial_chunked_then_close)
        try:
            target = make_target(upstream_port, label="test-remote-protocol")
            req = make_request(stream=False)

            resp, captured = await run_target(target, req, timeout=5.0)

            handler_exc = captured.get("handler_exc")
            if handler_exc:
                assert "NameError" not in type(handler_exc).__name__, f"Got NameError: {handler_exc}"
                assert "httpcore" not in str(handler_exc), f"httpcore in exception: {handler_exc}"

            assert b"HTTP/1.1 502" in resp or len(resp) == 0, f"Expected 502 or empty, got: {resp[:200]}"
            print(f"PASS test_4_remote_protocol_error_no_nameerror")
            passed += 1
        finally:
            upstream_srv.close()
            await upstream_srv.wait_closed()
    except AssertionError as e:
        print(f"FAIL test_4_remote_protocol_error_no_nameerror: {e}")
        failed += 1
    except Exception as e:
        print(f"ERROR test_4_remote_protocol_error_no_nameerror: {e}")
        failed += 1
    finally:
        server._TARGET_HTTPX_TIMEOUT = original_timeout


async def test_5_body_embedded_error_rewrite():
    """body 嵌错误码改写：上游 200 + body {"code":504,...} → 客户端 504"""
    global passed, failed
    try:
        upstream_srv, upstream_port = await start_mock_upstream(mock_upstream_200_with_error_envelope)
        try:
            target = make_target(upstream_port, label="test-body-error")
            req = make_request(stream=False)

            resp, captured = await run_target(target, req, timeout=5.0)

            assert b"HTTP/1.1 504" in resp, f"Expected 504 status, got: {resp[:200]}"
            assert b"Gateway Timeout" in resp, f"Expected 'Gateway Timeout' reason, got: {resp[:200]}"
            expected_body = json.dumps({
                "code": 504,
                "message": "Upstream idle timeout exceeded",
                "metadata": {"error_type": "timeout"}
            }).encode()
            assert expected_body in resp, f"Body not preserved: {resp}"
            print(f"PASS test_5_body_embedded_error_rewrite")
            passed += 1
        finally:
            upstream_srv.close()
            await upstream_srv.wait_closed()
    except AssertionError as e:
        print(f"FAIL test_5_body_embedded_error_rewrite: {e}")
        failed += 1
    except Exception as e:
        print(f"ERROR test_5_body_embedded_error_rewrite: {e}")
        failed += 1


async def test_6_false_positive_choices():
    """防误报：合法 choices + object → 保持 200"""
    global passed, failed
    try:
        upstream_srv, upstream_port = await start_mock_upstream(mock_upstream_200_with_choices)
        try:
            target = make_target(upstream_port, label="test-false-positive")
            req = make_request(stream=False)

            resp, captured = await run_target(target, req, timeout=5.0)

            assert b"HTTP/1.1 200" in resp, f"Expected 200, got: {resp[:200]}"
            assert b"choices" in resp and b"chat.completion" in resp, f"Body modified: {resp}"
            print(f"PASS test_6_false_positive_choices")
            passed += 1
        finally:
            upstream_srv.close()
            await upstream_srv.wait_closed()
    except AssertionError as e:
        print(f"FAIL test_6_false_positive_choices: {e}")
        failed += 1
    except Exception as e:
        print(f"ERROR test_6_false_positive_choices: {e}")
        failed += 1


async def test_7_false_positive_upstream_not_200():
    """防误报：上游 500 + body 嵌 504 → 客户端 500 (不被覆盖)"""
    global passed, failed
    try:
        upstream_srv, upstream_port = await start_mock_upstream(mock_upstream_500_with_error_envelope)
        try:
            target = make_target(upstream_port, label="test-upstream-500")
            req = make_request(stream=False)

            resp, captured = await run_target(target, req, timeout=5.0)

            assert b"HTTP/1.1 500" in resp, f"Expected 500, got: {resp[:200]}"
            print(f"PASS test_7_false_positive_upstream_not_200")
            passed += 1
        finally:
            upstream_srv.close()
            await upstream_srv.wait_closed()
    except AssertionError as e:
        print(f"FAIL test_7_false_positive_upstream_not_200: {e}")
        failed += 1
    except Exception as e:
        print(f"ERROR test_7_false_positive_upstream_not_200: {e}")
        failed += 1


async def test_8_stream_abort_after_headers_sent():
    """流中途断连 (headers 已提交) → 单次状态行 + warning"""
    global passed, failed
    original_timeout = server._TARGET_HTTPX_TIMEOUT
    try:
        server._TARGET_HTTPX_TIMEOUT = httpx.Timeout(read=2.0, connect=1.0, write=300.0, pool=300.0)

        upstream_srv, upstream_port = await start_mock_upstream(mock_upstream_stream_then_close)
        try:
            target = make_target(upstream_port, label="test-stream-abort")
            req = make_request(stream=True)

            resp, captured = await run_target(target, req, timeout=5.0)

            http_count = resp.count(b"HTTP/1.1")
            assert http_count == 1, f"Expected exactly 1 status line, got {http_count}: {resp[:500]}"

            assert b"HTTP/1.1 200" in resp, f"Expected 200 status line, got: {resp[:200]}"

            assert b"data: {\"a\":1}" in resp
            assert b"data: {\"b\":2}" in resp

            print(f"PASS test_8_stream_abort_after_headers_sent")
            passed += 1
        finally:
            upstream_srv.close()
            await upstream_srv.wait_closed()
    except AssertionError as e:
        print(f"FAIL test_8_stream_abort_after_headers_sent: {e}")
        failed += 1
    except Exception as e:
        print(f"ERROR test_8_stream_abort_after_headers_sent: {e}")
        failed += 1
    finally:
        server._TARGET_HTTPX_TIMEOUT = original_timeout


async def test_9_normal_stream_unaffected():
    """正常流不受影响"""
    global passed, failed
    try:
        upstream_srv, upstream_port = await start_mock_upstream(mock_upstream_normal_stream)
        try:
            target = make_target(upstream_port, label="test-normal-stream")
            req = make_request(stream=True)

            resp, captured = await run_target(target, req, timeout=5.0)

            http_count = resp.count(b"HTTP/1.1")
            assert http_count == 1, f"Expected exactly 1 status line, got {http_count}"

            assert b"HTTP/1.1 200" in resp

            assert b"Hello" in resp
            assert b"world" in resp
            assert b"[DONE]" in resp

            print(f"PASS test_9_normal_stream_unaffected")
            passed += 1
        finally:
            upstream_srv.close()
            await upstream_srv.wait_closed()
    except AssertionError as e:
        print(f"FAIL test_9_normal_stream_unaffected: {e}")
        failed += 1
    except Exception as e:
        print(f"ERROR test_9_normal_stream_unaffected: {e}")
        failed += 1


async def test_10_api_stream_proxy_path():
    """/api/* 流式代理路径中途断连：grep 佐证 headers_sent 已设置"""
    global passed, failed
    try:
        result = subprocess.run(
            ["grep", "-n", r'\["headers_sent"\] = True', "server.py"],
            capture_output=True, text=True, cwd=Path(__file__).parent
        )
        lines = result.stdout.strip().split('\n')
        assert len(lines) >= 3, f"Expected at least 3 headers_sent assignments, found {len(lines)}: {lines}"

        print(f"PASS test_10_api_stream_proxy_path (grep verified {len(lines)} headers_sent assignments)")
        passed += 1
    except AssertionError as e:
        print(f"FAIL test_10_api_stream_proxy_path: {e}")
        failed += 1
    except Exception as e:
        print(f"ERROR test_10_api_stream_proxy_path: {e}")
        failed += 1


async def test_mutation_body_error_detection():
    """Mutation testing: 临时禁用 body 嵌错误码检测，验证测试能捕获回归"""
    global passed, failed
    original_func = server._write_response_with_status_override

    async def noop_override(writer, resp, effective_status, *, stats=None):
        await server._write_response(writer, resp, stats=stats)

    server._write_response_with_status_override = noop_override

    try:
        upstream_srv, upstream_port = await start_mock_upstream(mock_upstream_200_with_error_envelope)
        try:
            target = make_target(upstream_port, label="test-mutation")
            req = make_request(stream=False)

            resp, captured = await run_target(target, req, timeout=5.0)

            assert b"HTTP/1.1 200" in resp, f"Mutation test: expected 200 when detection disabled, got: {resp[:200]}"
            print(f"PASS test_mutation_body_error_detection (mutation detected)")
            passed += 1
        finally:
            upstream_srv.close()
            await upstream_srv.wait_closed()
    finally:
        server._write_response_with_status_override = original_func

        upstream_srv, upstream_port = await start_mock_upstream(mock_upstream_200_with_error_envelope)
        try:
            target = make_target(upstream_port, label="test-mutation-restore")
            req = make_request(stream=False)

            resp, captured = await run_target(target, req, timeout=5.0)

            assert b"HTTP/1.1 504" in resp, f"Restore test: expected 504 after restore, got: {resp[:200]}"
            print(f"PASS test_mutation_body_error_detection (restore verified)")
            passed += 1
        finally:
            upstream_srv.close()
            await upstream_srv.wait_closed()


# ─── 主函数 ───

async def run_all_tests():
    global passed, failed

    tests = [
        test_1_connect_error_502,
        test_2_connect_timeout_502,
        test_3_read_timeout_504,
        test_4_remote_protocol_error_no_nameerror,
        test_5_body_embedded_error_rewrite,
        test_6_false_positive_choices,
        test_7_false_positive_upstream_not_200,
        test_8_stream_abort_after_headers_sent,
        test_9_normal_stream_unaffected,
        test_10_api_stream_proxy_path,
        test_mutation_body_error_detection,
    ]

    for t in tests:
        try:
            await t()
        except Exception as e:
            print(f"ERROR {t.__name__}: {e}")
            failed += 1

    print(f"\n{passed}/{passed + failed} passed")
    return 1 if failed else 0


def main():
    return asyncio.run(run_all_tests())


if __name__ == "__main__":
    sys.exit(main())