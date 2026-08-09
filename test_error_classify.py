"""
_classify_http_error 映射单测。

决策：_classify_http_error 定义在 server.py，import server 会触发模块级配置加载 /
凭据解密等全量副作用。为避免污染测试进程（与 test_usage_store.py 的「不加载 server」
契约一致），这里**复制函数体**（与 server.py L746 同步），仅做映射正确性验证。

用法: python test_error_classify.py
脚本式 assert + print + sys.exit（项目无 pytest，见 AGENTS.md §8）。
"""


def _classify_http_error(status_code: int) -> str:
    """与 server.py L746 _classify_http_error 同步的副本（仅用于测试映射正确性）。

    状态码 → 错误分类标签：
      401→401_auth / 402→402_billing / 403→403_forbidden / 429→429_rate_limit
      400<=c<500 → {c}_client
      500<=c<600 → {c}_server
      其余 → http_{c}（非 4xx/5xx 或异常输入兜底）
    """
    if status_code == 401:
        return "401_auth"
    if status_code == 402:
        return "402_billing"
    if status_code == 403:
        return "403_forbidden"
    if status_code == 429:
        return "429_rate_limit"
    if 400 <= status_code < 500:
        return f"{status_code}_client"
    if 500 <= status_code < 600:
        return f"{status_code}_server"
    return f"http_{status_code}"


CASES = [
    (401, "401_auth"),
    (402, "402_billing"),
    (403, "403_forbidden"),
    (429, "429_rate_limit"),
    (404, "404_client"),
    (422, "422_client"),
    (500, "500_server"),
    (502, "502_server"),
    (504, "504_server"),
    (302, "http_302"),   # 非 4xx/5xx
    (0, "http_0"),       # 异常输入兜底
    (-1, "http_-1"),     # 异常输入兜底
]


def main():
    failed = 0
    for code, expected in CASES:
        got = _classify_http_error(code)
        if got == expected:
            print(f"PASS classify({code}) -> {got!r}")
        else:
            print(f"FAIL classify({code}) -> {got!r}, expected {expected!r}")
            failed += 1
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
