"""
ModelRegistry 单元测试（TDD 第一步，RED）。

针对 P2 计划新增的 ModelRegistry 内存索引。该对象尚未实现，
本文件只写测试，运行时应因 ModelRegistry 不存在而失败（RED）。

用法: python test_model_registry.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# server 导入有全局副作用（启动日志等），但本环境验证可正常 import。
# _scan_dangling_refs 读全局 _TARGETS / _MODELS_CFG，故测试内临时注入这两个全局。
import server as _srv  # noqa: E402
from server import ModelRegistry  # noqa: E402

passed = 0
failed = 0


# ─── 复用 server._scan_dangling_refs 的辅助 ───
def _scan_with_cfg(cfg: dict):
    """临时把 cfg 注入 server 全局，调用 _scan_dangling_refs 后还原。"""
    old_targets = _srv._TARGETS
    old_models = _srv._MODELS_CFG
    try:
        _srv._TARGETS = cfg.get("targets", [])
        _srv._MODELS_CFG = {
            "models": cfg.get("models", []),
            "modelDefaults": cfg.get("modelDefaults", {}),
        }
        return _srv._scan_dangling_refs()
    finally:
        _srv._TARGETS = old_targets
        _srv._MODELS_CFG = old_models


def _keyed(items):
    return {(i["path"], i["msg"]) for i in items}


# ─── 测试 1：dangling 与 _scan_dangling_refs 一致 ───
def test_dangling_parity():
    """ModelRegistry(cfg).dangling 应与 server._scan_dangling_refs(cfg) 等价。"""
    cfg = {
        "modelDefaults": {"defaultPort": 8082},
        "targets": [
            {"label": "openrouter", "listenPort": 8090, "category": "free",
             "handler": "passthrough", "targetHost": "openrouter.ai", "models": []},
            {"label": "copilot", "listenPort": 8082, "category": "crack",
             "handler": "copilot", "targetHost": "x.com", "models": []},
        ],
        # 顶层 models 引用一个不存在的端口 → 期望产生一条 dangling
        "models": [
            {"name": "ghost", "aliases": [], "target": {"port": 9999, "model": "x"}},
        ],
    }
    reg = ModelRegistry(cfg)  # 尚未实现 → NameError
    expected = _keyed(_scan_with_cfg(cfg))
    got = _keyed(reg.dangling)
    assert got == expected, f"dangling 不一致:\n got={got}\n exp={expected}"


# ─── 测试 2：capabilities 推导 ───
def test_capabilities_derivation():
    """capabilities[port] 的 can_prune / modelsSource 按 handler 正确推导。"""
    cfg = {
        "modelDefaults": {"defaultPort": 8082},
        "targets": [
            # copilot handler → can_prune=True, modelsSource="copilot"
            {"label": "copilot", "listenPort": 8082, "category": "crack",
             "handler": "copilot", "targetHost": "x.com", "models": []},
            # 普通 crack passthrough → can_prune=False, modelsSource="codebuddy"
            {"label": "codebuddy", "listenPort": 8084, "category": "crack",
             "handler": "passthrough", "targetHost": "y.com", "models": []},
            # qclaw handler
            {"label": "qclaw", "listenPort": 8085, "category": "crack",
             "handler": "qclaw", "targetHost": "z.com", "models": []},
            # trae-work handler
            {"label": "trae-work", "listenPort": 8086, "category": "crack",
             "handler": "trae-work", "targetHost": "w.com", "models": []},
            # aggregator handler → modelsSource="aggregator"
            {"label": "aggregator", "listenPort": 8080, "category": "aggregate",
             "handler": "aggregator", "virtualModels": {}},
            # 8081 类（FastAPI 入口）→ modelsSource="anthropic"
            {"label": "anthropic", "listenPort": 8081, "category": "free",
             "handler": "passthrough", "targetHost": "a.com", "models": []},
        ],
    }
    reg = ModelRegistry(cfg)  # NameError
    caps = reg.capabilities
    # copilot 可 prune
    assert caps[8082]["can_prune"] is True, f"copilot 应可 prune: {caps[8082]}"
    assert caps[8082]["modelsSource"] == "copilot", caps[8082]
    # 非 copilot crack/passthrough 不可 prune（无上游 /models 能力）
    assert caps[8084]["can_prune"] is False, caps[8084]
    assert caps[8084]["modelsSource"] == "codebuddy", caps[8084]
    # 其它 modelsSource 取值
    assert caps[8085]["modelsSource"] == "qclaw", caps[8085]
    assert caps[8086]["modelsSource"] == "trae-work", caps[8086]
    assert caps[8080]["modelsSource"] == "aggregator", caps[8080]
    assert caps[8081]["modelsSource"] == "anthropic", caps[8081]


# ─── 测试 3：byPort 索引 ───
def test_by_port_index():
    """byPort 应为每个 listenPort 提供条目。"""
    cfg = {
        "modelDefaults": {"defaultPort": 8082},
        "targets": [
            {"label": "copilot", "listenPort": 8082, "category": "crack",
             "handler": "copilot", "targetHost": "x.com", "models": []},
            {"label": "codebuddy", "listenPort": 8084, "category": "crack",
             "handler": "passthrough", "targetHost": "y.com", "models": []},
            {"label": "openrouter", "listenPort": 8090, "category": "free",
             "handler": "passthrough", "targetHost": "o.com", "models": []},
        ],
        "models": [],
    }
    reg = ModelRegistry(cfg)  # NameError
    bp = reg.byPort
    for port in (8082, 8084, 8090):
        assert port in bp, f"byPort 缺少端口 {port}: {list(bp.keys())}"


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
