# 聚合网关 HTTP 适配层（从 server.py 拆分，零行为变化）
# 此处符号原样剪切自 server.py，逻辑/参数/返回值/常量均未改动。
#
# 注：_AGGREGATOR_ENGINE 是 server.py 的模块级可变全局（lifespan/_reload_targets/
# _config_watcher 均会 global 重赋值）。这里必须通过 `_srv._AGGREGATOR_ENGINE`
# 模块属性方式读写，绝不能 `from server import _AGGREGATOR_ENGINE`（值拷贝会丢失
# 后续热重载的重赋值，导致引擎状态不一致）。
import asyncio
import json
from datetime import datetime

import httpx

import server as _srv
from server import (
    get_http_client,
    logger,
    _PROXY_STRIP_RESP_HEADERS,
    _TARGET_STATS,
    _write_error_response,
    _write_response,
)
from gateways.aggregator import engine as _agg


async def _handle_aggregate_request(reader, writer, target, method, path, raw_path, headers, body):
    """聚合网关（8080）请求分发：解析虚拟模型 → AggregatorEngine 路由 → 转发到池成员真实端口。

    仅做路由/熔断编排，不解析任何 secretRef/apikeyEnv（聚合层不持有凭据，
    转发目标是本地其他 target 端口，鉴权由那些端口自身处理）。
    聚合层不透传客户端凭据（authorization/x-api-key）——凭据统一由各下游
    端口从 secrets.json 解析注入。
    """
    label = target["label"]
    if _srv._AGGREGATOR_ENGINE is None:
        try:
            _srv._AGGREGATOR_ENGINE = _agg.AggregatorEngine.from_target(target)
        except Exception as e:
            logger.exception(f"[{label}] AggregatorEngine 初始化失败")
            await _write_error_response(writer, 500, f"聚合网关初始化失败: {e}")
            return
    engine = _srv._AGGREGATOR_ENGINE

    try:
        body_json = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        body_json = {}

    virtual_model = body_json.get("model") if isinstance(body_json, dict) else None
    known_models = engine.list_virtual_models()
    if not virtual_model or virtual_model not in known_models:
        err_payload = json.dumps({
            "error": {
                "type": "invalid_request_error",
                "message": f"未知或缺失的虚拟模型 '{virtual_model}'，已配置模型: {known_models}",
            }
        })
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(err_payload.encode()), err_payload.encode()))
        await writer.drain(); writer.close(); return

    session_id = (
        headers.get("x-session-id")
        or headers.get("x-conversation-id")
        or (body_json.get("conversation_id") if isinstance(body_json, dict) else None)
        or (body_json.get("session_id") if isinstance(body_json, dict) else None)
        or (body_json.get("user") if isinstance(body_json, dict) else None)
    )

    async def send_fn(member, info):
        member_body = dict(body_json)
        member_body["model"] = member.model
        member_body_bytes = json.dumps(member_body, ensure_ascii=False).encode("utf-8")

        # 聚合层不透传客户端凭据（authorization / x-api-key）：
        # 转发目标是本地 target 端口，凭据由各下游端口自己从 secrets.json 解析注入
        # （crack 注入 secretRef、free/paid 客户端未带 key 时用 secrets.json 兜底）。
        # 客户端连 8080 时带的 key（如 dummy）对聚合层无意义，透传只会覆盖下游
        # 的真实凭据导致 401。
        fwd_headers = {k: v for k, v in headers.items()
                       if k.lower() not in ("host", "connection", "content-length", "transfer-encoding",
                                            "authorization", "x-api-key")}
        fwd_headers["host"] = f"127.0.0.1:{member.port}"

        client = await get_http_client()
        req = client.build_request(method, f"http://127.0.0.1:{member.port}{raw_path}", headers=fwd_headers, content=member_body_bytes)
        resp = await client.send(req, stream=True)
        return resp

    stats = _TARGET_STATS.setdefault(label, {
        "totalRequests": 0, "translated429": 0,
        "passthroughOk": 0, "passthroughError": 0,
        "startedAt": datetime.now().isoformat(),
    })
    stats["totalRequests"] += 1

    try:
        member, resp = await engine.route_request(virtual_model, session_id, send_fn)
    except _agg.AllPoolsExhausted as e:
        stats["passthroughError"] += 1
        await _write_error_response(writer, 503, f"聚合网关 '{virtual_model}' 池已耗尽: {e}")
        return
    except Exception as e:
        stats["passthroughError"] += 1
        logger.exception(f"[{label}] 聚合路由异常")
        await _write_error_response(writer, 502, f"聚合网关路由失败: {e}")
        return

    content_type = resp.headers.get("content-type", "")
    is_stream = "text/event-stream" in content_type
    if not is_stream:
        body_text = (await resp.aread()).decode("utf-8", errors="replace")
        if engine.quota_error(body_text):
            engine.trip(member.port, "quota_error")
        resp_headers = "".join(
            f"{k}: {v}\r\n" for k, v in resp.headers.items()
            if k.lower() not in _PROXY_STRIP_RESP_HEADERS
        )
        writer.write(f"HTTP/1.1 {resp.status_code} {resp.reason_phrase or 'OK'}\r\n{resp_headers}Content-Length: {len(body_text.encode())}\r\n\r\n".encode())
        writer.write(body_text.encode("utf-8"))
        await writer.drain()
        writer.close()
        stats["passthroughOk"] += 1
        return

    await _write_response(writer, resp, stats=stats)


async def _aggregator_prober():
    """每 5s 检查聚合网关的熔断端口是否到期，到期则发探测请求判定恢复。"""
    while True:
        await asyncio.sleep(5)
        engine = _srv._AGGREGATOR_ENGINE
        if engine is None:
            continue
        try:
            due_ports = engine.probe_due_ports()
            for port in due_ports:
                ok = False
                try:
                    client = await get_http_client()
                    resp = await client.post(
                        f"http://127.0.0.1:{port}/v1/chat/completions",
                        json={"model": "probe", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                        headers={"Content-Type": "application/json"},
                        timeout=httpx.Timeout(5.0),
                    )
                    ok = resp.status_code < 500
                except Exception:
                    ok = False
                engine.record_probe_result(port, ok)
        except Exception:
            logger.exception("aggregator prober error")
