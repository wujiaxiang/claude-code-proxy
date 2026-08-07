"""
config_store 单元测试（targets.json / secrets.json 加载、迁移、校验、打码、解析）。
用法: python test_targets_schema.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config_store
from config_store import validate_targets

passed = 0
failed = 0

# ─── 旧格式迁移 ───
def test_migrate_old_array_format():
    """旧 targets.json（数组）应迁移为 {modelDefaults, targets: [...]}。"""
    old = [
        {"label": "openrouter", "listenPort": 8090, "targetHost": "openrouter.ai",
         "targetPort": 443, "targetProtocol": "https", "routePrefix": "/api/v1", "models": []},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(old, f)
        p = Path(f.name)
    try:
        cfg = config_store.load_targets(p)
        assert cfg["modelDefaults"]["defaultPort"] == 8082, "默认转发端口应为 8082"
        assert isinstance(cfg["targets"], list) and len(cfg["targets"]) == 1
        t = cfg["targets"][0]
        assert t["category"] == "free", "旧条目默认 category 应为 free"
        assert t["handler"] == "passthrough", "旧条目默认 handler 应为 passthrough"
        assert t.get("isFree") is True, "旧 free 条目默认 isFree=true"
        assert "enabled" not in t or t["enabled"] is True, "旧条目默认 enabled"
    finally:
        p.unlink(missing_ok=True)


def test_load_new_object_format():
    """新格式（顶层对象）原样加载。"""
    new = {"modelDefaults": {"defaultPort": 8085}, "targets": [
        {"label": "qclaw", "listenPort": 8085, "category": "crack", "handler": "qclaw",
         "targetHost": "mmgrcalltoken.3g.qq.com", "targetPort": 443, "targetProtocol": "https",
         "routePrefix": "/aizone/v1", "crackTool": "crack_qclaw.py", "secretRef": "qclaw_api_key",
         "models": []},
    ], "models": [{"name": "sonnet", "aliases": [], "target": {"port": 8082, "model": "claude-sonnet"}}]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(new, f)
        p = Path(f.name)
    try:
        cfg = config_store.load_targets(p)
        assert cfg["modelDefaults"]["defaultPort"] == 8085
        assert cfg["targets"][0]["label"] == "qclaw"
        assert len(cfg["models"]) == 1 and cfg["models"][0]["name"] == "sonnet"
    finally:
        p.unlink(missing_ok=True)


# ─── 校验 ───
def test_validate_duplicate_labels():
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "a", "listenPort": 8082, "category": "crack", "handler": "passthrough", "targetHost": "x.com", "models": []},
        {"label": "a", "listenPort": 8083, "category": "free", "handler": "passthrough", "targetHost": "y.com", "models": []},
    ]}
    errors = config_store.validate_targets(cfg)
    assert any("label" in e["msg"] and "a" in e["msg"] for e in errors), f"应报重复 label，实际: {errors}"


def test_validate_duplicate_ports():
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "a", "listenPort": 8082, "category": "crack", "handler": "passthrough", "targetHost": "x.com", "models": []},
        {"label": "b", "listenPort": 8082, "category": "free", "handler": "passthrough", "targetHost": "y.com", "models": []},
    ]}
    errors = config_store.validate_targets(cfg)
    assert any("端口" in e["msg"] or "port" in e["msg"].lower() for e in errors), f"应报重复端口，实际: {errors}"


def test_validate_invalid_category():
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "a", "listenPort": 8082, "category": "hack", "handler": "passthrough", "targetHost": "x.com", "models": []},
    ]}
    errors = config_store.validate_targets(cfg)
    assert any("category" in e["msg"] for e in errors), f"应报非法 category，实际: {errors}"


def test_validate_missing_fields():
    cfg = {"anthropicForwardPort": 8082, "targets": [{"label": "a"}]}
    errors = config_store.validate_targets(cfg)
    assert len(errors) >= 1, "缺字段应报错"


def test_validate_clean_config_passes():
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "openrouter", "listenPort": 8090, "category": "free", "handler": "passthrough",
         "isFree": True, "targetHost": "openrouter.ai", "targetPort": 443,
         "targetProtocol": "https", "routePrefix": "/api/v1", "models": []},
    ]}
    errors = config_store.validate_targets(cfg)
    assert errors == [], f"合法配置不应有错误，实际: {errors}"


# ─── 聚合网关（aggregator）校验 ───
def test_validate_aggregator_target():
    """合法聚合 target：无 targetHost + 有效 virtualModels → 校验通过。"""
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "aggregator", "listenPort": 8080, "category": "aggregate", "handler": "aggregator",
         "enabled": True,
         "poolDefaults": {"defaultRetries": 2, "fallbackRetries": 1, "sessionAffinityTtlSeconds": 3600, "probeIntervalSeconds": 300},
         "quotaErrorPatterns": ["insufficient credit", "quota exceeded", "余额不足"],
         "virtualModels": {
             "agg:sonnet": {"defaultPool": [{"port": 8082, "model": "claude-sonnet-5", "weight": 3}, {"port": 8084, "model": "deepseek-v4-pro"}],
                            "fallbackPool": [{"port": 8090, "model": "openrouter/auto"}], "defaultRetries": 3, "fallbackRetries": 1},
             "agg:haiku": {"defaultPool": [{"port": 8082, "model": "claude-haiku-4.5"}], "fallbackPool": []}
         }}
    ]}
    errors = config_store.validate_targets(cfg)
    assert errors == [], f"合法聚合配置不应有错误，实际: {errors}"


def test_validate_aggregator_missing_virtual_models():
    """聚合 target 缺 virtualModels → 报错（含 label）。"""
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "aggregator", "listenPort": 8080, "category": "aggregate", "handler": "aggregator"},
    ]}
    errors = config_store.validate_targets(cfg)
    assert any("aggregator" in e["msg"] and "virtualModels" in e["msg"] for e in errors), f"应报缺少 virtualModels，实际: {errors}"


def test_validate_aggregator_empty_virtual_models():
    """聚合 target 的 virtualModels 为空 dict → 报错。"""
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "aggregator", "listenPort": 8080, "category": "aggregate", "handler": "aggregator",
         "virtualModels": {}},
    ]}
    errors = config_store.validate_targets(cfg)
    assert any("aggregator" in e["msg"] and "virtualModels" in e["msg"] for e in errors), f"应报空 virtualModels，实际: {errors}"


def test_validate_aggregator_empty_default_pool():
    """虚拟模型条目 defaultPool 为空 list → 报错（含虚拟模型 id）。"""
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "aggregator", "listenPort": 8080, "category": "aggregate", "handler": "aggregator",
         "virtualModels": {"agg:sonnet": {"defaultPool": []}}},
    ]}
    errors = config_store.validate_targets(cfg)
    assert any("agg:sonnet" in e["msg"] and "defaultPool" in e["msg"] for e in errors), f"应报 defaultPool 为空，实际: {errors}"


def test_validate_aggregator_bad_pool_member():
    """池成员缺 port/model → 报错。"""
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "aggregator", "listenPort": 8080, "category": "aggregate", "handler": "aggregator",
         "virtualModels": {"agg:sonnet": {"defaultPool": [{"port": 8082}]}}},
    ]}
    errors = config_store.validate_targets(cfg)
    assert any("agg:sonnet" in e["msg"] and "model" in e["msg"] for e in errors), f"应报成员缺 model，实际: {errors}"


def test_validate_aggregator_bad_weight():
    """池成员 weight 为负数 → 报错。"""
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "aggregator", "listenPort": 8080, "category": "aggregate", "handler": "aggregator",
         "virtualModels": {"agg:sonnet": {"defaultPool": [{"port": 8082, "model": "m", "weight": -1}]}}},
    ]}
    errors = config_store.validate_targets(cfg)
    assert any("agg:sonnet" in e["msg"] and "weight" in e["msg"] for e in errors), f"应报 weight 非法，实际: {errors}"


# ─── models / modelDefaults 顶层配置校验 ───
def test_validate_models_valid():
    """合法 modelDefaults + models（无重复别名）→ 校验通过。"""
    cfg = {"targets": [], "modelDefaults": {"defaultPort": 8082},
           "models": [{"name": "sonnet", "aliases": [], "target": {"port": 8080, "model": "agg:sonnet"}},
                      {"name": "haiku", "aliases": ["claude-haiku"], "target": {"port": 8082, "model": "claude-haiku-4.5"}}]}
    errors = config_store.validate_targets(cfg)
    assert errors == [], f"合法配置不应有错误，实际: {errors}"


def test_validate_models_bad_default_port():
    """modelDefaults.defaultPort 为负数 → 报错。"""
    cfg = {"targets": [], "modelDefaults": {"defaultPort": -1}, "models": []}
    errors = config_store.validate_targets(cfg)
    assert any("defaultPort" in e["msg"] for e in errors), f"应报 defaultPort 非法，实际: {errors}"


def test_validate_models_missing_field():
    """models[] 记录缺 target.model → 报错（含索引）。"""
    cfg = {"targets": [], "modelDefaults": {"defaultPort": 8082},
           "models": [{"name": "a", "aliases": [], "target": {"port": 8082}}]}
    errors = config_store.validate_targets(cfg)
    assert any(e["path"] == "models[0].target.model" for e in errors), f"应报缺 target.model，实际: {errors}"


def test_validate_models_duplicate_alias():
    """两条记录别名重复 → 报错（含重复字符串）。"""
    cfg = {"targets": [], "modelDefaults": {"defaultPort": 8082},
           "models": [{"name": "a", "aliases": ["dup"], "target": {"port": 8082, "model": "x"}},
                      {"name": "b", "aliases": ["dup"], "target": {"port": 8083, "model": "y"}}]}
    errors = config_store.validate_targets(cfg)
    assert any("dup" in e["msg"] for e in errors), f"应报重复别名，实际: {errors}"


# ─── P2 结构化错误（RED：validate_targets 当前返回 str，重构后应返回 {"path","msg"}） ───
def _assert_structured(errors, ctx=""):
    """严格判定每个元素是 dict 且含 path/msg 两键。字符串元素必然失败（防误 GREEN）。"""
    assert isinstance(errors, list), f"{ctx}: 返回值应为 list，实际 {type(errors).__name__}"
    assert errors, f"{ctx}: 应至少报一个错误，实际为空"
    for e in errors:
        assert isinstance(e, dict), f"{ctx}: 元素应为 dict（结构化错误），实际 {type(e).__name__}: {e!r}"
        assert "path" in e and "msg" in e, f"{ctx}: 元素应含 path/msg 两键，实际 keys={sorted(e)}"
        assert isinstance(e["path"], str) and isinstance(e["msg"], str), \
            f"{ctx}: path/msg 应为 str，实际 {e!r}"


def _paths(errors):
    """提取 path 集合；非 dict 元素直接跳过，保证字符串返回值无法误命中。"""
    return {e["path"] for e in errors if isinstance(e, dict) and "path" in e}


def test_validate_targets_returns_structured():
    """缺 label 的 target → 返回 [{"path","msg"}] 结构化列表，而非字符串列表。"""
    cfg = {"targets": [
        {"listenPort": 8082, "category": "crack", "handler": "passthrough",
         "targetHost": "x.com", "crackTool": "crack_x.py", "models": []},
    ]}
    errors = validate_targets(cfg)
    _assert_structured(errors, "缺 label")


def test_validate_targets_path_addressing():
    """3 个 target，index 2 缺 label → 存在 path == 'targets[2].label' 的错误项。"""
    ok = {"category": "free", "handler": "passthrough", "targetHost": "x.com", "models": []}
    cfg = {"targets": [
        {"label": "a", "listenPort": 8090, **ok},
        {"label": "b", "listenPort": 8091, **ok},
        {"listenPort": 8092, **ok},  # index 2 缺 label
    ]}
    errors = validate_targets(cfg)
    _assert_structured(errors, "targets[2] 缺 label")
    assert "targets[2].label" in _paths(errors), \
        f"应有 path='targets[2].label'，实际 paths={sorted(_paths(errors))}"


def test_validate_targets_model_path():
    """models[1] 非 dict → 错误 path 定位到 models[1]（含子路径前缀亦可）。"""
    cfg = {"targets": [], "modelDefaults": {"defaultPort": 8082},
           "models": [{"id": "a"}, "bad"]}
    errors = validate_targets(cfg)
    _assert_structured(errors, "models[1] 非 dict")
    assert any(p == "models[1]" or p.startswith("models[1].") for p in _paths(errors)), \
        f"应有 path 指向 models[1]，实际 paths={sorted(_paths(errors))}"


# ─── 顶层 server 段 schema ───
def _load_cfg_with(raw: dict) -> dict:
    """把 raw 写入临时 JSON 后 load_targets（绝不碰仓库 targets.json）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        p = Path(f.name)
    try:
        return config_store.load_targets(p)
    finally:
        p.unlink(missing_ok=True)


def test_server_section_defaults_when_absent():
    """缺失 server 段 → load_targets 返回完整默认（7 顶层键 + 关键默认值）。"""
    cfg = _load_cfg_with({"targets": [], "modelDefaults": {"defaultPort": 8082}, "models": []})
    srv = cfg["server"]
    assert set(srv) == set(config_store.DEFAULT_SERVER_CONFIG), \
        f"server 顶层键应与 DEFAULT_SERVER_CONFIG 一致，实际 {sorted(srv)}"
    assert len(srv) == 7, f"server 应有 7 个顶层键，实际 {len(srv)}: {sorted(srv)}"
    assert srv["listenPort"] == 8081, srv["listenPort"]
    assert srv["preferredProvider"] == "openai", srv["preferredProvider"]
    assert srv["log"]["file"] == "", repr(srv["log"]["file"])
    assert srv["log"]["debug"] is False, srv["log"]["debug"]
    assert srv["copilot"]["gheHost"] == "copilot-api.bmw.ghe.com", srv["copilot"]["gheHost"]
    assert "mmgrcalltoken" in srv["qclaw"]["baseUrl"], srv["qclaw"]["baseUrl"]
    assert srv["cache"]["enabled"] is True and srv["cache"]["maxSize"] == 500, srv["cache"]
    assert srv["legacyModels"] == {"big": "gpt-4.1", "medium": "gpt-4.1", "small": "gpt-4.1-mini"}, \
        srv["legacyModels"]


def test_server_section_deep_merge_log():
    """部分 server.log → 只覆盖 debug，其余 log 子键全默认，其他顶层段也全默认。"""
    cfg = _load_cfg_with({"targets": [], "server": {"log": {"debug": True}}})
    srv = cfg["server"]
    assert srv["log"]["debug"] is True, srv["log"]
    assert srv["log"]["file"] == "", srv["log"]
    assert srv["log"]["retentionDays"] == 7, srv["log"]
    assert srv["log"]["rotateWhen"] == "midnight", srv["log"]
    assert srv["log"]["rotateInterval"] == 1, srv["log"]
    assert srv["listenPort"] == 8081, "未提供的顶层标量应取默认"
    assert srv["cache"] == config_store.DEFAULT_SERVER_CONFIG["cache"], srv["cache"]


def test_server_section_deep_merge_copilot():
    """部分 server.copilot → 只覆盖 gheHost，其余 copilot 子键默认。"""
    cfg = _load_cfg_with({"targets": [], "server": {"copilot": {"gheHost": "x.example.com"}}})
    cop = cfg["server"]["copilot"]
    assert cop["gheHost"] == "x.example.com", cop
    assert cop["integrationId"] == "copilot-developer-cli", cop
    assert cop["bigModel"] == "claude-sonnet-4.6", cop
    assert cop["mediumModel"] == "claude-sonnet-4.6", cop
    assert cop["smallModel"] == "claude-haiku-4.5", cop
    assert cfg["server"]["log"] == config_store.DEFAULT_SERVER_CONFIG["log"], cfg["server"]["log"]


def test_validate_server_section_clean_passes():
    """合并出的默认 server 段 → 校验无错误。"""
    cfg = {"targets": [], "modelDefaults": {"defaultPort": 8082}, "models": [],
           "server": config_store._merge_server_config({})}
    errors = validate_targets(cfg)
    assert errors == [], f"合法 server 段不应有错误，实际: {errors}"


def _server_errors(server):
    """只带 server 段跑校验，返回结构化错误列表。"""
    return validate_targets({"targets": [], "models": [], "server": server})


def test_validate_server_type_errors():
    """server 段各类型错误 → path/msg 精确匹配。"""
    cases = [
        ({"cache": {"maxSize": "abc"}}, "server.cache.maxSize",
         "server.cache.maxSize must be a non-negative integer"),
        ({"listenPort": "abc"}, "server.listenPort",
         "server.listenPort must be a non-negative integer"),
        ({"log": {"debug": "yes"}}, "server.log.debug",
         "server.log.debug must be a boolean"),
        ("nope", "server", "server must be an object"),
    ]
    for server, want_path, want_msg in cases:
        errors = _server_errors(server)
        _assert_structured(errors, f"server={server!r}")
        assert want_path in _paths(errors), \
            f"server={server!r} 应报 path={want_path}，实际 {sorted(_paths(errors))}"
        assert any(e["path"] == want_path and e["msg"] == want_msg for e in errors), \
            f"server={server!r} 应报 msg={want_msg!r}，实际 {errors}"


def test_validate_server_preferred_provider():
    """server.preferredProvider 非法枚举 → 报错且 msg 列出合法值；合法值不报错。"""
    errors = _server_errors({"preferredProvider": "foo"})
    _assert_structured(errors, "preferredProvider=foo")
    assert "server.preferredProvider" in _paths(errors), sorted(_paths(errors))
    msg = next(e["msg"] for e in errors if e["path"] == "server.preferredProvider")
    assert msg.startswith("server.preferredProvider must be one of"), msg
    assert "openai" in msg and "anthropic" in msg, msg
    assert _server_errors({"preferredProvider": "openai"}) == [], "合法 provider 不应报错"


def test_validate_server_legacy_models_type_error():
    """server.legacyModels.big 为 int → 非空字符串错误；legacyModels 非 dict → object 错误。"""
    errors = _server_errors({"legacyModels": {"big": 123}})
    _assert_structured(errors, "legacyModels.big=123")
    assert any(e["path"] == "server.legacyModels.big"
               and e["msg"] == "server.legacyModels.big must be a non-empty string"
               for e in errors), errors
    errors2 = _server_errors({"legacyModels": "nope"})
    assert any(e["path"] == "server.legacyModels"
               and e["msg"] == "server.legacyModels must be an object"
               for e in errors2), errors2


def test_validate_server_listen_port_vs_target_port_current_behavior():
    """现状记录：validate_targets 只查 target 之间端口重复，不查与 server.listenPort 冲突。

    读 config_store.validate_targets 确认：ports dict 仅由 targets 填充，
    server.listenPort 未参与冲突检测。此测试锁定当前行为，若将来实现冲突校验会变红提醒。
    """
    tgt = {"category": "free", "handler": "passthrough", "targetHost": "x.com", "models": []}
    # 不冲突（target 8090 vs server 8081）→ 无错误
    no_conflict = {"targets": [{"label": "a", "listenPort": 8090, **tgt}], "models": [],
                   "server": {"listenPort": 8081}}
    assert validate_targets(no_conflict) == [], \
        f"不冲突时不应报错，实际: {validate_targets(no_conflict)}"
    # 冲突（target 8081 == server.listenPort 8081）→ 现状不报错
    conflict = {"targets": [{"label": "a", "listenPort": 8081, **tgt}], "models": [],
                "server": {"listenPort": 8081}}
    assert validate_targets(conflict) == [], \
        f"现状：server.listenPort 与 target 端口冲突不报错，实际: {validate_targets(conflict)}"
    # 对照：两个 target 端口重复仍报错（证明端口检测本身生效）
    dup = {"targets": [{"label": "a", "listenPort": 8081, **tgt},
                       {"label": "b", "listenPort": 8081, **tgt}], "models": []}
    assert any(e["path"] == "targets[1].listenPort" for e in validate_targets(dup)), \
        f"target 间端口重复应报错，实际: {validate_targets(dup)}"


# ─── secrets 读写与打码 ───
def test_secrets_roundtrip():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        p = Path(f.name)
    try:
        config_store.save_secrets({"copilot_token": "secret-abc"}, p)
        got = config_store.load_secrets(p)
        assert got == {"copilot_token": "secret-abc"}
    finally:
        p.unlink(missing_ok=True)


def test_load_secrets_missing_file():
    with tempfile.TemporaryDirectory() as d:
        got = config_store.load_secrets(Path(d) / "nope.json")
        assert got == {}, "缺失 secrets.json 应返回空 dict"


def test_mask_secret():
    assert config_store.mask_secret("") == ""
    assert config_store.mask_secret("sk-abc123xyz") == "sk-ab...xyz"
    assert config_store.mask_secret("a") == "a"


# ─── secret 解析优先级 ───
def test_resolve_secret_precedence():
    secrets = {"copilot_token": "from-secrets"}
    os.environ["COPILOT_GHE_TOKEN"] = "from-env"
    t1 = {"label": "copilot", "secretRef": "copilot_token"}   # secrets 优先
    assert config_store.resolve_secret(t1, secrets) == "from-secrets"
    t2 = {"label": "copilot2", "apikeyEnv": "COPILOT_GHE_TOKEN"}  # 无 secretRef → env
    assert config_store.resolve_secret(t2, secrets) == "from-env"
    t3 = {"label": "nokey"}                                   # 都没有 → ""
    assert config_store.resolve_secret(t3, secrets) == ""
    del os.environ["COPILOT_GHE_TOKEN"]


# ─── handler 模型映射（server.py 的函数经 import 测试） ───
import server as _srv


def test_resolve_model_alias_name_hit():
    """命中 name → 返回 target。"""
    models = [{"name": "sonnet", "aliases": [], "target": {"port": 8080, "model": "agg:sonnet"}}]
    r = config_store._resolve_model_alias(models, "sonnet")
    assert r == {"port": 8080, "model": "agg:sonnet"}, r


def test_resolve_model_alias_alias_hit():
    """命中 aliases → 返回 target。"""
    models = [{"name": "sonnet", "aliases": ["claude-sonnet"], "target": {"port": 8080, "model": "agg:sonnet"}}]
    r = config_store._resolve_model_alias(models, "claude-sonnet")
    assert r == {"port": 8080, "model": "agg:sonnet"}, r


def test_resolve_model_alias_miss():
    """未命中 → None。"""
    assert config_store._resolve_model_alias([], "nope") is None
    assert config_store._resolve_model_alias([{"name": "a", "aliases": [], "target": {"port": 1, "model": "m"}}], "unknown") is None


def test_resolve_model_alias_dict_input():
    """传完整 cfg dict（含 models key）也可解析。"""
    cfg = {"models": [{"name": "sonnet", "aliases": [], "target": {"port": 8080, "model": "agg:sonnet"}}]}
    r = config_store._resolve_model_alias(cfg, "sonnet")
    assert r == {"port": 8080, "model": "agg:sonnet"}, r


# ─── API 路由存在性测试（inspect 源码，不需服务运行） ───

import ast as _ast
import inspect as _inspect


def test_api_targets_shape():
    """通过 inspect 源码确认 /api/targets GET 路由已定义（server.py 或 dashboard 子包）。"""
    found = False
    # 扫描 server.py + dashboard/ 全部 .py（路由已拆分到 dashboard/api_*.py）
    fnames = ["server.py", "dashboard/routes.py"]
    dash_dir = Path(__file__).parent / "dashboard"
    if dash_dir.is_dir():
        fnames += sorted(str(p.relative_to(Path(__file__).parent))
                         for p in dash_dir.glob("*.py") if p.name != "routes.py")
    for fname in fnames:
        path = Path(__file__).parent / fname
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            tree = _ast.parse(f.read())
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    # 找 @app.get(...) / @dashboard_router.get(...) 装饰器
                    if isinstance(decorator, _ast.Call) and isinstance(decorator.func, _ast.Attribute):
                        if decorator.func.attr == "get":
                            for arg in decorator.args:
                                if isinstance(arg, _ast.Constant) and arg.value == "/api/targets":
                                    found = True
                                    break
        if found:
            break
    assert found, "server.py 或 dashboard/ 子包中应有 /api/targets GET 路由"


def test_targets_json_top_level_object():
    """仓库 targets.json 应为顶层对象（含 anthropicForwardPort + targets 数组）。"""
    with open(Path(__file__).parent / "targets.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    assert isinstance(cfg, dict), "targets.json 顶层应为对象"
    assert "modelDefaults" in cfg, "应含 modelDefaults"
    assert isinstance(cfg.get("models"), list), "models 应为数组"
    assert isinstance(cfg.get("targets"), list), "targets 应为数组"
    assert len(cfg["targets"]) >= 9, f"至少 9 个 target，实际 {len(cfg['targets'])}"


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
            print(f"ERROR {t.__name__}: {e}")
            globals()["failed"] += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


def test_repo_targets_file_valid():
    """仓库内 targets.json 应为新格式且校验通过。"""
    cfg = config_store.load_targets(config_store.TARGETS_PATH)
    errors = config_store.validate_targets(cfg)
    assert errors == [], f"targets.json 校验失败: {errors}"
    labels = {t["label"] for t in cfg["targets"]}
    for expected in ("copilot", "codebuddy", "qclaw", "trae-work",
                     "openrouter", "nvidia", "gemini", "opencode-zen", "open-go"):
        assert expected in labels, f"缺少 target: {expected}"
    ports = {t["listenPort"] for t in cfg["targets"]}
    for expected_port in (8082, 8084, 8085, 8086, 8090, 8091, 8092, 8093, 8094):
        assert expected_port in ports, f"缺少端口: {expected_port}"
    # trae-work 预留：确认存在即可（enabled 状态为基线数据，不在此断言）
    tw = next(t for t in cfg["targets"] if t["label"] == "trae-work")
    assert tw is not None


def test_model_stats_structure():
    """模型级统计结构：label → model → 四计数。"""
    import server as _srv
    _srv._MODEL_STATS.clear()
    _srv._bump_model_stats("copilot", "claude-opus-4.8", "ok")
    _srv._bump_model_stats("copilot", "claude-opus-4.8", "translated429")
    s = _srv._MODEL_STATS["copilot"]["claude-opus-4.8"]
    assert s["requests"] == 2 and s["ok"] == 1 and s["translated429"] == 1 and s["err"] == 0, f"统计错误: {s}"
    print(f"PASS test_model_stats_structure: model stats structure correct")


def test_handler_prepare_body_cross_port():
    """_handler_prepare_body 三元组返回：跨端口信号 / 同端口改写 / 未命中透传。"""
    import server as _srv
    _srv._MODELS_CFG["models"] = [
        {"name": "sonnet", "aliases": [], "target": {"port": 8080, "model": "agg:sonnet"}},
    ]
    _srv._MODELS_CFG["modelDefaults"] = {"defaultPort": 8082}
    try:
        # 跨端口：请求 target 是 8082，命中 sonnet（target.port=8080）→ cross_port_target 非空，body model 不改
        b, j, cross = _srv._handler_prepare_body(
            {"label": "x", "handler": "passthrough", "listenPort": 8082},
            b'{"model": "sonnet", "messages": []}')
        assert j is not None, (j, cross)
        assert cross == {"port": 8080, "model": "agg:sonnet"}, (j, cross)
        assert j["model"] == "sonnet", "跨端口命中不应改写 body model（由调用方处理）"
        # 同端口：请求 target 是 8080 → 只改写 model 为 agg:sonnet，cross 为 None
        b2, j2, cross2 = _srv._handler_prepare_body(
            {"label": "agg", "handler": "passthrough", "listenPort": 8080},
            b'{"model": "sonnet", "messages": []}')
        assert j2 is not None, (j2, cross2)
        assert cross2 is None and j2["model"] == "agg:sonnet", (j2, cross2)
        # 未命中：model 原样，cross 为 None
        b3, j3, cross3 = _srv._handler_prepare_body(
            {"label": "x", "handler": "passthrough", "listenPort": 8082},
            b'{"model": "nope-xyz", "messages": []}')
        assert j3 is not None, (j3, cross3)
        assert cross3 is None and j3["model"] == "nope-xyz", (j3, cross3)
    finally:
        _srv._MODELS_CFG["models"] = []
        _srv._MODELS_CFG["modelDefaults"] = {"defaultPort": 8082}


# ─── P2: _get_target_models 统一接口契约测试（RED：函数尚未实现）───
def _inject_targets(targets, models=None, model_defaults=None):
    """临时把 fixture 注入 server 全局，返回还原函数。"""
    import server as _srv
    saved_t = _srv._TARGETS
    saved_m = _srv._MODELS_CFG
    _srv._TARGETS = targets
    if models is not None or model_defaults is not None:
        _srv._MODELS_CFG = {
            "models": models or [],
            "modelDefaults": model_defaults or {"defaultPort": 8082},
        }
    def restore():
        # 必须逐条 setattr：setattr 返回 None，用 and 串联会短路导致后者永不执行，
        # _MODELS_CFG 泄漏到后续测试（曾使 8081/validate 系列在全量跑时集体假失败）。
        _srv._TARGETS = saved_t
        _srv._MODELS_CFG = saved_m

    return restore


def test_get_target_models_copilot():
    """copilot handler → 上游 /models 拉取（mock），source=copilot。"""
    import server as _srv
    from unittest.mock import patch
    tgt = {"label": "copilot-8082", "listenPort": 8082, "category": "crack",
           "handler": "copilot", "targetHost": "x", "crackTool": "gh"}
    restore = _inject_targets([tgt])
    try:
        with patch("server._fetch_live_models", return_value=["gpt-4", "gpt-3.5-turbo"]):
            models = _srv._get_target_models("copilot-8082")
        assert models, "copilot 应返回非空模型列表"
        assert all(isinstance(m, dict) and m.get("source") == "copilot" for m in models), \
            f"copilot 模型 source 应为 copilot: {models}"
    finally:
        restore()


def test_get_target_models_static():
    """codebuddy/qclaw 类 → target.get('models') 静态来源，source 正确。"""
    import server as _srv
    tgt = {"label": "codebuddy-8084", "listenPort": 8084, "category": "crack",
           "handler": "passthrough", "targetHost": "x",
           "models": [{"id": "m1", "enabled": True}]}
    restore = _inject_targets([tgt])
    try:
        models = _srv._get_target_models("codebuddy-8084")
        assert models, "静态模型应非空"
        assert models[0]["id"] == "m1", f"应含 m1: {models}"
        assert models[0].get("source") in ("codebuddy", "qclaw", "trae-work", "passthrough"), \
            f"source 应标记静态来源: {models[0]}"
    finally:
        restore()


def test_get_target_models_anthropic():
    """8081 anthropic 端口 → 等价于 _anthropic_port_models()，source=anthropic。"""
    import server as _srv
    tgt = {"label": "anthropic-compatible", "listenPort": 8081, "category": "free",
           "handler": "passthrough", "targetHost": "x"}
    restore = _inject_targets([tgt],
                               models=[{"name": "sonnet", "aliases": ["s"], "target": {"port": 8082, "model": "claude-sonnet"}}],
                               model_defaults={"defaultPort": 8081})
    try:
        models = _srv._get_target_models("anthropic-compatible")
        ref = _srv._anthropic_port_models()
        assert models, "anthropic 端口应返回模型"
        assert [m["id"] for m in models] == [m["id"] for m in ref], \
            f"应与 _anthropic_port_models 一致: {models} != {ref}"
        assert all(m.get("source") == "anthropic" for m in models), \
            f"source 应为 anthropic: {models}"
    finally:
        restore()


def test_get_target_models_aggregator():
    """aggregator → virtualModels，source=aggregator。"""
    import server as _srv
    tgt = {"label": "agg-8080", "listenPort": 8080, "category": "aggregate",
           "handler": "aggregator",
           "virtualModels": {"agg:sonnet": {"defaultPool": [{"port": 8082, "model": "claude-sonnet"}]}}}
    restore = _inject_targets([tgt])
    try:
        models = _srv._get_target_models("agg-8080")
        assert models, "聚合网关应返回虚拟模型"
        assert all(m.get("source") == "aggregator" for m in models), \
            f"source 应为 aggregator: {models}"
        assert any(m["id"] == "agg:sonnet" for m in models), f"应含 agg:sonnet: {models}"
    finally:
        restore()


if __name__ == "__main__":
    main()
