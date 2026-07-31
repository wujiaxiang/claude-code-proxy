"""Dashboard 增强验收测试（TDD Red-Green）

验证管理页面 (/dashboard) 两个新能力：
1. 模型列表详细展示：不再是纯 <li><code> 列表，而是带美化名的表格
2. 流量统计信息：请求总数 / 成功率 / 运行时长 + 可视化（进度条）

运行前提：claude-code-proxy 服务运行中（8082 端口，dashboard 代理自 8081 FastAPI）。
用法: python test_dashboard.py
"""
import http.client
import sys

HOST = "127.0.0.1"
PORT = 8082


def _get_dashboard():
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request("GET", "/dashboard")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    return resp.status, body


def test_dashboard_reachable():
    status, body = _get_dashboard()
    assert status == 200, f"dashboard HTTP {status}"
    # 必须包含各端口卡片
    for port in ("8082", "8084", "8090", "8091"):
        assert port in body, f"dashboard 缺少端口 {port} 的卡片"


def test_models_rendered_as_detailed_table():
    """模型列表以表格形式展示（不再是纯 <li> 列表）。"""
    _, body = _get_dashboard()
    assert "<table" in body, "模型列表应为表格形式"
    assert "model-table" in body, "模型表格应有 model-table 类"
    # 表格包含 codebuddy 与 nvidia 的模型 id
    assert "glm-5.2" in body, "模型表格应包含 glm-5.2"
    assert "deepseek-v4-pro" in body, "模型表格应包含 deepseek-v4-pro"


def test_models_have_humanized_names():
    """模型应有美化名（GLM 5.2 / DeepSeek V4 Pro），而非纯 id。"""
    _, body = _get_dashboard()
    assert "GLM" in body, "模型应有人类可读名称（GLM）"
    assert "DeepSeek" in body, "模型应有人类可读名称（DeepSeek）"


def test_traffic_stats_shown():
    """流量统计：请求总数 + 成功率百分比 + 运行时长。"""
    _, body = _get_dashboard()
    assert "请求" in body, "应显示请求统计"
    assert "%" in body, "应显示成功率百分比"
    assert ("时长" in body) or ("运行" in body), "应显示运行时长"


def test_traffic_visualized():
    """流量应有可视化元素（进度条/条形图）。"""
    _, body = _get_dashboard()
    assert ("progress" in body) or ("rate-bar" in body) or ("traffic-bar" in body), "应有流量可视化元素"


def test_vendor_stats_breakdown():
    """vendor 卡片应显示 ok / 429 / error 明细。"""
    _, body = _get_dashboard()
    assert "429" in body, "应显示 429 翻译计数"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
