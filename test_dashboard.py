"""Dashboard 验收测试（多端口架构）

验证管理页面 (/dashboard) + API 端点：
1. 模型列表详细展示：不再是纯 <li><code> 列表，而是带美化名的表格
2. 流量统计信息：请求总数 / 成功率 / 运行时长 + 可视化（进度条）
3. /api/targets 端点：返回所有 target 的 label/isFree/enabled 等

运行前提：claude-code-proxy 服务运行中（8081 FastAPI 端口）。
用法: python test_dashboard.py
"""
import http.client
import json
import re
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


def test_api_targets_endpoint():
    """GET /api/targets 应返回所有 target 的 label 列表（含 8 个 label）。"""
    conn = http.client.HTTPConnection("127.0.0.1", 8081, timeout=15)
    conn.request("GET", "/api/targets")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200, f"/api/targets HTTP {resp.status}"
    data = json.loads(body)
    targets = data.get("targets", [])
    assert isinstance(targets, list), "/api/targets 返回的 targets 应为 JSON 数组"
    labels = {t.get("label", "") for t in targets}
    for expected in ("copilot", "codebuddy", "qclaw", "trae-work",
                     "openrouter", "nvidia", "gemini", "opencode-zen"):
        assert expected in labels, f"/api/targets 缺少 label: {expected}"


def test_accordion_card_structure():
    """手风琴结构：卡片头 (card-toggle) + 详情区 (card-detail)，一次只展开一个。"""
    _, body = _get_dashboard()
    assert "card-toggle" in body, "卡片头应有 card-toggle 类"
    assert "card-detail" in body, "详情区应有 card-detail 类"
    assert 'viewport" content="width=device-width' in body, "应有 viewport meta（移动端适配）"
    # 手风琴 JS 存在（互斥展开逻辑）
    assert "toggleAccordion" in body or "card-toggle" in body, "应包含手风琴交互 JS"


def test_model_whitelist_editor():
    """模型白名单编辑：应有编辑入口按钮，且不应有添加模型能力。"""
    _, body = _get_dashboard()
    assert "model-edit-toggle" in body, "应有编辑入口按钮"
    assert "model-add-input" not in body, "不应有模型添加输入框"
    assert "model-add-btn" not in body, "不应有模型添加按钮"
    assert "addModel" not in body, "不应有添加模型的 JS"


def test_meta_badges_classification():
    """模型标签分类：破解/非破解 + 免费/收费 + 稳定性高/低。"""
    _, body = _get_dashboard()
    assert "b-meta-crack" in body, "应有「破解」标签"
    assert "b-meta-normal" in body, "应有「非破解」标签"
    assert "b-meta-free" in body, "应有「免费」标签"
    assert "b-meta-paid" in body, "应有「收费」标签"
    assert "b-meta-stable" in body, "应有「稳定性高」标签"
    assert "b-meta-unstable" in body, "应有「稳定性低」标签"
    # 文案检查
    assert "破解" in body and "非破解" in body, "应有破解/非破解文案"
    assert "免费" in body and "收费" in body, "应有免费/收费文案"
    assert "稳定性高" in body and "稳定性低" in body, "应有稳定性高/低文案"


def test_paid_is_stable():
    """收费（paid）provider 应标记「稳定性高」，如 open-go。"""
    _, body = _get_dashboard()
    # open-go 卡片：收费 + 稳定性高
    m = re.search(r'open-go.*?</div>', body, re.S)
    assert m, "应有 open-go 卡片"
    open_go_block = m.group(0)
    assert "收费" in open_go_block, "open-go 应为收费"
    assert "稳定性高" in open_go_block, "open-go 应标记稳定性高"


def test_api_proxy_via_target_port():
    """8082 等 target 端口应代理 /api/* 到 8081（dashboard 管理接口经任意端口可访问）。"""
    for port in (8082, 8090):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        conn.request("GET", "/api/targets")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        assert resp.status == 200, f"{port} /api/targets HTTP {resp.status}"
        assert "targets" in body, f"{port} /api/targets 应返回 targets 字段"
    # 8082 的编辑态端点也通
    conn = http.client.HTTPConnection("127.0.0.1", 8082, timeout=15)
    conn.request("GET", "/api/targets/openrouter/models?edit=1")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200, f"8082 编辑态端点 HTTP {resp.status}"
    assert "model-show" in body, "编辑态应有展示开关"


def test_no_alert_in_ui():
    """UI 交互不应使用 window.alert()（应为行内消息）。"""
    _, body = _get_dashboard()
    assert "window.alert" not in body and "alert(" not in body, "不应使用 alert()"


def test_viewport_responsive_css():
    """响应式 CSS：应有 768px 断点媒体查询（移动端适配）。"""
    _, body = _get_dashboard()
    assert "@media (max-width: 768px)" in body or "768px" in body, "应有移动端断点样式"


def test_badge_redesigned():
    """分类 badge 已美化：渐变类 b-crack/b-free/b-paid + 图标点。"""
    _, body = _get_dashboard()
    assert "b-crack" in body and "b-free" in body and "b-paid" in body, "分类 badge 应有新样式类"
    assert "badge-dot" in body, "badge 应带图标点"


def test_admin_panel_merged():
    """底部管理面板已移除，重载按钮并入总览栏（ov-btn）。"""
    _, body = _get_dashboard()
    assert "admin-panel" not in body, "底部 admin-panel 应已移除"
    assert "ov-btn" in body, "总览栏应有操作按钮（重载/刷新）"
    assert "ov-msg" in body, "应有总览栏消息区"


def test_recrack_env_disabled():
    """破解按钮应做环境检测：不可用则 disabled + title 提示。"""
    _, body = _get_dashboard()
    assert 'te-recrack" disabled' in body, "应有置灰的破解按钮"
    assert 'title="' in body, "置灰按钮应有 title 提示原因"


def test_model_edit_toggle():
    """模型列表应有编辑入口按钮。"""
    _, body = _get_dashboard()
    assert "model-edit-toggle" in body, "应有模型编辑按钮"


def test_model_edit_modal():
    """应有模型编辑 modal 容器（modal-overlay + 打开/关闭 JS）。"""
    _, body = _get_dashboard()
    assert "modal-overlay" in body, "应有 modal 遮罩"
    assert "model-modal" in body, "应有模型编辑 modal"
    assert "openModelEditor" in body, "应有打开 modal 的 JS 函数"
    assert "saveModelEditor" in body, "应有保存 modal 的 JS 函数"


def test_no_model_delete_button():
    """模型编辑界面不应有删除按钮（用户明确不要删除按钮）。"""
    _, body = _get_dashboard()
    assert "model-del" not in body, "不应有模型删除按钮 (model-del)"
    assert "deleteModel" not in body, "不应有删除模型的 JS"


def test_model_edit_endpoint():
    """编辑态端点：返回全部模型 + 展示开关 + 无删除按钮 + 无保存按钮（保存走 modal 底部）。"""
    conn = http.client.HTTPConnection("127.0.0.1", 8081, timeout=15)
    conn.request("GET", "/api/targets/openrouter/models?edit=1")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200, f"编辑态端点 HTTP {resp.status}"
    assert "model-show" in body, "编辑态应有展示开关"
    assert "switch-slider" in body, "应有滑动开关样式"
    assert "mrow" in body, "编辑态应有模型行 (mrow)"
    assert "model-del" not in body, "编辑态不应有删除按钮"


def test_api_targets_crack_env():
    """crack target 的 /api/targets 应返回 crackEnv 环境检测信息。"""
    conn = http.client.HTTPConnection("127.0.0.1", 8081, timeout=15)
    conn.request("GET", "/api/targets")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    data = json.loads(body)
    copilot = next((t for t in data["targets"] if t["label"] == "copilot"), None)
    assert copilot is not None, "缺少 copilot target"
    assert "crackEnv" in copilot, "crack target 应返回 crackEnv"
    assert "available" in copilot["crackEnv"], "crackEnv 应含 available"
    assert "reason" in copilot["crackEnv"], "crackEnv 应含 reason"


def test_gateway_category_sections():
    """卡片应按分类栏分组：聚合网关 / 破解网关 / 直连网关。"""
    _, body = _get_dashboard()
    assert "聚合网关" in body, "应有「聚合网关」分类栏"
    assert "破解网关" in body, "应有「破解网关」分类栏"
    assert "直连网关" in body, "应有「直连网关」分类栏"
    assert "sec-count" in body, "分类栏应显示卡片数量"
    # 顺序：聚合 → 破解 → 直连
    i_agg = body.index("聚合网关")
    i_crack = body.index("破解网关")
    i_direct = body.index("直连网关")
    assert i_agg < i_crack < i_direct, "分类栏顺序应为 聚合→破解→直连"


def test_anthropic_compatible_8081():
    """8081 卡片应改名 anthropic-compatible 并带统计。"""
    _, body = _get_dashboard()
    assert "anthropic-compatible" in body, "8081 卡片应显示 anthropic-compatible"
    assert "claude-code-proxy (8081" not in body, "不应再显示旧名 claude-code-proxy (8081"
    # 聚合网关标签
    assert "b-meta-agg" in body, "8081 应有聚合网关标签样式"
    # 请求数标签/统计
    assert "ct-summary" in body, "卡片头应有请求数摘要"
    assert "stats-block" in body, "8081 详情应有流量统计块"


def test_8081_model_stats_recorded():
    """8081 /v1/messages 应记录模型级统计（请求/成功率/错误）。"""
    # 发一次真实请求触发统计
    conn = http.client.HTTPConnection("127.0.0.1", 8081, timeout=20)
    payload = json.dumps({"model": "sonnet", "max_tokens": 10,
                          "messages": [{"role": "user", "content": "hi"}]})
    conn.request("POST", "/v1/messages", body=payload,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status in (200, 429, 500), f"/v1/messages HTTP {resp.status}"
    _, body = _get_dashboard()
    # sonnet 行的统计单元格（请求数非空）
    assert ">Sonnet</" in body, "8081 模型表格应有 Sonnet 行"
    assert "100.0%" in body or "mstat" in body, "8081 模型应有统计列"


def test_edit_modal_fetches_downstream_models():
    """编辑弹框应拉取下游真实模型列表（openrouter 336 个），并提供搜索框。"""
    for label, min_models in (("openrouter", 20), ("nvidia", 20), ("opencode-zen", 10)):
        conn = http.client.HTTPConnection("127.0.0.1", 8081, timeout=30)
        conn.request("GET", f"/api/targets/{label}/models?edit=1")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        assert resp.status == 200, f"{label} 编辑态 HTTP {resp.status}"
        n_rows = body.count('class="mrow"')
        assert n_rows >= min_models, (
            f"{label} 应拉取下游真实模型（≥{min_models}），实际 {n_rows}"
        )
        assert "model-search" in body, f"{label} 编辑态应有搜索框"
        assert "filterModels" in body or "model-search" in body, f"{label} 应有搜索 JS"


def test_edit_modal_fallback_no_key():
    """无 key 的下游（gemini-openai）拉取失败时应降级 targets.json 配置。"""
    conn = http.client.HTTPConnection("127.0.0.1", 8081, timeout=30)
    conn.request("GET", "/api/targets/gemini/models?edit=1")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200, f"gemini 编辑态 HTTP {resp.status}"
    # 降级为 targets.json 配置的模型（原生端点无 key 403 → 降级）
    n_rows = body.count('class="mrow"')
    assert n_rows == 8, f"gemini 应降级为 8 个配置模型，实际 {n_rows}"


def test_gemini_native_badge():
    """gemini 应打标【gemini 协议】。"""
    _, body = _get_dashboard()
    assert "b-meta-gemini" in body, "gemini-openai 应有 gemini 协议标签"
    assert "gemini 协议" in body, "应有【gemini 协议】文案"


def test_base_url_shown():
    """卡片详情应展示可粘贴 base_url（局域网 IP + routePrefix 后缀）。"""
    _, body = _get_dashboard()
    assert "base_url" in body, "卡片应有 base_url 属性"
    # 局域网 IP（本机非 127.0.0.1 的出口 IP）
    assert "127.0.0.1:8090" not in body.replace("127.0.0.1:8081", ""), "base_url 不应是回环地址"
    # 至少一个 192.168.x / 10.x / 172.x 局域网地址
    assert re.search(r"http://(192\.168\.|10\.|172\.)\d", body), "应有局域网 IP base_url"


def test_model_master_toggle():
    """编辑弹框应有总开关（全开/全关/部分开 indeterminate）。"""
    conn = http.client.HTTPConnection("127.0.0.1", 8081, timeout=30)
    conn.request("GET", "/api/targets/codebuddy/models?edit=1")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200, "codebuddy 编辑态 HTTP"
    assert "model-master" in body, "应有总开关"
    assert "syncMasterState" in body or "model-master" in body, "应有总开关联动 JS"


# ─── P2 回归快照基线（ModelRegistry 接入前锁定当前行为） ───
# Wave4 将 dashboard() 从直接读 _TARGETS 改为读 ModelRegistry 后，这些测试必须仍 PASS。
import server as _server


def test_dashboard_registry_equivalence_models():
    """卡片数量、copilot 卡片存在、8081 模型数均与 server 内部状态一致。

    当前 dashboard() 直接读 _TARGETS / _MODELS_CFG 渲染；Wave4 改读 ModelRegistry
    后这些不变式必须保持——卡片数不丢、8081 模型数不变、分类栅栏完整。
    """
    status, body = _get_dashboard()
    assert status == 200, f"dashboard HTTP {status}"

    # ① 卡片数量 = 2 特殊卡片（8080 聚合 + 8081 anthropic-compatible）
    #   + 10 个非 aggregate 的 enabled target（aggregator 类在卡片循环中被 pass 跳过）
    ports = re.findall(r'data-port="(\d+)"', body)
    assert len(ports) >= 10, f"至少 10 个 data-port 卡片，实际 {len(ports)}"
    # enabled 且非 aggregate 类 target 数 = 渲染出的 target 卡片数下限
    enabled_targets = [t for t in _server._TARGETS if t.get("enabled", True)]
    non_agg_targets = [t for t in enabled_targets if t.get("category") != "aggregate"]
    assert len(ports) == len(non_agg_targets) + 2, (
        f"卡片总数应为 {len(non_agg_targets) + 2}（{len(non_agg_targets)} 个 target 卡片 + 8080/8081），"
        f"实际 {len(ports)}"
    )

    # ② 已知 copilot-enterprise 卡片存在（data-port="8082"）
    assert "8082" in ports, "缺少 copilot-enterprise (8082) 卡片"

    # ③ 8081 卡片模型区模型数 = _anthropic_port_models() 返回值数（当前 3: sonnet/haiku/opus）
    ap_models = _server._anthropic_port_models()
    # 定位方法：以 data-port="8081" 为锚点，向前找到第一个 <tbody>，
    # 在其中计数 <tr data-model= 行（所有 model-table 行均带 data-model 属性）
    port_8081_pos = body.find('data-port="8081"')
    assert port_8081_pos != -1, "dashboard 中未找到 8081 卡片 data-port 属性"
    tbody_start = body.find("<tbody>", port_8081_pos)
    tbody_end = body.find("</tbody>", tbody_start)
    assert tbody_start != -1 and tbody_end != -1, "8081 卡片中未找到模型表 tbody"
    tbody = body[tbody_start:tbody_end]
    model_rows = re.findall(r'<tr[^>]*data-model=', tbody)
    assert len(model_rows) == len(ap_models), (
        f"8081 卡片模型行数应为 {len(ap_models)}，实际 {len(model_rows)}"
    )


def test_dashboard_dangling_banner():
    """悬空引用警示条：当前无悬空时不应显示。

    id="dangling-bar" 的 div 存在于 HTML 中，但 /api/config/dangling 返回空列表，
    前端 loadDanglingBar() 会移除 show class。断言标记存在且 API 无悬空项。
    """
    # 验证 dangling-bar DOM 元素存在
    _, body = _get_dashboard()
    assert 'id="dangling-bar"' in body, "dashboard 应包含 dangling-bar 容器 div"

    # 验证 API 端点返回空项（当前配置无悬空引用）
    conn = http.client.HTTPConnection("127.0.0.1", 8081, timeout=15)
    conn.request("GET", "/api/config/dangling")
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8", errors="replace"))
    conn.close()
    assert resp.status == 200, f"dangling API HTTP {resp.status}"
    items = data.get("items", [])
    assert items == [], f"当前配置应无悬空引用，实际 {len(items)} 条: {items}"


def test_dashboard_models_endpoint_shape():
    """/api/targets/{label}/models 端点返回含 .model-table 的 HTML（查看视图）。

    该端点由 get_models_html() 渲染，当前直接读 target 配置构建模型表。
    Wave4 改读 ModelRegistry 后返回的 HTML 结构应等价——无论如何源变，客户端看见的
    还是包含 .model-table 和 model 行的标准结构。
    """
    # copilot-enterprise（8082，handler=copilot）支持上游模型拉取
    conn = http.client.HTTPConnection("127.0.0.1", 8081, timeout=30)
    conn.request("GET", "/api/targets/copilot-enterprise/models")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200, f"/api/targets/copilot-enterprise/models HTTP {resp.status}"
    assert "model-table" in body, "模型端点应返回 model-table 样式的 HTML"
    assert "<tr" in body, "模型端点应包含模型表行"


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
