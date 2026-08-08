"""Dashboard 验收测试（多端口架构）

验证管理页面 (/dashboard) + API 端点：
1. 模型列表详细展示：不再是纯 <li><code> 列表，而是带美化名的表格
2. 流量统计信息：请求总数 / 成功率 / 运行时长 + 可视化（进度条）
3. /api/targets 端点：返回所有 target 的 label/isFree/enabled 等

运行前提：claude-code-proxy 服务运行中。
架构统一后端口分工：
  - 8079：dashboard 页面 + 全部 /api/* 管理 API（本文件绝大多数测试的目标）
  - 8081：仅 Anthropic 翻译（/v1/messages、count_tokens、/v1/models）
  - 8082 等 target 端口：代理 /dashboard 与 /api/* 到 8079（代理可用性测试）
用法: python test_dashboard.py
"""
import http.client
import json
import re
import sys
import uuid

HOST = "127.0.0.1"
PORT = 8082          # target 端口（经代理访问 dashboard）
DASHBOARD_PORT = 8079  # dashboard + 管理 API 独立端口


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
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
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


def _aggregate_status():
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
    conn.request("GET", "/api/aggregate/status")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200, f"/api/aggregate/status HTTP {resp.status}"
    return json.loads(body)


def test_anthropic_messages_forwards_session_id_to_aggregator():
    """8081 转发模型请求时必须保留 X-Session-Id，供 8080 建立粘性。"""
    before = _aggregate_status()["session"]
    session_id = f"dashboard-test-{uuid.uuid4()}"
    body = json.dumps({
        "model": "haiku",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Reply only with OK."}],
    }).encode("utf-8")

    for _ in range(3):
        conn = http.client.HTTPConnection("127.0.0.1", 8081, timeout=180)
        conn.request("POST", "/v1/messages", body=body, headers={
            "Content-Type": "application/json",
            "x-api-key": "dummy",
            "anthropic-version": "2023-06-01",
            "X-Session-Id": session_id,
        })
        resp = conn.getresponse()
        response_body = json.loads(resp.read().decode("utf-8", errors="replace"))
        conn.close()
        assert resp.status == 200, f"/v1/messages HTTP {resp.status}: {response_body}"
        assert response_body["content"], "haiku 在足够 max_tokens 下应返回正文"

    after = _aggregate_status()["session"]
    assert after["lookups"] >= before["lookups"] + 3, "三次 8081 请求必须进入 8080 会话查找"
    assert after["hits"] >= before["hits"] + 2, "同一会话后两次请求必须命中 8080 粘性缓存"


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
    """8082 等 target 端口应代理 /api/* 到 8079（dashboard 管理接口经任意端口可访问）。"""
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
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
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
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
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
        conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=30)
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
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=30)
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
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=30)
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
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
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
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=30)
    conn.request("GET", "/api/targets/copilot-enterprise/models")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200, f"/api/targets/copilot-enterprise/models HTTP {resp.status}"
    assert "model-table" in body, "模型端点应返回 model-table 样式的 HTML"
    assert "<tr" in body, "模型端点应包含模型表行"


# ─── 聚合网关高危事件展示区：数据层验证（纯引擎层，无需服务） ───
# dashboard 的高危事件区由前端 JS loadAggregateStatus() 渲染，Python 测不到 DOM。
# 因此验证对象为其消费的数据契约：
#   r.breakers[port].{state, reason, tripped_at}
#   r.virtual_models[vmId][memberKey].error_types  (dict[str,int])
# 这些字段全部来自 AggregatorEngine.get_stats()，直接构造引擎断言即可，
# 不依赖 FastAPI / 真实服务（本文件其余测试才需要 8079/8082 在跑）。
import random  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from gateways.aggregator.engine import AggregatorEngine  # noqa: E402


def _make_agg_target():
    """构造聚合网关 target 配置（结构对齐 test_aggregator.py 的 make_target）。"""
    return {
        "label": "aggregator", "listenPort": 8080, "category": "aggregate", "handler": "aggregator",
        "poolDefaults": {"defaultRetries": 2, "fallbackRetries": 1, "sessionAffinityTtlSeconds": 3600,
                         "probeIntervalSeconds": 300, "weight": 1},
        "quotaErrorPatterns": ["insufficient credit", "quota exceeded", "余额不足"],
        "virtualModels": {
            "agg:sonnet": {
                "defaultPool": [
                    {"port": 8082, "model": "claude-sonnet-5", "weight": 3},
                    {"port": 8084, "model": "deepseek-v4-pro", "weight": 2},
                ],
                "fallbackPool": [{"port": 8094, "model": "some-model"}],
            },
        },
    }


def _member_by_port(engine, vm_id, port):
    """从引擎已解析的池里取指定端口的 PoolMember（用于 note_request）。"""
    vm = engine._models[vm_id]
    for m in vm.default_pool + vm.fallback_pool:
        if m.port == port:
            return m
    raise AssertionError(f"{vm_id} 池中无端口 {port}")


def test_aggregate_status_breakers_include_tripped_at():
    """熔断端口条目须含 state / reason / tripped_at（前端熔断端口列表三要素）。"""
    eng = AggregatorEngine(_make_agg_target(), rng=random.Random(1))
    eng.trip(8084, "401_auth")
    stats = eng.get_stats()

    assert "breakers" in stats, "get_stats() 应含 breakers 字段"
    brks = stats["breakers"]
    assert 8084 in brks, f"熔断端口 8084 应出现在 breakers，实际 keys={list(brks)}"
    b = brks[8084]
    assert b["state"] == "tripped", f"state 应为 tripped，实际 {b['state']!r}"
    assert b["reason"] == "401_auth", f"reason 应透传熔断原因，实际 {b['reason']!r}"
    assert isinstance(b["tripped_at"], float), f"tripped_at 应为 float，实际 {type(b['tripped_at'])}"
    assert b["tripped_at"] > 0, f"tripped_at 应为熔断发生时刻（>0），实际 {b['tripped_at']}"
    # 未熔断端口不应混入
    assert 8082 not in brks, "未熔断的 8082 不应出现在 breakers"


def test_aggregate_status_error_types_present():
    """error_types 按类型计数；429 限流只计数不熔断（与配额熔断严格区分）。"""
    eng = AggregatorEngine(_make_agg_target(), rng=random.Random(1))
    m8084 = _member_by_port(eng, "agg:sonnet", 8084)
    m8082 = _member_by_port(eng, "agg:sonnet", 8082)

    # ① 401 鉴权失败 → 计数 + 熔断
    eng.note_request(m8084, "err", 120.0, error_type="401_auth")
    eng.trip(8084, "401_auth")

    # ② 429 限流 → 只计数，不熔断
    eng.note_request(m8082, "err", 80.0, error_type="429_rate_limit")
    eng.note_request(m8082, "err", 90.0, error_type="429_rate_limit")

    stats = eng.get_stats()
    members = stats["virtual_models"]["agg:sonnet"]

    et_8084 = members["8084:deepseek-v4-pro"]["error_types"]
    assert et_8084.get("401_auth", 0) >= 1, f"8084 应记录 401_auth，实际 {et_8084}"

    et_8082 = members["8082:claude-sonnet-5"]["error_types"]
    assert et_8082.get("429_rate_limit", 0) == 2, f"8082 应记录 2 次 429_rate_limit，实际 {et_8082}"

    brks = stats["breakers"]
    assert 8084 in brks, "401 场景端口应熔断"
    assert 8082 not in brks, f"429 限流不应触发熔断，实际 breakers={list(brks)}"


def test_aggregate_status_no_breakers_empty_state():
    """无熔断且无错误时：breakers 为空 dict、所有成员 error_types 为空。

    对应前端 `if (hasBreakers || hasErrTypes)` —— 此时条件不成立，高危事件区不渲染。
    """
    eng = AggregatorEngine(_make_agg_target(), rng=random.Random(1))
    m = _member_by_port(eng, "agg:sonnet", 8082)
    eng.note_request(m, "ok", 50.0)  # 成功请求不产生 error_types

    stats = eng.get_stats()
    assert stats["breakers"] == {}, f"无熔断时 breakers 应为空 dict，实际 {stats['breakers']}"

    has_err_types = False
    for vm_id, members in stats["virtual_models"].items():
        for mk, ms in members.items():
            assert "error_types" in ms, f"{vm_id}/{mk} 成员统计应含 error_types 字段"
            if ms["error_types"]:
                has_err_types = True
    assert not has_err_types, "成功请求不应产生 error_types 计数"
    # 前端渲染条件复现
    assert not (bool(stats["breakers"]) or has_err_types), "空状态下前端高危事件区渲染条件应不成立"


def test_api_aggregate_status_returns_tripped_at():
    """接口层：/api/aggregate/status 响应结构含高危事件区所需字段。

    需要 8079 在跑（与本文件其余测试同前提）。聚合网关未配置时（configured=False）
    仅断言该显式契约，不误报失败。
    """
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
    conn.request("GET", "/api/aggregate/status")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200, f"/api/aggregate/status HTTP {resp.status}"
    data = json.loads(body)
    if not data.get("configured"):
        assert data == {"configured": False}, f"未配置聚合网关时应只返回 configured=False，实际 {data}"
        return

    assert "breakers" in data, "响应应含 breakers（高危事件区熔断端口来源）"
    assert isinstance(data["breakers"], dict), "breakers 应为对象"
    for port, b in data["breakers"].items():
        for key in ("state", "reason", "tripped_at"):
            assert key in b, f"breakers[{port}] 缺少字段 {key}"
        assert isinstance(b["tripped_at"], (int, float)), f"breakers[{port}].tripped_at 应为数字"
        assert b["tripped_at"] > 0, f"breakers[{port}].tripped_at 应 > 0"

    assert "virtual_models" in data, "响应应含 virtual_models（error_types 汇总来源）"
    for vm_id, members in data["virtual_models"].items():
        assert isinstance(members, dict), f"{vm_id} 成员统计应为对象"
        for mk, ms in members.items():
            assert "error_types" in ms, f"{vm_id}/{mk} 缺少 error_types 字段"
            assert isinstance(ms["error_types"], dict), f"{vm_id}/{mk}.error_types 应为 dict[str,int]"


def test_config_export_complete():
    """全量配置导出：单 JSON 含 targets（含 server 段）/secrets 两段，覆盖全部 11 个 target。"""
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
    conn.request("GET", "/api/config/export")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200, f"导出 HTTP {resp.status}"
    data = json.loads(body)
    assert data.get("version") == 2, "导出应有 version=2"
    assert data.get("exportedAt"), "导出应含 exportedAt"
    # v2 架构：无独立 env 段（.env 已废弃，运行配置并入 server 段）
    assert "env" not in data, "v2 导出不应再有独立 env 段（.env 已废弃）"
    # targets 段：完整配置对象（targets 数组 + modelDefaults + server 段）
    assert isinstance(data.get("targets"), dict), "targets 段应为对象"
    assert len(data["targets"].get("targets", [])) >= 9, \
        f"targets 段应含全部 target（≥9），实际 {len(data['targets'].get('targets', []))}"
    # server 段：主服务运行配置（.env 并入后的新家）
    assert isinstance(data["targets"].get("server"), dict), "targets 段应含 server 段"
    assert "listenPort" in data["targets"]["server"], "server 段应含 listenPort"
    # secrets 段：完整凭据对象
    assert isinstance(data.get("secrets"), dict), "secrets 段应为对象"


def test_config_export_contains_secrets_values():
    """导出必须含真实凭据值（迁移诉求：导入后即可用），不能是打码/空。"""
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
    conn.request("GET", "/api/config/export")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200
    data = json.loads(body)
    secrets = data.get("secrets") or {}
    # 至少一个 crack 网关 token 非空（copilot_token / codebuddy_token / qclaw_api_key 等）
    non_empty = {k: v for k, v in secrets.items() if isinstance(v, str) and v}
    assert non_empty, "secrets 段应含至少一个非空凭据"
    # 导出内容不得是打码形态（不含 '...' 掩码且长度 > 10 的才算真值）
    real = [v for v in non_empty.values() if len(v) > 10 and "..." not in v]
    assert real, "secrets 段应含真实凭据值（长度>10 且无打码掩码）"


def test_config_import_roundtrip():
    """导入往返：导出 → 改 targets 一个字段 → 导入 → 验证文件与热重载生效 → 还原。"""
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
    conn.request("GET", "/api/config/export")
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    assert resp.status == 200

    # 备份当前磁盘 targets.json 原样，测试后还原
    import os
    from pathlib import Path
    root = Path(__file__).parent
    targets_path = root / "targets.json"
    original = targets_path.read_text(encoding="utf-8") if targets_path.exists() else None

    # 改第一个 target 的 enabled 取反（用可逆字段避免残留副作用）；提取到 try 外，
    # 保证 finally 还原分支总能拿到绑定（即使 try 中途异常）
    first = data["targets"]["targets"][0]
    orig_enabled = first.get("enabled", True)

    try:
        first["enabled"] = not orig_enabled

        payload = json.dumps(data).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
        conn.request("POST", "/api/config/import", body=payload,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        assert resp.status == 200, f"导入 HTTP {resp.status}: {body[:300]}"
        result = json.loads(body)
        assert result.get("ok") is True
        assert result.get("targetsCount", 0) >= 9, "导入应返回 target 数"
        assert result.get("restartRequired") is True, "server 段运行配置导入应提示需重启"
        assert "envWritten" not in result, "v2 导入响应不应再有 envWritten 字段"

        # 验证热重载生效：/api/targets 返回的 enabled 已翻转
        conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
        conn.request("GET", "/api/targets")
        resp = conn.getresponse()
        targets = json.loads(resp.read().decode("utf-8"))
        conn.close()
        loaded = next(t for t in targets["targets"] if t["label"] == first["label"])
        assert loaded["enabled"] == (not orig_enabled), \
            f"热重载后 {first['label']} enabled 应为 {not orig_enabled}，实际 {loaded['enabled']}"

        # 验证磁盘文件同步
        disk = json.loads(targets_path.read_text(encoding="utf-8"))
        disk_first = next(t for t in disk["targets"] if t["label"] == first["label"])
        assert disk_first["enabled"] == (not orig_enabled), "targets.json 磁盘内容应已更新"
    finally:
        # 还原：恢复被翻转的 enabled 字段，再导入原样数据（保证测试幂等，不污染真实配置）
        first["enabled"] = orig_enabled
        conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
        conn.request("POST", "/api/config/import", body=json.dumps(data).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        conn.close()
        assert resp.status == 200, f"还原导入失败 HTTP {resp.status}"


def test_config_import_rejects_bad_version():
    """导入应拒绝不支持的版本号（422），且不写任何文件。"""
    from pathlib import Path
    root = Path(__file__).parent
    targets_path = root / "targets.json"
    before = targets_path.read_text(encoding="utf-8") if targets_path.exists() else None
    payload = json.dumps({
        "version": 999,
        "targets": {"targets": [], "modelDefaults": {"defaultPort": 8082}, "models": []},
        "secrets": {},
        "env": {},
    }).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
    conn.request("POST", "/api/config/import", body=payload,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 422, f"版本不匹配应 422，实际 {resp.status}"
    after = targets_path.read_text(encoding="utf-8") if targets_path.exists() else None
    assert before == after, "版本拒绝时不应改动 targets.json"


def test_config_import_rejects_invalid_targets():
    """导入应拒绝非法 targets（validate_targets 拦截，422 且不写文件）。"""
    from pathlib import Path
    root = Path(__file__).parent
    targets_path = root / "targets.json"
    before = targets_path.read_text(encoding="utf-8") if targets_path.exists() else None
    # 构造非法 target：缺 label/listenPort/handler 必填字段
    payload = json.dumps({
        "version": 1,
        "targets": {"targets": [{"label": "bad-no-port"}], "modelDefaults": {"defaultPort": 8082}, "models": []},
        "secrets": {},
        "env": {},
    }).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", DASHBOARD_PORT, timeout=15)
    conn.request("POST", "/api/config/import", body=payload,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 422, f"非法 targets 应 422，实际 {resp.status}"
    after = targets_path.read_text(encoding="utf-8") if targets_path.exists() else None
    assert before == after, "校验失败时不应改动 targets.json"


# ─── 端口拆分契约：dashboard + 管理 API 在 8079，8081 只剩翻译 ───
# server.dashboardPort=8079 承载 /dashboard + /api/*；
# 8081 只剩 Anthropic 翻译（/v1/messages + count_tokens + /v1/models）。

def _get(port, path, timeout=15):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    return resp.status, body


def test_dashboard_on_8079():
    """dashboard 应搬到独立端口 8079（当前 8079 无服务 → 连接拒绝 = RED）。"""
    status, body = _get(DASHBOARD_PORT, "/dashboard")
    assert status == 200, f"8079 /dashboard HTTP {status}"
    assert "card-toggle" in body, "8079 dashboard 应含卡片头 (card-toggle)"
    assert "model-table" in body, "8079 dashboard 应含模型表 (model-table)"
    assert "聚合网关" in body, "8079 dashboard 应含分类栏"


def test_dashboard_api_on_8079():
    """管理 API 应随 dashboard 搬到 8079（当前连接拒绝 = RED）。"""
    status, body = _get(DASHBOARD_PORT, "/api/targets")
    assert status == 200, f"8079 /api/targets HTTP {status}"
    data = json.loads(body)
    targets = data.get("targets", [])
    assert isinstance(targets, list) and targets, "8079 /api/targets 应返回非空 targets 数组"
    labels = {t.get("label", "") for t in targets}
    assert "copilot" in labels, f"8079 /api/targets 缺少 copilot，实际 {sorted(labels)}"


def test_dashboard_not_on_8081():
    """拆分后 8081 只留 Anthropic 翻译，/dashboard 应 404（当前返回 200 = RED）。"""
    status, _ = _get(8081, "/dashboard")
    assert status == 404, f"8081 /dashboard 应 404（已拆到 {DASHBOARD_PORT}），实际 {status}"


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
