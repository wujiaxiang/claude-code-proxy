# 横向扩展多端口架构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 claude-code-proxy 从"8082 单端口 + PREFERRED_PROVIDER 动态切换"改造为"一端口一供应商"的横向扩展模式：破解类(8082 copilot / 8084 codebuddy / 8085 qclaw / 8086 trae-work) + 免费类(8090-8094)，统一透传引擎 + 独立破解工具 + dashboard 管理界面 + 热生效。

**Architecture:** 除 8081(Anthropic 入口 + dashboard) 外，所有端口统一为 OpenAI 协议透传引擎（targets.json 驱动，handler 字段区分 passthrough/copilot/qclaw 的 header 注入与 body 清理差异）。破解逻辑从 server.py 拆为独立 CLI 脚本（crack_*.py），提取 token 写入 secrets.json（gitignore，热更新）。dashboard 升级为可写管理界面（REST API + 表单），mtime 轮询实现热生效。

**Tech Stack:** Python 3.10+ / FastAPI / uvicorn / asyncio TCP / httpx / pydantic

## Global Constraints

- 设计规格：`docs/superpowers/specs/2026-07-31-multi-port-architecture-design.md`（已批准）
- Python 3.10+（`requires-python = ">=3.10"`）；venv 位于 `.venv/`，测试用 `.venv/bin/python`（Windows 为 `.venv\Scripts\python.exe`）
- 项目无 pytest：新测试文件用自定义 runner（`python test_xxx.py`，main 收集 `test_*` 函数，返回非 0 表示失败），与 test_suite.py / test_dashboard.py 一致
- **破解工具是纯独立脚本**，不 import server.py，只依赖标准库 + json + os（crack_qclaw.py 允许 import cryptography，因 venv 已有）
- 所有 httpx 客户端必须 `trust_env=False`（绕过系统代理）；QClaw 上游必须 `User-Agent: OpenAI/JS 6.39.1`
- 配置优先级：`secrets.json` > 环境变量(`apikeyEnv`) > 客户端透传
- 端口规划：8081 dashboard/Anthropic 入口（不动）/ 8082 copilot / 8084 codebuddy / 8085 qclaw / 8086 trae-work(enabled:false) / 8090 openrouter / 8091 nvidia / 8092 gemini-openai / 8093 opencode-zen / 8094 open-go
- `isFree` 字段本次只存储 + API 暴露，不驱动重试行为差异（重试仍为 5xx 重试 3 次 + 429 翻译）
- 8081 本次不改协议翻译逻辑，仅 `anthropicForwardPort` 可配置（默认 8082）
- 提交规范：Conventional Commits（`feat:`/`fix:`/`refactor:`/`test:`/`docs:`）；提交前 `git status` 检查，不 `git add .`（`.env`、`*.log`、`.venv/`、`secrets.json` 不入库）

---

### Task 1: config_store.py — targets.json 新 schema 加载/迁移/校验

**Files:**
- Create: `config_store.py`
- Test: `test_targets_schema.py`

**Interfaces:**
- Consumes: 无（纯逻辑模块，不依赖 server.py）
- Produces:
  - `TARGETS_PATH: Path` — `Path(__file__).parent / "targets.json"`
  - `SECRETS_PATH: Path` — `Path(__file__).parent / "secrets.json"`
  - `DEFAULT_TARGETS: dict` — 顶层默认结构 `{"anthropicForwardPort": 8082, "targets": [...]}`
  - `VALID_CATEGORIES = ("crack", "free", "paid")`
  - `VALID_HANDLERS = ("passthrough", "copilot", "qclaw")`
  - `load_targets(path: Path = TARGETS_PATH) -> dict` — 加载并迁移，返回 `{"anthropicForwardPort": int, "targets": list}`
  - `validate_targets(cfg: dict) -> list[str]` — 返回错误消息列表（空 = 通过）
  - `save_targets(cfg: dict, path: Path = TARGETS_PATH) -> None` — 原子写（临时文件 + rename）
  - `load_secrets(path: Path = SECRETS_PATH) -> dict`
  - `save_secrets(secrets: dict, path: Path = SECRETS_PATH) -> None` — 原子写
  - `mask_secret(value: str) -> str` — 打码：`sk-abc...xyz`，空返回 `""`
  - `resolve_secret(target: dict, secrets: dict) -> str` — `secretRef` → secrets → `apikeyEnv` 环境变量 → 空字符串

- [ ] **Step 1: 写失败测试 test_targets_schema.py**

```python
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


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/python test_targets_schema.py`
Expected: `ImportError: No module named 'config_store'`（文件不存在），退出码非 0

- [ ] **Step 3: 实现 config_store.py**

```python
"""配置存储层：targets.json / secrets.json 加载、迁移、校验、原子写。

独立于 server.py，供透传引擎、dashboard API、破解工具共用。
优先级：secrets.json > 环境变量(apikeyEnv) > 客户端透传。
"""
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("config_store")

TARGETS_PATH = Path(__file__).parent / "targets.json"
SECRETS_PATH = Path(__file__).parent / "secrets.json"

VALID_CATEGORIES = ("crack", "free", "paid")
VALID_HANDLERS = ("passthrough", "copilot", "qclaw")

_REQUIRED_FIELDS = ("label", "listenPort", "category", "handler", "targetHost")


def load_targets(path: Path = TARGETS_PATH) -> dict:
    """加载 targets.json，兼容旧数组格式，自动迁移为新对象格式。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        logger.warning(f"targets.json not found: {path}, using empty config")
        return {"anthropicForwardPort": 8082, "targets": []}
    except json.JSONDecodeError as e:
        logger.error(f"targets.json invalid JSON: {e}")
        return {"anthropicForwardPort": 8082, "targets": []}

    if isinstance(raw, list):
        # 旧格式：数组 → 迁移
        logger.warning("targets.json 是旧数组格式，自动迁移为新对象格式")
        migrated = []
        for t in raw:
            category = t.get("category", "free")
            migrated.append({
                **t,
                "category": category,
                "handler": t.get("handler", "passthrough"),
                "isFree": t.get("isFree", category == "free"),
                "enabled": t.get("enabled", True),
            })
        cfg = {"anthropicForwardPort": 8082, "targets": migrated}
        try:
            save_targets(cfg, path)  # 回写迁移结果
            logger.info(f"targets.json migrated to new format: {path}")
        except Exception as e:
            logger.warning(f"failed to write migrated targets.json: {e}")
        return cfg

    # 新格式
    targets = raw.get("targets", [])
    normalized = []
    for t in targets:
        category = t.get("category", "free")
        normalized.append({
            **t,
            "category": category,
            "handler": t.get("handler", "passthrough"),
            "isFree": t.get("isFree", category == "free"),
            "enabled": t.get("enabled", True),
        })
    return {
        "anthropicForwardPort": raw.get("anthropicForwardPort", 8082),
        "targets": normalized,
    }


def validate_targets(cfg: dict) -> list:
    """校验配置，返回错误消息列表（空 = 通过）。"""
    errors = []
    targets = cfg.get("targets", [])
    labels = {}
    ports = {}

    for t in targets:
        label = t.get("label", "")
        for field in _REQUIRED_FIELDS:
            if not t.get(field):
                errors.append(f"target '{label}' 缺少必需字段: {field}")
        if t.get("category") not in VALID_CATEGORIES:
            errors.append(f"target '{label}' category 非法: {t.get('category')}（合法: {VALID_CATEGORIES}）")
        if t.get("handler") not in VALID_HANDLERS:
            errors.append(f"target '{label}' handler 非法: {t.get('handler')}（合法: {VALID_HANDLERS}）")
        if label in labels:
            errors.append(f"重复 label: '{label}'")
        labels[label] = True
        port = t.get("listenPort")
        if port in ports:
            errors.append(f"端口 {port} 被多个 target 占用 ({ports[port]}, {label})")
        ports[port] = label
        if t.get("category") == "crack" and not t.get("crackTool") and t.get("enabled", True):
            errors.append(f"crack target '{label}' 缺少 crackTool")
    return errors


def save_targets(cfg: dict, path: Path = TARGETS_PATH) -> None:
    """原子写 targets.json（临时文件 + rename，避免写一半）。"""
    _atomic_write_json(cfg, path)


def load_secrets(path: Path = SECRETS_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"secrets.json invalid JSON: {e}")
        return {}


def save_secrets(secrets: dict, path: Path = SECRETS_PATH) -> None:
    _atomic_write_json(secrets, path)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return value
    return f"{value[:4]}...{value[-3:]}"


def resolve_secret(target: dict, secrets: dict) -> str:
    """解析 target 的私密 token：secretRef → secrets.json → apikeyEnv 环境变量。"""
    ref = target.get("secretRef")
    if ref and secrets.get(ref):
        return secrets[ref]
    env_key = target.get("apikeyEnv")
    if env_key:
        return os.environ.get(env_key, "")
    return ""


def _atomic_write_json(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/python test_targets_schema.py`
Expected: 全部 PASS，`13/13 passed`，退出码 0

- [ ] **Step 5: 提交**

```bash
git add config_store.py test_targets_schema.py
git commit -m "feat: add config_store for targets/secrets loading, migration and validation"
```

---

### Task 2: targets.json 迁移到新 schema

**Files:**
- Modify: `targets.json`（数组 → 新对象格式，含全部 10 个端口）
- Test: `test_targets_schema.py`（复用，加载真实文件）

**Interfaces:**
- Consumes: `config_store.load_targets / validate_targets`（Task 1）
- Produces: 新格式 `targets.json`（顶层 `anthropicForwardPort` + `targets` 数组），供 Task 3 的引擎消费

- [ ] **Step 1: 写失败测试（先验证旧文件会迁移失败/不满足新断言）**

在 `test_targets_schema.py` 末尾追加：

```python
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
```

Run: `.venv/bin/python test_targets_schema.py`
Expected: `FAIL test_repo_targets_file_valid`（当前 targets.json 是旧数组格式，缺 copilot/qclaw 等条目）

- [ ] **Step 2: 重写 targets.json**

```json
{
  "anthropicForwardPort": 8082,
  "targets": [
    {
      "label": "copilot",
      "listenPort": 8082,
      "category": "crack",
      "handler": "copilot",
      "targetHost": "copilot-api.bmw.ghe.com",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "",
      "crackTool": "crack_copilot.py",
      "secretRef": "copilot_token",
      "apikeyEnv": "COPILOT_GHE_TOKEN",
      "models": ["claude-opus-4.8", "claude-sonnet-5", "claude-haiku-4.5", "gpt-5.5", "gpt-4.1", "gemini-2.5-pro"],
      "extraHeaders": { "Copilot-Integration-Id": "copilot-developer-cli" },
      "modelMapping": { "opus": "claude-opus-4.8", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4.5" }
    },
    {
      "label": "codebuddy",
      "listenPort": 8084,
      "category": "crack",
      "handler": "passthrough",
      "targetHost": "copilot.tencent.com",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/v2",
      "crackTool": "crack_codebuddy.py",
      "secretRef": "codebuddy_token",
      "apikeyEnv": "CODEBUDDY_TOKEN",
      "models": ["deepseek-v4-pro", "deepseek-v4-flash", "glm-5.2", "glm-5.1", "kimi-k2.7", "kimi-k2.6", "minimax-m3", "minimax-m2.7"]
    },
    {
      "label": "qclaw",
      "listenPort": 8085,
      "category": "crack",
      "handler": "qclaw",
      "targetHost": "mmgrcalltoken.3g.qq.com",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/aizone/v1",
      "crackTool": "crack_qclaw.py",
      "secretRef": "qclaw_api_key",
      "apikeyEnv": "QCLAW_API_KEY",
      "models": ["pool-deepseek-v4-pro", "pool-deepseek-v4-flash", "pool-glm-5.2", "pool-glm-5.1", "pool-kimi-k2.7-code-highspeed", "pool-kimi-k2.6", "pool-minimax-m3", "pool-minimax-m2.7"],
      "modelMapping": { "opus": "pool-deepseek-v4-pro", "sonnet": "pool-deepseek-v4-pro", "haiku": "pool-deepseek-v4-flash" },
      "reasoning": { "big": "high", "medium": "low", "small": "low" }
    },
    {
      "label": "trae-work",
      "listenPort": 8086,
      "category": "crack",
      "handler": "passthrough",
      "targetHost": "",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "",
      "crackTool": "crack_traework.py",
      "secretRef": "trae_work_token",
      "models": [],
      "enabled": false
    },
    {
      "label": "openrouter",
      "listenPort": 8090,
      "category": "free",
      "handler": "passthrough",
      "isFree": true,
      "targetHost": "openrouter.ai",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/api/v1",
      "models": ["nvidia/nemotron-3-ultra-550b-a55b:free", "openai/gpt-oss-20b:free", "google/gemma-4-26b-a4b-it:free"]
    },
    {
      "label": "nvidia",
      "listenPort": 8091,
      "category": "free",
      "handler": "passthrough",
      "isFree": true,
      "targetHost": "integrate.api.nvidia.com",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/v1",
      "models": ["deepseek-ai/deepseek-v4-flash", "deepseek-ai/deepseek-v4-pro", "qwen/qwen3-coder-480b-a35b-instruct", "meta/llama-4-maverick-17b-128e-instruct"]
    },
    {
      "label": "gemini-openai",
      "listenPort": 8092,
      "category": "free",
      "handler": "passthrough",
      "isFree": true,
      "targetHost": "generativelanguage.googleapis.com",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/v1beta/openai",
      "models": ["gemini-2.5-pro", "gemini-2.5-flash"]
    },
    {
      "label": "opencode-zen",
      "listenPort": 8093,
      "category": "free",
      "handler": "passthrough",
      "isFree": true,
      "targetHost": "opencode.ai",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/zen/v1",
      "models": ["deepseek-v4-flash-free", "mimo-v2.5-free", "big-pickle", "north-mini-code-free"]
    },
    {
      "label": "open-go",
      "listenPort": 8094,
      "category": "paid",
      "handler": "passthrough",
      "isFree": false,
      "targetHost": "opencode.ai",
      "targetPort": 443,
      "targetProtocol": "https",
      "routePrefix": "/zen/go/v1",
      "models": ["deepseek-v4-pro", "kimi-k2.7", "glm-5.2"]
    }
  ]
}
```

- [ ] **Step 3: 运行测试验证通过**

Run: `.venv/bin/python test_targets_schema.py`
Expected: `test_repo_targets_file_valid` PASS，全部通过

- [ ] **Step 4: 提交**

```bash
git add targets.json test_targets_schema.py
git commit -m "feat: migrate targets.json to new multi-port schema (crack/free/paid categories)"
```

---

### Task 3: 统一透传引擎 — handler 分发 + 鉴权注入 + 401 缺 token

**Files:**
- Modify: `server.py`（735-942 区域：vendor target 加载改造；1254-1349 区域：`_handle_vendor_request` → `_handle_target_request`）
- Test: `test_targets_schema.py`（追加 handler 相关单元测试）

**Interfaces:**
- Consumes: `config_store`（Task 1）—— `load_targets/validate_targets/load_secrets/resolve_secret/mask_secret`
- Produces（server.py 内）:
  - `_TARGETS: list` — 规范化后的 target 列表（含 enabled/isFree/category/handler 默认值）
  - `_SECRETS: dict` — secrets.json 内容（热重载时更新）
  - `_TARGET_STATS: Dict[str, dict]` — 每 target 统计（替代 `_VENDOR_STATS`）
  - `_handler_prepare_headers(target, fwd_headers, body_json) -> dict` — 按 handler 注入 extraHeaders/UA/Authorization
  - `_handler_prepare_body(target, body_bytes) -> tuple[bytes, dict]` — 按 handler 做模型映射/body 清理，返回 (new_body, body_json)
  - `_apply_model_mapping(target, body_json) -> dict` — `modelMapping` 中的 key（opus/sonnet/haiku）替换为真实模型
  - `_handle_target_request(reader, writer, target)` — 统一请求处理（原 `_handle_vendor_request` 升级）
  - `_target_server(host, port, target)` — 启动单端口 server

- [ ] **Step 1: 追加 handler 单元测试（test_targets_schema.py）**

```python
# ─── handler 模型映射（server.py 的函数经 import 测试） ───
import server as _srv  # 在文件末尾 main() 之前


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
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/python test_targets_schema.py`
Expected: `FAIL test_apply_model_mapping`（server.py 尚无 `_apply_model_mapping`）

- [ ] **Step 3: 改造 server.py**

**3a. 替换 vendor target 加载区（735-942 行区域）：**

将：

```python
_VENDOR_TARGETS: list = []
_VENDOR_STATS: Dict[str, dict] = {}
_VENDOR_RETRY_AFTER = int(os.environ.get("VENDOR_RETRY_AFTER_SECONDS", "3"))
```

替换为：

```python
import config_store as _cfg

# ─── 统一透传引擎配置（targets.json 驱动）───
_VENDOR_RETRY_AFTER = int(os.environ.get("VENDOR_RETRY_AFTER_SECONDS", "3"))
_TARGETS: list = []
_SECRETS: dict = {}
_TARGET_STATS: Dict[str, dict] = {}
_ANTHROPIC_FORWARD_PORT = 8082
```

**3b. 替换 `_load_vendor_targets`（924-942 行）：**

```python
def _load_vendor_targets():
    """加载 targets.json + secrets.json，规范化并初始化统计。"""
    global _TARGETS, _SECRETS, _ANTHROPIC_FORWARD_PORT
    cfg = _cfg.load_targets()
    errors = _cfg.validate_targets(cfg)
    if errors:
        for e in errors:
            logger.warning(f"targets.json 配置错误: {e}")
    _TARGETS = cfg.get("targets", [])
    _ANTHROPIC_FORWARD_PORT = cfg.get("anthropicForwardPort", 8082)
    _SECRETS = _cfg.load_secrets()
    for t in _TARGETS:
        label = t["label"]
        if label not in _TARGET_STATS:
            _TARGET_STATS[label] = {
                "totalRequests": 0, "translated429": 0,
                "passthroughOk": 0, "passthroughError": 0,
                "startedAt": datetime.now().isoformat(),
            }
    print(f"🔀 Targets loaded: {len(_TARGETS)} targets, anthropicForwardPort={_ANTHROPIC_FORWARD_PORT}")
```

**3c. 在 `_resolve_auth`（894-921 行）之后追加 handler 辅助函数：**

```python
def _apply_model_mapping(target: dict, body_json: dict) -> dict:
    """按 target.modelMapping 将 Anthropic 别名（opus/sonnet/haiku）替换为真实模型。"""
    mapping = target.get("modelMapping") or {}
    model = body_json.get("model")
    if model and model in mapping:
        body_json["model"] = mapping[model]
    return body_json


def _handler_prepare_body(target: dict, body_bytes: bytes):
    """按 handler 类型处理请求体：模型映射 + qclaw body 清理。
    返回 (new_body_bytes, body_json_or_None)。
    """
    handler = target.get("handler", "passthrough")
    if handler == "passthrough":
        return body_bytes, None
    try:
        body_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except Exception:
        return body_bytes, None
    if target.get("modelMapping"):
        body_json = _apply_model_mapping(target, body_json)
    if handler == "qclaw":
        body_json = _clean_qclaw_body(body_json)
    return json.dumps(body_json, ensure_ascii=False).encode("utf-8"), body_json


def _handler_prepare_headers(target: dict, fwd_headers: dict, body_json: dict) -> dict:
    """按 handler 类型注入认证与补充 header。"""
    handler = target.get("handler", "passthrough")
    # 认证：crack 类注入 secrets token（secrets.json > apikeyEnv）
    if target.get("category") == "crack":
        token = _cfg.resolve_secret(target, _SECRETS)
        if token:
            fwd_headers["authorization"] = f"Bearer {token}"
    # 补充 header（如 copilot 的 Copilot-Integration-Id）
    for k, v in (target.get("extraHeaders") or {}).items():
        fwd_headers[k] = v
    # qclaw 上游要求 UA
    if handler == "qclaw":
        fwd_headers["User-Agent"] = "OpenAI/JS 6.39.1"
    return fwd_headers
```

**3d. 升级 `_handle_vendor_request`（1254-1341 行）为 `_handle_target_request`：**

```python
async def _handle_target_request(reader, writer, target):
    """统一透传引擎：处理单个 target 端口的全部请求。
    与原 _handle_vendor_request 兼容，新增 handler 分发 / 鉴权注入 / 401 缺 token。
    """
    label = target["label"]
    stats = _TARGET_STATS.setdefault(label, {
        "totalRequests": 0, "translated429": 0,
        "passthroughOk": 0, "passthroughError": 0,
        "startedAt": datetime.now().isoformat(),
    })
    try:
        method, path, raw_path, headers, body = await _parse_http_request(reader)
        if method is None:
            return

        # ── 内建 JSON 端点 ──
        if path == "/__proxy_info__":
            payload = json.dumps({
                "label": label, "listenPort": target["listenPort"],
                "category": target.get("category", ""),
                "handler": target.get("handler", "passthrough"),
                "isFree": target.get("isFree", False),
                "targetHost": target["targetHost"], "targetPort": target.get("targetPort", 443),
                "targetProtocol": target.get("targetProtocol", "https"),
                "models": target.get("models", []),
                "retryAfterSeconds": _VENDOR_RETRY_AFTER,
                "errorPatterns": [p.pattern for p in _VENDOR_ERROR_PATTERNS],
                "startedAt": stats["startedAt"],
                "secretSet": bool(_cfg.resolve_secret(target, _SECRETS)),
            })
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload.encode()), payload.encode()))
            await writer.drain(); writer.close(); return

        if path == "/__proxy_stats__":
            payload = json.dumps({"label": label, "listenPort": target["listenPort"], **stats})
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload.encode()), payload.encode()))
            await writer.drain(); writer.close(); return

        # ── /dashboard：代理到 8081 FastAPI ──
        if path == "/dashboard" and method == "GET":
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), trust_env=False) as c:
                resp = await c.get("http://127.0.0.1:8081/dashboard")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\n\r\n%s" % (len(resp.content), resp.content))
                await writer.drain()
            writer.close(); return

        stats["totalRequests"] += 1

        # ── crack 类缺 token → 401（不转发上游）──
        if target.get("category") == "crack" and not _cfg.resolve_secret(target, _SECRETS):
            err_payload = json.dumps({
                "error": {
                    "type": "missing_token",
                    "message": f"请到 dashboard (http://127.0.0.1:8081/dashboard) 填写 {target.get('secretRef', label)} token",
                }
            })
            writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(err_payload.encode()), err_payload.encode()))
            await writer.drain(); writer.close(); return

        # ── 上游转发（含路径重写 + handler body/header 处理）──
        transport = "https" if target.get("targetProtocol", "https") == "https" else "http"
        upstream_path = raw_path
        route_prefix = target.get("routePrefix")
        if route_prefix and upstream_path.startswith("/v1"):
            upstream_path = route_prefix + upstream_path[3:]
        upstream_url = f"{transport}://{target['targetHost']}:{target.get('targetPort', 443)}{upstream_path}"

        body_bytes, body_json = _handler_prepare_body(target, body)
        fwd_headers = _resolve_auth(headers, target=target)
        fwd_headers["host"] = target["targetHost"]
        fwd_headers = _handler_prepare_headers(target, fwd_headers, body_json)

        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False) as client:
            req = client.build_request(method, upstream_url, headers=fwd_headers, content=body_bytes if body_bytes else None)
            resp = await client.send(req, stream=True)

            content_type = resp.headers.get("content-type", "")
            is_stream = "text/event-stream" in content_type

            if is_stream:
                status, _ = await _write_response(writer, resp, stats=stats)
                if status and status >= 400:
                    logger.warning(f"[{label}] stream returned HTTP {status}")
                return

            # 非流式：先读 body，再判断是否要翻译 429
            resp_body = await resp.aread()
            body_text = resp_body.decode("utf-8", errors="replace")
            status = resp.status_code

            if status >= 400:
                logger.warning(f"[{label}] HTTP {status}: {body_text[:300]}")

            if _vendor_body_retryable(body_text):
                stats["translated429"] += 1
                logger.info(f"[{label}] translated HTTP {status} → 429 (rate_limit_error)")
                err_payload = json.dumps({
                    "error": {
                        "type": "rate_limit_error",
                        "message": "Upstream temporarily over capacity.",
                        "original_status": resp.status_code,
                    }
                })
                writer.write(
                    f"HTTP/1.1 429 Too Many Requests\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Retry-After: {_VENDOR_RETRY_AFTER}\r\n"
                    f"Content-Length: {len(err_payload.encode())}\r\n"
                    f"\r\n{err_payload}".encode()
                )
                await writer.drain()
                writer.close()
            else:
                await _write_response(writer, resp, stats=stats)
    except Exception:
        stats["passthroughError"] += 1
        logger.exception(f"[{label}] target proxy exception")
        try:
            await _write_error_response(writer, 503, f"Proxy error for {label}")
        except Exception:
            pass
```

**3e. 更新 `_vendor_server`（1344-1349 行）引用：**

```python
async def _vendor_server(host, port, target):
    server = await asyncio.start_server(
        lambda r, w: _handle_target_request(r, w, target),
        host=host, port=port,
    )
    print(f"🔀 [{target['label']}] 0.0.0.0:{port} -> {target.get('targetProtocol','https')}://{target['targetHost']}:{target.get('targetPort', 443)}")
    return server
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/python test_targets_schema.py`
Expected: `test_apply_model_mapping` / `test_apply_model_mapping_no_mapping` PASS

注意：`import server` 会执行 server.py 顶层代码（加载 litellm、tiktoken 等），耗时约 5-20s 属正常。

- [ ] **Step 5: 提交**

```bash
git add server.py test_targets_schema.py
git commit -m "refactor: unified passthrough engine with handler dispatch and secret injection"
```

---

### Task 4: lifespan 启动改造 — targets 驱动多端口 + 8082 固定 copilot

**Files:**
- Modify: `server.py`（465-517 行 lifespan；1005-1088 行 `_handle_openai_proxy_request` / `_openai_server`；1091-1251 行 8081 转发）

**Interfaces:**
- Consumes: `_TARGETS / _TARGET_STATS / _handle_target_request / _vendor_server`（Task 3）
- Produces:
  - `_target_servers: Dict[int, asyncio.Server]` — port → server（热重载用）
  - lifespan 中启动所有 enabled target 端口（含 8082 copilot target），移除 `_openai_server` 特殊处理
  - 8081 `_handle_anthropic_proxy_request` 转发目标改为 `_ANTHROPIC_FORWARD_PORT`

- [ ] **Step 1: 改造 lifespan（465-517 行）**

将：

```python
    # ── 启动 asyncio TCP 服务器（8082 OpenAI, 8090 openrouter, 8091 nvidia）──
    # 8081 Anthropic 由 uvicorn FastAPI 处理（不在此处启动）
    _vendor_servers = []
    for t in _VENDOR_TARGETS:
        srv = await _vendor_server("0.0.0.0", t["listenPort"], t)
        _vendor_servers.append(srv)
    _openai_srv = await _openai_server("0.0.0.0", _OPENAI_PORT)
    _vendor_servers.append(_openai_srv)
```

替换为：

```python
    # ── 启动 asyncio TCP 服务器（每个 enabled target 一个端口）──
    # 8081 Anthropic 由 uvicorn FastAPI 处理（不在此处启动）
    global _target_servers
    _target_servers = {}
    for t in _TARGETS:
        if not t.get("enabled", True):
            print(f"⏸️  [{t['label']}] port {t['listenPort']} skipped (enabled=false)")
            continue
        try:
            srv = await _vendor_server("0.0.0.0", t["listenPort"], t)
            _target_servers[t["listenPort"]] = srv
        except OSError as e:
            logger.error(f"⚠️  无法监听端口 {t['listenPort']} ({t['label']}): {e}")
```

并将清理区（508-511 行）：

```python
    # 停掉透明反代服务器
    for srv in _vendor_servers:
        srv.close()
        await srv.wait_closed()
```

替换为：

```python
    # 停掉所有 target 服务器
    for port, srv in _target_servers.items():
        srv.close()
        await srv.wait_closed()
```

- [ ] **Step 2: 改造 `_handle_openai_proxy_request` / `_openai_server`（1005-1088 行）**

将整个 `_handle_openai_proxy_request` 函数体替换为对统一引擎的转发（8082 现在是 copilot target，由 `_handle_target_request` 处理；此函数仅保留 `/dashboard` 代理与 `/__proxy_info__` 自检兜底）：

```python
async def _handle_openai_proxy_request(reader, writer):
    """8082 OpenAI 端口：固定为 copilot target（经统一透传引擎）。
    保留为兼容入口：若 8082 未在 targets.json 中配置，则此函数兜底转发。
    """
    copilot_target = next((t for t in _TARGETS if t["listenPort"] == _OPENAI_PORT), None)
    if copilot_target:
        await _handle_target_request(reader, writer, copilot_target)
        return
    # 兜底：无 target 配置时保持旧行为（透传 + 基本自检）
    try:
        method, path, raw_path, headers, body = await _parse_http_request(reader)
        if method is None:
            return
        if path == "/__proxy_info__":
            payload = json.dumps({
                "label": "claude-code-openai", "listenPort": _OPENAI_PORT,
                "targetHost": "unconfigured", "targetPort": 443, "targetProtocol": "https",
                "models": [], "retryAfterSeconds": 0, "errorPatterns": [],
                "startedAt": _OPENAI_STATS["startedAt"],
            })
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(payload.encode()), payload.encode()))
            await writer.drain(); writer.close(); return
        await _write_error_response(writer, 503, "8082 target not configured in targets.json")
    except Exception:
        _OPENAI_STATS["passthroughError"] += 1
        try:
            await _write_error_response(writer, 503, "OpenAI proxy error")
        except Exception:
            pass
```

`_openai_server`（1085-1088 行）保持不变（仍启动 8082，经上述函数转发到 copilot target）。

- [ ] **Step 3: 改造 8081 转发目标（1163-1173 行）**

将：

```python
            fwd_headers = {
                "content-type": "application/json",
                "host": "127.0.0.1:8082",
                "content-length": str(len(openai_payload)),
            }
            if headers.get("authorization"):
                fwd_headers["authorization"] = headers["authorization"]
            if headers.get("x-api-key"):
                fwd_headers["x-api-key"] = headers["x-api-key"]

            upstream_url = "http://127.0.0.1:8082/v1/chat/completions"
```

替换为：

```python
            fwd_headers = {
                "content-type": "application/json",
                "host": f"127.0.0.1:{_ANTHROPIC_FORWARD_PORT}",
                "content-length": str(len(openai_payload)),
            }
            if headers.get("authorization"):
                fwd_headers["authorization"] = headers["authorization"]
            if headers.get("x-api-key"):
                fwd_headers["x-api-key"] = headers["x-api-key"]

            upstream_url = f"http://127.0.0.1:{_ANTHROPIC_FORWARD_PORT}/v1/chat/completions"
```

并将 1213-1216 行的兜底透传同样改为 `_ANTHROPIC_FORWARD_PORT`：

```python
        # ── 其余请求：透传到 anthropicForwardPort ──
        upstream_url = f"http://127.0.0.1:{_ANTHROPIC_FORWARD_PORT}{raw_path}"
        fwd_headers = {k: v for k, v in headers.items() if k not in ("host", "connection")}
        fwd_headers["host"] = f"127.0.0.1:{_ANTHROPIC_FORWARD_PORT}"
```

- [ ] **Step 4: 语法检查 + 启动冒烟测试**

Run:
```bash
.venv/bin/python -c "import ast; ast.parse(open('server.py').read()); print('syntax OK')"
timeout 25 .venv/bin/python server.py --port 8081 2>&1 | head -40 &
sleep 8
curl -s http://127.0.0.1:8081/__proxy_info__ ; echo
curl -s http://127.0.0.1:8082/__proxy_info__ ; echo
curl -s http://127.0.0.1:8085/__proxy_info__ ; echo
curl -s http://127.0.0.1:8090/__proxy_info__ ; echo
curl -s http://127.0.0.1:8093/__proxy_info__ ; echo
wait
```
Expected:
- 启动日志显示各 target 端口绑定（8082/8084/8085/8090/8091/8092/8093/8094）
- 8086 显示 `skipped (enabled=false)`
- 各 `/__proxy_info__` 返回对应 label/category/handler
- 8085 qclaw 若本机无 key 显示 `secretSet: false`（Linux 下 DPAPI 不可用属预期）

- [ ] **Step 5: 提交**

```bash
git add server.py
git commit -m "refactor: drive multi-port startup from targets.json, fix 8082 as copilot, honor anthropicForwardPort"
```

---

### Task 5: 破解工具 — crack_qclaw / crack_codebuddy / crack_copilot / crack_traework

**Files:**
- Create: `crack_qclaw.py`
- Create: `crack_codebuddy.py`
- Create: `crack_copilot.py`
- Create: `crack_traework.py`
- Test: `test_crack_tools.py`

**Interfaces:**
- Consumes: `config_store.SECRETS_PATH / save_secrets`（Task 1）
- Produces（统一 CLI 约定，供 Task 6 启动调用 + dashboard recrack）:
  - `python crack_<vendor>.py [--secrets secrets.json] [--force]`
  - 成功：写入 secrets.json 对应键，stdout `✅ ...`，退出码 0
  - 失败：stdout `❌ ...` + 引导文案，退出码 1
  - `--force`：即使已有 key 也重新提取

- [ ] **Step 1: 写失败测试 test_crack_tools.py**

```python
"""
破解工具统一 CLI 测试（无真实环境时优雅失败 + --force/--secrets 参数行为）。
用法: python test_crack_tools.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
passed = 0
failed = 0


def _run_tool(name, *args, env=None):
    return subprocess.run(
        [sys.executable, str(ROOT / name), *args],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {})},
    )


def test_crack_qclaw_fails_gracefully_linux():
    """Linux 无 QClaw 环境：应优雅失败，退出码非 0，stdout 有引导文案。"""
    if sys.platform == "win32":
        return  # Windows 上可能真实解密，跳过
    with tempfile.TemporaryDirectory() as d:
        secrets_path = Path(d) / "secrets.json"
        r = _run_tool("crack_qclaw.py", "--secrets", str(secrets_path),
                      env={"APPDATA": str(Path(d) / "no-qclaw")})
        assert r.returncode != 0, f"应失败退出，实际 rc={r.returncode}\n{r.stdout}\n{r.stderr}"
        assert "❌" in r.stdout or "无法" in r.stdout, f"应有失败提示，实际: {r.stdout}"
        # 不应写入 secrets.json
        assert not secrets_path.exists() or "qclaw_api_key" not in (secrets_path.read_text() if secrets_path.exists() else "")


def test_crack_tools_all_exist():
    for name in ("crack_qclaw.py", "crack_codebuddy.py", "crack_copilot.py", "crack_traework.py"):
        assert (ROOT / name).exists(), f"缺少 {name}"


def test_crack_tool_help_or_usage():
    r = _run_tool("crack_qclaw.py", "--help")
    assert r.returncode == 0 or "用法" in r.stdout or "--secrets" in r.stdout


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


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 运行测试验证失败**

Run: `.venv/bin/python test_crack_tools.py`
Expected: `FAIL test_crack_tools_all_exist`（脚本不存在）

- [ ] **Step 3: 实现 crack_qclaw.py**

```python
"""crack_qclaw.py — 提取 QClaw API Key（Windows DPAPI 解密），写入 secrets.json。

用法:
  python crack_qclaw.py [--secrets secrets.json] [--force]

独立脚本，不 import server.py。仅依赖标准库 + cryptography（venv 已有）。
成功退出码 0；失败退出码 1 + 引导文案。
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def _dpapi_unprotect(encrypted_bytes: bytes) -> bytes:
    import ctypes
    import ctypes.wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _DATA_BLOB(len(encrypted_bytes),
                         ctypes.cast(ctypes.c_char_p(encrypted_bytes),
                                     ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB)
    ]
    crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError(f"CryptUnprotectData failed (WinError {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _decrypt_qclaw_api_key() -> str:
    env_key = os.environ.get("QCLAW_API_KEY", "").strip()
    if env_key:
        return env_key
    if sys.platform != "win32":
        return ""  # DPAPI 仅 Windows
    try:
        appdata = os.environ.get("APPDATA", "")
        app_store = os.path.join(appdata, "QClaw", "app-store.json")
        local_state = os.path.join(appdata, "QClaw", "Local State")
        if not os.path.exists(app_store):
            return ""
        with open(app_store, "r", encoding="utf-8") as f:
            store = json.load(f)
        entry = store.get("authGateway.providers.qclaw.apiKey")
        if entry is None:
            return ""
        cipher_b64 = entry["cipherText"] if isinstance(entry, dict) else entry
        raw = base64.b64decode(cipher_b64)
        if raw[:3] != b"v10" or not os.path.exists(local_state):
            return ""
        with open(local_state, "r", encoding="utf-8") as f:
            ls = json.load(f)
        enc_key = base64.b64decode(ls["os_crypt"]["encrypted_key"])
        if enc_key[:5] != b"DPAPI":
            return ""
        aes_key = _dpapi_unprotect(enc_key[5:])
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        encrypted = raw[3:]
        nonce = encrypted[:12]
        ct_and_tag = encrypted[12:]
        return AESGCM(aes_key).decrypt(nonce, ct_and_tag, None).decode("utf-8").strip()
    except Exception:
        return ""


def _load_secrets(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 QClaw API Key 并写入 secrets.json")
    parser.add_argument("--secrets", default=str(SCRIPT_DIR / "secrets.json"), help="secrets.json 路径")
    parser.add_argument("--force", action="store_true", help="即使已有 key 也重新提取")
    args = parser.parse_args()

    secrets_path = Path(args.secrets)
    secrets = _load_secrets(secrets_path)
    if not args.force and secrets.get("qclaw_api_key"):
        print(f"✅ QClaw API Key 已存在（{secrets['qclaw_api_key'][:6]}...），跳过提取（用 --force 强制重新提取）")
        return 0

    key = _decrypt_qclaw_api_key()
    if not key:
        print("❌ 无法本地提取 QClaw API Key")
        print("   引导：在已登录 QClaw 的 Windows 机器上运行本脚本，")
        print("        或手工获取 key 后到 dashboard (http://127.0.0.1:8081/dashboard) 填写。")
        return 1

    secrets["qclaw_api_key"] = key
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ QClaw API Key 已更新: {key[:8]}...{key[-4:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 实现 crack_codebuddy.py**

```python
"""crack_codebuddy.py — 提取 CodeBuddy token，写入 secrets.json。

用法:
  python crack_codebuddy.py [--secrets secrets.json] [--force]

独立脚本。当前实现探测本机 CodeBuddy 客户端目录；未找到时优雅失败并给出引导。
"""
import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

# CodeBuddy 客户端可能的安装目录（Windows）
_POSSIBLE_DIRS = [
    os.environ.get("LOCALAPPDATA", ""),
    os.environ.get("APPDATA", ""),
    str(Path.home()),
]


def _find_codebuddy_token() -> str:
    """探测 CodeBuddy 客户端本地存储。返回 token 或空串。
    具体提取逻辑待按实际客户端版本实现（当前为骨架）。"""
    return ""


def _load_secrets(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 CodeBuddy token 并写入 secrets.json")
    parser.add_argument("--secrets", default=str(SCRIPT_DIR / "secrets.json"), help="secrets.json 路径")
    parser.add_argument("--force", action="store_true", help="即使已有 key 也重新提取")
    args = parser.parse_args()

    secrets_path = Path(args.secrets)
    secrets = _load_secrets(secrets_path)
    if not args.force and secrets.get("codebuddy_token"):
        print("✅ CodeBuddy token 已存在，跳过提取（用 --force 强制重新提取）")
        return 0

    token = _find_codebuddy_token()
    if not token:
        print("❌ 无法本地提取 CodeBuddy token")
        print("   引导：在已登录 CodeBuddy 的机器上运行本脚本，")
        print("        或手工获取 token 后到 dashboard (http://127.0.0.1:8081/dashboard) 填写。")
        return 1

    secrets["codebuddy_token"] = token
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ CodeBuddy token 已更新: {token[:8]}...{token[-4:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: 实现 crack_copilot.py**

```python
"""crack_copilot.py — 提取 GitHub Copilot GHE token，写入 secrets.json。

用法:
  python crack_copilot.py [--secrets secrets.json] [--force]

独立脚本。优先尝试本机 gh CLI（gh auth token），失败则引导手工获取。
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def _try_gh_cli() -> str:
    """尝试 gh auth token（需要已登录 GitHub Enterprise）。"""
    try:
        r = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=15,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _find_copilot_token() -> str:
    """探测本机 Copilot 客户端安装目录（骨架）。优先 gh CLI。"""
    token = _try_gh_cli()
    if token:
        return token
    return ""


def _load_secrets(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 Copilot GHE token 并写入 secrets.json")
    parser.add_argument("--secrets", default=str(SCRIPT_DIR / "secrets.json"), help="secrets.json 路径")
    parser.add_argument("--force", action="store_true", help="即使已有 key 也重新提取")
    args = parser.parse_args()

    secrets_path = Path(args.secrets)
    secrets = _load_secrets(secrets_path)
    if not args.force and secrets.get("copilot_token"):
        print("✅ Copilot token 已存在，跳过提取（用 --force 强制重新提取）")
        return 0

    token = _find_copilot_token()
    if not token:
        print("❌ 无法本地提取 Copilot token")
        print("   引导：在已登录 GitHub CLI 的机器上运行 `gh auth token` 获取，")
        print("        或手工获取 token 后到 dashboard (http://127.0.0.1:8081/dashboard) 填写。")
        return 1

    secrets["copilot_token"] = token
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Copilot token 已更新: {token[:8]}...{token[-4:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: 实现 crack_traework.py**

```python
"""crack_traework.py — 提取 Trae Work token，写入 secrets.json（预留骨架）。

用法:
  python crack_traework.py [--secrets secrets.json] [--force]

Trae Work 破解逻辑尚未实现，当前始终优雅失败并给出引导。
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def _find_traework_token() -> str:
    return ""


def _load_secrets(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 Trae Work token 并写入 secrets.json（预留）")
    parser.add_argument("--secrets", default=str(SCRIPT_DIR / "secrets.json"), help="secrets.json 路径")
    parser.add_argument("--force", action="store_true", help="即使已有 key 也重新提取")
    args = parser.parse_args()

    secrets_path = Path(args.secrets)
    secrets = _load_secrets(secrets_path)
    if not args.force and secrets.get("trae_work_token"):
        print("✅ Trae Work token 已存在，跳过提取")
        return 0

    token = _find_traework_token()
    if not token:
        print("❌ Trae Work 破解逻辑尚未实现（预留）")
        print("   请后续补充 crack_traework.py 的提取逻辑。")
        return 1

    secrets["trae_work_token"] = token
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Trae Work token 已更新: {token[:8]}...{token[-4:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: 运行测试验证通过**

Run: `.venv/bin/python test_crack_tools.py`
Expected: 全部 PASS

手动验证：
```bash
.venv/bin/python crack_qclaw.py --secrets /tmp/secrets-test.json
echo "exit=$?"
cat /tmp/secrets-test.json 2>/dev/null || echo "(secrets 未写入，符合预期)"
rm -f /tmp/secrets-test.json
```

- [ ] **Step 8: 提交**

```bash
git add crack_qclaw.py crack_codebuddy.py crack_copilot.py crack_traework.py test_crack_tools.py
git commit -m "feat: add standalone crack tools (qclaw/codebuddy/copilot/traework) with unified CLI"
```

---

### Task 6: 启动时自动调用破解工具 + secrets 热加载

**Files:**
- Modify: `server.py`（lifespan 区 465-517；新增 `_run_crack_tool` 辅助函数）

**Interfaces:**
- Consumes: `crack_*.py`（Task 5），`_cfg.load_secrets`（Task 1）
- Produces:
  - `_run_crack_tool(crack_tool: str) -> bool` — subprocess 调用破解工具（超时 30s），返回是否成功
  - `_refresh_secrets() -> None` — 重读 secrets.json 到 `_SECRETS`
  - lifespan 启动时对缺 key 的 crack target 自动调用 crackTool

- [ ] **Step 1: 在 server.py 中实现破解工具调用 + secrets 刷新**

在 `_load_vendor_targets` 之后追加：

```python
def _refresh_secrets():
    """重读 secrets.json 到内存（热生效）。"""
    global _SECRETS
    _SECRETS = _cfg.load_secrets()
    logger.info(f"🔑 secrets.json reloaded ({len(_SECRETS)} keys)")


def _run_crack_tool(crack_tool: str) -> bool:
    """调用破解工具脚本提取 token（超时 30s）。成功返回 True。"""
    import subprocess
    script = Path(__file__).parent / crack_tool
    if not script.exists():
        logger.warning(f"破解工具不存在: {script}")
        return False
    try:
        r = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=30,
            cwd=str(Path(__file__).parent),
        )
        if r.returncode == 0:
            logger.info(f"🔓 破解工具 {crack_tool} 成功: {r.stdout.strip()[:200]}")
            _refresh_secrets()
            return True
        logger.warning(f"🔓 破解工具 {crack_tool} 失败 (rc={r.returncode}): {r.stdout.strip()[:200]}")
        return False
    except subprocess.TimeoutExpired:
        logger.warning(f"🔓 破解工具 {crack_tool} 超时（30s）")
        return False
    except Exception as e:
        logger.warning(f"🔓 破解工具 {crack_tool} 异常: {e}")
        return False
```

- [ ] **Step 2: lifespan 启动时对缺 key 的 crack target 调用破解工具**

在 lifespan 的"启动 asyncio TCP 服务器"之前插入：

```python
    # ── 破解类 target：缺 key 时自动调用破解工具提取 ──
    for t in _TARGETS:
        if t.get("category") == "crack" and t.get("enabled", True):
            if not _cfg.resolve_secret(t, _SECRETS) and t.get("crackTool"):
                print(f"🔓 [{t['label']}] 缺 token，调用破解工具 {t['crackTool']} ...")
                _run_crack_tool(t["crackTool"])
            else:
                has = bool(_cfg.resolve_secret(t, _SECRETS))
                print(f"🔑 [{t['label']}] token {'已就绪' if has else '缺失（跳过破解，dashboard 可补）'}")
```

- [ ] **Step 3: 冒烟验证**

Run:
```bash
timeout 20 .venv/bin/python server.py --port 8081 2>&1 | grep -E "🔓|🔑|🔀|⏸️|token|Targets loaded" | head -20 &
sleep 8
curl -s http://127.0.0.1:8085/__proxy_info__ | python3 -c "import json,sys; d=json.load(sys.stdin); print('8085 secretSet:', d['secretSet'])"
curl -s http://127.0.0.1:8082/__proxy_info__ | python3 -c "import json,sys; d=json.load(sys.stdin); print('8082 secretSet:', d['secretSet'])"
wait
```
Expected:
- 启动日志：copilot/codebuddy/qclaw 显示"缺 token，调用破解工具"（Linux 环境提取失败，但日志有引导）
- 8085 secretSet: false（Linux 无 DPAPI）

- [ ] **Step 4: 提交**

```bash
git add server.py
git commit -m "feat: auto-run crack tools at startup for missing tokens, hot-reload secrets"
```

---

### Task 7: 热重载 — mtime 轮询 + 端口 diff

**Files:**
- Modify: `server.py`（lifespan 465-517；新增 `_config_watcher` / `_reload_targets`）

**Interfaces:**
- Consumes: `_cfg.load_targets/validate_targets/load_secrets`（Task 1），`_target_servers`（Task 4）
- Produces:
  - `_target_servers: Dict[int, asyncio.Server]`（全局，Task 4 已声明）
  - `_reload_targets() -> list[str]` — 重载配置并 diff 端口（新增起 server / 移除关 server / 保留更新），返回变更描述列表
  - `_config_watcher()` — 每 2s 轮询 targets.json / secrets.json mtime，变更时重载

- [ ] **Step 1: 实现重载与轮询函数**

在 `_load_vendor_targets` 附近追加：

```python
_target_servers: Dict[int, asyncio.Server] = {}
_config_mtimes: Dict[str, float] = {}


async def _reload_targets() -> list:
    """重载 targets.json / secrets.json，diff 端口并动态增删 server。"""
    global _TARGETS, _SECRETS, _ANTHROPIC_FORWARD_PORT
    changes = []
    cfg = _cfg.load_targets()
    errors = _cfg.validate_targets(cfg)
    if errors:
        logger.error(f"配置校验失败，拒绝重载: {errors}")
        return [f"❌ 校验失败: {errors}"]
    _TARGETS = cfg.get("targets", [])
    _ANTHROPIC_FORWARD_PORT = cfg.get("anthropicForwardPort", 8082)
    _SECRETS = _cfg.load_secrets()

    # 统计表补新 target
    for t in _TARGETS:
        if t["label"] not in _TARGET_STATS:
            _TARGET_STATS[t["label"]] = {
                "totalRequests": 0, "translated429": 0,
                "passthroughOk": 0, "passthroughError": 0,
                "startedAt": datetime.now().isoformat(),
            }

    # diff 端口
    wanted = {t["listenPort"]: t for t in _TARGETS if t.get("enabled", True)}
    for port in list(_target_servers.keys()):
        if port not in wanted:
            _target_servers[port].close()
            await _target_servers[port].wait_closed()
            del _target_servers[port]
            changes.append(f"移除端口 {port}")
    for port, t in wanted.items():
        if port not in _target_servers:
            try:
                srv = await _vendor_server("0.0.0.0", port, t)
                _target_servers[port] = srv
                changes.append(f"新增端口 {port} ({t['label']})")
            except OSError as e:
                logger.error(f"无法监听端口 {port}: {e}")
    logger.info(f"♻️  配置热重载完成: {changes if changes else '无端口变化'}")
    return changes


async def _config_watcher():
    """每 2s 轮询 targets.json / secrets.json mtime，变更即重载。"""
    while True:
        await asyncio.sleep(2)
        try:
            for path in (_cfg.TARGETS_PATH, _cfg.SECRETS_PATH):
                try:
                    mtime = path.stat().st_mtime
                except FileNotFoundError:
                    mtime = 0
                if _config_mtimes.get(str(path)) is None:
                    _config_mtimes[str(path)] = mtime
                elif mtime != _config_mtimes[str(path)]:
                    _config_mtimes[str(path)] = mtime
                    logger.info(f"♻️  检测到 {path.name} 变更")
                    await _reload_targets()
                    break
        except Exception as e:
            logger.warning(f"config watcher error: {e}")
```

- [ ] **Step 2: lifespan 启动 watcher**

在 lifespan 启动所有 target server 之后、`yield` 之前追加：

```python
    # ── 启动配置热重载 watcher ──
    watcher_task = asyncio.create_task(_config_watcher())
```

在 `yield` 之后的清理区追加：

```python
    # 停止配置 watcher
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass
```

- [ ] **Step 3: 冒烟验证热重载**

Run:
```bash
timeout 40 .venv/bin/python server.py --port 8081 2>&1 | grep -E "♻️|🔀|⏸️" &
sleep 8
# 增加一个临时 target 到 targets.json（用 python 原子改）
.venv/bin/python -c "
import json
from pathlib import Path
p = Path('targets.json')
cfg = json.loads(p.read_text())
cfg['targets'].append({'label': 'temp-test', 'listenPort': 8099, 'category': 'free', 'handler': 'passthrough', 'isFree': True, 'targetHost': 'example.com', 'targetPort': 443, 'targetProtocol': 'https', 'routePrefix': '', 'models': []})
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
print('temp target added')
"
sleep 5
curl -s http://127.0.0.1:8099/__proxy_info__ | python3 -m json.tool | head -8
# 恢复
.venv/bin/python -c "
import json
from pathlib import Path
p = Path('targets.json')
cfg = json.loads(p.read_text())
cfg['targets'] = [t for t in cfg['targets'] if t['label'] != 'temp-test']
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
print('temp target removed')
"
sleep 5
curl -s -m 3 http://127.0.0.1:8099/__proxy_info__ || echo "8099 已关闭（符合预期）"
wait
```
Expected: 日志显示"检测到 targets.json 变更"→"新增端口 8099"→ 请求 8099 成功 → 移除后 8099 连接失败

- [ ] **Step 4: 提交**

```bash
git add server.py
git commit -m "feat: hot reload targets/secrets via mtime polling with port diff"
```

---

### Task 8: dashboard REST API + 管理表单

**Files:**
- Modify: `server.py`（dashboard 区 4284+；新增 REST API 路由）

**Interfaces:**
- Consumes: `_TARGETS / _TARGET_STATS / _SECRETS / _reload_targets / _run_crack_tool / _cfg`（Task 3/6/7）
- Produces（8081 FastAPI 路由）:
  - `GET /api/targets` → `{"anthropicForwardPort": int, "targets": [{...target, "secretSet": bool, "secretMasked": str}], "stats": {...}}`
  - `PUT /api/targets/{label}`（body: 部分字段）→ 更新 targets.json + 重载
  - `PUT /api/secrets/{label}`（body: `{"value": "..."}`）→ 更新 secrets.json + 重载
  - `POST /api/targets/{label}/recrack` → 调 crackTool + 重载 secrets
  - `POST /api/reload` → 手动重载
  - dashboard 页面追加管理表单（HTML + JS）

- [ ] **Step 1: 追加 REST API 路由（在 dashboard 路由之前）**

在 `@app.get("/dashboard")` 之前插入：

```python
@app.get("/api/targets")
async def api_targets():
    """返回全部 target 配置 + secrets 元信息（key 打码）+ 统计。"""
    result = []
    for t in _TARGETS:
        secret = _cfg.resolve_secret(t, _SECRETS)
        result.append({
            **t,
            "secretSet": bool(secret),
            "secretMasked": _cfg.mask_secret(secret),
            "stats": _TARGET_STATS.get(t["label"], {}),
        })
    return {
        "anthropicForwardPort": _ANTHROPIC_FORWARD_PORT,
        "targets": result,
    }


class TargetUpdate(BaseModel):
    label: Optional[str] = None
    listenPort: Optional[int] = None
    category: Optional[str] = None
    handler: Optional[str] = None
    isFree: Optional[bool] = None
    enabled: Optional[bool] = None
    targetHost: Optional[str] = None
    targetPort: Optional[int] = None
    targetProtocol: Optional[str] = None
    routePrefix: Optional[str] = None
    models: Optional[List[str]] = None
    crackTool: Optional[str] = None
    secretRef: Optional[str] = None
    apikeyEnv: Optional[str] = None


@app.put("/api/targets/{label}")
async def api_update_target(label: str, update: TargetUpdate):
    """更新 target 非私密字段，写 targets.json 并热重载。"""
    cfg = _cfg.load_targets()
    for t in cfg["targets"]:
        if t["label"] == label:
            payload = update.model_dump(exclude_none=True)
            payload.pop("label", None)
            t.update(payload)
            break
    else:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    errors = _cfg.validate_targets(cfg)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    _cfg.save_targets(cfg)
    await _reload_targets()
    return {"ok": True, "label": label}


class SecretUpdate(BaseModel):
    value: str = ""


@app.put("/api/secrets/{label}")
async def api_update_secret(label: str, update: SecretUpdate):
    """更新 target 的私密 key/token，写 secrets.json 并热加载。"""
    cfg = _cfg.load_targets()
    target = next((t for t in cfg["targets"] if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    ref = target.get("secretRef")
    if not ref:
        raise HTTPException(status_code=422, detail=f"target '{label}' 未配置 secretRef")
    secrets = _cfg.load_secrets()
    if update.value:
        secrets[ref] = update.value
    else:
        secrets.pop(ref, None)
    _cfg.save_secrets(secrets)
    _refresh_secrets()
    return {"ok": True, "label": label, "secretRef": ref, "secretSet": bool(update.value)}


@app.post("/api/targets/{label}/recrack")
async def api_recrack(label: str):
    """触发破解工具重新提取 token。"""
    target = next((t for t in _TARGETS if t["label"] == label), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target '{label}' 不存在")
    tool = target.get("crackTool")
    if not tool:
        raise HTTPException(status_code=422, detail=f"target '{label}' 无 crackTool")
    ok = _run_crack_tool(tool)
    if not ok:
        return {"ok": False, "label": label, "message": "破解工具执行失败，请查看日志或手工填写"}
    return {"ok": True, "label": label, "message": "破解工具执行成功"}


@app.post("/api/reload")
async def api_reload():
    changes = await _reload_targets()
    return {"ok": True, "changes": changes}
```

- [ ] **Step 2: dashboard 追加管理卡片（在现有 cards 列表后追加 target 卡片循环）**

在 `cards = []` 初始化后追加动态 target 卡片（放在现有 4 张硬编码卡片之后）：

```python
    # ── 动态 target 卡片（targets.json 驱动）──
    for t in _TARGETS:
        port = t["listenPort"]
        r = next((x for x in results if x["listenPort"] == port), None)
        if r is None:
            try:
                r = await _fetch(port)
            except Exception:
                r = {"label": t["label"], "listenPort": port, "upstream": "?", "models": [], "total": 0, "ok": 0, "translated": 0, "err": 0, "alive": False, "startedAt": ""}
        category = t.get("category", "free")
        badge_map = {"crack": "破解·质量高", "free": "免费·不破解", "paid": "收费·不破解"}
        badge_class_map = {"crack": "blue", "free": "green", "paid": "orange"}
        secret = _cfg.resolve_secret(t, _SECRETS)
        kv = [
            ("分类", badge_map.get(category, category)),
            ("handler", t.get("handler", "passthrough")),
            ("上游", f"{t.get('targetProtocol','https')}://{t['targetHost']}:{t.get('targetPort',443)}{t.get('routePrefix','')}"),
            ("token", ("已配置 " + _cfg.mask_secret(secret)) if secret else "⚠️ 缺失（点击卡片编辑/破解）"),
        ]
        if t.get("isFree") is not None:
            kv.append(("isFree", "是（免费）" if t["isFree"] else "否（收费）"))
        if t.get("enabled") is False:
            kv.append(("状态", "预留（未监听）"))
        cards.append(_build_card_html(
            name=f"{t['label']} ({port})",
            note="统一透传引擎 · targets.json 驱动",
            kind_badge=badge_map.get(category, category),
            status_badge=f"{r['total']} 请求" if r["alive"] else ("未监听" if t.get("enabled") is False else "离线"),
            status_badge_class=badge_class_map.get(category, "gray") if r["alive"] else "red",
            kv_items=kv,
            models=t.get("models", []),
            stats_detail=_make_stats_detail(r),
            description=f"category={category} · handler={t.get('handler','passthrough')} · isFree={t.get('isFree')}",
            accent_class=f"accent-{port}",
        ))
```

并更新 `results` 的 gather 调用（4310 行）为动态端口列表：

```python
    _dash_ports = [t["listenPort"] for t in _TARGETS if t.get("enabled", True)]
    results = await asyncio.gather(*[_fetch(p) for p in _dash_ports]) if _dash_ports else []
```

（注：移除硬编码 `_fetch(8082), _fetch(8090), _fetch(8091), _fetch(8084)`）

- [ ] **Step 3: dashboard HTML 追加管理脚本**

在 dashboard HTML 的 `<script>` 区（或 HTML 末尾）追加表单与 JS（fetch 调用 REST API）。在 dashboard 函数返回的 HTML 字符串中、`</body>` 前追加：

```html
<div class="admin-panel">
  <h3>⚙️ 管理操作</h3>
  <div id="admin-msg"></div>
  <table class="model-table" id="admin-table">
    <thead><tr><th>label</th><th>端口</th><th>分类</th><th>isFree</th><th>token</th><th>操作</th></tr></thead>
    <tbody id="admin-tbody"></tbody>
  </table>
  <p><button onclick="doReload()">♻️ 手动重载配置</button></p>
</div>
<script>
async function loadAdmin() {
  const resp = await fetch('/api/targets');
  const data = await resp.json();
  const tbody = document.getElementById('admin-tbody');
  tbody.innerHTML = '';
  for (const t of data.targets) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${t.label}</td>
      <td>${t.listenPort}</td>
      <td>${t.category}${t.isFree ? ' (免费)' : ''}</td>
      <td><input type="checkbox" ${t.isFree ? 'checked' : ''} onchange="setIsFree('${t.label}', this.checked)"></td>
      <td>${t.secretSet ? t.secretMasked : '<span style="color:red">缺失</span>'}
          <input id="secret-${t.label}" type="password" placeholder="新 token">
          <button onclick="saveSecret('${t.label}')">保存</button></td>
      <td>${t.category === 'crack' && t.crackTool ? `<button onclick="recrack('${t.label}')">重新破解</button>` : ''}</td>`;
    tbody.appendChild(tr);
  }
}
async function saveSecret(label) {
  const v = document.getElementById('secret-' + label).value;
  const resp = await fetch('/api/secrets/' + label, {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: v})
  });
  const r = await resp.json();
  document.getElementById('admin-msg').textContent = JSON.stringify(r);
  loadAdmin();
}
async function setIsFree(label, val) {
  const resp = await fetch('/api/targets/' + label, {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({isFree: val})
  });
  document.getElementById('admin-msg').textContent = JSON.stringify(await resp.json());
}
async function recrack(label) {
  const resp = await fetch('/api/targets/' + label + '/recrack', {method: 'POST'});
  document.getElementById('admin-msg').textContent = JSON.stringify(await resp.json());
  loadAdmin();
}
async function doReload() {
  const resp = await fetch('/api/reload', {method: 'POST'});
  document.getElementById('admin-msg').textContent = JSON.stringify(await resp.json());
}
loadAdmin();
</script>
```

- [ ] **Step 4: 集成测试 test_dashboard.py 扩展**

在 `test_dashboard.py` 追加：

```python
def test_api_targets_lists_all_ports():
    """GET /api/targets 应返回全部端口与分类。"""
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request("GET", "/api/targets")
    resp = conn.getresponse()
    body = json.loads(resp.read().decode("utf-8"))
    conn.close()
    assert resp.status == 200
    ports = {t["listenPort"] for t in body["targets"]}
    for p in (8082, 8084, 8085, 8086, 8090, 8091, 8092, 8093, 8094):
        assert p in ports, f"缺少端口 {p}"
    cats = {t["category"] for t in body["targets"]}
    assert "crack" in cats and "free" in cats and "paid" in cats, f"应含三类标签: {cats}"


def test_api_secret_update_hot_reload():
    """PUT /api/secrets/{label} 更新后 /__proxy_info__ secretSet 应变 true。"""
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request("PUT", "/api/secrets/qclaw", json.dumps({"value": "sk-test-hot"}), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    assert resp.status == 200, f"PUT secret 失败: {body}"
    # 清理：恢复空
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request("PUT", "/api/secrets/qclaw", json.dumps({"value": ""}), {"Content-Type": "application/json"})
    resp = conn.getresponse(); resp.read(); conn.close()
```

将 test_dashboard.py 顶部 `import http.client` 补充 `import json`。

- [ ] **Step 5: 运行集成测试**

Run: `.venv/bin/python test_dashboard.py`
Expected: 全部 PASS（含新增 API 测试）

Run: `.venv/bin/python test_targets_schema.py` 和 `.venv/bin/python test_crack_tools.py` — 仍全部 PASS

- [ ] **Step 6: 提交**

```bash
git add server.py test_dashboard.py
git commit -m "feat: add dashboard REST API (targets/secrets/recrack/reload) and admin form"
```

---

### Task 9: 测试更新 + 回归 + 文档

**Files:**
- Modify: `test_suite.py`（8082 行为断言更新）
- Modify: `.gitignore`（加 secrets.json）
- Modify: `README.md` / `README-zh.md` / `AGENTS.md` / `CHANGELOG.md`
- Create: `secrets.json`（示例，含空值占位）

**Interfaces:**
- Consumes: 全部前序任务产出

- [ ] **Step 1: .gitignore 追加 secrets.json**

在 `.gitignore` 追加：

```gitignore
# 私密 key/token（dashboard 热更新，不入库）
secrets.json
```

并创建 `secrets.json`（含空占位，运行时由破解工具/dashboard 填充）：

```json
{
  "copilot_token": "",
  "codebuddy_token": "",
  "qclaw_api_key": "",
  "trae_work_token": ""
}
```

Run: `git check-ignore secrets.json && echo "ignored OK"`

- [ ] **Step 2: 更新 test_suite.py 的 provider 断言**

test_suite.py 顶部（35-43 行）目前按 `PREFERRED_PROVIDER` 选模型。由于 8082 现在固定 copilot，更新为 copilot 模型映射断言：

将 35-43 行替换为：

```python
# ─── 8082 固定为 copilot target（横向扩展模式）───
# 模型名：Anthropic 别名经 copilot target 的 modelMapping 映射
_PROVIDER = os.environ.get("PREFERRED_PROVIDER", "copilot").lower()
if _PROVIDER == "qclaw":
    # 直接打 8085（qclaw 端口）时用 pool-* 模型
    OAI_MODEL_BIG = os.environ.get("BIG_MODEL", "pool-glm-5.2")
    OAI_MODEL_MED = os.environ.get("MEDIUM_MODEL", "pool-deepseek-v4-pro")
    OAI_MODEL_SMALL = os.environ.get("SMALL_MODEL", "pool-deepseek-v4-flash")
else:
    # 默认：8082 = copilot，经 modelMapping 映射
    OAI_MODEL_BIG = "opus"
    OAI_MODEL_MED = "sonnet"
    OAI_MODEL_SMALL = "haiku"
```

- [ ] **Step 3: 回归测试**

Run: `.venv/bin/python test_targets_schema.py` → 全 PASS
Run: `.venv/bin/python test_crack_tools.py` → 全 PASS
Run: `.venv/bin/python test_dashboard.py` → 全 PASS（需服务运行）
Run: `.venv/bin/python test_suite.py --simple` → 核对 8082 copilot 行为（无真实上游 token 时 401 属预期，核心链路以 test_dashboard 为准）

- [ ] **Step 4: 更新文档**

**README.md** 架构章节（"Architecture (4 Ports)"）替换为多端口表格 + 新配置说明：

```markdown
## Architecture (Multi-Port) 🏗️

| 端口 | 供应商 | 分类 | handler |
|------|--------|------|---------|
| 8081 | Anthropic 入口 + dashboard | — | FastAPI |
| 8082 | copilot | crack | copilot |
| 8084 | codebuddy | crack | passthrough |
| 8085 | claw (QClaw) | crack | qclaw |
| 8086 | trae-work (预留) | crack | passthrough |
| 8090 | openrouter | free | passthrough |
| 8091 | nvidia | free | passthrough |
| 8092 | gemini-openai | free | passthrough |
| 8093 | opencode-zen | free | passthrough |
| 8094 | open-go | paid | passthrough |

- **统一透传引擎**：所有端口共享 HTTP 解析/转发/429 翻译/重试逻辑，由 `targets.json` 驱动
- **分类**：`crack`（破解，注入 secrets.json token）/ `free`（免费，透传客户端 key）/ `paid`（收费，透传客户端 key）
- **isFree**：管理界面维护，标记供应商 key 是否免费（重试策略预留字段）
- **破解工具**：`crack_qclaw.py` / `crack_codebuddy.py` / `crack_copilot.py` / `crack_traework.py`，启动时自动调用，可独立 CLI 运行
- **管理界面**：`http://127.0.0.1:8081/dashboard` 可编辑 token/isFree，热生效无需重启
```

**README-zh.md** 同样更新（中文版）。

**AGENTS.md**：
- 更新端口表（第 4 节 API 端点）
- 更新 `.env` 说明：PREFERRED_PROVIDER 废弃，配置移入 targets.json
- 更新 QClaw 章节：key 现在由 crack_qclaw.py 提取写 secrets.json
- 更新代码结构行号说明

**CHANGELOG.md** 追加：

```markdown
## [Unreleased]

### Added
- 横向扩展多端口架构：一端口一供应商（8082 copilot / 8084 codebuddy / 8085 qclaw / 8086 trae-work 预留 / 8090-8094 免费代理）
- targets.json 新 schema（category/isFree/handler/crackTool/secretRef/enabled/modelMapping/reasoning）
- secrets.json 私密 key/token 存储（gitignore，dashboard 热更新）
- 独立破解工具 crack_qclaw / crack_codebuddy / crack_copilot / crack_traework（统一 CLI）
- dashboard 管理界面：REST API（/api/targets、/api/secrets、/api/reload、recrack）+ token/isFree 编辑表单
- 配置热重载：mtime 轮询（2s），端口动态增删，无需重启

### Changed
- 8082 从 PREFERRED_PROVIDER 动态切换改为固定 copilot
- qclaw 配置从 .env 迁入 targets.json 的 8085 条目
- 8081 转发目标由 anthropicForwardPort 配置（默认 8082）
```

- [ ] **Step 5: 提交**

```bash
git add .gitignore secrets.json test_suite.py README.md README-zh.md AGENTS.md CHANGELOG.md
git commit -m "docs: update docs for multi-port architecture, add secrets.json, update tests"
```

---

### Task 10: 全量回归 + 收尾

**Files:** 无新文件（验证 + 清理）

- [ ] **Step 1: 全量测试回归**

Run:
```bash
.venv/bin/python test_targets_schema.py
.venv/bin/python test_crack_tools.py
.venv/bin/python test_dashboard.py
```
Expected: 全部 PASS

- [ ] **Step 2: 全端口启动冒烟**

Run:
```bash
timeout 30 .venv/bin/python server.py --port 8081 2>&1 | tee /tmp/multiport-smoke.log &
sleep 10
for p in 8081 8082 8084 8085 8090 8091 8092 8093 8094; do
  echo "--- :$p ---"
  curl -s -m 3 http://127.0.0.1:$p/__proxy_info__ | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('label'), d.get('category'), d.get('handler'))" 2>/dev/null || echo "端口 $p 异常"
done
grep -E "⏸️|skipped" /tmp/multiport-smoke.log || echo "8086 未监听（enabled=false 生效）"
wait
```
Expected: 8 个端口（8081-8094 除 8086）全部响应，8086 未监听

- [ ] **Step 3: 确认 git 状态干净（无意外文件）**

Run: `git status --short`
Expected: 仅有意提交的文件；`.env` / `*.log` / `.venv/` / `secrets.json` 不出现

- [ ] **Step 4: 总结交付**

向用户汇报：
1. 端口映射表（10 端口）
2. 新文件清单（config_store.py / crack_*.py × 4 / secrets.json / 测试 × 2）
3. 管理界面用法（/dashboard → token/isFree 编辑）
4. 破解工具用法（独立 CLI + 启动自动调用）
5. 已知限制（trae-work 未实现破解、open-go 上游待确认、isFree 未驱动重试）
