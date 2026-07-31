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

passed = 0
failed = 0

# ─── 旧格式迁移 ───
def test_migrate_old_array_format():
    """旧 targets.json（数组）应迁移为 {anthropicForwardPort, targets: [...]}。"""
    old = [
        {"label": "openrouter", "listenPort": 8090, "targetHost": "openrouter.ai",
         "targetPort": 443, "targetProtocol": "https", "routePrefix": "/api/v1", "models": []},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(old, f)
        p = Path(f.name)
    try:
        cfg = config_store.load_targets(p)
        assert cfg["anthropicForwardPort"] == 8082, "默认转发端口应为 8082"
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
    new = {"anthropicForwardPort": 8085, "targets": [
        {"label": "qclaw", "listenPort": 8085, "category": "crack", "handler": "qclaw",
         "targetHost": "mmgrcalltoken.3g.qq.com", "targetPort": 443, "targetProtocol": "https",
         "routePrefix": "/aizone/v1", "crackTool": "crack_qclaw.py", "secretRef": "qclaw_api_key",
         "models": []},
    ]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(new, f)
        p = Path(f.name)
    try:
        cfg = config_store.load_targets(p)
        assert cfg["anthropicForwardPort"] == 8085
        assert cfg["targets"][0]["label"] == "qclaw"
    finally:
        p.unlink(missing_ok=True)


# ─── 校验 ───
def test_validate_duplicate_labels():
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "a", "listenPort": 8082, "category": "crack", "handler": "passthrough", "targetHost": "x.com", "models": []},
        {"label": "a", "listenPort": 8083, "category": "free", "handler": "passthrough", "targetHost": "y.com", "models": []},
    ]}
    errors = config_store.validate_targets(cfg)
    assert any("label" in e and "a" in e for e in errors), f"应报重复 label，实际: {errors}"


def test_validate_duplicate_ports():
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "a", "listenPort": 8082, "category": "crack", "handler": "passthrough", "targetHost": "x.com", "models": []},
        {"label": "b", "listenPort": 8082, "category": "free", "handler": "passthrough", "targetHost": "y.com", "models": []},
    ]}
    errors = config_store.validate_targets(cfg)
    assert any("端口" in e or "port" in e.lower() for e in errors), f"应报重复端口，实际: {errors}"


def test_validate_invalid_category():
    cfg = {"anthropicForwardPort": 8082, "targets": [
        {"label": "a", "listenPort": 8082, "category": "hack", "handler": "passthrough", "targetHost": "x.com", "models": []},
    ]}
    errors = config_store.validate_targets(cfg)
    assert any("category" in e for e in errors), f"应报非法 category，实际: {errors}"


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


def test_apply_model_mapping():
    t = {"modelMapping": {"opus": "pool-deepseek-v4-pro", "sonnet": "pool-deepseek-v4-pro", "haiku": "pool-deepseek-v4-flash"}}
    body = {"model": "opus", "messages": []}
    mapped = _srv._apply_model_mapping(t, body)
    assert mapped["model"] == "pool-deepseek-v4-pro", f"opus 应映射为 pool-deepseek-v4-pro，实际 {mapped['model']}"
    body2 = {"model": "pool-glm-5.2", "messages": []}
    mapped2 = _srv._apply_model_mapping(t, body2)
    assert mapped2["model"] == "pool-glm-5.2", "非别名模型不应被映射"


def test_apply_model_mapping_no_mapping():
    t = {}
    body = {"model": "gpt-4.1", "messages": []}
    mapped = _srv._apply_model_mapping(t, body)
    assert mapped["model"] == "gpt-4.1"


# ─── API 路由存在性测试（inspect 源码，不需服务运行） ───

import ast as _ast
import inspect as _inspect


def test_api_targets_shape():
    """通过 inspect 源码确认 server.py 路由中定义了 /api/targets GET 函数。"""
    # 加载 server.py 的 AST
    with open(Path(__file__).parent / "server.py", "r", encoding="utf-8") as f:
        tree = _ast.parse(f.read())
    # 遍历顶层函数定义（含 async def）
    found = False
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                # 找 @app.get(...) 装饰器
                if isinstance(decorator, _ast.Call) and hasattr(decorator.func, "attr"):
                    if decorator.func.attr == "get":
                        for arg in decorator.args:
                            if isinstance(arg, _ast.Constant) and arg.value == "/api/targets":
                                found = True
                                break
    assert found, "server.py 中应有 @app.get('/api/targets') 路由"


def test_targets_json_top_level_object():
    """仓库 targets.json 应为顶层对象（含 anthropicForwardPort + targets 数组）。"""
    with open(Path(__file__).parent / "targets.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    assert isinstance(cfg, dict), "targets.json 顶层应为对象"
    assert "anthropicForwardPort" in cfg, "应含 anthropicForwardPort"
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
                     "openrouter", "nvidia", "gemini-openai", "opencode-zen", "open-go"):
        assert expected in labels, f"缺少 target: {expected}"
    ports = {t["listenPort"] for t in cfg["targets"]}
    for expected_port in (8082, 8084, 8085, 8086, 8090, 8091, 8092, 8093, 8094):
        assert expected_port in ports, f"缺少端口: {expected_port}"
    # trae-work 预留：enabled=false
    tw = next(t for t in cfg["targets"] if t["label"] == "trae-work")
    assert tw.get("enabled") is False, "trae-work 应 enabled=false"


if __name__ == "__main__":
    sys.exit(main())
