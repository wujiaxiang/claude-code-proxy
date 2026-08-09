"""
gateways/rtk 单元测试（RTK token-saver：smart_truncate / compress_text /
compress_tool_results / format_rtk_log / safe_apply）。
用法: python test_rtk.py

隔离约定：全程纯函数调用——不启动 server.py、不监听端口、不落盘。本文件顺带锁定
「gateways.rtk 可在无 server 时独立 import」这一契约（logger 用 sys.modules 探测 + fallback）。
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gateways.rtk as rtk

passed = 0
failed = 0


def _long_text(n_lines=400, filler="0123456789abcdefghij"):
    """造一段 n_lines 行、每行有实质内容的文本（保证 >MIN_COMPRESS_SIZE 字节）。"""
    return "\n".join(f"line {i:04d} {filler}" for i in range(n_lines))


def _nbytes(s):
    return len(s.encode("utf-8"))


def _fresh_stats():
    return {"bytesBefore": 0, "bytesAfter": 0, "hits": []}


# ─── (a) 小文本不压 ───
def test_a_small_text_untouched():
    # 行数不足 → smart_truncate 原样返回
    short_lines = "\n".join(f"line {i}" for i in range(rtk.SMART_TRUNCATE_MIN_LINES - 1))
    assert rtk.smart_truncate(short_lines) == short_lines, "行数 <250 应原样返回"

    # 恰好 249 行边界 / 250 行边界语义
    exactly_min = "\n".join("x" * 40 for _ in range(rtk.SMART_TRUNCATE_MIN_LINES))
    assert rtk.smart_truncate(exactly_min) != exactly_min, "恰好 250 行应被截断（< 才放行）"

    # 字节不足 MIN_COMPRESS_SIZE → compress_text 原样返回，且不记 hits
    stats = _fresh_stats()
    tiny = "\n".join("" for _ in range(300))   # 300 行空行 = 299 字节 < 500
    assert _nbytes(tiny) < rtk.MIN_COMPRESS_SIZE, "构造前提：该文本应 <500 字节"
    out = rtk.compress_text(tiny, stats, "claude-string")
    assert out == tiny, "字节 <MIN_COMPRESS_SIZE 应原样返回"
    assert stats["hits"] == [], "未压缩不应记 hits"
    assert stats["bytesBefore"] == 0 and stats["bytesAfter"] == 0

    # 超过 RAW_CAP 也原样返回（用 monkeypatch 降低上限避免造 10MiB）
    orig_cap = rtk.RAW_CAP
    try:
        rtk.RAW_CAP = 1000
        big = _long_text(400)
        assert _nbytes(big) > 1000
        stats2 = _fresh_stats()
        assert rtk.compress_text(big, stats2, "claude-string") == big, ">RAW_CAP 应原样返回"
        assert stats2["hits"] == []
    finally:
        rtk.RAW_CAP = orig_cap


# ─── (b) 大文本被压且变短 ───
def test_b_large_text_compressed_and_shorter():
    text = _long_text(400)
    stats = _fresh_stats()
    out = rtk.compress_text(text, stats, "claude-string")

    assert out != text, "大文本应被压缩"
    assert _nbytes(out) < _nbytes(text), "压缩后应更短"

    cut = 400 - rtk.SMART_TRUNCATE_HEAD - rtk.SMART_TRUNCATE_TAIL
    marker = "... +%d lines truncated" % cut
    assert marker in out, f"应含精确标记 {marker!r}，实际输出片段: {out[:200]!r}"

    lines = out.split("\n")
    assert len(lines) == rtk.SMART_TRUNCATE_HEAD + 1 + rtk.SMART_TRUNCATE_TAIL, \
        f"输出行数应为 120+1+60=181，实际 {len(lines)}"
    assert lines[0] == "line 0000 0123456789abcdefghij", "头部应保留原首行"
    assert lines[rtk.SMART_TRUNCATE_HEAD] == marker, "标记应恰在第 121 行"
    assert lines[-1] == "line 0399 0123456789abcdefghij", "尾部应保留原末行"

    # stats 记账正确
    assert stats["bytesBefore"] == _nbytes(text)
    assert stats["bytesAfter"] == _nbytes(out)
    assert len(stats["hits"]) == 1
    hit = stats["hits"][0]
    assert hit["shape"] == "claude-string"
    assert hit["filter"] == rtk.FILTER_NAME == "smart-truncate"
    assert hit["saved"] == _nbytes(text) - _nbytes(out) > 0

    # format_rtk_log 模板
    line = rtk.format_rtk_log(stats)
    saved = stats["bytesBefore"] - stats["bytesAfter"]
    pct = round(saved / stats["bytesBefore"] * 100, 1)
    expect = "[RTK] saved %dB / %dB (%s%%) via [smart-truncate] hits=1" % (
        saved, stats["bytesBefore"], pct)
    assert line == expect, f"日志模板不匹配:\n  实际 {line!r}\n  期望 {expect!r}"
    assert rtk.format_rtk_log(_fresh_stats()) is None, "空 hits 应返回 None"


# ─── (c) is_error 的 tool_result 原样保留 ───
def test_c_is_error_tool_result_preserved():
    long_text = _long_text(400)
    messages = [{
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": long_text},
        ],
    }]
    before = copy.deepcopy(messages)
    res = rtk.compress_tool_results(messages)

    assert messages == before, "is_error=True 的 tool_result 不得被改动（保留错误栈）"
    assert res["compressed"] is False
    assert res["stats"]["hits"] == []
    assert res["stats"]["bytesBefore"] == 0 and res["stats"]["bytesAfter"] == 0

    # 对照：同样内容但 is_error=False 应被压
    messages2 = [{
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content": long_text},
        ],
    }]
    res2 = rtk.compress_tool_results(messages2)
    assert res2["compressed"] is True, "is_error=False 应正常压缩"
    assert messages2[0]["content"][0]["content"] != long_text


# ─── (d) array 形态 tool_result 被压缩 ───
def test_d_array_shape_tool_result_compressed():
    long_text = _long_text(400)
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "t1",
                "content": [
                    {"type": "text", "text": long_text},
                    {"type": "text", "text": "short tail"},
                    {"type": "image", "source": {"data": "x" * 2000}},
                ],
            },
        ],
    }]
    res = rtk.compress_tool_results(messages)
    parts = messages[0]["content"][0]["content"]

    assert res["compressed"] is True
    assert parts[0]["text"] != long_text, "长 text part 应被压缩"
    assert "lines truncated" in parts[0]["text"]
    assert parts[1]["text"] == "short tail", "短 text part 不动"
    assert parts[2]["source"]["data"] == "x" * 2000, "非 text part 不得被动"
    assert len(res["stats"]["hits"]) == 1
    assert res["stats"]["hits"][0]["shape"] == "claude-array"

    # string 形态 shape 标记
    messages_s = [{
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t2", "content": long_text}],
    }]
    res_s = rtk.compress_tool_results(messages_s)
    assert res_s["stats"]["hits"][0]["shape"] == "claude-string"


# ─── (e) 空/退化输入的护栏 ───
def test_e_empty_and_degenerate_guardrails():
    assert rtk.smart_truncate("") == "", "空串应原样返回"
    assert rtk.smart_truncate("\n") == "\n", "单换行应原样返回"

    only_newlines = "\n" * 300  # 301 行全空
    out = rtk.smart_truncate(only_newlines)
    # 行数够 → 会被截断，但结果不得为空；且 compress_text 若不变短则回退原文
    assert out != "", "截断结果不得为空"
    stats = _fresh_stats()
    assert rtk.compress_text(only_newlines, stats, "claude-string") == only_newlines, \
        "字节 <MIN_COMPRESS_SIZE 应原样返回"

    # 过滤器返回空 / 变长 → compress_text 回退原文
    text = _long_text(400)

    def _empty(_):
        return ""

    def _longer(t):
        return t + "x" * 100

    assert rtk.safe_apply(_empty, text) == "", "safe_apply 本身不判空，只保证类型安全"
    assert rtk.safe_apply(_longer, text) == text + "x" * 100

    orig = rtk.smart_truncate
    try:
        rtk.smart_truncate = _empty
        s1 = _fresh_stats()
        assert rtk.compress_text(text, s1, "claude-string") == text, "过滤器返回空应回退原文"
        assert s1["hits"] == []

        rtk.smart_truncate = _longer
        s2 = _fresh_stats()
        assert rtk.compress_text(text, s2, "claude-string") == text, "过滤器变长应回退原文"
        assert s2["hits"] == []
    finally:
        rtk.smart_truncate = orig

    # safe_apply 三种退化：非可调用 / 抛异常 / 返回非 str
    assert rtk.safe_apply(None, text) == text, "非可调用应返回原文"
    assert rtk.safe_apply(lambda _: 1 / 0, text) == text, "抛异常应返回原文"
    assert rtk.safe_apply(lambda _: 123, text) == text, "返回非 str 应返回原文"

    # format_rtk_log 的 bytesBefore=0 分支
    weird = {"bytesBefore": 0, "bytesAfter": 0,
             "hits": [{"shape": "s", "filter": "smart-truncate", "saved": 0}]}
    line = rtk.format_rtk_log(weird)
    assert line is not None and "(0%)" in line, f"bytesBefore=0 应用 '0' 占位，实际 {line!r}"


# ─── (f) 无 tool_result 的消息零影响 + 独立 import 契约 ───
def test_f_non_tool_result_untouched_and_standalone_import():
    long_text = _long_text(400)
    messages = [
        {"role": "system", "content": long_text},
        {"role": "assistant", "content": [
            {"type": "text", "text": long_text},
            {"type": "thinking", "thinking": long_text, "signature": "sig"},
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": long_text}},
        ]},
        {"role": "user", "content": [{"type": "image",
                                      "source": {"type": "base64", "data": "y" * 3000}}]},
    ]
    before = copy.deepcopy(messages)
    res = rtk.compress_tool_results(messages)

    assert messages == before, "非 tool_result 内容必须逐字节不变"
    assert res["compressed"] is False
    assert res["stats"] == {"bytesBefore": 0, "bytesAfter": 0, "hits": []}

    # 畸形输入不崩（降级契约）
    for bad in (None, [], [None], [{"content": None}], [{"content": "str"}],
                [{"content": [{"type": "tool_result"}]}],
                [{"content": [{"type": "tool_result", "content": 123}]}]):
        r = rtk.compress_tool_results(bad)
        assert isinstance(r, dict) and "stats" in r and "compressed" in r, \
            f"畸形输入 {bad!r} 应降级返回结构完整的 dict"

    # 独立 import 契约：本进程不得因 import gateways.rtk 而加载 server.py
    assert "server" not in sys.modules, \
        "测试进程不应加载 server.py（gateways.rtk 必须能独立 import）"
    assert rtk._log() is not None, "_log() 在无 server 时应回落到本地 logger"

    # 常量精确值锁定
    assert rtk.RAW_CAP == 10 * 1024 * 1024
    assert rtk.MIN_COMPRESS_SIZE == 500
    assert rtk.SMART_TRUNCATE_HEAD == 120
    assert rtk.SMART_TRUNCATE_TAIL == 60
    assert rtk.SMART_TRUNCATE_MIN_LINES == 250
    assert rtk.FILTER_NAME == "smart-truncate"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            globals()["passed"] += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            globals()["failed"] += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            globals()["failed"] += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
