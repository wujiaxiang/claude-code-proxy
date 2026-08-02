"""
trae-work 端到端回归测试（打 8086，需代理运行中）

用例集合：scripts/test-cases/trae-work/*.json（14 个，覆盖模型/协议/历史矩阵）
注意：端到端测试会真实请求 Trae 上游（消耗 crack 额度），跑完整套约 14 次调用。

用法:
  .venv/bin/python test_trae_work_e2e.py           # 跑全部用例
  .venv/bin/python test_trae_work_e2e.py --only seed-tool-history   # 按文件名前缀选跑
  .venv/bin/python test_trae_work_e2e.py --port 8086

环境变量:
  PROXY_HOST / PROXY_PORT  覆盖默认 127.0.0.1:8086
"""
import argparse
import http.client
import json
import os
import sys

HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("PROXY_PORT", "8086"))
CASES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "test-cases", "trae-work")
TIMEOUT = 120

PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL {name} {detail}")


def _is_valid_json(s: str) -> bool:
    """arguments 字符串必须是合法 JSON（tool_calls 协议要求）。"""
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def post_case(body: dict):
    """POST /v1/chat/completions，返回 (http_code, text)"""
    payload = json.dumps(body).encode("utf-8")
    conn = http.client.HTTPConnection(HOST, PORT, timeout=TIMEOUT)
    conn.request("POST", "/v1/chat/completions", body=payload,
                 headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"})
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", errors="replace")
    conn.close()
    return resp.status, data


def main():
    global PASS, FAIL
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="只跑文件名以该前缀开头的用例")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    if not os.path.isdir(CASES_DIR):
        print(f"用例目录不存在: {CASES_DIR}")
        sys.exit(2)

    files = sorted(f for f in os.listdir(CASES_DIR) if f.endswith(".json"))
    if args.only:
        files = [f for f in files if f.startswith(args.only)]
    if not files:
        print(f"没有匹配的用例: {args.only or '(空)'}")
        sys.exit(2)

    print(f"trae-work 端到端回归（{HOST}:{args.port}）共 {len(files)} 个用例\n")

    for fname in files:
        path = os.path.join(CASES_DIR, fname)
        try:
            body = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"FAIL 读取 {fname}: {e}")
            FAIL += 1
            FAILED.append(f"{fname}: 读取失败 {e}")
            continue

        print(f"[{fname}] model={body.get('model')} stream={body.get('stream')} "
              f"tools={'Y' if body.get('tools') else 'N'} "
              f"历史消息数={len(body.get('messages', []))}")
        try:
            status, text = post_case(body)
        except Exception as e:
            print(f"  FAIL 请求异常: {e}")
            FAIL += 1
            FAILED.append(f"{fname}: 请求异常 {e}")
            continue

        # 非流式：期望完整 JSON 且 content 非空
        if not body.get("stream", False):
            check(f"{fname} HTTP 200", status == 200, f"got {status}")
            if status == 200:
                try:
                    data = json.loads(text)
                    msg = (data.get("choices") or [{}])[0].get("message", {})
                    content = msg.get("content") or ""
                    tool_calls = msg.get("tool_calls")
                    check(f"{fname} content 或 tool_calls 非空",
                          bool(content.strip()) or bool(tool_calls),
                          f"content={content[:100]!r} tool_calls={bool(tool_calls)}")
                    if tool_calls:
                        for tc in tool_calls:
                            args = tc.get("function", {}).get("arguments", "")
                            check(f"{fname} arguments 是合法 JSON", _is_valid_json(args),
                                  f"arguments={args[:120]!r}")
                except Exception as e:
                    check(f"{fname} 合法 JSON", False, f"parse err: {e}")
            continue

        # 流式：期望 SSE 200 + data 行 >= 2 + 特征
        check(f"{fname} HTTP 200", status == 200, f"got {status}")
        if status != 200:
            check(f"{fname} 错误体", False, text[:200])
            continue
        data_lines = [l for l in text.split("\n") if l.startswith("data:")]
        # 有效内容行（排除 [DONE] 和空 delta）
        content_lines = [l for l in data_lines if l.strip() != "data: [DONE]" and '"delta":{}' not in l]
        check(f"{fname} 至少 final+[DONE]", len(data_lines) >= 2, f"data_lines={len(data_lines)}")
        # 历史含工具调用的用例：必须有实际输出（曾出现 0 chunks 空响应 bug）
        has_tool_history = any(m.get("tool_calls") or m.get("role") == "tool"
                               for m in body.get("messages", []))
        if has_tool_history:
            check(f"{fname} 工具历史场景非空", len(content_lines) >= 2,
                  f"只有 {len(content_lines)} 个内容行（疑似空响应）")
        # tools 定义用例：期望 tool_calls 输出且结构完整（id/index/arguments JSON）
        if body.get("tools"):
            found_tc = False
            tc_ok = True
            finish_reason = None
            for l in data_lines:
                if l.strip() == "data: [DONE]":
                    continue
                try:
                    c = json.loads(l[5:].strip())
                    delta = c.get("choices", [{}])[0].get("delta", {})
                    if c.get("choices") and c["choices"][0].get("finish_reason"):
                        finish_reason = c["choices"][0]["finish_reason"]
                    tcs = delta.get("tool_calls")
                    if tcs:
                        found_tc = True
                        for tc in tcs:
                            if not tc.get("id"):
                                tc_ok = False
                                print(f"    tool_calls 缺 id: {tc}")
                            if tc.get("index") is None:
                                tc_ok = False
                                print(f"    tool_calls 缺 index: {tc}")
                            args = tc.get("function", {}).get("arguments", "")
                            if not _is_valid_json(args):
                                tc_ok = False
                                print(f"    arguments 非合法 JSON: {args[:120]!r}")
                except Exception:
                    continue
            check(f"{fname} 输出 tool_calls", found_tc,
                  "未检测到 tool_calls（工具调用路径失效）")
            check(f"{fname} tool_calls 结构有效(id/index/arguments)", tc_ok, "见上方明细")
            if found_tc:
                check(f"{fname} finish_reason=tool_calls", finish_reason == "tool_calls",
                      f"got {finish_reason!r}（客户端据此判断是否执行工具）")
        # 不允许 DSML/[Tool Call:] 原始标记泄漏到 content（透传即 IDE 不识别）。
        # 只检查 delta.content（正文），排除 reasoning_content（思考里提到工具调用属正常）。
        leak = False
        for l in data_lines:
            if l.strip() == "data: [DONE]":
                continue
            try:
                c = json.loads(l[5:].strip())
                delta = c.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content") or ""
                if content and (("Tool Call:" in content) or ("<｜" in content and "DSML" in content)):
                    leak = True
                    print(f"    leak content: {content[:120]!r}")
            except Exception:
                continue
        check(f"{fname} 无 DSML/[Tool Call:] 文本泄漏", not leak, "原始标记透传给了客户端")
        # 不允许 busy abort（排队即繁忙是预期行为，但这里不应出现）
        check(f"{fname} 无繁忙中断", "模型繁忙" not in text, "排队繁忙返回")

    print(f"\n结果: {PASS} passed, {FAIL} failed")
    if FAILED:
        print("失败项:")
        for f in FAILED:
            print(f"  - {f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
